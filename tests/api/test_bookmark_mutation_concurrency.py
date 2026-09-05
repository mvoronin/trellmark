import asyncio
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone

import pytest
from fastapi.routing import APIRoute, iter_route_contexts
from sqlalchemy import event, text

from tests.backup.helpers import ObservedBackupFactory
from tests.bookmarks import helpers as bookmark_helpers
from tests.bookmarks.helpers import (
    icon_cache_service,
    make_icon_service,
    seed_url,
    url_payload,
)
from tests.helpers import (
    RecordingTitleFetcher,
    http_json,
    logical_bookmark_snapshot,
    public_bookmark_snapshot,
    run_async,
)
from trellmark.app import create_app
from trellmark.backup.api import ImportDocument
from trellmark.backup.application import import_document_in_uow
from trellmark.backup.domain import normalize_import_document
from trellmark.backup.persistence import PostgresBackupUnitOfWorkFactory
from trellmark.bookmarks import domain, persistence
from trellmark.bookmarks.application import BookmarksApplicationService
from trellmark.platform import runtime
from trellmark.platform.runtime import get_engine

ORDINARY_CONFLICT = {"error": "Another bookmark change is in progress. Try again."}
IMPORT_CONFLICT = {
    "error": (
        "Another bookmark change is in progress. "
        "No import changes were saved. Try again."
    ),
    "code": "import_conflict",
}


@pytest.mark.parametrize("query", ["list_groups", "group_by_name"])
def test_group_read_keeps_visibility_and_content_in_one_snapshot(database, query):
    bookmark_helpers.seed_group("Snapshot", domains=["before.example"])
    document = normalize_import_document(
        {
            "version": 1,
            "exported_at": "2026-09-05T00:00:00Z",
            "groups": [
                {
                    "name": "Snapshot",
                    "parent": None,
                    "position": 0,
                    "nsfw": True,
                    "domains": ["after.example"],
                    "urls": [
                        {
                            "url": "https://private.example/synthetic",
                            "created_at": "2026-09-05T00:00:00Z",
                            "important": False,
                            "title": None,
                        }
                    ],
                }
            ],
        }
    )
    engine = get_engine()
    imported = False

    def before_query(_connection, _cursor, statement, *_args):
        nonlocal imported
        content_query = (
            "FROM url_groups JOIN urls"
            if query == "list_groups"
            else "FROM group_domains"
        )
        if not imported and content_query in statement:
            imported = True
            import_document_in_uow(
                PostgresBackupUnitOfWorkFactory(get_engine), document
            )

    queries = persistence.PostgresGroupQueries(get_engine)

    def read_group():
        if query == "list_groups":
            return next(
                group for group in queries.list_groups() if group.name == "Snapshot"
            )
        return queries.group_by_name("Snapshot")

    event.listen(engine, "before_cursor_execute", before_query)
    try:
        before = read_group()
    finally:
        event.remove(engine, "before_cursor_execute", before_query)
    assert imported
    assert before is not None and before.nsfw is False
    assert before.domains == ("before.example",)
    assert before.urls == ()
    after = read_group()
    assert after is not None and after.nsfw is True
    assert after.domains == ("after.example",)
    if query == "list_groups":
        assert [url.url for url in after.urls] == ["https://private.example/synthetic"]
    with engine.connect() as connection:
        assert (
            connection.exec_driver_sql("SHOW transaction_isolation").scalar()
            == "read committed"
        )
        assert (
            connection.exec_driver_sql("SHOW transaction_read_only").scalar() == "off"
        )


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
        for group in bookmark_helpers.group_payloads()
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
    first = bookmark_helpers.seed_group("First")
    second = bookmark_helpers.seed_group("Second")
    assert first is not None and second is not None
    return "/api/groups/order", {
        "parent_id": None,
        "group_ids": [second["id"], first["id"], _default_group_id()],
    }


