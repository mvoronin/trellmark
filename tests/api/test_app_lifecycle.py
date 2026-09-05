import asyncio
from io import StringIO

import pytest
from sqlalchemy import create_engine, inspect, text

import trellmark
from tests.helpers import CURRENT_HEAD, db_connection, db_query, run_async
from tests.postgres import TEST_LOGIN, TEST_PASSWORD
from trellmark import app as app_module
from trellmark import cli, config
from trellmark.bookmarks.application import BookmarksApplicationService
from trellmark.identity.persistence import (
    LoginRejected,
    create_login_session,
    verify_seeded_identity,
)
from trellmark.platform import runtime


def test_create_app_does_not_touch_the_database(postgres_server, monkeypatch):
    """Building the app must not connect, let alone migrate.

    The database is created here but never migrated, so any connection the
    factory made would have to fail or leave a schema behind.
    """
    url = postgres_server.create_database()
    monkeypatch.setattr(
        config, "DATABASE_URL", url.render_as_string(hide_password=False)
    )
    runtime.dispose_engine()
    try:
        trellmark.create_app()

        assert _table_names(url) == []
    finally:
        runtime.dispose_engine()
        postgres_server.drop_database(url)


def test_create_app_composes_one_immutable_bookmarks_service():
    application = trellmark.create_app()

    service = application.state.bookmarks_application_service

    assert isinstance(service, BookmarksApplicationService)
    assert application.state.bookmarks_application_service is service


def test_create_app_captures_four_bounded_services_and_one_lazy_engine_factory(
    monkeypatch,
):
    from dataclasses import FrozenInstanceError, fields

    constructed = {}

    def no_checkout():
        pytest.fail(
            "Construction and OpenAPI export must not open a database connection."
        )

    monkeypatch.setattr(runtime, "get_engine", no_checkout)

    def observe(name):
        original = getattr(app_module, name)

        def construct(*args, **kwargs):
            result = original(*args, **kwargs)
            constructed.setdefault(name, []).append(result)
            return result

        monkeypatch.setattr(app_module, name, construct)

    for name in (
        "BookmarksApplicationService",
        "SiteIconCacheApplicationService",
        "IdentityApplicationService",
        "BackupApplicationService",
    ):
        observe(name)

    application = app_module.create_app()
    application.openapi()
    bookmarks = application.state.bookmarks_application_service
    identity = application.state.identity_application_service
    backup = application.state.backup_application_service
    cache = bookmarks.icon_gateway._cache
    services = (bookmarks, cache, identity, backup)
    assert all(len(instances) == 1 for instances in constructed.values())
    assert {id(instances[0]) for instances in constructed.values()} == {
        id(service) for service in services
    }
    runners = [service.work_runner for service in services]
    assert [runner.limiter.total_tokens for runner in runners] == [4, 2, 2, 1]
    assert len({id(runner.limiter) for runner in runners}) == 4
    for service in services:
        with pytest.raises(FrozenInstanceError):
            service.work_runner = object()
        for field in fields(service):
            port = getattr(service, field.name)
            if hasattr(port, "engine_factory"):
                assert port.engine_factory is no_checkout


def test_lifespan_disposes_engine_even_when_captured_icon_drain_fails(monkeypatch):
    from tests.api.test_site_icon_api import RecordingIconService

    failure = RuntimeError("synthetic drain failure")
    events = []

    class Gateway(RecordingIconService):
        async def wait_for_idle(self):
            events.append("drain")
            raise failure

    monkeypatch.setattr(runtime, "dispose_engine", lambda: events.append("dispose"))
    application = app_module.create_app(icon_service=Gateway())

    async def exercise():
        async with application.router.lifespan_context(application):
            pass

    with pytest.raises(RuntimeError) as caught:
        run_async(exercise)
    assert caught.value is failure
    assert events == ["drain", "dispose"]


