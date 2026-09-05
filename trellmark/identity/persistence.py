import hashlib
import hmac
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import TracebackType
from typing import assert_never, cast

from argon2 import PasswordHasher, extract_parameters
from argon2.exceptions import InvalidHashError, VerificationError
from argon2.low_level import Type
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    and_,
    delete,
    func,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection, Engine, RowMapping, Transaction
from sqlalchemy.exc import SQLAlchemyError

from ..platform.runtime import get_engine, read_connection
from .application import (
    authenticate_session_with_ports,
    login_in_uow,
    revoke_session_in_uow,
    rotate_password_in_uow,
)
from .domain import (
    CSRF_SECRET_BYTES,
    GLOBAL_BLOCK_LIFETIME,
    GLOBAL_BUCKET_KEY,
    GLOBAL_FAILURE_LIMIT,
    PASSWORD_HASH_BYTES,
    PASSWORD_MEMORY_KIB,
    PASSWORD_PARALLELISM,
    PASSWORD_SALT_BYTES,
    PASSWORD_TIME_COST,
    SESSION_ABSOLUTE_LIFETIME,
    SESSION_IDLE_LIFETIME,
    SESSION_LAST_USE_COALESCE,
    SESSION_PRUNE_LIMIT,
    SESSION_SECRET_BYTES,
    SOURCE_BLOCK_LIFETIME,
    SOURCE_FAILURE_LIMIT,
    THROTTLE_PRUNE_LIMIT,
    THROTTLE_WINDOW,
    AuthSession,
    IdentityCleaned,
    LoginCommand,
    LoginCreated,
    LoginFailureRecorded,
    LoginOutcome,
    LoginThrottled,
    PasswordRotated,
    RevokeSessionCommand,
    RotatePasswordCommand,
    SeededIdentityInvalid,
    SeededIdentityOutcome,
    SeededIdentityValid,
    SessionAuthenticated,
    SessionCommand,
    SessionMissing,
    SessionOutcome,
    SessionRevoked,
    decode_session_secrets,
    retry_after_seconds,
    throttle_window_expired,
    urlsafe_encode,
)

PASSWORD_HASHER = PasswordHasher(
    time_cost=PASSWORD_TIME_COST,
    memory_cost=PASSWORD_MEMORY_KIB,
    parallelism=PASSWORD_PARALLELISM,
    hash_len=PASSWORD_HASH_BYTES,
    salt_len=PASSWORD_SALT_BYTES,
    type=Type.ID,
)

# A normal Argon2id verifier used only to equalize syntactically valid unknown
# logins. Its input has no authentication meaning and grants no access.
DUMMY_PASSWORD_HASH = (
    "$argon2id$v=19$m=19456,t=2,p=1$"
    "/NugjqiIU8py8h7Kcv6dlw$"
    "9TlyDzyIYzAEWKLYHTm4IwspVxK5I1zBnamce16nuyQ"
)

metadata = MetaData()