def _prepare_edit_group(_base_url: str) -> tuple[str, object]:
    group = bookmark_helpers.seed_group("Editable")
    assert group is not None
    return f"/api/groups/{group['id']}", {"name": "Renamed"}


def _prepare_delete_group(_base_url: str) -> tuple[str, object]:
    group = bookmark_helpers.seed_group("Deletable")
    assert group is not None
    return f"/api/groups/{group['id']}", {"url_action": "delete"}


def _prepare_create_url(_base_url: str) -> tuple[str, object]:
    return "/api/urls", {"url": "https://created.example/path"}


def _prepare_move_url(_base_url: str) -> tuple[str, object]:
    group = bookmark_helpers.seed_group("Target")
    record = bookmark_helpers.seed_url("https://move.example/path")
    assert group is not None and record is not None
    return f"/api/urls/{record['id']}/group", {
        "group_id": group["id"],
        "source_group_id": _default_group_id(),
    }


def _prepare_set_important(_base_url: str) -> tuple[str, object]:
    record = bookmark_helpers.seed_url("https://important.example/path")
    assert record is not None
    return f"/api/urls/{record['id']}/important", {"important": True}


def _prepare_refresh_title(_base_url: str) -> tuple[str, None]:
    record = seed_url("https://title.example/path", title="Old title")
    assert record is not None
    return f"/api/urls/{record['id']}/refresh-title", None


def _prepare_refresh_metadata(_base_url: str) -> tuple[str, None]:
    record = bookmark_helpers.seed_url(
        "https://metadata.example/path", title="Old title"
    )
    assert record is not None
    return f"/api/urls/{record['id']}/refresh-metadata", None


def _prepare_edit_url(_base_url: str) -> tuple[str, object]:
    record = bookmark_helpers.seed_url("https://edit.example/path", title="Old title")
    assert record is not None
    return f"/api/urls/{record['id']}", {
        "title": "New title",
        "version": record["version"],
    }


def _prepare_delete_url(_base_url: str) -> tuple[str, None]:
    record = bookmark_helpers.seed_url("https://delete.example/path")
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
            "namespace": persistence.BOOKMARK_MUTATION_LOCK_NAMESPACE,
            "key": persistence.BOOKMARK_MUTATION_LOCK_KEY,
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


def _assert_immediate_conflict(work, expected=ORDINARY_CONFLICT, *, while_held=None):
    with get_engine().connect() as blocker:
        with blocker.begin():
            _hold_bookmark_mutation_gate(blocker)
            thread, outcome = _run_in_thread(work)
            thread.join(timeout=1)
            finished_while_held = not thread.is_alive()
            if while_held is not None:
                while_held()

    thread.join(timeout=5)
    assert not thread.is_alive(), "bookmark mutation worker did not finish"
    assert finished_while_held, "bookmark mutation waited for the held gate"
    assert "error" not in outcome
    assert outcome["value"] == (409, expected)