def test_identity_boundary_and_router_capture_one_service_through_lifespan(monkeypatch):
    from inspect import getclosurevars

    from fastapi.routing import APIRoute, iter_route_contexts

    from trellmark.identity.application import IdentityApplicationService
    from trellmark.identity.boundary import (
        AuthenticationBoundaryMiddleware,
        NoStoreResponseMiddleware,
    )
    from trellmark.identity.domain import SessionCommand, SessionMissing

    constructed = []
    called = []
    disposed = []

    def construct(**ports):
        service = IdentityApplicationService(**ports)
        constructed.append(service)
        return service

    async def authenticate(instance, command):
        called.append((instance, command))
        return SessionMissing()

    monkeypatch.setattr(app_module, "IdentityApplicationService", construct)
    monkeypatch.setattr(
        IdentityApplicationService, "authenticate_session", authenticate
    )
    monkeypatch.setattr(runtime, "dispose_engine", lambda: disposed.append(True))
    monkeypatch.setattr(config, "PUBLIC_ORIGIN", "http://localhost")
    application = app_module.create_app()
    [service] = constructed
    assert service is application.state.identity_application_service
    assert service.work_runner.limiter.total_tokens == 2
    assert [middleware.cls for middleware in application.user_middleware] == [
        NoStoreResponseMiddleware,
        AuthenticationBoundaryMiddleware,
        app_module.RequestBodyLimitMiddleware,
    ]
    assert application.user_middleware[1].kwargs == {"service": service}
    routes = [
        context.route
        for context in iter_route_contexts(application.routes)
        if isinstance(context.route, APIRoute)
        and context.route.endpoint.__module__ == "trellmark.identity.api"
    ]
    assert len(routes) == 5
    for route in routes:
        if route.path != "/api/health":
            assert any(
                value is service
                for value in getclosurevars(route.endpoint).nonlocals.values()
            )

    application.state.identity_application_service = object()

    async def exercise():
        async with application.router.lifespan_context(application):
            for path, expected in [("/api/auth/session", 200), ("/api/urls", 401)]:
                messages = []

                async def receive():
                    return {"type": "http.request", "body": b""}

                async def send(message):
                    messages.append(message)

                await application(
                    {
                        "type": "http",
                        "method": "GET",
                        "path": path,
                        "query_string": b"",
                        "headers": [],
                    },
                    receive,
                    send,
                )
                assert messages[0]["status"] == expected
                assert (b"cache-control", b"no-store") in messages[0]["headers"]
                assert called[-1][0] is service
                assert called[-1][1] == SessionCommand(None)
            assert disposed == []

    run_async(exercise)
    assert len(called) == 2
    assert constructed == [service]
    assert disposed == [True]


def test_create_app_lifespan_uses_one_parameter_hiding_engine(database):
    first = runtime.get_engine()
    second = runtime.get_engine()

    assert first is second
    assert first.hide_parameters is True


def test_identity_boundary_and_router_share_bounded_complete_database_scopes(
    database, monkeypatch
):
    import threading

    import anyio
    from sqlalchemy import event

    from trellmark.identity.persistence import PostgresIdentityRepository

    _, cookie = create_login_session("203.0.113.20", TEST_LOGIN, TEST_PASSWORD)
    monkeypatch.setattr(config, "PUBLIC_ORIGIN", "http://localhost")
    application = app_module.create_app()
    service = application.state.identity_application_service
    engine = runtime.get_engine()
    database_events = []

    def listener(name):
        def record(*_args):
            database_events.append((name, threading.get_ident()))

        return record

    listeners = {
        name: listener(name)
        for name in (
            "checkout",
            "begin",
            "before_cursor_execute",
            "commit",
            "rollback",
            "checkin",
        )
    }

    async def exercise():
        entered = [anyio.Event(), anyio.Event()]
        release = threading.Event()
        lock = threading.Lock()
        completed = {}
        calls = []
        loop_thread = threading.get_ident()
        original = PostgresIdentityRepository.authenticate_session

        def authenticate(repository, command):
            outcome = original(repository, command)
            with lock:
                index = len(calls)
                calls.append(threading.get_ident())
            anyio.from_thread.run_sync(entered[index].set)
            assert release.wait(5), "Session worker was not released."
            return outcome

        monkeypatch.setattr(
            PostgresIdentityRepository, "authenticate_session", authenticate
        )

        async def invoke(path):
            messages = []

            async def receive():
                return {"type": "http.request", "body": b""}

            async def send(message):
                messages.append(message)

            await application(
                {
                    "type": "http",
                    "method": "GET",
                    "path": path,
                    "query_string": b"",
                    "headers": [
                        (b"cookie", f"trellmark_session_dev={cookie}".encode())
                    ],
                },
                receive,
                send,
            )
            assert (b"cache-control", b"no-store") in messages[0]["headers"]
            completed[path] = messages[0]["status"]

        with anyio.fail_after(5):
            async with anyio.create_task_group() as tasks:
                try:
                    tasks.start_soon(invoke, "/api/missing-private-route")
                    await entered[0].wait()
                    tasks.start_soon(invoke, "/api/auth/session")
                    await entered[1].wait()
                    tasks.start_soon(invoke, "/internal/ready")
                    await anyio.wait_all_tasks_blocked()
                    limiter = service.work_runner.limiter
                    assert limiter.total_tokens == limiter.borrowed_tokens == 2
                    assert limiter.statistics().tasks_waiting == 1
                    assert len(calls) == 2
                    assert loop_thread not in calls
                    assert [name for name, _ in database_events].count("checkout") == 2
                    assert all(name != "checkin" for name, _ in database_events)
                    # Process health stays responsive while both session transactions
                    # hold workers and readiness waits for that same limiter.
                    await invoke("/api/health")
                    assert completed == {"/api/health": 200}
                finally:
                    release.set()
        assert completed == {
            "/api/health": 200,
            "/api/missing-private-route": 404,
            "/api/auth/session": 200,
            "/internal/ready": 200,
        }
        assert service.work_runner.limiter.borrowed_tokens == 0
        active = {}
        scopes = []
        for name, thread in database_events:
            assert thread != loop_thread
            if name == "checkout":
                assert thread not in active
                active[thread] = []
            active[thread].append(name)
            if name == "checkin":
                scopes.append(active.pop(thread))
        assert active == {}
        assert len(scopes) == 3
        assert all(scope.count("begin") == 1 for scope in scopes)
        assert sum(scope.count("commit") for scope in scopes) == 2
        assert sum(scope.count("rollback") for scope in scopes) == 1

    for name, callback in listeners.items():
        event.listen(engine, name, callback)
    try:
        run_async(exercise)
    finally:
        for name, callback in listeners.items():
            event.remove(engine, name, callback)


