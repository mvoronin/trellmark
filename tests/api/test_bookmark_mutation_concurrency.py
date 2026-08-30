import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import text

import trellmark
from tests.helpers import RecordingTitleFetcher, http_json
from trellmark import storage
from trellmark.app import create_app
from trellmark.models import ImportDocument

ORDINARY_CONFLICT = {"error": "Another bookmark change is in progress. Try again."}
IMPORT_CONFLICT = {
    "error": (
        "Another bookmark change is in progress. "
        "No import changes were saved. Try again."
    ),
    "code": "import_conflict",
}


class ObservedTitleFetcher:
    def __init__(self) -> None:
        self.fetched = threading.Event()

    async def __call__(self, _url: str) -> str:
        self.fetched.set()
        return "Fetched before the gate"


@dataclass(frozen=True)
class BookmarkMutationCase:
    name: str
    method: str
    route_path: str
    prepare: Callable[[str], tuple[str, object | None]]


def _default_group_id() -> int:
    return next(
        group["id"]
        for group in trellmark.read_group_records()
        if group["name"] == "default"
    )


def _import_payload(group_name: str = "Imported") -> dict[str, object]:
    return {
        "version": 1,
        "exported_at": "2026-08-30T00:00:00Z",
        "groups": [
            {
                "name": group_name,
                "parent": None,
                "position": 0,
                "nsfw": False,
                "domains": [],
                "urls": [],
            }
        ],
    }


def _prepare_create_group(_base_url: str) -> tuple[str, object]:
    return "/api/groups", {"name": "Created"}


def _prepare_reorder_groups(_base_url: str) -> tuple[str, object]:
    first = trellmark.add_group("First")
    second = trellmark.add_group("Second")
    assert first is not None and second is not None
    return "/api/groups/order", {
        "parent_id": None,
        "group_ids": [second["id"], first["id"], _default_group_id()],
    }


def _prepare_edit_group(_base_url: str) -> tuple[str, object]:
    group = trellmark.add_group("Editable")
    assert group is not None
    return f"/api/groups/{group['id']}", {"name": "Renamed"}


def _prepare_delete_group(_base_url: str) -> tuple[str, object]:
    group = trellmark.add_group("Deletable")
    assert group is not None
    return f"/api/groups/{group['id']}", {"url_action": "delete"}


def _prepare_create_url(_base_url: str) -> tuple[str, object]:
    return "/api/urls", {"url": "https://created.example/path"}


def _prepare_move_url(_base_url: str) -> tuple[str, object]:
    group = trellmark.add_group("Target")
    record = trellmark.add_url("https://move.example/path")
    assert group is not None and record is not None
    return f"/api/urls/{record['id']}/group", {
        "group_id": group["id"],
        "source_group_id": _default_group_id(),
    }


def _prepare_set_important(_base_url: str) -> tuple[str, object]:
    record = trellmark.add_url("https://important.example/path")
    assert record is not None
    return f"/api/urls/{record['id']}/important", {"important": True}


def _prepare_refresh_title(_base_url: str) -> tuple[str, None]:
    record = trellmark.add_url("https://title.example/path", title="Old title")
    assert record is not None
    return f"/api/urls/{record['id']}/refresh-title", None


def _prepare_refresh_metadata(_base_url: str) -> tuple[str, None]:
    record = trellmark.add_url("https://metadata.example/path", title="Old title")
    assert record is not None
    return f"/api/urls/{record['id']}/refresh-metadata", None


def _prepare_edit_url(_base_url: str) -> tuple[str, object]:
    record = trellmark.add_url("https://edit.example/path", title="Old title")
    assert record is not None
    return f"/api/urls/{record['id']}", {
        "title": "New title",
        "version": record["version"],
    }


def _prepare_delete_url(_base_url: str) -> tuple[str, None]:
    record = trellmark.add_url("https://delete.example/path")
    assert record is not None
    return f"/api/urls/{record['id']}?group_id={_default_group_id()}", None