def _registered_bookmark_mutations() -> set[tuple[str, str]]:
    registered: set[tuple[str, str]] = set()
    for context in iter_route_contexts(create_app().routes):
        route = context.route
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
def test_every_bookmark_writer_conflicts_immediately(app, case, monkeypatch):
    base_url, _ = app
    path, payload = case.prepare(base_url)
    before = logical_bookmark_snapshot()
    public_before = public_bookmark_snapshot(base_url)
    gate_attempts = []
    engine = get_engine()

    def capture_gate(_connection, _cursor, statement, _parameters, _context, _many):
        if "pg_try_advisory_xact_lock" in statement:
            gate_attempts.append(statement)

    ordinary_method = {
        "edit_url": "edit_url",
        "move_url_group": "move_url",
        "delete_url": "remove_url",
        "set_important": "set_important",
    }.get(case.name)
    group_method = {
        "create_group": "create_group",
        "reorder_groups": "reorder_groups",
        "edit_group": "update_group",
        "delete_group": "delete_group",
    }.get(case.name)
    title_method = {
        "create_url": "create_url",
        "refresh_url_title": "refresh_url_title",
    }.get(case.name)
    service_method = ordinary_method or group_method or title_method
    services = []
    if service_method is not None:
        original = getattr(BookmarksApplicationService, service_method)

        async def observed(application, command):
            services.append(application)
            assert isinstance(
                application.logical_uow_factory,
                persistence.PostgresLogicalBookmarkUnitOfWorkFactory,
            )
            assert isinstance(application.url_queries, persistence.PostgresURLQueries)
            assert isinstance(
                application.group_queries, persistence.PostgresGroupQueries
            )
            return await original(application, command)

        monkeypatch.setattr(BookmarksApplicationService, service_method, observed)

    def assert_reads_available():
        # The same held lock covers rejection and independent public reads.
        assert public_bookmark_snapshot(base_url) == public_before

    event.listen(engine, "before_cursor_execute", capture_gate)
    try:
        _assert_immediate_conflict(
            lambda: http_json(base_url, path, method=case.method, payload=payload),
            expected=IMPORT_CONFLICT if case.name == "import" else ORDINARY_CONFLICT,
            while_held=assert_reads_available,
        )
        # One lock for the blocker and exactly one failed try-lock for the
        # request. The request returns before release, with no hidden retry.
        assert len(gate_attempts) == 2
        assert logical_bookmark_snapshot() == before
        assert public_bookmark_snapshot(base_url) == public_before
        if service_method is not None:
            assert len(services) == 1
        status, result = http_json(base_url, path, method=case.method, payload=payload)
    finally:
        event.remove(engine, "before_cursor_execute", capture_gate)
    assert status == (201 if case.name in {"create_url", "create_group"} else 200)
    assert logical_bookmark_snapshot() != before
    if service_method is not None:
        # Create retries with an insert and a separate post-fetch title UoW.
        assert len(gate_attempts) == (4 if case.name == "create_url" else 3)
        assert len(services) == 2 and services[0] is services[1]
    if ordinary_method is not None:
        if case.name == "edit_url":
            assert result["url"]["version"] == payload["version"] + 1
            assert result["url"]["title"] == payload["title"]
        elif case.name == "move_url_group":
            assert services[0].url_queries.url_group_ids(result["url"]["id"]) == (
                payload["group_id"],
            )
            assert result["url"]["version"] == 1
        elif case.name == "delete_url":
            assert services[0].url_queries.url_by_id(result["url"]["id"]) is None
        else:
            assert result["url"]["important"] is True
            assert result["url"]["version"] == 1


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
        lambda: import_document_in_uow(
            ObservedBackupFactory(hold_import),
            winner_document.to_domain(),
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
    with persistence.PostgresLogicalBookmarkUnitOfWorkFactory(get_engine)() as uow:
        created = uow.groups.create_group(domain.CreateGroup("Ordinary Winner"))
        assert isinstance(created, domain.GroupCreated)
        ordinary_winner_observation = _observed_response_while_held(
            lambda: http_json(
                base_url,
                "/api/import",
                method="POST",
                payload=competing_document,
            )
        )
        uow.commit()

    _assert_observed_conflict(ordinary_winner_observation, expected=IMPORT_CONFLICT)


def test_reads_and_export_remain_available_while_gate_is_held(app):
    base_url, _ = app
    group = bookmark_helpers.seed_group("Reading")
    record = bookmark_helpers.seed_url("https://available.example/path")
    assert group is not None and record is not None

    with runtime.get_engine().connect() as blocker:
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
    ("active_operation", "queued_operation"),
    [
        ("import", "import"),
        ("import", "export"),
        ("export", "import"),
    ],
)
def test_import_export_capacity_one_encloses_complete_worker_scope(
    database, active_operation, queued_operation
):
    application = create_app()
    service = application.state.backup_application_service
    bookmarks = application.state.bookmarks_application_service
    engine = get_engine()
    release = threading.Event()
    worker_threads = []
    connections = []
    transaction_events = []

    async def exercise():
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        loop_thread = threading.get_ident()

        def observe_sql(connection, _cursor, statement, *_args):
            if not connections and (
                "pg_try_advisory_xact_lock" in statement
                or (
                    statement.startswith("SELECT groups.id,")
                    and "FROM groups ORDER BY" in statement
                )
            ):
                connections.append(connection)
                worker_threads.append(threading.get_ident())
                loop.call_soon_threadsafe(started.set)
                assert release.wait(timeout=5)
            if connections and connection is connections[0]:
                transaction_events.append(("sql", threading.get_ident()))

        def observe_end(connection):
            if connections and connection is connections[0]:
                transaction_events.append(("end", threading.get_ident()))

        def observe_checkin(_dbapi, _record):
            if transaction_events and transaction_events[-1][0] == "end":
                transaction_events.append(("close", threading.get_ident()))

        async def invoke(operation, name):
            if operation == "import":
                return await service.import_document(
                    normalize_import_document(_import_payload(name))
                )
            return await service.export_document("2026-08-30T00:00:00Z")

        listeners = (
            ("before_cursor_execute", observe_sql),
            ("commit", observe_end),
            ("rollback", observe_end),
            ("checkin", observe_checkin),
        )
        for name, listener in listeners:
            event.listen(engine, name, listener)
        first = asyncio.create_task(invoke(active_operation, "First"))
        second = None
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            assert worker_threads[0] != loop_thread
            second = asyncio.create_task(invoke(queued_operation, "Second"))

            async def queued():
                while service.work_runner.limiter.statistics().tasks_waiting != 1:
                    await asyncio.sleep(0)

            await asyncio.wait_for(queued(), timeout=5)
            assert service.work_runner.limiter.borrowed_tokens == 1
            assert engine.pool.checkedout() == 1
            # The event loop and the independent Bookmark read worker both
            # progress while Backup's first complete connection scope waits.
            groups = await asyncio.wait_for(bookmarks.list_groups(), timeout=5)
            assert [group.name for group in groups] == ["default"]
            assert not first.done() and not second.done()
        finally:
            release.set()
            tasks = [first] if second is None else [first, second]
            await asyncio.gather(*tasks)
            for name, listener in listeners:
                event.remove(engine, name, listener)
        assert transaction_events[-1][0] == "close"
        assert {thread for _, thread in transaction_events} == set(worker_threads)
        assert service.work_runner.limiter.borrowed_tokens == 0
        assert engine.pool.checkedout() == 0

    run_async(exercise)


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
    record = seed_url("https://fetch-order.example/path", title="Old title")
    assert record is not None

    with get_engine().connect() as blocker:
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
    current = url_payload(record["id"])
    assert current is not None and current["title"] == "Old title"