def test_create_app_lifespan_disposes_shared_platform_engine(monkeypatch):
    disposed = []
    monkeypatch.setattr(runtime, "dispose_engine", lambda: disposed.append(True))
    application = app_module.create_app()

    async def enter_and_exit_lifespan():
        async with application.router.lifespan_context(application):
            assert disposed == []

    asyncio.run(enter_and_exit_lifespan())

    assert disposed == [True]


def test_lifespan_drains_captured_icon_gateway_before_disposal(monkeypatch):
    from tests.api.test_site_icon_api import RecordingIconService

    events = []

    class Gateway(RecordingIconService):
        async def wait_for_idle(self):
            events.append("drain")
            await asyncio.sleep(0)
            assert events == ["drain"]
            events.append("idle")

    gateway = Gateway()
    application = app_module.create_app(icon_service=gateway)
    application.state.site_icon_service = RecordingIconService()
    monkeypatch.setattr(runtime, "dispose_engine", lambda: events.append("dispose"))

    async def lifespan():
        async with application.router.lifespan_context(application):
            assert (
                application.state.bookmarks_application_service.icon_gateway is gateway
            )

    run_async(lifespan)
    assert events == ["drain", "idle", "dispose"]


@pytest.mark.parametrize("blocked_stage", ["lookup", "refresh", "persist"])
def test_lifespan_drains_real_icon_lookup_refresh_and_cache_write(
    database, monkeypatch, blocked_stage
):
    from tests.bookmarks.helpers import icon_cache_service
    from trellmark.bookmarks.domain import SiteIcon
    from trellmark.bookmarks.integrations import SiteIconService

    events = []
    monkeypatch.setattr(runtime, "dispose_engine", lambda: events.append("dispose"))

    async def exercise():
        blocked = asyncio.Event()
        release = asyncio.Event()
        draining = asyncio.Event()
        cache = icon_cache_service()
        icon = SiteIcon(b"\x89PNG\r\n\x1a\nlifespan-test", "image/png")

        async def barrier(stage):
            if stage == blocked_stage:
                blocked.set()
                await release.wait()
            assert "dispose" not in events

        class Cache:
            async def read(self, origin):
                await barrier("lookup")
                return await cache.read(origin)

            async def success(self, origin, value, fetched_at, retry_after):
                await barrier("persist")
                result = await cache.success(origin, value, fetched_at, retry_after)
                events.append("persisted")
                return result

            async def failure(self, *_args):
                pytest.fail("The valid icon must be cached successfully.")

        async def fetch(_url):
            await barrier("refresh")
            return icon

        class Gateway(SiteIconService):
            async def wait_for_idle(self):
                draining.set()
                await super().wait_for_idle()
                events.append("idle")

        gateway = Gateway(cache=Cache(), fetcher=fetch)
        application = app_module.create_app(icon_service=gateway)

        async def lifespan():
            async with application.router.lifespan_context(application):
                pass

        async with asyncio.timeout(5):
            lookup = asyncio.create_task(gateway.get("https://example.com/path"))
            await blocked.wait()
            shutdown = asyncio.create_task(lifespan())
            await draining.wait()
            assert not shutdown.done()
            assert events == []
            release.set()
            assert await lookup == icon
            await shutdown
        assert events == ["persisted", "idle", "dispose"]
        stored = await cache.read("https://example.com")
        assert stored.icon_bytes == icon.data

    run_async(exercise)


