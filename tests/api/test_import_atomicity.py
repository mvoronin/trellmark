import json
import threading
from collections import Counter
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from urllib import error, request

import pytest
from anyio import CapacityLimiter
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from tests.backup.helpers import ObservedBackupFactory
from tests.bookmarks import helpers as bookmark_helpers
from tests.helpers import (
    _authentication_headers,
    capture_storage_statements,
    group_in,
    group_names_in,
    grouped_urls_in,
    http_json,
    http_raw,
    logical_bookmark_snapshot,
    public_bookmark_snapshot,
    run_async,
)
from trellmark.app import MAX_REQUEST_BODY_BYTES, create_app
from trellmark.backup import domain as backup_domain
from trellmark.backup.api import ImportDocument
from trellmark.backup.application import BackupApplicationService, ImportInvalid
from trellmark.backup.persistence import (
    PostgresBackupUnitOfWorkFactory,
    PostgresExportSnapshotFactory,
)
from trellmark.bookmarks import persistence
from trellmark.platform import runtime
from trellmark.platform.runtime import AnyIOWorkRunner

INVALID_IMPORT = {"error": "Invalid import file.", "code": "invalid_import"}
IMPORT_CONFLICT = {
    "error": (
        "Another bookmark change is in progress. "
        "No import changes were saved. Try again."
    ),
    "code": "import_conflict",
}
IMPORT_FAILED = {
    "error": "Import failed. No import changes were saved. Try again.",
    "code": "import_failed",
}


def _single_group_document(*, with_url: bool = True) -> dict[str, object]:
    urls: list[dict[str, object]] = []
    if with_url:
        urls.append(
            {
                "url": "https://atomic.example/article",
                "title": "Atomic article",
                "created_at": "2026-08-30T06:00:00Z",
                "important": True,
            }
        )
    return {
        "version": 1,
        "exported_at": "2026-08-30T06:01:00Z",
        "groups": [
            {
                "name": "Atomic",
                "parent": None,
                "position": 0,
                "nsfw": False,
                "domains": ["atomic.example"],
                "urls": urls,
            }
        ],
    }


def _is_dml(statement: str) -> bool:
    return statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))


def test_import_complete_transaction_runs_outside_request_thread(app, monkeypatch):
    base_url, _ = app
    assert http_json(base_url, "/api/groups")[0] == 200
    engine = runtime.get_engine()
    request_threads = []
    work_events = []
    import_connections = set()
    original = ImportDocument.to_domain

    def observe_transport(document):
        request_threads.append(threading.get_ident())
        return original(document)

    def observe_sql(connection, _cursor, statement, *_args):
        if "pg_try_advisory_xact_lock" in statement:
            import_connections.add(connection)
        if connection in import_connections:
            work_events.append(("sql", threading.get_ident()))

    def observe_commit(connection):
        if connection in import_connections:
            work_events.append(("commit", threading.get_ident()))

    def observe_return(_dbapi, _record):
        if work_events and work_events[-1][0] == "commit":
            work_events.append(("close", threading.get_ident()))

    monkeypatch.setattr(ImportDocument, "to_domain", observe_transport)
    listeners = (
        ("before_cursor_execute", observe_sql),
        ("commit", observe_commit),
        ("checkin", observe_return),
    )
    for name, listener in listeners:
        event.listen(engine, name, listener)
    try:
        assert (
            http_json(
                base_url, "/api/import", method="POST", payload=_single_group_document()
            )[0]
            == 200
        )
    finally:
        for name, listener in listeners:
            event.remove(engine, name, listener)
    assert len(import_connections) == 1
    assert work_events[-2:][0][0] == "commit"
    assert work_events[-1][0] == "close"
    assert len({thread for _, thread in work_events}) == 1
    assert set(request_threads).isdisjoint(thread for _, thread in work_events)