def test_site_icon_cache_write_remains_available_while_gate_is_held(app):
    _, _ = app
    retry_after = datetime.now(timezone.utc)

    with runtime.get_engine().connect() as blocker:
        with blocker.begin():
            _hold_bookmark_mutation_gate(blocker)
            thread, outcome, finished_while_held = _observed_response_while_held(
                lambda: run_async(
                    lambda: icon_cache_service().failure(
                        "https://cache.example", retry_after
                    )
                )
            )

    thread.join(timeout=5)
    assert not thread.is_alive()
    assert finished_while_held, "private icon cache waited for the bookmark gate"
    assert "error" not in outcome
    assert outcome["value"].origin == "https://cache.example"


def test_derived_icon_cache_sql_is_ungated_and_touches_only_cache(database):
    engine = get_engine()
    before = logical_bookmark_snapshot()
    service = icon_cache_service()
    statements = []
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def capture(_connection, _cursor, statement, *_args):
        statements.append(statement.lower())

    async def exercise():
        positive = await service.success(
            "https://isolated-cache.example",
            domain.SiteIcon(b"\x89PNG\r\n\x1a\nvalue", "image/png"),
            now,
            now,
        )
        negative = await service.failure("https://isolated-cache.example", now)
        assert (
            positive == negative == await service.read("https://isolated-cache.example")
        )
        return negative

    with engine.connect() as blocker, blocker.begin():
        _hold_bookmark_mutation_gate(blocker)
        event.listen(engine, "before_cursor_execute", capture)
        try:
            worker, result = _run_in_thread(lambda: run_async(exercise))
            worker.join(timeout=5)
            assert not worker.is_alive(), "cache waited for the logical gate"
        finally:
            event.remove(engine, "before_cursor_execute", capture)
    worker.join(timeout=5)
    assert "error" not in result
    assert result["value"].origin == "https://isolated-cache.example"
    assert len(statements) == 3
    assert all("site_icon_cache" in statement for statement in statements)
    assert all("advisory" not in statement for statement in statements)
    assert all(
        not any(
            table in statement
            for table in ("groups", "urls", "url_groups", "group_domains")
        )
        for statement in statements
    )
    assert logical_bookmark_snapshot() == before


