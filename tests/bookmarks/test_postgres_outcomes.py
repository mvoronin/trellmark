from dataclasses import replace

import pytest
from anyio import CapacityLimiter, run
from sqlalchemy import event, literal_column, text
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError

from tests.bookmarks import helpers as bookmark_helpers
from tests.bookmarks.helpers import make_icon_service
from tests.helpers import RecordingTitleFetcher, logical_bookmark_snapshot
from trellmark.bookmarks import domain, persistence
from trellmark.bookmarks.application import BookmarksApplicationService
from trellmark.platform.runtime import AnyIOWorkRunner, get_engine


def service(factory=None):
    return BookmarksApplicationService(
        AnyIOWorkRunner(CapacityLimiter(4)),
        factory or persistence.PostgresLogicalBookmarkUnitOfWorkFactory(get_engine),
        persistence.PostgresGroupQueries(get_engine),
        persistence.PostgresURLQueries(get_engine),
        RecordingTitleFetcher(),
        make_icon_service(),
    )


def assert_gate_available():
    with get_engine().connect() as connection:
        assert (
            connection.scalar(text("SELECT pg_try_advisory_xact_lock(7502, 0)")) is True
        )


def create_group(name, *, parent_id=None):
    async def exercise():
        return await service().create_group(
            domain.CreateGroup(name, parent_id=parent_id)
        )

    outcome = run(exercise)
    assert isinstance(outcome, domain.GroupCreated)
    return outcome.record


@pytest.mark.parametrize("method", ["create_group", "update_group"])
def test_group_exact_unique_constraint_translates_and_releases_gate(database, method):
    create_group("First")
    second = create_group("Second")
    command = (
        domain.CreateGroup("FIRST")
        if method == "create_group"
        else domain.UpdateGroup(second.id, name="FIRST", nsfw=True)
    )
    before = logical_bookmark_snapshot()
    observed = []
    engine = get_engine()

    def capture(context):
        observed.append(context.sqlalchemy_exception)

    event.listen(engine, "handle_error", capture)
    try:

        async def exercise():
            return await getattr(service(), method)(command)

        assert run(exercise) == domain.GroupNameConflict("FIRST")
    finally:
        event.remove(engine, "handle_error", capture)
    assert len(observed) == 1
    assert observed[0].orig.diag.constraint_name == "uq_groups_name_lower"
    assert logical_bookmark_snapshot() == before
    assert_gate_available()


@pytest.mark.parametrize("method", ["create_group", "update_group"])
@pytest.mark.parametrize(
    "constraint", ["uq_groups_name_lower_other", "unrelated_group_uniqueness"]
)
def test_group_near_name_uniqueness_crosses_service_unchanged(
    database, method, constraint
):
    create_group("First")
    second = create_group("Second")
    before = logical_bookmark_snapshot()
    observed = []
    engine = get_engine()

    class UnitOfWork(persistence.PostgresLogicalBookmarkUnitOfWork):
        def __enter__(self):
            super().__enter__()
            # PostgreSQL DDL rolls back with this failed root transaction.
            statement = {
                "uq_groups_name_lower_other": "ALTER INDEX uq_groups_name_lower RENAME TO uq_groups_name_lower_other",
                "unrelated_group_uniqueness": "ALTER INDEX uq_groups_name_lower RENAME TO unrelated_group_uniqueness",
            }[constraint]
            self.groups.connection.execute(text(statement))
            return self

    def capture(context):
        observed.append(context.sqlalchemy_exception)

    event.listen(engine, "handle_error", capture)
    try:

        async def exercise():
            command = (
                domain.CreateGroup("FIRST")
                if method == "create_group"
                else domain.UpdateGroup(second.id, name="FIRST")
            )
            return await getattr(service(lambda: UnitOfWork(get_engine)), method)(
                command
            )

        with pytest.raises(IntegrityError) as caught:
            run(exercise)
    finally:
        event.remove(engine, "handle_error", capture)
    assert len(observed) == 1
    assert caught.value is observed[0]
    assert caught.value.orig.diag.constraint_name == constraint
    assert logical_bookmark_snapshot() == before
    assert_gate_available()
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT to_regclass('uq_groups_name_lower')"))
            is not None
        )
        assert (
            connection.scalar(text("SELECT to_regclass(:name)"), {"name": constraint})
            is None
        )