def test_import_uses_single_connection_and_outer_transaction(app, monkeypatch):
    base_url, _ = app
    # Authenticate before instrumenting content storage so the identity boundary's
    # independent transaction cannot be mistaken for an import connection.
    status, _ = http_json(base_url, "/api/groups")
    assert status == 200

    statements, engine = capture_storage_statements(monkeypatch)
    connection_identities = set()

    def capture_import_connection(connection, _cursor, statement, *_args):
        if "pg_try_advisory_xact_lock" in statement:
            connection_identities.add(
                (id(connection), id(connection.get_transaction()))
            )

    event.listen(engine, "before_cursor_execute", capture_import_connection)
    try:
        status, payload = http_json(
            base_url,
            "/api/import",
            method="POST",
            payload=_single_group_document(),
        )
    finally:
        engine.dispose()

    assert status == 200
    assert set(payload) == {"imported", "skipped", "groups"}
    assert payload["imported"] == 1
    assert payload["skipped"] == 0
    assert group_names_in(payload) == ["Atomic", "default"]
    assert grouped_urls_in(payload, "Atomic") == ["https://atomic.example/article"]
    assert group_in(payload, "Atomic")["domains"] == ["atomic.example"]
    assert connection_identities and len(connection_identities) == 1
    assert len(set(connection_identities)) == 1
    assert any("pg_try_advisory_xact_lock" in statement for statement in statements)


def test_import_validation_before_dml(app, monkeypatch):
    base_url, _ = app
    status, _ = http_json(base_url, "/api/groups")
    assert status == 200

    statements, engine = capture_storage_statements(monkeypatch)
    original_validate = backup_domain.validate_resulting_import_hierarchy
    validation_statement_count: list[int] = []

    def validate_inside_transaction(groups, existing_groups):
        validation_statement_count.append(len(statements))
        assert any("pg_try_advisory_xact_lock" in statement for statement in statements)
        assert not any(_is_dml(statement) for statement in statements)
        assert any("FOR UPDATE" in statement for statement in statements)
        return original_validate(groups, existing_groups)

    monkeypatch.setattr(
        backup_domain,
        "validate_resulting_import_hierarchy",
        validate_inside_transaction,
    )
    try:
        status, payload = http_json(
            base_url,
            "/api/import",
            method="POST",
            payload=_single_group_document(with_url=False),
        )
    finally:
        engine.dispose()

    assert status == 200
    assert payload["imported"] == 0
    assert payload["skipped"] == 0
    assert validation_statement_count
    first_dml_index = next(
        index for index, statement in enumerate(statements) if _is_dml(statement)
    )
    gate_index = next(
        index
        for index, statement in enumerate(statements)
        if "pg_try_advisory_xact_lock" in statement
    )
    assert gate_index < validation_statement_count[0] <= first_dml_index


EXPECTED_IMPORT_MUTATION_STAGES = (
    "group_metadata",
    "hierarchy_detach",
    "hierarchy_attach",
    "sibling_order",
    "url_insert",
    "membership_insert",
    "url_metadata",
)


def _rollback_document() -> ImportDocument:
    return ImportDocument.model_validate(
        {
            "version": 1,
            "exported_at": "2026-08-30T06:30:00Z",
            "groups": [
                {
                    "name": "Imported Child",
                    "parent": "Destination",
                    "position": 0,
                    "nsfw": True,
                    "domains": ["rollback.example"],
                    "urls": [
                        {
                            "url": "https://rollback.example/new",
                            "title": "First metadata",
                            "created_at": "2026-08-30T06:30:01Z",
                            "important": True,
                        },
                        {
                            "url": "https://existing.example/item",
                            "title": "Ignored duplicate metadata",
                            "created_at": "2026-08-30T06:30:02Z",
                            "important": True,
                        },
                    ],
                },
                {
                    "name": "New Root",
                    "parent": None,
                    "position": 0,
                    "nsfw": False,
                    "domains": [],
                    "urls": [
                        {
                            "url": "https://rollback.example/new",
                            "title": "Later metadata",
                            "created_at": "2026-08-30T06:30:03Z",
                            "important": False,
                        }
                    ],
                },
            ],
        }
    )


def _seed_rollback_state() -> None:
    old_parent = bookmark_helpers.seed_group("Old Parent")
    destination = bookmark_helpers.seed_group("Destination")
    assert old_parent is not None
    assert destination is not None
    imported_child = bookmark_helpers.seed_group(
        "Imported Child",
        domains=["old.example"],
        parent_id=old_parent["id"],
    )
    assert imported_child is not None
    existing = bookmark_helpers.seed_url(
        "https://existing.example/item",
        title="Existing metadata",
    )
    assert existing is not None
    bookmark_helpers.seed_created_at(
        existing["id"],
        backup_domain.import_timestamp(
            _rollback_document().to_domain().groups[0].urls[0].created_at
        ),
    )


