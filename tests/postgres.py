"""Disposable PostgreSQL for the test suite.

The app has no SQLite path any more, so every database test needs a real
server. Two ways to get one, in order:

1. `TRELLMARK_TEST_DATABASE_URL` — use the server it names and start nothing.
2. Otherwise, start a throwaway `postgres` container on an ephemeral port with
   Podman and remove it when the session ends.

Neither path may ever touch a production database, so `reset_database()`
refuses to truncate anything whose DSN does not carry the marker below.
"""

import os
import secrets
import socket
import subprocess
import time

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import OperationalError

TEST_DATABASE_URL_ENV = "TRELLMARK_TEST_DATABASE_URL"

# Every database this harness is allowed to destroy carries this in its name.
# A DSN without it is assumed to be someone's real data.
TEST_DB_MARKER = "trellmark_test_"

POSTGRES_IMAGE = os.environ.get(
    "TRELLMARK_TEST_POSTGRES_IMAGE", "docker.io/library/postgres:18-alpine"
)
CONTAINER_PASSWORD = "trellmark-test"
STARTUP_TIMEOUT_SECONDS = 60


class PostgresUnavailable(RuntimeError):
    """Raised when no test database can be provided.

    Deliberately fatal rather than a skip: silently skipping every database
    test would turn a broken environment into a green run.
    """


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


class PostgresServer:
    """A PostgreSQL server the tests may create and destroy databases on."""

    def __init__(self, admin_url: URL, container: str | None) -> None:
        self.admin_url = admin_url
        self.container = container

    def create_database(self) -> URL:
        name = f"{TEST_DB_MARKER}{secrets.token_hex(6)}"
        with self._admin_connection() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        return self.admin_url.set(database=name)

    def drop_database(self, url: URL) -> None:
        name = url.database
        if not name or not name.startswith(TEST_DB_MARKER):
            raise RuntimeError(f"Refusing to drop non-test database {name!r}.")
        with self._admin_connection() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))

    def _admin_connection(self):
        # AUTOCOMMIT because CREATE/DROP DATABASE cannot run inside a
        # transaction block.
        engine = create_engine(
            self.admin_url, isolation_level="AUTOCOMMIT", poolclass=None
        )
        return engine.connect()

    def stop(self) -> None:
        if self.container:
            _run(["podman", "rm", "--force", "--volumes", self.container])


def start_server() -> PostgresServer:
    configured = os.environ.get(TEST_DATABASE_URL_ENV)
    if configured:
        return _use_configured_server(configured)
    return _start_container()


def _use_configured_server(dsn: str) -> PostgresServer:
    try:
        url = make_url(dsn)
    except Exception as error:
        raise PostgresUnavailable(
            f"{TEST_DATABASE_URL_ENV} is not a valid database URL."
        ) from error

    url = url.set(drivername="postgresql+psycopg")
    if not url.database:
        url = url.set(database="postgres")
    _wait_for_ready(url, deadline=time.monotonic() + 10)
    return PostgresServer(url, container=None)