users_table = Table(
    "users",
    metadata,
    Column("id", BigInteger, Identity(always=False), primary_key=True),
    Column("username", Text, nullable=False),
    Column("role", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("role = 'admin'", name="ck_users_role"),
    CheckConstraint("status IN ('active', 'inactive')", name="ck_users_status"),
)
Index("uq_users_username_lower", func.lower(users_table.c.username), unique=True)

password_credentials_table = Table(
    "password_credentials",
    metadata,
    Column(
        "user_id",
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("password_hash", Text, nullable=False),
    Column("changed_at", DateTime(timezone=True), nullable=False),
)

web_sessions_table = Table(
    "web_sessions",
    metadata,
    Column("id", BigInteger, Identity(always=False), primary_key=True),
    Column(
        "user_id",
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("secret_hash", LargeBinary(32), nullable=False),
    Column("csrf_secret_hash", LargeBinary(32), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("last_used_at", DateTime(timezone=True), nullable=False),
    Column("idle_expires_at", DateTime(timezone=True), nullable=False),
    Column("absolute_expires_at", DateTime(timezone=True), nullable=False),
    Column("revoked_at", DateTime(timezone=True), nullable=True),
    UniqueConstraint("secret_hash", name="uq_web_sessions_secret_hash"),
    CheckConstraint(
        "last_used_at >= created_at AND idle_expires_at > created_at "
        "AND absolute_expires_at > created_at",
        name="ck_web_sessions_lifetimes",
    ),
)
Index("ix_web_sessions_user_id", web_sessions_table.c.user_id)
Index(
    "ix_web_sessions_idle_expires_at",
    web_sessions_table.c.idle_expires_at,
    web_sessions_table.c.id,
)
Index(
    "ix_web_sessions_absolute_expires_at",
    web_sessions_table.c.absolute_expires_at,
    web_sessions_table.c.id,
)
Index(
    "ix_web_sessions_revoked_at",
    web_sessions_table.c.revoked_at,
    web_sessions_table.c.id,
    postgresql_where=web_sessions_table.c.revoked_at.is_not(None),
)

auth_login_throttle_table = Table(
    "auth_login_throttle",
    metadata,
    Column("scope", Text, primary_key=True),
    Column("bucket_key", Text, primary_key=True),
    Column("window_started_at", DateTime(timezone=True), nullable=False),
    Column("failure_count", Integer, nullable=False),
    Column("blocked_until", DateTime(timezone=True), nullable=True),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("scope IN ('source', 'global')", name="ck_auth_throttle_scope"),
    CheckConstraint("failure_count >= 0", name="ck_auth_throttle_failure_count"),
    CheckConstraint(
        "length(bucket_key) BETWEEN 1 AND 128",
        name="ck_auth_throttle_bucket_key_length",
    ),
)
Index(
    "ix_auth_login_throttle_updated_at",
    auth_login_throttle_table.c.updated_at,
)


class LoginRejected(Exception):
    pass


class LoginBlocked(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__("Login is temporarily throttled.")
        self.retry_after = retry_after


type EngineFactory = Callable[[], Engine]


@dataclass(frozen=True, slots=True)
class PostgresIdentityRepository:
    connection: Connection

    def login(self, command: LoginCommand) -> LoginOutcome:
        return _attempt_login(
            self.connection,
            source_key=command.source,
            login=command.login,
            password=command.password,
        )

    def authenticate_session(self, command: SessionCommand) -> SessionOutcome:
        return _authenticate_session_on(self.connection, command)

    def rotate_password(self, command: RotatePasswordCommand) -> PasswordRotated:
        return _rotate_password_on(self.connection, command)

    def revoke_session(self, command: RevokeSessionCommand) -> SessionRevoked:
        self.connection.execute(
            update(web_sessions_table)
            .where(
                web_sessions_table.c.id == command.session_id,
                web_sessions_table.c.revoked_at.is_(None),
            )
            .values(revoked_at=_utc_now())
        )
        return SessionRevoked()

    def cleanup(self) -> IdentityCleaned:
        now = _utc_now()
        _prune_throttles(self.connection, now)
        _prune_sessions(self.connection, now)
        return IdentityCleaned()


@dataclass(slots=True)
class PostgresIdentityUnitOfWork:
    engine_factory: EngineFactory
    identity: PostgresIdentityRepository = field(init=False)
    _connection: Connection | None = field(init=False, default=None)
    _transaction: Transaction | None = field(init=False, default=None)

    def __enter__(self) -> "PostgresIdentityUnitOfWork":
        if self._connection is not None:
            raise RuntimeError("Identity unit of work is already active.")
        connection = self.engine_factory().connect()
        self._connection = connection
        try:
            self._transaction = connection.begin()
            self.identity = PostgresIdentityRepository(connection)
            return self
        except BaseException:
            try:
                connection.close()
            except BaseException:
                # Preserve the failed begin/bind operation; cleanup is still
                # attempted and may fail during the same connection outage.
                pass
            finally:
                self._connection = None
                self._transaction = None
            raise

    def commit(self) -> None:
        transaction = self._transaction
        if transaction is None or not transaction.is_active:
            raise RuntimeError("Identity unit of work is not active.")
        transaction.commit()

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        try:
            self._close()
        except BaseException:
            # A cleanup error must never replace the operation/commit error
            # being carried back through the worker to the safe HTTP boundary.
            if exception is None:
                raise

    def _close(self) -> None:
        connection = self._connection
        transaction = self._transaction
        try:
            if transaction is not None and transaction.is_active:
                transaction.rollback()
        finally:
            try:
                if connection is not None:
                    connection.close()
            finally:
                self._connection = None
                self._transaction = None


@dataclass(frozen=True, slots=True)
class PostgresIdentityUnitOfWorkFactory:
    engine_factory: EngineFactory

    def __call__(self) -> PostgresIdentityUnitOfWork:
        return PostgresIdentityUnitOfWork(self.engine_factory)


@dataclass(frozen=True, slots=True)
class PostgresIdentityQueries:
    engine_factory: EngineFactory

    def lookup_session(self, cookie_value: str | None) -> SessionOutcome:
        with read_connection(self.engine_factory()) as connection:
            return _authenticate_session_on(
                connection, SessionCommand(cookie_value, touch=False)
            )

    def seeded_identity(self) -> SeededIdentityOutcome:
        with read_connection(self.engine_factory()) as connection:
            return _seeded_identity_on(connection)

    def database_ready(self) -> bool:
        with read_connection(self.engine_factory()) as connection:
            connection.execute(select(1)).scalar_one()
        return True


# Synchronous compatibility adapters for transport/CLI migrations in Plans
# 02-13, 02-20, and 02-14. They share application transaction decisions; all
# async service calls use the injected bounded runner.
def create_login_session(
    source: str, login: str, password: str
) -> tuple[AuthSession, str]:
    outcome = login_in_uow(
        PostgresIdentityUnitOfWorkFactory(get_engine),
        LoginCommand(source, login, password),
    )
    match outcome:
        case LoginCreated(session, cookie_value):
            return session, cookie_value
        case LoginFailureRecorded():
            raise LoginRejected
        case LoginThrottled(retry_after):
            raise LoginBlocked(retry_after)
    assert_never(outcome)


def authenticate_session(
    cookie_value: str | None, *, touch: bool = True
) -> AuthSession | None:
    outcome = authenticate_session_with_ports(
        PostgresIdentityUnitOfWorkFactory(get_engine),
        PostgresIdentityQueries(get_engine),
        SessionCommand(cookie_value, touch=touch),
    )
    match outcome:
        case SessionAuthenticated(session):
            return session
        case SessionMissing():
            return None
    assert_never(outcome)


def revoke_session(session: AuthSession) -> None:
    revoke_session_in_uow(
        PostgresIdentityUnitOfWorkFactory(get_engine), RevokeSessionCommand(session.id)
    )


def set_administrator_password(password: str) -> int:
    command = RotatePasswordCommand(password)
    try:
        return rotate_password_in_uow(
            PostgresIdentityUnitOfWorkFactory(get_engine), command
        ).revoked_sessions
    except SQLAlchemyError as error:
        raise RuntimeError("Could not update the administrator credential.") from error


def verify_seeded_identity() -> None:
    try:
        outcome = PostgresIdentityQueries(get_engine).seeded_identity()
    except SQLAlchemyError as error:
        raise RuntimeError(
            "The seeded administrator credential is unavailable."
        ) from error
    match outcome:
        case SeededIdentityValid():
            return
        case SeededIdentityInvalid():
            raise RuntimeError(
                "The seeded administrator credential is missing or invalid."
            )
    assert_never(outcome)


def database_ready() -> bool:
    try:
        return PostgresIdentityQueries(get_engine).database_ready()
    except SQLAlchemyError:
        return False


def _attempt_login(
    connection: Connection,
    *,
    source_key: str,
    login: str,
    password: str,
) -> LoginOutcome:
    now = _utc_now()
    _prune_throttles(connection, now)
    _ensure_throttle_bucket(connection, "global", GLOBAL_BUCKET_KEY, now)
    # Hold this global row lock through Argon2id verification: serialization
    # bounds memory use even when many login requests occupy worker threads.
    global_bucket = _lock_bucket(connection, "global", GLOBAL_BUCKET_KEY)
    global_bucket = _refresh_bucket(connection, global_bucket, now)
    retry_after = _active_retry_after(global_bucket, now)
    if retry_after is not None:
        return LoginThrottled(retry_after)

    _ensure_throttle_bucket(connection, "source", source_key, now)
    source_bucket = _lock_bucket(connection, "source", source_key)
    source_bucket = _refresh_bucket(connection, source_bucket, now)
    retry_after = _active_retry_after(source_bucket, now)
    if retry_after is not None:
        return LoginThrottled(retry_after)

    credential = (
        connection.execute(
            select(
                users_table.c.id,
                users_table.c.username,
                users_table.c.role,
                users_table.c.status,
                password_credentials_table.c.password_hash,
            )
            .select_from(
                users_table.outerjoin(
                    password_credentials_table,
                    password_credentials_table.c.user_id == users_table.c.id,
                )
            )
            .where(func.lower(users_table.c.username) == login)
            .with_for_update(of=users_table)
        )
        .mappings()
        .one_or_none()
    )

    eligible = bool(
        credential is not None
        and credential["role"] == "admin"
        and credential["status"] == "active"
        and isinstance(credential["password_hash"], str)
    )
    password_hash = (
        cast(str, credential["password_hash"])
        if eligible and credential is not None
        else DUMMY_PASSWORD_HASH
    )
    verified = _verify_password(password_hash, password)
    if eligible and not verified and password_hash != DUMMY_PASSWORD_HASH:
        # A corrupt verifier must not turn the only known login into a cheap
        # timing oracle. Startup normally rejects it before requests are served.
        try:
            extract_parameters(password_hash)
        except InvalidHashError:
            _verify_password(DUMMY_PASSWORD_HASH, password)

    if not eligible or not verified or credential is None:
        _record_failure(
            connection,
            global_bucket,
            now,
            GLOBAL_FAILURE_LIMIT,
            GLOBAL_BLOCK_LIFETIME,
        )
        _record_failure(
            connection,
            source_bucket,
            now,
            SOURCE_FAILURE_LIMIT,
            SOURCE_BLOCK_LIFETIME,
        )
        return LoginFailureRecorded()

    user_id = cast(int, credential["id"])
    if PASSWORD_HASHER.check_needs_rehash(password_hash):
        connection.execute(
            update(password_credentials_table)
            .where(password_credentials_table.c.user_id == user_id)
            .values(password_hash=PASSWORD_HASHER.hash(password), changed_at=now)
        )

    connection.execute(
        delete(auth_login_throttle_table).where(
            auth_login_throttle_table.c.scope == "source",
            auth_login_throttle_table.c.bucket_key == source_key,
        )
    )
    _prune_sessions(connection, now)
    cookie_value, session_hash, csrf_hash, csrf_token = _new_session_secrets()
    absolute_expires_at = now + SESSION_ABSOLUTE_LIFETIME
    session_id = connection.execute(
        insert(web_sessions_table)
        .values(
            user_id=user_id,
            secret_hash=session_hash,
            csrf_secret_hash=csrf_hash,
            created_at=now,
            last_used_at=now,
            idle_expires_at=min(now + SESSION_IDLE_LIFETIME, absolute_expires_at),
            absolute_expires_at=absolute_expires_at,
        )
        .returning(web_sessions_table.c.id)
    ).scalar_one()
    session = AuthSession(
        id=cast(int, session_id),
        user_id=user_id,
        login=cast(str, credential["username"]),
        csrf_token=csrf_token,
    )
    return LoginCreated(session=session, cookie_value=cookie_value)


def _authenticate_session_on(
    connection: Connection, command: SessionCommand
) -> SessionOutcome:
    decoded = decode_session_secrets(command.cookie_value)
    if decoded is None:
        return SessionMissing()
    session_hash, csrf_hash, csrf_token = decoded

    now = _utc_now()
    row = (
        connection.execute(
            select(
                web_sessions_table.c.id,
                web_sessions_table.c.user_id,
                web_sessions_table.c.csrf_secret_hash,
                web_sessions_table.c.last_used_at,
                web_sessions_table.c.absolute_expires_at,
                users_table.c.username,
            )
            .select_from(
                web_sessions_table.join(
                    users_table, users_table.c.id == web_sessions_table.c.user_id
                )
            )
            .where(
                web_sessions_table.c.secret_hash == session_hash,
                web_sessions_table.c.revoked_at.is_(None),
                web_sessions_table.c.idle_expires_at > now,
                web_sessions_table.c.absolute_expires_at > now,
                users_table.c.status == "active",
                users_table.c.role == "admin",
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None or not hmac.compare_digest(
        cast(bytes, row["csrf_secret_hash"]), csrf_hash
    ):
        return SessionMissing()

    if command.touch and cast(datetime, row["last_used_at"]) <= (
        now - SESSION_LAST_USE_COALESCE
    ):
        absolute_expires_at = cast(datetime, row["absolute_expires_at"])
        connection.execute(
            update(web_sessions_table)
            .where(
                web_sessions_table.c.id == row["id"],
                web_sessions_table.c.last_used_at <= (now - SESSION_LAST_USE_COALESCE),
            )
            .values(
                last_used_at=now,
                idle_expires_at=min(now + SESSION_IDLE_LIFETIME, absolute_expires_at),
            )
        )

    return SessionAuthenticated(
        AuthSession(
            id=cast(int, row["id"]),
            user_id=cast(int, row["user_id"]),
            login=cast(str, row["username"]),
            csrf_token=csrf_token,
        )
    )


def _rotate_password_on(
    connection: Connection, command: RotatePasswordCommand
) -> PasswordRotated:
    password_hash = PASSWORD_HASHER.hash(command.password)
    now = _utc_now()
    # Keep lock order compatible with login: throttle rows precede the
    # administrator row. This also makes the new password immediately
    # usable after operator recovery from a blocked login.
    connection.execute(delete(auth_login_throttle_table))
    user_id = connection.execute(
        select(users_table.c.id)
        .where(
            users_table.c.username == "admin",
            users_table.c.role == "admin",
            users_table.c.status == "active",
        )
        .with_for_update()
    ).scalar_one_or_none()
    if user_id is None:
        raise RuntimeError("The active administrator credential is unavailable.")

    updated = connection.execute(
        update(password_credentials_table)
        .where(password_credentials_table.c.user_id == user_id)
        .values(password_hash=password_hash, changed_at=now)
    )
    if updated.rowcount != 1:
        raise RuntimeError("The active administrator credential is unavailable.")

    revoked = connection.execute(
        update(web_sessions_table)
        .where(web_sessions_table.c.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    return PasswordRotated(max(0, revoked.rowcount))


def _seeded_identity_on(connection: Connection) -> SeededIdentityOutcome:
    rows = (
        connection.execute(
            select(
                users_table.c.username,
                users_table.c.role,
                users_table.c.status,
                password_credentials_table.c.password_hash,
            ).select_from(
                users_table.outerjoin(
                    password_credentials_table,
                    password_credentials_table.c.user_id == users_table.c.id,
                )
            )
        )
        .mappings()
        .all()
    )
    valid = False
    if len(rows) == 1:
        row = rows[0]
        password_hash = row["password_hash"]
        if (
            row["username"] == "admin"
            and row["role"] == "admin"
            and row["status"] == "active"
            and isinstance(password_hash, str)
        ):
            valid = _supported_password_hash(password_hash)
    return SeededIdentityValid() if valid else SeededIdentityInvalid()


def _supported_password_hash(password_hash: str) -> bool:
    try:
        parameters = extract_parameters(password_hash)
    except InvalidHashError:
        return False
    return bool(
        parameters.type is Type.ID
        and parameters.version == 19
        and parameters.memory_cost >= PASSWORD_MEMORY_KIB
        and parameters.time_cost >= PASSWORD_TIME_COST
        and parameters.parallelism >= PASSWORD_PARALLELISM
        and parameters.salt_len >= PASSWORD_SALT_BYTES
        and parameters.hash_len >= PASSWORD_HASH_BYTES
    )


def _verify_password(password_hash: str, password: str) -> bool:
    try:
        return bool(PASSWORD_HASHER.verify(password_hash, password))
    except InvalidHashError, VerificationError:
        return False


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_throttle_bucket(
    connection: Connection,
    scope: str,
    bucket_key: str,
    now: datetime,
) -> None:
    connection.execute(
        pg_insert(auth_login_throttle_table)
        .values(
            scope=scope,
            bucket_key=bucket_key,
            window_started_at=now,
            failure_count=0,
            updated_at=now,
        )
        .on_conflict_do_nothing(
            index_elements=[
                auth_login_throttle_table.c.scope,
                auth_login_throttle_table.c.bucket_key,
            ]
        )
    )


def _lock_bucket(connection: Connection, scope: str, bucket_key: str) -> RowMapping:
    return (
        connection.execute(
            select(auth_login_throttle_table)
            .where(
                auth_login_throttle_table.c.scope == scope,
                auth_login_throttle_table.c.bucket_key == bucket_key,
            )
            .with_for_update()
        )
        .mappings()
        .one()
    )


def _refresh_bucket(
    connection: Connection, bucket: RowMapping, now: datetime
) -> RowMapping:
    blocked_until = cast(datetime | None, bucket["blocked_until"])
    window_started_at = cast(datetime, bucket["window_started_at"])
    if throttle_window_expired(window_started_at, blocked_until, now):
        connection.execute(
            update(auth_login_throttle_table)
            .where(
                auth_login_throttle_table.c.scope == bucket["scope"],
                auth_login_throttle_table.c.bucket_key == bucket["bucket_key"],
            )
            .values(
                window_started_at=now,
                failure_count=0,
                blocked_until=None,
                updated_at=now,
            )
        )
        return _lock_bucket(
            connection, cast(str, bucket["scope"]), cast(str, bucket["bucket_key"])
        )
    return bucket


def _active_retry_after(bucket: RowMapping, now: datetime) -> int | None:
    return retry_after_seconds(cast(datetime | None, bucket["blocked_until"]), now)


def _record_failure(
    connection: Connection,
    bucket: RowMapping,
    now: datetime,
    limit: int,
    block_lifetime: timedelta,
) -> None:
    failure_count = cast(int, bucket["failure_count"]) + 1
    connection.execute(
        update(auth_login_throttle_table)
        .where(
            auth_login_throttle_table.c.scope == bucket["scope"],
            auth_login_throttle_table.c.bucket_key == bucket["bucket_key"],
        )
        .values(
            failure_count=failure_count,
            blocked_until=(now + block_lifetime if failure_count >= limit else None),
            updated_at=now,
        )
    )


def _prune_throttles(connection: Connection, now: datetime) -> None:
    stale_before = now - (THROTTLE_WINDOW + SOURCE_BLOCK_LIFETIME)
    stale_ids = (
        select(
            auth_login_throttle_table.c.scope,
            auth_login_throttle_table.c.bucket_key,
        )
        .where(
            auth_login_throttle_table.c.scope == "source",
            auth_login_throttle_table.c.updated_at < stale_before,
        )
        .order_by(auth_login_throttle_table.c.updated_at)
        .limit(THROTTLE_PRUNE_LIMIT)
        .cte("stale_throttles")
    )
    connection.execute(
        delete(auth_login_throttle_table).where(
            and_(
                auth_login_throttle_table.c.scope == stale_ids.c.scope,
                auth_login_throttle_table.c.bucket_key == stale_ids.c.bucket_key,
            )
        )
    )


def _prune_sessions(connection: Connection, now: datetime) -> None:
    expired_ids = (
        select(web_sessions_table.c.id)
        .where(
            or_(
                web_sessions_table.c.revoked_at.is_not(None),
                web_sessions_table.c.idle_expires_at <= now,
                web_sessions_table.c.absolute_expires_at <= now,
            )
        )
        .order_by(web_sessions_table.c.id)
        .limit(SESSION_PRUNE_LIMIT)
    )
    connection.execute(
        delete(web_sessions_table).where(web_sessions_table.c.id.in_(expired_ids))
    )


def _new_session_secrets() -> tuple[str, bytes, bytes, str]:
    session_secret = secrets.token_bytes(SESSION_SECRET_BYTES)
    csrf_secret = secrets.token_bytes(CSRF_SECRET_BYTES)
    cookie_value = urlsafe_encode(session_secret + csrf_secret)
    return (
        cookie_value,
        hashlib.sha256(session_secret).digest(),
        hashlib.sha256(csrf_secret).digest(),
        urlsafe_encode(csrf_secret),
    )