@pytest.mark.parametrize("failure_stage", EXPECTED_IMPORT_MUTATION_STAGES)
def test_import_rollback_each_mutation_stage_and_retry_cleanly(
    app,
    failure_stage,
):
    base_url, _ = app
    _seed_rollback_state()
    document = _rollback_document()
    direct_before = logical_bookmark_snapshot()
    public_before = public_bookmark_snapshot(base_url)
    reached_stages: list[str] = []
    occurrences: dict[str, int] = {}
    # Fail the last ordering and membership callbacks in the rich document so
    # the matrix proves both the earliest mutation and a late repeated-URL
    # membership roll back, not only the first occurrence of every stage.
    failure_occurrence = {
        "sibling_order": 2,
        "membership_insert": 2,
    }.get(failure_stage, 1)

    def fail_after_stage(stage: str) -> None:
        reached_stages.append(stage)
        occurrences[stage] = occurrences.get(stage, 0) + 1
        if stage == failure_stage and occurrences[stage] == failure_occurrence:
            raise RuntimeError("Synthetic import stage failure.")

    with pytest.raises(RuntimeError, match="Synthetic import stage failure"):
        service = BackupApplicationService(
            AnyIOWorkRunner(CapacityLimiter(1)),
            ObservedBackupFactory(fail_after_stage),
            PostgresExportSnapshotFactory(runtime.get_engine),
        )
        run_async(lambda: service.import_document(document.to_domain()))

    assert failure_stage in reached_stages
    assert logical_bookmark_snapshot() == direct_before
    assert public_bookmark_snapshot(base_url) == public_before

    status, payload = http_json(
        base_url,
        "/api/import",
        method="POST",
        payload=document.model_dump(mode="json"),
    )
    assert status == 200
    assert payload["imported"] == 1
    assert payload["skipped"] == 1
    assert grouped_urls_in(payload, "Imported Child") == [
        "https://rollback.example/new"
    ]
    assert grouped_urls_in(payload, "New Root") == ["https://rollback.example/new"]
    imported = group_in(payload, "Imported Child")["urls"][0]
    assert imported["title"] == "First metadata"
    assert imported["created_at"] == "2026-08-30T06:30:01Z"
    assert imported["important"] is True


STAGE_OCCURRENCES = {
    "group_metadata": 3,
    "hierarchy_detach": 2,
    "hierarchy_attach": 2,
    "sibling_order": 3,
    "url_insert": 2,
    "membership_insert": 3,
    "url_metadata": 2,
}


@pytest.mark.parametrize(
    ("failure_stage", "failure_occurrence"),
    [
        (stage, occurrence)
        for stage in EXPECTED_IMPORT_MUTATION_STAGES
        for occurrence in range(1, STAGE_OCCURRENCES[stage] + 1)
    ],
)
def test_import_http_rollback_every_stage_occurrence_and_identical_retry(
    app, backup_uow_factory, failure_stage, failure_occurrence
):
    base_url, _ = app
    _seed_rollback_state()
    parent = bookmark_helpers.group_by_name("Old Parent")
    second = bookmark_helpers.seed_group("Second Child", parent_id=parent["id"])
    assert second is not None
    payload = _rollback_document().model_dump(mode="json")
    payload["groups"].append(
        {
            "name": "Second Child",
            "parent": "New Root",
            "position": 0,
            "nsfw": True,
            "domains": ["second.example"],
            "urls": [
                {
                    "url": "https://second.example/new",
                    "title": "Second metadata",
                    "created_at": "2026-08-30T06:30:04Z",
                    "important": True,
                }
            ],
        }
    )
    direct_before = logical_bookmark_snapshot()
    public_before = public_bookmark_snapshot(base_url)
    reached = []
    failure = SQLAlchemyError("Synthetic stage failure; do not expose.")

    def fail_after_mutation(stage):
        reached.append(stage)
        if stage == failure_stage and reached.count(stage) == failure_occurrence:
            raise failure

    backup_uow_factory.observer = fail_after_mutation
    status, body, cache = _raw_import_response(
        base_url,
        body=json.dumps(payload).encode(),
        headers={
            **_authentication_headers(base_url),
            "Origin": base_url,
            "Content-Type": "application/json",
        },
    )
    assert reached[-1] == failure_stage
    assert reached.count(failure_stage) == failure_occurrence
    assert (status, json.loads(body), cache) == (500, IMPORT_FAILED, "no-store")
    assert logical_bookmark_snapshot() == direct_before
    assert public_bookmark_snapshot(base_url) == public_before

    reached.clear()
    backup_uow_factory.observer = reached.append
    status, result = http_json(base_url, "/api/import", method="POST", payload=payload)
    assert status == 200 and result["imported"] == 2 and result["skipped"] == 1
    assert Counter(reached) == STAGE_OCCURRENCES
    assert grouped_urls_in(result, "Imported Child") == ["https://rollback.example/new"]
    assert grouped_urls_in(result, "New Root") == ["https://rollback.example/new"]
    assert grouped_urls_in(result, "Second Child") == ["https://second.example/new"]
    first = group_in(result, "Imported Child")["urls"][0]
    assert (first["title"], first["created_at"], first["important"]) == (
        "First metadata",
        "2026-08-30T06:30:01Z",
        True,
    )
    second_result = group_in(result, "Second Child")
    assert second_result["id"] == second["id"]
    assert second_result["parent_id"] == group_in(result, "New Root")["id"]
    assert second_result["domains"] == ["second.example"]
    assert second_result["urls"][0]["created_at"] == "2026-08-30T06:30:04Z"


