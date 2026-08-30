import threading
from urllib import error, request

import pytest
from sqlalchemy import text

import trellmark
from tests.helpers import (
    _authentication_headers,
    capture_connection_identities,
    capture_storage_statements,
    group_in,
    group_names_in,
    grouped_urls_in,
    http_json,
    http_raw,
    logical_bookmark_snapshot,
    public_bookmark_snapshot,
)
from trellmark import handlers, storage
from trellmark.app import MAX_REQUEST_BODY_BYTES, create_app
from trellmark.models import ImportDocument

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


def test_import_uses_single_connection_and_outer_transaction(app, monkeypatch):
    base_url, _ = app
    # Authenticate before instrumenting content storage so the identity boundary's
    # independent transaction cannot be mistaken for an import connection.
    status, _ = http_json(base_url, "/api/groups")
    assert status == 200

    statements, engine = capture_storage_statements(monkeypatch)
    connection_identities, identity_engine = capture_connection_identities(monkeypatch)
    assert identity_engine is engine
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
    original_validate = ImportDocument.validate_against
    validation_statement_count: list[int] = []

    def validate_inside_transaction(self, existing_groups):
        validation_statement_count.append(len(statements))
        assert any("pg_try_advisory_xact_lock" in statement for statement in statements)
        assert not any(_is_dml(statement) for statement in statements)
        return original_validate(self, existing_groups)

    monkeypatch.setattr(ImportDocument, "validate_against", validate_inside_transaction)
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
    old_parent = trellmark.add_group("Old Parent")
    destination = trellmark.add_group("Destination")
    assert old_parent is not None
    assert destination is not None
    imported_child = trellmark.add_group(
        "Imported Child",
        domains=["old.example"],
        parent_id=old_parent["id"],
    )
    assert imported_child is not None
    existing = trellmark.add_url(
        "https://existing.example/item",
        title="Existing metadata",
    )
    assert existing is not None
    trellmark.update_url_created_at(
        existing["id"],
        _rollback_document().to_storage_document()["groups"][0]["urls"][0][
            "created_at"
        ],
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

    assert storage.IMPORT_MUTATION_STAGES == EXPECTED_IMPORT_MUTATION_STAGES
    with pytest.raises(RuntimeError, match="Synthetic import stage failure"):
        storage.import_saved_data(
            document.to_storage_document(),
            validate_against=document.validate_against,
            after_stage=fail_after_stage,
        )

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


def test_invalid_imports_execute_no_dml(app, monkeypatch):
    base_url, _ = app
    parent = trellmark.add_group("Parent")
    assert parent is not None
    child = trellmark.add_group("Child", parent_id=parent["id"])
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

    with trellmark.get_engine().connect() as blocker:
        with blocker.begin():
            acquired = blocker.scalar(
                text("SELECT pg_try_advisory_xact_lock(:namespace, :key)"),
                {
                    "namespace": storage.BOOKMARK_MUTATION_LOCK_NAMESPACE,
                    "key": storage.BOOKMARK_MUTATION_LOCK_KEY,
                },
            )
            assert acquired is True
            worker = threading.Thread(target=import_while_gate_is_held)
            worker.start()
            worker.join(timeout=1)
            assert not worker.is_alive(), "import waited for the held mutation gate"

    worker.join(timeout=5)
    assert outcome["response"] == (409, IMPORT_CONFLICT)


def test_import_post_validation_value_error_is_500_after_rollback(app, monkeypatch):
    base_url, _ = app
    _seed_rollback_state()
    document = _rollback_document()
    direct_before = logical_bookmark_snapshot()
    public_before = public_bookmark_snapshot(base_url)
    original_import = handlers.import_saved_data
    leak_markers = (
        "SELECT password_hash FROM administrator",
        "postgresql://admin:secret@database.invalid/trellmark",
        "private-host.invalid",
        'payload={"url":"https://private.invalid"}',
    )

    def fail_after_validation(document, *, validate_against):
        def fail_after_stage(_stage: str) -> None:
            raise ValueError(" | ".join(leak_markers))

        return original_import(
            document,
            validate_against=validate_against,
            after_stage=fail_after_stage,
        )

    monkeypatch.setattr(handlers, "import_saved_data", fail_after_validation)

    status, payload = http_json(
        base_url,
        "/api/import",
        method="POST",
        payload=document.model_dump(mode="json"),
    )

    assert logical_bookmark_snapshot() == direct_before
    assert public_bookmark_snapshot(base_url) == public_before
    assert (status, payload) == (500, IMPORT_FAILED)
    rendered = str(payload)
    assert all(marker not in rendered for marker in leak_markers)


def test_import_unexpected_failure_is_redacted_after_rollback(app, monkeypatch):
    base_url, _ = app
    original_import = handlers.import_saved_data
    leak_marker = "synthetic SQL and credential marker"

    def fail_unexpectedly(document, *, validate_against):
        def fail_after_stage(_stage: str) -> None:
            raise RuntimeError(leak_marker)

        return original_import(
            document,
            validate_against=validate_against,
            after_stage=fail_after_stage,
        )

    monkeypatch.setattr(handlers, "import_saved_data", fail_unexpectedly)
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