@pytest.mark.parametrize(
    "method,reason,sqlstate",
    [
        ("create_group", "missing", "GH001"),
        ("create_group", "deep", "GH003"),
        ("update_group", "missing", "GH001"),
        ("update_group", "self", "GH002"),
        ("update_group", "descendant", "GH002"),
        ("update_group", "deep", "GH003"),
    ],
)
def test_group_real_hierarchy_sqlstates_translate_and_roll_back(
    database, monkeypatch, method, reason, sqlstate
):
    root = create_group("Root")
    child = create_group("Child", parent_id=root.id)
    grandchild = create_group("Grandchild", parent_id=child.id)
    leaf = create_group("Leaf")
    group = root if reason == "descendant" else leaf
    parent_id = {
        "missing": 999,
        "self": group.id,
        "descendant": child.id,
        "deep": grandchild.id,
    }[reason]
    expected = {
        "GH001": domain.ParentNotFound(parent_id),
        "GH002": domain.ParentIsSelfOrDescendant(parent_id),
        "GH003": domain.GroupDepthExceeded(),
    }[sqlstate]
    command = (
        domain.CreateGroup("Rejected", parent_id=parent_id)
        if method == "create_group"
        else domain.UpdateGroup(
            group.id, name="Rejected", nsfw=True, parent_id=parent_id
        )
    )
    before = logical_bookmark_snapshot()
    observed = []
    engine = get_engine()
    # Bypass only the friendly pre-check. The unchanged migration trigger
    # executes the actual INSERT/UPDATE and supplies authoritative diagnostics.
    monkeypatch.setattr(persistence, "_validate_parent", lambda *_args, **_kwargs: None)

    def capture(context):
        observed.append(context.sqlalchemy_exception)

    event.listen(engine, "handle_error", capture)
    try:

        async def exercise():
            return await getattr(service(), method)(command)

        assert run(exercise) == expected
    finally:
        event.remove(engine, "handle_error", capture)
    assert len(observed) == 1
    assert observed[0].orig.sqlstate == sqlstate
    assert logical_bookmark_snapshot() == before
    assert_gate_available()
    assert create_group("After Rejection").parent_id is None


@pytest.mark.parametrize("method", ["create_group", "update_group"])
@pytest.mark.parametrize(
    "failure,sqlstate",
    [
        ("not-null", "23502"),
        ("unknown-column", "42703"),
        ("unknown-hierarchy", "GH004"),
    ],
)
def test_group_unrelated_database_error_crosses_async_service_unchanged(
    database, method, failure, sqlstate
):
    record = create_group("Original")
    before = logical_bookmark_snapshot()
    engine = get_engine()
    observed = []

    def corrupt_statement(_connection, statement, multiparams, params, _options):
        if (
            getattr(statement, "is_insert", False)
            or getattr(statement, "is_update", False)
        ) and statement.table.name == "groups":
            if failure == "not-null":
                statement = statement.values(nsfw=None)
            elif failure == "unknown-column":
                statement = statement.values(
                    name=literal_column("synthetic_unknown_column")
                )
            else:
                statement = text(
                    "DO $$ BEGIN RAISE EXCEPTION 'synthetic hierarchy failure' USING ERRCODE = 'GH004'; END $$"
                )
        return statement, multiparams, params

    def capture(context):
        observed.append(context.sqlalchemy_exception)

    event.listen(engine, "before_execute", corrupt_statement, retval=True)
    event.listen(engine, "handle_error", capture)
    try:

        async def exercise():
            command = (
                domain.CreateGroup("Changed")
                if method == "create_group"
                else domain.UpdateGroup(record.id, name="Changed")
            )
            return await getattr(service(), method)(command)

        with pytest.raises(DBAPIError) as caught:
            run(exercise)
    finally:
        event.remove(engine, "before_execute", corrupt_statement)
        event.remove(engine, "handle_error", capture)
    assert len(observed) == 1
    assert caught.value is observed[0]
    assert caught.value.orig.sqlstate == sqlstate
    assert isinstance(caught.value, IntegrityError) is (failure == "not-null")
    assert logical_bookmark_snapshot() == before
    assert_gate_available()


def test_url_exact_unique_constraint_translates_and_releases_gate(database):
    first = bookmark_helpers.seed_url("https://one.example", "One")
    second = bookmark_helpers.seed_url("https://two.example", "Two")
    before = logical_bookmark_snapshot()
    observed = []
    engine = get_engine()

    def capture(context):
        observed.append(context.sqlalchemy_exception)

    event.listen(engine, "handle_error", capture)
    try:

        async def exercise():
            return await service().edit_url(
                domain.EditURL(second["id"], 1, url="ONE.example/", title="Changed")
            )

        outcome = run(exercise)
    finally:
        event.remove(engine, "handle_error", capture)
    assert outcome == domain.URLConflict(first["url"])
    assert len(observed) == 1
    assert observed[0].orig.diag.constraint_name == "uq_urls_url"
    assert logical_bookmark_snapshot() == before
    assert_gate_available()