def test_import_production_exposes_no_fault_callback_or_stage_registry():
    root = Path(__file__).resolve().parents[2] / "trellmark"
    for path in root.rglob("*.py"):
        source = path.read_text()
        assert "IMPORT_MUTATION_STAGES" not in source
        assert "ImportStageHook" not in source
        assert "after_stage" not in source


@pytest.mark.parametrize("scope", ["import", "export"])
@pytest.mark.parametrize("failure_point", ["operation", "commit", "enter"])
def test_import_and_export_preserve_primary_failure_through_cleanup(
    database, scope, failure_point
):
    if scope == "export" and failure_point == "commit":
        # Export has no commit capability; cover snapshot configuration failure.
        failure_point = "configure"
    engine = runtime.get_engine()
    failure = RuntimeError("Primary adapter failure")
    events = []

    class TransactionProxy:
        def __init__(self, inner):
            self.inner = inner

        @property
        def is_active(self):
            return self.inner.is_active

        def commit(self):
            events.append("commit")
            raise failure

        def rollback(self):
            events.append("rollback")
            self.inner.rollback()
            raise RuntimeError("Secondary rollback failure")

    class ConnectionProxy:
        def __init__(self, inner):
            self.inner = inner

        def begin(self):
            if failure_point == "enter":
                raise failure
            return TransactionProxy(self.inner.begin())

        def execution_options(self, **options):
            if failure_point == "configure":
                raise failure
            return self.inner.execution_options(**options)

        def close(self):
            events.append("close")
            self.inner.close()
            raise RuntimeError("Secondary close failure")

        def __getattr__(self, name):
            return getattr(self.inner, name)

    class EngineProxy:
        def connect(self):
            events.append("connect")
            return ConnectionProxy(engine.connect())

    factory_type = (
        PostgresBackupUnitOfWorkFactory
        if scope == "import"
        else PostgresExportSnapshotFactory
    )
    factory = factory_type(EngineProxy)
    with pytest.raises(RuntimeError) as caught:
        with factory() as uow:
            if failure_point == "commit":
                uow.commit()
            raise failure
    assert caught.value is failure
    assert events[-1] == "close"
    assert events.count("rollback") == (
        0 if failure_point in {"enter", "configure"} else 1
    )
    assert engine.pool.checkedout() == 0


def test_import_coordinator_defaults_to_rollback_and_commits_only_success(database):
    factory = PostgresBackupUnitOfWorkFactory(runtime.get_engine)
    before = logical_bookmark_snapshot()
    with factory() as uow:
        uow.bookmarks.restore_group("Uncommitted", False, (), None)
    assert logical_bookmark_snapshot() == before
    service = create_app().state.backup_application_service
    assert service.work_runner.limiter.total_tokens == 1
    with pytest.raises(FrozenInstanceError):
        service.uow_factory = factory
    invalid = _single_group_document(with_url=False)
    invalid["groups"][0]["parent"] = "Missing"
    outcome = run_async(
        lambda: service.import_document(
            backup_domain.normalize_import_document(invalid)
        )
    )
    assert outcome == ImportInvalid()
    assert logical_bookmark_snapshot() == before

    failure = RuntimeError("Original worker failure")

    def fail(_stage):
        raise failure

    failing = replace(service, uow_factory=ObservedBackupFactory(fail))
    with pytest.raises(RuntimeError) as caught:
        run_async(
            lambda: failing.import_document(
                backup_domain.normalize_import_document(_single_group_document())
            )
        )
    assert caught.value is failure
    assert logical_bookmark_snapshot() == before
    assert runtime.get_engine().pool.checkedout() == 0