def _table_names(url):
    engine = create_engine(url, poolclass=None)
    try:
        return sorted(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_serve_verifies_database_before_starting_api(database, monkeypatch):
    started = {}
    events = []

    def fake_verify_db_at_head():
        events.append("verify")
        runtime.verify_db_at_head()

    def fake_run(application, host, port, proxy_headers, forwarded_allow_ips):
        events.append("run")
        started["application"] = application
        started["host"] = host
        started["port"] = port
        started["proxy_headers"] = proxy_headers
        started["forwarded_allow_ips"] = forwarded_allow_ips
        [(version,)] = db_query("SELECT version_num FROM alembic_version")
        started["version"] = version

    monkeypatch.setattr(cli, "verify_db_at_head", fake_verify_db_at_head)
    monkeypatch.setattr(cli.uvicorn, "run", fake_run)
    monkeypatch.setattr(config, "PUBLIC_ORIGIN", "https://urls.example.com")

    cli.serve(type("Args", (), {"host": "127.0.0.1", "port": 8000})())

    assert events == ["verify", "run"]
    application = started.pop("application")
    assert application.title == "Trellmark API"
    assert started == {
        "host": "127.0.0.1",
        "port": 8000,
        "proxy_headers": True,
        "forwarded_allow_ips": "127.0.0.1,::1",
        "version": CURRENT_HEAD,
    }


def test_set_password_uses_hidden_confirmation_and_revokes_sessions(
    database, monkeypatch, capsys
):
    session, _ = create_login_session("127.0.0.1", TEST_LOGIN, TEST_PASSWORD)
    new_password = "operator-rotated-test-password"
    answers = iter([new_password, new_password])
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: next(answers))

    cli.main(["set-password"])

    output = capsys.readouterr().out
    assert output == "Administrator password updated; revoked 1 session.\n"
    assert new_password not in output
    assert db_query(
        "SELECT COUNT(*) FROM web_sessions "
        "WHERE id = :session_id AND revoked_at IS NOT NULL",
        session_id=session.id,
    ) == [(1,)]
    with pytest.raises(LoginRejected):
        create_login_session("127.0.0.1", TEST_LOGIN, TEST_PASSWORD)
    create_login_session("127.0.0.1", TEST_LOGIN, new_password)