@pytest.mark.parametrize(
    "constraint", ["uq_urls_url_other", "unrelated_url_uniqueness"]
)
def test_url_unrelated_unique_constraint_preserves_error_identity(database, constraint):
    bookmark_helpers.seed_url("https://one.example")
    second = bookmark_helpers.seed_url("https://two.example")
    before = logical_bookmark_snapshot()
    observed = []
    engine = get_engine()

    def capture(context):
        observed.append(context.sqlalchemy_exception)

    event.listen(engine, "handle_error", capture)
    try:
        with pytest.raises(IntegrityError) as caught:
            with persistence.PostgresLogicalBookmarkUnitOfWork(get_engine) as uow:
                # This test-only schema rename is rolled back with the failed
                # transaction. It proves exact diagnostics from PostgreSQL.
                statement = {
                    "uq_urls_url_other": "ALTER TABLE urls RENAME CONSTRAINT uq_urls_url TO uq_urls_url_other",
                    "unrelated_url_uniqueness": "ALTER TABLE urls RENAME CONSTRAINT uq_urls_url TO unrelated_url_uniqueness",
                }[constraint]
                uow.bookmarks.connection.execute(text(statement))
                uow.bookmarks.edit_url(
                    domain.EditURL(second["id"], 1, url="https://one.example")
                )
    finally:
        event.remove(engine, "handle_error", capture)
    assert len(observed) == 1
    assert caught.value is observed[0]
    assert caught.value.orig.diag.constraint_name == constraint
    assert logical_bookmark_snapshot() == before
    assert_gate_available()


@pytest.mark.parametrize("failure", ["not-null", "unknown-column"])
def test_url_unrelated_database_error_crosses_async_service_unchanged(
    database, failure
):
    record = bookmark_helpers.seed_url("https://one.example")
    before = logical_bookmark_snapshot()
    engine = get_engine()
    observed = []

    def corrupt_statement(_connection, statement, multiparams, params, _options):
        if getattr(statement, "is_update", False) and statement.table.name == "urls":
            if failure == "not-null":
                statement = statement.values(important=None)
            else:
                statement = statement.values(
                    title=literal_column("synthetic_unknown_column")
                )
        return statement, multiparams, params

    def capture(context):
        observed.append(context.sqlalchemy_exception)

    event.listen(engine, "before_execute", corrupt_statement, retval=True)
    event.listen(engine, "handle_error", capture)
    try:

        async def exercise():
            return await service().edit_url(
                domain.EditURL(record["id"], 1, title="New")
            )

        with pytest.raises(
            IntegrityError if failure == "not-null" else ProgrammingError
        ) as caught:
            run(exercise)
    finally:
        event.remove(engine, "before_execute", corrupt_statement)
        event.remove(engine, "handle_error", capture)
    assert len(observed) == 1
    assert caught.value is observed[0]
    assert logical_bookmark_snapshot() == before
    assert_gate_available()


@pytest.mark.parametrize("state", ["success", "stale", "missing"])
def test_url_conditional_write_precedes_missing_or_stale_query(database, state):
    record = bookmark_helpers.seed_url("https://version.example")
    statements = []
    engine = get_engine()

    def capture(_connection, _cursor, statement, parameters, _context, _many):
        statements.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", capture)
    try:

        async def exercise():
            return await service().edit_url(
                domain.EditURL(
                    999 if state == "missing" else record["id"],
                    999 if state == "stale" else 1,
                    title="Changed",
                )
            )

        outcome = run(exercise)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert "pg_try_advisory_xact_lock" in statements[0][0]
    sql, params = statements[1]
    assert sql.startswith("UPDATE urls")
    assert "urls.id =" in sql and "urls.version =" in sql
    assert sum(statement.startswith("UPDATE urls") for statement, _ in statements) == 1
    if state == "success":
        assert isinstance(outcome, domain.URLUpdated)
        assert type(outcome.record.version) is int and outcome.record.version == 2
        assert len(statements) == 2
    else:
        assert isinstance(
            outcome,
            domain.URLNotFound if state == "missing" else domain.URLVersionConflict,
        )
        assert statements[2][0].startswith("SELECT urls.id")
    assert_gate_available()