def _prepare_import(_base_url: str) -> tuple[str, object]:
    return "/api/import", _import_payload()


BOOKMARK_MUTATION_CASES = (
    BookmarkMutationCase("create_group", "POST", "/api/groups", _prepare_create_group),
    BookmarkMutationCase(
        "reorder_groups", "PATCH", "/api/groups/order", _prepare_reorder_groups
    ),
    BookmarkMutationCase(
        "edit_group", "PATCH", "/api/groups/{group_id}", _prepare_edit_group
    ),
    BookmarkMutationCase(
        "delete_group", "DELETE", "/api/groups/{group_id}", _prepare_delete_group
    ),
    BookmarkMutationCase("create_url", "POST", "/api/urls", _prepare_create_url),
    BookmarkMutationCase(
        "move_url_group",
        "PATCH",
        "/api/urls/{url_id}/group",
        _prepare_move_url,
    ),
    BookmarkMutationCase(
        "set_important",
        "PATCH",
        "/api/urls/{url_id}/important",
        _prepare_set_important,
    ),
    BookmarkMutationCase(
        "refresh_url_title",
        "POST",
        "/api/urls/{url_id}/refresh-title",
        _prepare_refresh_title,
    ),
    BookmarkMutationCase(
        "refresh_url_metadata",
        "POST",
        "/api/urls/{url_id}/refresh-metadata",
        _prepare_refresh_metadata,
    ),
    BookmarkMutationCase("edit_url", "PATCH", "/api/urls/{url_id}", _prepare_edit_url),
    BookmarkMutationCase(
        "delete_url", "DELETE", "/api/urls/{url_id}", _prepare_delete_url
    ),
    BookmarkMutationCase("import", "POST", "/api/import", _prepare_import),
)


def _hold_bookmark_mutation_gate(connection) -> None:
    acquired = connection.scalar(
        text("SELECT pg_try_advisory_xact_lock(:namespace, :key)"),
        {
            "namespace": storage.BOOKMARK_MUTATION_LOCK_NAMESPACE,
            "key": storage.BOOKMARK_MUTATION_LOCK_KEY,
        },
    )
    assert acquired is True


def _run_in_thread(work):
    outcome = {}

    def target():
        try:
            outcome["value"] = work()
        except BaseException as error:  # pragma: no cover - surfaced by assertions
            outcome["error"] = error

    thread = threading.Thread(target=target)
    thread.start()
    return thread, outcome


def _assert_immediate_conflict(work, expected=ORDINARY_CONFLICT):
    with trellmark.get_engine().connect() as blocker:
        with blocker.begin():
            _hold_bookmark_mutation_gate(blocker)
            thread, outcome = _run_in_thread(work)
            thread.join(timeout=1)
            finished_while_held = not thread.is_alive()

    thread.join(timeout=5)
    assert not thread.is_alive(), "bookmark mutation worker did not finish"
    assert finished_while_held, "bookmark mutation waited for the held gate"
    assert "error" not in outcome
    assert outcome["value"] == (409, expected)


def _registered_bookmark_mutations() -> set[tuple[str, str]]:
    registered: set[tuple[str, str]] = set()
    for route in create_app().routes:
        if not isinstance(route, APIRoute):
            continue
        if route.path != "/api/import" and not route.path.startswith(
            ("/api/groups", "/api/urls")
        ):
            continue
        for method in route.methods or set():
            if method in {"POST", "PATCH", "DELETE"}:
                registered.add((method, route.path))
    return registered


def test_bookmark_writer_matrix_matches_registered_routes():
    assert {
        (case.method, case.route_path) for case in BOOKMARK_MUTATION_CASES
    } == _registered_bookmark_mutations()