def test_set_password_rejects_mismatched_confirmation(database, monkeypatch):
    answers = iter(["first-long-enough-password", "different-long-password"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: next(answers))

    with pytest.raises(SystemExit, match="passwords do not match"):
        cli.main(["set-password"])

    create_login_session("127.0.0.1", TEST_LOGIN, TEST_PASSWORD)


def test_set_password_stdin_accepts_one_secret_line(database, monkeypatch, capsys):
    new_password = "stdin-rotated-test-password"
    monkeypatch.setattr(cli.sys, "stdin", StringIO(f"{new_password}\n"))

    cli.main(["set-password", "--password-stdin"])

    assert capsys.readouterr().out == (
        "Administrator password updated; revoked 0 sessions.\n"
    )
    create_login_session("127.0.0.1", TEST_LOGIN, new_password)
    with pytest.raises(LoginRejected):
        create_login_session("127.0.0.1", TEST_LOGIN, f"{new_password}\n")


def test_verify_db_at_head_rejects_database_without_a_schema(
    postgres_server, monkeypatch
):
    url = postgres_server.create_database()
    monkeypatch.setattr(
        config, "DATABASE_URL", url.render_as_string(hide_password=False)
    )
    runtime.dispose_engine()
    try:
        with pytest.raises(RuntimeError, match="has no trellmark schema"):
            runtime.verify_db_at_head()
    finally:
        runtime.dispose_engine()
        postgres_server.drop_database(url)


def test_verify_db_at_head_reports_an_unreachable_server(monkeypatch):
    """An unreachable database must not read as a missing migration.

    Port 1 has nothing listening, so this is a connection failure and the
    operator needs to be told that rather than sent to run migrations.
    """
    monkeypatch.setattr(
        config,
        "DATABASE_URL",
        "postgresql+psycopg://trellmark:secret@127.0.0.1:1/trellmark",
    )
    runtime.dispose_engine()
    try:
        with pytest.raises(RuntimeError, match="Cannot connect to PostgreSQL") as error:
            runtime.verify_db_at_head()
        assert "secret" not in str(error.value)
    finally:
        runtime.dispose_engine()


def test_verify_db_at_head_reports_a_stale_revision(database):
    with db_connection() as connection:
        connection.execute(
            text("UPDATE alembic_version SET version_num = '0000_ancient'")
        )
    try:
        with pytest.raises(RuntimeError, match="is at revision 0000_ancient"):
            runtime.verify_db_at_head()
    finally:
        with db_connection() as connection:
            connection.execute(
                text("UPDATE alembic_version SET version_num = :head"),
                {"head": CURRENT_HEAD},
            )


def test_verify_db_at_head_accepts_current_database(database):
    runtime.verify_db_at_head()


def test_seeded_identity_is_verified_before_startup(database):
    verify_seeded_identity()

    with db_connection() as connection:
        connection.execute(
            text("UPDATE password_credentials SET password_hash = 'malformed'")
        )

    with pytest.raises(RuntimeError, match="credential is missing or invalid") as error:
        verify_seeded_identity()
    assert "malformed" not in str(error.value)


def test_seeded_identity_rejects_truncated_argon_material(database):
    truncated = "$argon2id$v=19$m=19456,t=2,p=1$AA$AA"
    with db_connection() as connection:
        connection.execute(
            text("UPDATE password_credentials SET password_hash = :password_hash"),
            {"password_hash": truncated},
        )

    with pytest.raises(RuntimeError, match="credential is missing or invalid") as error:
        verify_seeded_identity()
    assert truncated not in str(error.value)


def test_serve_requires_public_origin_before_starting(database, monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_ORIGIN", None)
    monkeypatch.setattr(
        cli.uvicorn,
        "run",
        lambda *_args, **_kwargs: pytest.fail("uvicorn must not start"),
    )

    with pytest.raises(SystemExit, match="TRELLMARK_PUBLIC_ORIGIN is not set"):
        cli.serve(type("Args", (), {"host": "127.0.0.1", "port": 8000})())


def test_missing_database_url_is_a_startup_error(monkeypatch):
    """Unset configuration must say so, not guess at a local database.

    A default DSN would either connect to whatever happens to be listening on
    localhost or report a connection failure that sends the operator looking
    for a network problem instead of a missing environment variable.
    """
    monkeypatch.setattr(config, "DATABASE_URL", None)
    runtime.dispose_engine()
    try:
        with pytest.raises(RuntimeError, match="TRELLMARK_DATABASE_URL is not set"):
            runtime.verify_db_at_head()
        assert config.redacted_database_url() == "<TRELLMARK_DATABASE_URL not set>"
    finally:
        runtime.dispose_engine()


def test_non_postgresql_database_url_is_rejected(monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", "sqlite:///urls.db")
    runtime.dispose_engine()
    try:
        with pytest.raises(RuntimeError, match="must be a PostgreSQL URL"):
            runtime.verify_db_at_head()
    finally:
        runtime.dispose_engine()


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql+psycopg://trellmark:p@ss@127.0.0.1:5432/trellmark",
        "postgresql://trellmark:has@sign@db.internal:5432/trellmark",
    ],
)
def test_unencoded_password_in_a_manual_dsn_is_rejected(monkeypatch, dsn):
    """A host can never contain '@'; seeing one means the password leaked into it.

    Redaction masks only the *parsed* password, so the bytes that spilled into
    the host position would be printed. Rejecting the DSN makes redaction fail
    closed instead.
    """
    monkeypatch.setattr(config, "DATABASE_URL", dsn)
    runtime.dispose_engine()
    try:
        with pytest.raises(RuntimeError, match="the host contains '@'"):
            config.database_url()

        redacted = config.redacted_database_url()
        assert redacted == "<invalid database URL>"
        assert "ss@" not in redacted
        assert "sign" not in redacted
    finally:
        runtime.dispose_engine()


def test_percent_encoded_password_is_accepted_and_redacted(monkeypatch):
    monkeypatch.setattr(
        config,
        "DATABASE_URL",
        "postgresql+psycopg://trellmark:p%40ss@127.0.0.1:5432/trellmark",
    )
    runtime.dispose_engine()
    try:
        assert config.database_url().password == "p@ss"
        assert (
            config.redacted_database_url()
            == "postgresql+psycopg://trellmark:***@127.0.0.1:5432/trellmark"
        )
    finally:
        runtime.dispose_engine()