def test_invalid_imports_execute_no_dml(app, monkeypatch):
    base_url, _ = app
    parent = bookmark_helpers.seed_group("Parent")
    assert parent is not None
    child = bookmark_helpers.seed_group("Child", parent_id=parent["id"])
    assert child is not None
    status, _ = http_json(base_url, "/api/groups")
    assert status == 200
    statements, engine = capture_storage_statements(monkeypatch)
    invalid_documents = [
        {
            "version": 1,
            "exported_at": "2026-08-30T06:40:00Z",
            "groups": [],
        },
        {
            "version": 1,
            "exported_at": "2026-08-30T06:40:00Z",
            "groups": [
                {"name": "Same", "position": 0, "urls": []},
                {"name": "same", "position": 1, "urls": []},
            ],
        },
        {
            "version": 1,
            "exported_at": "2026-08-30T06:40:00Z",
            "groups": [
                {"name": "A", "position": 0, "urls": []},
                {"name": "B", "position": 0, "urls": []},
            ],
        },
        {
            "version": 1,
            "exported_at": "2026-08-30T06:40:00Z",
            "groups": [
                {
                    "name": "Parent",
                    "parent": "Child",
                    "position": 0,
                    "urls": [],
                }
            ],
        },
    ]

    try:
        for malformed_body in (b"{", b"null"):
            status, payload = http_raw(
                base_url,
                "/api/import",
                method="POST",
                body=malformed_body,
                content_type="application/json",
            )
            assert (status, payload) == (422, INVALID_IMPORT)
        for document in invalid_documents:
            status, payload = http_json(
                base_url,
                "/api/import",
                method="POST",
                payload=document,
            )
            assert (status, payload) == (422, INVALID_IMPORT)
    finally:
        engine.dispose()

    assert not any(_is_dml(statement) for statement in statements)


def test_import_error_contract_reports_held_gate(app):
    base_url, _ = app
    assert http_json(base_url, "/api/groups")[0] == 200
    outcome: dict[str, object] = {}

    def import_while_gate_is_held() -> None:
        outcome["response"] = http_json(
            base_url,
            "/api/import",
            method="POST",
            payload=_single_group_document(with_url=False),
        )

    with runtime.get_engine().connect() as blocker:
        with blocker.begin():
            acquired = blocker.scalar(
                text("SELECT pg_try_advisory_xact_lock(:namespace, :key)"),
                {
                    "namespace": persistence.BOOKMARK_MUTATION_LOCK_NAMESPACE,
                    "key": persistence.BOOKMARK_MUTATION_LOCK_KEY,
                },
            )
            assert acquired is True
            worker = threading.Thread(target=import_while_gate_is_held)
            worker.start()
            worker.join(timeout=1)
            assert not worker.is_alive(), "import waited for the held mutation gate"

    worker.join(timeout=5)
    assert outcome["response"] == (409, IMPORT_CONFLICT)


def test_import_post_validation_database_error_is_500_after_rollback(
    app,
    caplog,
    backup_uow_factory,
):
    base_url, _ = app
    _seed_rollback_state()
    document = _rollback_document()
    direct_before = logical_bookmark_snapshot()
    public_before = public_bookmark_snapshot(base_url)
    leak_markers = (
        "SELECT password_hash FROM administrator",
        "postgresql://admin:secret@database.invalid/trellmark",
        "private-host.invalid",
        'payload={"url":"https://private.invalid"}',
        "private-session-token",
        "private saved content",
    )

    def fail_after_stage(_stage: str) -> None:
        raise OperationalError(
            leak_markers[0],
            {"private_parameter": " | ".join(leak_markers[1:])},
            RuntimeError(" | ".join(leak_markers)),
            hide_parameters=True,
        )

    backup_uow_factory.observer = fail_after_stage

    request_headers = {
        **_authentication_headers(base_url),
        "Content-Type": "application/json",
        "Origin": base_url,
    }
    status, response_body, cache_control = _raw_import_response(
        base_url,
        body=json.dumps(document.model_dump(mode="json")).encode("utf-8"),
        headers=request_headers,
    )
    payload = json.loads(response_body)

    assert logical_bookmark_snapshot() == direct_before
    assert public_bookmark_snapshot(base_url) == public_before
    assert (status, payload) == (500, IMPORT_FAILED)
    assert cache_control == "no-store"
    rendered = str(payload)
    assert all(marker not in rendered for marker in leak_markers)
    assert all(marker not in caplog.text for marker in leak_markers)


