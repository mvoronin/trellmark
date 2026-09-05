import asyncio
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock
from typing import TypeVar

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from anyio import CancelScope, CapacityLimiter, to_thread
from sqlalchemy import Column, MetaData, Table, Text, create_engine, select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import OperationalError, ProgrammingError, SQLAlchemyError

from .. import config

DATABASE_POOL_SIZE = 5
DATABASE_MAX_OVERFLOW = 5
DATABASE_POOL_CAPACITY = DATABASE_POOL_SIZE + DATABASE_MAX_OVERFLOW

BOOKMARKS_WORK_CAPACITY = 4
BOOKMARKS_DERIVED_WORK_CAPACITY = 2
IDENTITY_WORK_CAPACITY = 2
BACKUP_WORK_CAPACITY = 1

_ALLOCATED_WORK_CAPACITY = (
    BOOKMARKS_WORK_CAPACITY
    + BOOKMARKS_DERIVED_WORK_CAPACITY
    + IDENTITY_WORK_CAPACITY
    + BACKUP_WORK_CAPACITY
)
if _ALLOCATED_WORK_CAPACITY >= DATABASE_POOL_CAPACITY:
    raise RuntimeError("Work capacities must reserve one database pool checkout.")

ResultT = TypeVar("ResultT")


@dataclass(frozen=True, slots=True)
class AnyIOWorkRunner:
    limiter: CapacityLimiter

    async def run(self, work: Callable[[], ResultT]) -> ResultT:
        # Admission remains cancellable. Once admitted, keep the workload token
        # until the whole worker (including transaction cleanup) has finished.
        async with self.limiter:
            with CancelScope(shield=True):
                task = asyncio.create_task(
                    to_thread.run_sync(
                        work,
                        abandon_on_cancel=False,
                        # The parent owns admission; avoid acquiring its token
                        # a second time in the shielded worker task.
                        limiter=CapacityLimiter(float("inf")),
                    )
                )
                try:
                    return await asyncio.shield(task)
                except asyncio.CancelledError:
                    # Raw asyncio cancellation bypasses AnyIO cancel scopes.
                    # Repeated cancellation must not detach a live DB worker.
                    while not task.done():
                        try:
                            await asyncio.shield(task)
                        except asyncio.CancelledError:
                            continue
                    # Preserve the original worker failure over cancellation.
                    task.result()
                    raise

        raise RuntimeError("Worker scope exited without a result.")


@contextmanager
def read_connection(engine: Engine) -> Generator[Connection]:
    """Close a read checkout without hiding the original query failure."""
    connection = engine.connect()
    try:
        yield connection
    except BaseException:
        try:
            connection.close()
        except BaseException:
            # Cleanup can fail during the same outage as the query.
            pass
        raise
    else:
        connection.close()


_engine: Engine | None = None
_engine_url: str | None = None
_engine_lock = Lock()


def get_engine() -> Engine:
    """Return the process-lifetime PostgreSQL engine, creating it lazily."""
    global _engine, _engine_url
    url = config.database_url()
    rendered = url.render_as_string(hide_password=False)
    with _engine_lock:
        if _engine is not None and _engine_url == rendered:
            return _engine

        _dispose_engine_unlocked()
        _engine = create_engine(
            url,
            future=True,
            pool_size=DATABASE_POOL_SIZE,
            max_overflow=DATABASE_MAX_OVERFLOW,
            pool_pre_ping=True,
            pool_recycle=1800,
            hide_parameters=True,
        )
        _engine_url = rendered
        return _engine


def dispose_engine() -> None:
    """Dispose pooled connections and forget the process-lifetime engine."""
    with _engine_lock:
        _dispose_engine_unlocked()


def _dispose_engine_unlocked() -> None:
    global _engine, _engine_url
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _engine_url = None


alembic_version_table = Table(
    "alembic_version",
    MetaData(),
    Column("version_num", Text, primary_key=True),
)


def alembic_config() -> Config:
    # No sqlalchemy.url is set here: migrations/env.py reads the DSN from
    # trellmark.config directly, so a password never passes through ConfigParser
    # (where a '%' would need escaping) or lands in alembic.ini.
    return Config(str(config.BASE_DIR / "alembic.ini"))


def run_migrations() -> None:
    command.upgrade(alembic_config(), "head")


def verify_db_at_head() -> None:
    """Fail with a distinct, actionable message for each startup failure mode.

    Connectivity, authentication, a missing schema, and a stale revision are
    four different operator problems; collapsing them into one "not migrated"
    string sends the operator to the wrong fix.
    """
    expected_heads = set(ScriptDirectory.from_config(alembic_config()).get_heads())
    if not expected_heads:
        return

    target = config.redacted_database_url()
    try:
        with get_engine().connect() as connection:
            current_heads = set(
                connection.execute(
                    select(alembic_version_table.c.version_num)
                ).scalars()
            )
    except OperationalError as error:
        raise RuntimeError(
            f"Cannot connect to PostgreSQL at {target}: {_db_error_text(error)}"
        ) from error
    except ProgrammingError as error:
        raise RuntimeError(
            f"PostgreSQL at {target} has no trellmark schema; "
            "run `just migrate` (or `python server.py migrate`)."
        ) from error
    except SQLAlchemyError as error:
        raise RuntimeError(
            f"PostgreSQL at {target} is unusable: {_db_error_text(error)}"
        ) from error

    if current_heads != expected_heads:
        raise RuntimeError(
            f"PostgreSQL at {target} is at revision "
            f"{', '.join(sorted(current_heads)) or '(none)'} but this build "
            f"expects {', '.join(sorted(expected_heads))}; run `just migrate`."
        )


def _db_error_text(error: SQLAlchemyError) -> str:
    """Render a database error without leaking the DSN password.

    Driver errors embed the connection string, so the raw text is not safe to
    print. Prefer the DBAPI cause, which carries the server's message only.
    """
    cause = getattr(error, "orig", None)
    text = str(cause) if cause is not None else error.__class__.__name__
    return " ".join(text.split()) or error.__class__.__name__
