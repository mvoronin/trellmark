import asyncio
import socket
import threading
import time

import pytest
import uvicorn

import trellmark
from tests.helpers import clear_authentication
from tests.postgres import reset_database, start_server
from trellmark import config
from trellmark.cli import FORWARDED_ALLOW_IPS
from trellmark.site_icons import SiteIconService


async def _no_title_fetcher(url):
    return None


async def _no_icon_fetcher(url):
    return None


@pytest.fixture(scope="session")
def postgres_server():
    server = start_server()
    yield server
    server.stop()


@pytest.fixture(scope="session")
def migrated_database(postgres_server):
    """One migrated database for the whole session.

    Alembic runs once here; per-test isolation is truncate-and-reseed in
    `database`, which is what keeps a 260-test suite against a real server
    affordable.
    """
    url = postgres_server.create_database()
    original = config.DATABASE_URL
    config.DATABASE_URL = url.render_as_string(hide_password=False)
    try:
        trellmark.run_migrations()
    finally:
        trellmark.dispose_engine()
        config.DATABASE_URL = original

    yield url

    postgres_server.drop_database(url)


@pytest.fixture
def database(migrated_database, monkeypatch):
    """Point the app at the test database and hand it back empty."""
    reset_database(migrated_database)
    monkeypatch.setattr(
        config, "DATABASE_URL", migrated_database.render_as_string(hide_password=False)
    )
    trellmark.dispose_engine()
    yield migrated_database
    trellmark.dispose_engine()


@pytest.fixture
def empty_database(postgres_server, monkeypatch):
    """An empty, unmigrated database of its own.

    For the tests that need to watch migrations build a schema from nothing;
    the shared `database` fixture is already at head.
    """
    url = postgres_server.create_database()
    monkeypatch.setattr(
        config, "DATABASE_URL", url.render_as_string(hide_password=False)
    )
    trellmark.dispose_engine()
    yield url
    trellmark.dispose_engine()
    postgres_server.drop_database(url)


@pytest.fixture
def title_fetcher(request):
    return getattr(request, "param", _no_title_fetcher)


@pytest.fixture
def icon_service(request):
    configured = getattr(request, "param", None)
    if configured is None:
        return SiteIconService(fetcher=_no_icon_fetcher)
    return configured() if callable(configured) else configured


@pytest.fixture
def app(database, title_fetcher, icon_service, monkeypatch):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    base_url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    monkeypatch.setattr(config, "PUBLIC_ORIGIN", base_url)
    clear_authentication(base_url)
    application = trellmark.create_app(
        title_fetcher=title_fetcher,
        icon_service=icon_service,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            application,
            host="127.0.0.1",
            port=0,
            log_level="critical",
            access_log=False,
            lifespan="on",
            proxy_headers=True,
            forwarded_allow_ips=FORWARDED_ALLOW_IPS,
        )
    )

    def serve():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(server.serve(sockets=[sock]))
        finally:
            loop.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started, "test server did not start"

    yield base_url, database

    server.should_exit = True
    thread.join(timeout=5)
    assert not thread.is_alive(), "test server did not stop"
    clear_authentication(base_url)