def test_import_unexpected_failure_is_redacted_after_rollback(app, backup_uow_factory):
    base_url, _ = app
    leak_marker = "synthetic SQL and credential marker"

    def fail_after_stage(_stage: str) -> None:
        raise SQLAlchemyError(leak_marker)

    backup_uow_factory.observer = fail_after_stage
    status, payload = http_json(
        base_url,
        "/api/import",
        method="POST",
        payload=_single_group_document(with_url=False),
    )

    assert (status, payload) == (500, IMPORT_FAILED)
    assert leak_marker not in str(payload)


def test_import_openapi_documents_all_statuses_without_leaks():
    operation = create_app().openapi()["paths"]["/api/import"]["post"]
    responses = operation["responses"]

    assert set(responses) == {"200", "409", "422", "500"}
    assert operation["security"] == [{"CookieAuth": []}]
    expected = {
        "409": ("ImportConflictResponse", IMPORT_CONFLICT),
        "422": ("InvalidImportResponse", INVALID_IMPORT),
        "500": ("ImportFailedResponse", IMPORT_FAILED),
    }
    serialized = str(responses)
    for status, (schema_name, example) in expected.items():
        content = responses[status]["content"]["application/json"]
        assert content["schema"] == {"$ref": f"#/components/schemas/{schema_name}"}
        assert content["example"] == example
    assert "synthetic SQL and credential marker" not in serialized


def _raw_import_response(base_url, *, body: bytes, headers: dict[str, str]):
    import_request = request.Request(
        base_url + "/api/import",
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        response = request.urlopen(import_request, timeout=5)
    except error.HTTPError as caught:
        response = caught
    with response:
        return (
            response.status,
            response.read().decode("utf-8"),
            response.headers["Cache-Control"],
        )


@pytest.mark.parametrize(
    ("headers", "body", "expected_status", "expected_body"),
    [
        (
            {"Content-Type": "application/json"},
            b"{",
            401,
            '{"error":"Authentication required."}',
        ),
        (
            {"Content-Type": "application/json", "Origin": "https://other.invalid"},
            b"{}",
            403,
            '{"error":"Request origin not allowed."}',
        ),
        (
            {"Content-Type": "application/json"},
            b"{}",
            403,
            '{"error":"CSRF validation failed."}',
        ),
        (
            {"Content-Type": "application/json"},
            b"x" * (MAX_REQUEST_BODY_BYTES + 1),
            413,
            '{"error":"Request body too large."}',
        ),
    ],
    ids=("authentication", "origin", "csrf", "body_limit"),
)
def test_import_security_boundary_remains_intact(
    app,
    headers,
    body,
    expected_status,
    expected_body,
):
    base_url, _ = app
    request_headers = dict(headers)
    if expected_status != 401:
        request_headers.update(_authentication_headers(base_url))
    if expected_body == '{"error":"CSRF validation failed."}':
        request_headers.pop("X-CSRF-Token")
        request_headers["Origin"] = base_url
    elif expected_status in {413, 422}:
        request_headers["Origin"] = base_url

    status, response_body, cache_control = _raw_import_response(
        base_url,
        body=body,
        headers=request_headers,
    )

    assert (status, response_body) == (expected_status, expected_body)
    assert cache_control == "no-store"


def test_import_accepts_one_empty_group(app):
    base_url, _ = app
    status, payload = http_json(
        base_url,
        "/api/import",
        method="POST",
        payload=_single_group_document(with_url=False),
    )

    assert status == 200
    assert payload["imported"] == 0
    assert payload["skipped"] == 0
    assert group_names_in(payload) == ["Atomic", "default"]