def test_url_membership_outcomes_preserve_order_flags_and_orphan_deletion(database):
    first = bookmark_helpers.seed_group("First", domains=["shared.example"])
    second = bookmark_helpers.seed_group(
        "Second", nsfw=True, domains=["shared.example"]
    )
    target = bookmark_helpers.seed_group("Target")
    saved = bookmark_helpers.seed_url("https://shared.example", "Shared")
    before = logical_bookmark_snapshot()

    async def exercise():
        application = service()
        record = await application.url_by_id(saved["id"])
        assert await application.url_group_ids(record.id) == (first["id"], second["id"])
        rejections = (
            (domain.MoveURL(999, target["id"]), domain.URLNotFound(999)),
            (domain.MoveURL(record.id, 999), domain.GroupNotFound(999)),
            (
                domain.MoveURL(record.id, target["id"]),
                domain.URLSourceRequired(record.id, (first["id"], second["id"])),
            ),
            (
                domain.MoveURL(record.id, target["id"], 999),
                domain.URLMembershipNotFound(record.id, 999),
            ),
        )
        for command, expected in rejections:
            assert await application.move_url(command) == expected
            assert logical_bookmark_snapshot() == before
            assert_gate_available()
        assert await application.remove_url(
            domain.RemoveURL(record.id, 999)
        ) == domain.URLMembershipNotFound(record.id, 999)
        assert await application.remove_url(
            domain.RemoveURL(999, 1)
        ) == domain.URLNotFound(999)
        assert await application.set_important(
            domain.SetImportant(999, True)
        ) == domain.URLNotFound(999)
        assert logical_bookmark_snapshot() == before
        assert_gate_available()
        same_group = await application.move_url(
            domain.MoveURL(record.id, first["id"], first["id"])
        )
        assert same_group == domain.URLMoved(record, first["id"], first["id"])
        moved = await application.move_url(
            domain.MoveURL(record.id, target["id"], first["id"])
        )
        assert moved == domain.URLMoved(record, target["id"], first["id"])
        assert await application.url_group_ids(record.id) == (
            second["id"],
            target["id"],
        )
        flagged = await application.set_important(domain.SetImportant(record.id, True))
        assert flagged.record == replace(record, important=True)
        # Moving into an existing membership coalesces the two direct memberships.
        await application.move_url(
            domain.MoveURL(record.id, target["id"], second["id"])
        )
        assert await application.url_group_ids(record.id) == (target["id"],)
        with get_engine().begin() as connection:
            persistence.PostgresBookmarkRepository(connection).add_membership(
                record.id, second["id"]
            )
        removed = await application.remove_url(
            domain.RemoveURL(record.id, target["id"])
        )
        assert removed == domain.URLRemoved(flagged.record, target["id"])
        assert await application.url_by_id(record.id) == flagged.record
        assert await application.url_group_ids(record.id) == (second["id"],)
        await application.remove_url(domain.RemoveURL(record.id, second["id"]))
        assert await application.url_by_id(record.id) is None
        assert await application.url_group_ids(record.id) == ()
        assert [group.nsfw for group in await application.list_groups()] == [
            False,
            False,
            True,
            False,
        ]
        assert_gate_available()

    run(exercise)


@pytest.mark.parametrize(
    "method", ["edit_url", "move_url", "remove_url", "set_important"]
)
@pytest.mark.parametrize("failure", ["rejection", "repository", "commit"])
def test_url_mutation_rollback_after_dml_and_gate_release(database, method, failure):
    record = bookmark_helpers.seed_url("https://rollback.example", "Original")
    target = bookmark_helpers.seed_group("Target")
    commands = {
        "edit_url": domain.EditURL(record["id"], 1, title="Changed"),
        "move_url": domain.MoveURL(record["id"], target["id"]),
        "remove_url": domain.RemoveURL(record["id"], 1),
        "set_important": domain.SetImportant(record["id"], True),
    }
    before = logical_bookmark_snapshot()
    marker = RuntimeError("synthetic adapter failure")

    class Repository:
        def __init__(self, delegate):
            self.delegate = delegate

        def __getattr__(self, name):
            def execute(command):
                getattr(self.delegate, name)(command)
                if failure == "repository":
                    raise marker
                return domain.URLNotFound(command.url_id)

            return execute

    class UnitOfWork(persistence.PostgresLogicalBookmarkUnitOfWork):
        def __enter__(self):
            super().__enter__()
            if failure != "commit":
                self.bookmarks = Repository(self.bookmarks)
            return self

        def commit(self):
            if failure == "commit":
                raise marker
            super().commit()

    async def exercise():
        application = service(lambda: UnitOfWork(get_engine))
        if failure == "rejection":
            assert await getattr(application, method)(
                commands[method]
            ) == domain.URLNotFound(record["id"])
        else:
            with pytest.raises(RuntimeError) as caught:
                await getattr(application, method)(commands[method])
            assert caught.value is marker
        assert logical_bookmark_snapshot() == before
        assert_gate_available()
        outcome = await getattr(service(), method)(commands[method])
        assert isinstance(
            outcome,
            (
                domain.URLUpdated,
                domain.URLMoved,
                domain.URLRemoved,
                domain.SetImportantSucceeded,
            ),
        )
        assert_gate_available()

    run(exercise)