@pytest.mark.parametrize(
    "title_fetcher",
    [RecordingTitleFetcher("Fetched title")],
    indirect=True,
)
@pytest.mark.parametrize("case", BOOKMARK_MUTATION_CASES, ids=lambda case: case.name)
def test_every_bookmark_writer_conflicts_immediately(app, case):
    base_url, _ = app
    path, payload = case.prepare(base_url)

    _assert_immediate_conflict(
        lambda: http_json(
            base_url,
            path,
            method=case.method,
            payload=payload,
        ),
        expected=IMPORT_CONFLICT if case.name == "import" else ORDINARY_CONFLICT,
    )


def _observed_response_while_held(work):
    thread, outcome = _run_in_thread(work)
    thread.join(timeout=1)
    return thread, outcome, not thread.is_alive()


def _assert_observed_conflict(observation, expected=ORDINARY_CONFLICT) -> None:
    thread, outcome, finished_while_held = observation
    thread.join(timeout=5)
    assert not thread.is_alive(), "competing request did not finish"
    assert finished_while_held, "competing request waited for the winner"
    assert "error" not in outcome
    assert outcome["value"] == (409, expected)


def test_import_directions_conflict_before_the_winner_releases(app):
    base_url, _ = app
    winner_document = ImportDocument.model_validate(_import_payload("Import Winner"))
    winner_started = threading.Event()
    release_winner = threading.Event()

    def hold_import(stage: str) -> None:
        if stage == "group_metadata":
            winner_started.set()
            assert release_winner.wait(timeout=5), "test did not release import"

    winner_thread, winner_outcome = _run_in_thread(
        lambda: storage.import_saved_data(
            winner_document.to_storage_document(),
            validate_against=winner_document.validate_against,
            after_stage=hold_import,
        )
    )
    assert winner_started.wait(timeout=5), "import did not reach its mutation stage"
    try:
        import_winner_observations = [
            _observed_response_while_held(
                lambda: http_json(
                    base_url,
                    "/api/groups",
                    method="POST",
                    payload={"name": "Ordinary Loser"},
                )
            ),
            _observed_response_while_held(
                lambda: http_json(
                    base_url,
                    "/api/import",
                    method="POST",
                    payload=_import_payload("Import Loser"),
                )
            ),
        ]
    finally:
        release_winner.set()

    winner_thread.join(timeout=5)
    assert not winner_thread.is_alive()
    assert "error" not in winner_outcome
    _assert_observed_conflict(import_winner_observations[0])
    _assert_observed_conflict(
        import_winner_observations[1],
        expected=IMPORT_CONFLICT,
    )

    competing_document = _import_payload("Import After Ordinary")
    with storage._bookmark_mutation() as connection:
        created, error = storage._create_group_on(connection, "Ordinary Winner")
        assert created is not None and error is None
        ordinary_winner_observation = _observed_response_while_held(
            lambda: http_json(
                base_url,
                "/api/import",
                method="POST",
                payload=competing_document,
            )
        )

    _assert_observed_conflict(ordinary_winner_observation, expected=IMPORT_CONFLICT)


def test_reads_and_export_remain_available_while_gate_is_held(app):
    base_url, _ = app
    group = trellmark.add_group("Reading")
    record = trellmark.add_url("https://available.example/path")
    assert group is not None and record is not None

    with trellmark.get_engine().connect() as blocker:
        with blocker.begin():
            _hold_bookmark_mutation_gate(blocker)
            observations = {
                path: _observed_response_while_held(
                    lambda path=path: http_json(base_url, path)
                )
                for path in ("/api/groups", "/api/urls", "/api/export")
            }

    for path, (thread, outcome, finished_while_held) in observations.items():
        thread.join(timeout=5)
        assert not thread.is_alive(), f"{path} did not finish"
        assert finished_while_held, f"{path} waited for the bookmark gate"
        assert "error" not in outcome
        status, payload = outcome["value"]
        assert status == 200
        if path == "/api/groups":
            assert {item["name"] for item in payload["groups"]} == {
                "default",
                "Reading",
            }
        elif path == "/api/urls":
            assert [item["url"] for item in payload["urls"]] == [
                "https://available.example/path"
            ]
        else:
            assert payload["version"] == 1
            assert "exported_at" in payload