def test_derived_cache_capacity_is_two_and_independent_of_logical_work(database):
    engine = get_engine()
    service = icon_cache_service()
    assert service.work_runner.limiter.total_tokens == 2
    active = 0
    maximum = 0
    lock = threading.Lock()
    release = threading.Event()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    async def exercise():
        loop = asyncio.get_running_loop()
        two_started = asyncio.Event()

        def hold_write(_connection, _cursor, statement, *_args):
            nonlocal active, maximum
            if not statement.startswith("INSERT INTO site_icon_cache"):
                return
            with lock:
                active += 1
                maximum = max(active, maximum)
                if active == 2:
                    loop.call_soon_threadsafe(two_started.set)
            assert release.wait(timeout=5)
            with lock:
                active -= 1

        event.listen(engine, "before_cursor_execute", hold_write)
        tasks = [
            asyncio.create_task(service.failure(f"https://capacity-{i}.example", now))
            for i in range(3)
        ]
        try:
            await asyncio.wait_for(two_started.wait(), timeout=5)
            assert service.work_runner.limiter.borrowed_tokens == 2
            assert service.work_runner.limiter.statistics().tasks_waiting == 1
            assert engine.pool.checkedout() == 2
            # A third, logical scope can acquire its own gate while cache workers wait.
            with persistence.PostgresLogicalBookmarkUnitOfWorkFactory(
                get_engine
            )() as uow:
                assert uow.bookmarks.url_by_id(999) is None
        finally:
            release.set()
            await asyncio.gather(*tasks)
            event.remove(engine, "before_cursor_execute", hold_write)

    run_async(exercise)
    assert maximum == 2