def _start_container() -> PostgresServer:
    if not _has_podman():
        raise PostgresUnavailable(
            "The test suite needs PostgreSQL. Install Podman so the harness can "
            f"start one, or set {TEST_DATABASE_URL_ENV} to an existing server "
            "(for example postgresql://user:pass@127.0.0.1:5432/postgres)."
        )

    port = _free_port()
    name = f"trellmark-test-pg-{secrets.token_hex(4)}"
    # Intentionally keep Podman's default `missing` policy here. `just check`
    # is offline-capable, unlike the strict online refreshes in run-container
    # and deploy. README.md documents the explicit pre-test pull for operators
    # who want to refresh this disposable test dependency.
    started = _run(
        [
            "podman",
            "run",
            "--detach",
            "--rm",
            "--name",
            name,
            "--publish",
            f"127.0.0.1:{port}:5432",
            "--env",
            f"POSTGRES_PASSWORD={CONTAINER_PASSWORD}",
            "--env",
            "POSTGRES_USER=trellmark",
            "--env",
            "POSTGRES_DB=postgres",
            # The data is thrown away with the container; durability costs
            # startup and per-test time we have no use for here.
            POSTGRES_IMAGE,
            "-c",
            "fsync=off",
            "-c",
            "full_page_writes=off",
            "-c",
            "synchronous_commit=off",
        ]
    )
    if started.returncode != 0:
        raise PostgresUnavailable(
            f"Could not start the {POSTGRES_IMAGE} test container: "
            f"{started.stderr.strip()}"
        )

    url = URL.create(
        "postgresql+psycopg",
        username="trellmark",
        password=CONTAINER_PASSWORD,
        host="127.0.0.1",
        port=port,
        database="postgres",
    )
    server = PostgresServer(url, container=name)
    try:
        _wait_for_ready(url, deadline=time.monotonic() + STARTUP_TIMEOUT_SECONDS)
    except PostgresUnavailable:
        server.stop()
        raise
    return server


def _has_podman() -> bool:
    try:
        return _run(["podman", "--version"], timeout=15).returncode == 0
    except OSError, subprocess.SubprocessError:
        return False


def _wait_for_ready(url: URL, deadline: float) -> None:
    """Poll until the server accepts a real query, not just a TCP connection.

    The container publishes its port before initdb finishes, so a connect
    attempt is the only honest readiness check.
    """
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            engine = create_engine(url, poolclass=None)
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            engine.dispose()
            return
        except OperationalError as error:
            last_error = error
            time.sleep(0.25)
    raise PostgresUnavailable(
        f"PostgreSQL at {url.render_as_string(hide_password=True)} did not become "
        f"ready in time: {last_error}"
    )


TABLES_IN_TRUNCATE_ORDER = (
    "site_icon_cache",
    "web_sessions",
    "auth_login_throttle",
    "password_credentials",
    "users",
    "url_groups",
    "group_domains",
    "urls",
    "groups",
)
TEST_LOGIN = "admin"
TEST_PASSWORD = "trellmark-test-password-only"
TEST_PASSWORD_HASH = (
    "$argon2id$v=19$m=19456,t=2,p=1$"
    "UsGNtB9+KHNUtYO1Xt+wxg$"
    "6NpI2lvUlT0cz543NPkU43cOc2+eYGJkjSe0c0dtT7c"
)


def reset_database(url: URL) -> None:
    """Return a migrated database to its just-migrated state.

    Truncate-and-reseed rather than a fresh database per test: ~257 of the
    suite's tests touch storage, and running Alembic for each one is not
    affordable against a real server.
    """
    name = url.database
    if not name or not name.startswith(TEST_DB_MARKER):
        raise RuntimeError(f"Refusing to truncate non-test database {name!r}.")

    engine = create_engine(url, poolclass=None)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "TRUNCATE TABLE "
                    + ", ".join(f'"{table}"' for table in TABLES_IN_TRUNCATE_ORDER)
                    + " RESTART IDENTITY CASCADE"
                )
            )
            # The baseline migration seeds this; TRUNCATE removed it.
            connection.execute(
                text("INSERT INTO \"groups\" (name, position) VALUES ('default', 0)")
            )
            user_id = connection.execute(
                text(
                    "INSERT INTO users (username, role, status, created_at, updated_at) "
                    "VALUES (:login, 'admin', 'active', now(), now()) RETURNING id"
                ),
                {"login": TEST_LOGIN},
            ).scalar_one()
            connection.execute(
                text(
                    "INSERT INTO password_credentials "
                    "(user_id, password_hash, changed_at) "
                    "VALUES (:user_id, :password_hash, now())"
                ),
                {"user_id": user_id, "password_hash": TEST_PASSWORD_HASH},
            )
    finally:
        engine.dispose()