@pytest.mark.parametrize(
    ("endpoint", "title_fetcher"),
    [
        ("refresh-title", ObservedTitleFetcher()),
        ("refresh-metadata", ObservedTitleFetcher()),
    ],
    indirect=["title_fetcher"],
)
def test_metadata_fetch_precedes_the_gated_title_write(
    app,
    title_fetcher,
    endpoint,
):
    base_url, _ = app
    record = trellmark.add_url("https://fetch-order.example/path", title="Old title")
    assert record is not None

    with trellmark.get_engine().connect() as blocker:
        with blocker.begin():
            _hold_bookmark_mutation_gate(blocker)
            observation = _observed_response_while_held(
                lambda: http_json(
                    base_url,
                    f"/api/urls/{record['id']}/{endpoint}",
                    method="POST",
                )
            )
            assert title_fetcher.fetched.is_set(), "title fetch did not finish first"

    _assert_observed_conflict(observation)
    current = trellmark.read_url_record_by_id(record["id"])
    assert current is not None and current["title"] == "Old title"


def test_site_icon_cache_write_remains_available_while_gate_is_held(app):
    _, _ = app
    retry_after = datetime.now(timezone.utc)

    with trellmark.get_engine().connect() as blocker:
        with blocker.begin():
            _hold_bookmark_mutation_gate(blocker)
            thread, outcome, finished_while_held = _observed_response_while_held(
                lambda: storage.upsert_site_icon_failure(
                    "https://cache.example",
                    retry_after,
                )
            )

    thread.join(timeout=5)
    assert not thread.is_alive()
    assert finished_while_held, "private icon cache waited for the bookmark gate"
    assert "error" not in outcome
    assert outcome["value"]["origin"] == "https://cache.example"


def test_gate_releases_after_commit(app):
    _, _ = app
    committed, error = trellmark.create_group_record("Committed")
    assert committed is not None and error is None

    retried, retry_error = trellmark.create_group_record("After Commit")

    assert retried is not None and retry_error is None


def test_gate_releases_after_rollback(app, monkeypatch):
    _, _ = app
    insert_domains = storage._insert_group_domains

    def fail_after_group_insert(connection, group_id, domains):
        insert_domains(connection, group_id, domains)
        raise RuntimeError("synthetic rollback")

    monkeypatch.setattr(storage, "_insert_group_domains", fail_after_group_insert)
    with pytest.raises(RuntimeError, match="synthetic rollback"):
        trellmark.create_group_record(
            "Rolled Back",
            domains=["rollback.example"],
        )
    monkeypatch.setattr(storage, "_insert_group_domains", insert_domains)

    retried, retry_error = trellmark.create_group_record("After Rollback")

    assert trellmark.read_group_record_by_name("Rolled Back") is None
    assert retried is not None and retry_error is None


def test_gate_releases_after_import_validation_rejection(app):
    base_url, _ = app
    invalid = _import_payload("Invalid Child")
    invalid_groups = invalid["groups"]
    assert isinstance(invalid_groups, list)
    invalid_groups[0]["parent"] = "Missing Parent"

    status, _ = http_json(
        base_url,
        "/api/import",
        method="POST",
        payload=invalid,
    )
    retried, retry_error = trellmark.create_group_record("After Rejection")

    assert status == 422
    assert retried is not None and retry_error is None


def test_gate_releases_after_import_conflict_for_one_explicit_manual_retry(app):
    base_url, _ = app
    document = _import_payload("Manual Retry")

    _assert_immediate_conflict(
        lambda: http_json(
            base_url,
            "/api/import",
            method="POST",
            payload=document,
        ),
        expected=IMPORT_CONFLICT,
    )

    status, payload = http_json(
        base_url,
        "/api/import",
        method="POST",
        payload=document,
    )

    assert status == 200
    assert payload["imported"] == 0
    assert any(group["name"] == "Manual Retry" for group in payload["groups"])