@pytest.mark.parametrize(
    "change",
    [
        "none",
        "edit",
        "remove",
        "important",
        "no_title_edit",
        "no_title_remove",
        "gate_held",
    ],
)
def test_metadata_fetch_barriers_precede_gate_and_revalidation(
    database, bookmarks_service, change
):
    record = seed_url("https://metadata-barrier.example", "Original")
    engine = get_engine()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    async def exercise():
        release_fetch = asyncio.Event()
        title_started = asyncio.Event()
        icon_started = asyncio.Event()
        gates = []

        async def title(_url):
            title_started.set()
            await release_fetch.wait()
            return None if change.startswith("no_title") else "Fetched"

        async def icon(_url):
            icon_started.set()
            await release_fetch.wait()
            return domain.SiteIcon(b"\x89PNG\r\n\x1a\nbarrier", "image/png")

        gateway = make_icon_service(fetcher=icon, clock=lambda: now)
        service = replace(bookmarks_service, title_fetcher=title, icon_gateway=gateway)

        def capture_gate(_connection, _cursor, statement, *_args):
            if "pg_try_advisory_xact_lock" in statement:
                gates.append(release_fetch.is_set())

        with engine.connect() as blocker:
            transaction = blocker.begin()
            _hold_bookmark_mutation_gate(blocker)
            event.listen(engine, "before_cursor_execute", capture_gate)
            task = asyncio.create_task(service.refresh_url_metadata(record["id"]))
            try:
                await asyncio.wait_for(
                    asyncio.gather(title_started.wait(), icon_started.wait()), timeout=5
                )
                assert gates == []
                assert engine.pool.checkedout() == 1
                assert service.work_runner.limiter.borrowed_tokens == 0
                assert gateway._cache.work_runner.limiter.borrowed_tokens == 0
                if change != "gate_held":
                    transaction.rollback()
                if change.endswith("edit"):
                    changed = await bookmarks_service.edit_url(
                        domain.EditURL(record["id"], record["version"], title="Manual")
                    )
                    assert isinstance(changed, domain.URLUpdated)
                elif change.endswith("remove"):
                    removed = await bookmarks_service.remove_url(
                        domain.RemoveURL(record["id"], 1)
                    )
                    assert isinstance(removed, domain.URLRemoved)
                elif change == "important":
                    changed = await bookmarks_service.set_important(
                        domain.SetImportant(record["id"], True)
                    )
                    assert isinstance(changed, domain.SetImportantSucceeded)
                gates.clear()
                release_fetch.set()
                if change == "gate_held":
                    with pytest.raises(domain.BookmarkMutationConflict):
                        await asyncio.wait_for(task, timeout=5)
                    result = None
                else:
                    result = await asyncio.wait_for(task, timeout=5)
            finally:
                release_fetch.set()
                if transaction.is_active:
                    transaction.rollback()
                await asyncio.gather(task, return_exceptions=True)
                event.remove(engine, "before_cursor_execute", capture_gate)
        assert gates == ([] if change.startswith("no_title") else [True])
        cached = await gateway._cache.read("https://metadata-barrier.example")
        assert cached is not None and cached.icon_bytes is not None
        return result

    result = run_async(exercise)
    current = persistence.PostgresURLQueries(get_engine).url_by_id(record["id"])
    if change == "edit":
        assert result == domain.URLVersionConflict(record["id"], record["version"])
        assert current.title == "Manual" and current.version == 2
    elif change.endswith("remove"):
        assert result == domain.URLNotFound(record["id"])
        assert current is None
    elif change == "gate_held":
        assert result is None and current.title == "Original" and current.version == 1
    else:
        assert isinstance(result, domain.URLMetadataRefreshed)
        assert result.icon_updated is True
        assert result.title_updated is (change != "no_title_edit")
        assert result.record == current
        assert current.title == ("Manual" if change == "no_title_edit" else "Fetched")
        assert current.version == 2
        assert current.important is (change == "important")


def test_gate_releases_after_commit(app):
    _, _ = app
    committed = bookmark_helpers.create_group_outcome("Committed")
    assert isinstance(committed, domain.GroupCreated)

    retried = bookmark_helpers.create_group_outcome("After Commit")

    assert isinstance(retried, domain.GroupCreated)


def test_gate_releases_after_rollback(app, monkeypatch):
    _, _ = app
    insert_domains = persistence._insert_group_domains

    def fail_after_group_insert(connection, group_id, domains):
        insert_domains(connection, group_id, domains)
        raise RuntimeError("synthetic rollback")

    monkeypatch.setattr(persistence, "_insert_group_domains", fail_after_group_insert)
    with pytest.raises(RuntimeError, match="synthetic rollback"):
        bookmark_helpers.create_group_outcome(
            "Rolled Back",
            domains=["rollback.example"],
        )
    monkeypatch.setattr(persistence, "_insert_group_domains", insert_domains)

    retried = bookmark_helpers.create_group_outcome("After Rollback")

    assert bookmark_helpers.group_by_name("Rolled Back") is None
    assert isinstance(retried, domain.GroupCreated)


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
    retried = bookmark_helpers.create_group_outcome("After Rejection")

    assert status == 422
    assert isinstance(retried, domain.GroupCreated)


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
