import base64
import hashlib
import hmac
import math
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import cast

from argon2 import extract_parameters
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
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.exc import SQLAlchemyError

from ..storage import get_engine
from .policy import (
    ADMIN_PASSWORD_MAX_BYTES,
    ADMIN_PASSWORD_MIN_BYTES,
    CSRF_SECRET_BYTES,
    DUMMY_PASSWORD_HASH,
    GLOBAL_BLOCK_LIFETIME,
    GLOBAL_BUCKET_KEY,
    GLOBAL_FAILURE_LIMIT,
    MAX_RETRY_AFTER_SECONDS,
    MAX_SOURCE_KEY_LENGTH,
    PASSWORD_HASH_BYTES,
    PASSWORD_HASHER,
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


@dataclass(frozen=True)
class AuthSession:
    id: int
    user_id: int
    login: str
    csrf_token: str


class LoginRejected(Exception):
    pass


class LoginBlocked(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__("Login is temporarily throttled.")
        self.retry_after = retry_after


@dataclass(frozen=True)
class _LoginCreated:
    session: AuthSession
    cookie_value: str


@dataclass(frozen=True)
class _LoginBlocked:
    retry_after: int


@dataclass(frozen=True)
class _LoginRejected:
    pass


type _LoginResult = _LoginCreated | _LoginBlocked | _LoginRejected


def create_login_session(
    source: str, login: str, password: str
) -> tuple[AuthSession, str]:
    """Verify one password attempt and atomically persist its policy outcome."""
    normalized_login = login.strip().lower()
    source_key = _bounded_source_key(source)
    with get_engine().begin() as connection:
        result = _attempt_login(
            connection,
            source_key=source_key,
            login=normalized_login,
            password=password,
        )

    if isinstance(result, _LoginBlocked):
        raise LoginBlocked(result.retry_after)
    if isinstance(result, _LoginRejected):
        raise LoginRejected
    return result.session, result.cookie_value


def _attempt_login(
    connection: Connection,
    *,
    source_key: str,
    login: str,
    password: str,
) -> _LoginResult:
    now = _utc_now()
    _prune_throttles(connection, now)
    _ensure_throttle_bucket(connection, "global", GLOBAL_BUCKET_KEY, now)
    # Hold this global row lock through Argon2id verification: serialization
    # bounds memory use even when many login requests occupy worker threads.
    global_bucket = _lock_bucket(connection, "global", GLOBAL_BUCKET_KEY)
    global_bucket = _refresh_bucket(connection, global_bucket, now)
    retry_after = _active_retry_after(global_bucket, now)
    if retry_after is not None:
        return _LoginBlocked(retry_after)

    _ensure_throttle_bucket(connection, "source", source_key, now)
    source_bucket = _lock_bucket(connection, "source", source_key)
    source_bucket = _refresh_bucket(connection, source_bucket, now)
    retry_after = _active_retry_after(source_bucket, now)
    if retry_after is not None:
        return _LoginBlocked(retry_after)

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
        return _LoginRejected()

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
    return _LoginCreated(session=session, cookie_value=cookie_value)


def authenticate_session(
    cookie_value: str | None, *, touch: bool = True
) -> AuthSession | None:
    decoded = _decode_session_secrets(cookie_value)
    if decoded is None:
        return None
    session_hash, csrf_hash, csrf_token = decoded

    with get_engine().begin() as connection:
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
            return None

        if touch and cast(datetime, row["last_used_at"]) <= (
            now - SESSION_LAST_USE_COALESCE
        ):
            absolute_expires_at = cast(datetime, row["absolute_expires_at"])
            connection.execute(
                update(web_sessions_table)
                .where(
                    web_sessions_table.c.id == row["id"],
                    web_sessions_table.c.last_used_at
                    <= (now - SESSION_LAST_USE_COALESCE),
                )
                .values(
                    last_used_at=now,
                    idle_expires_at=min(
                        now + SESSION_IDLE_LIFETIME, absolute_expires_at
                    ),
                )
            )

        return AuthSession(
            id=cast(int, row["id"]),
            user_id=cast(int, row["user_id"]),
            login=cast(str, row["username"]),
            csrf_token=csrf_token,
        )


def csrf_matches(session: AuthSession, supplied_token: str | None) -> bool:
    return (
        supplied_token is not None
        and supplied_token.isascii()
        and hmac.compare_digest(session.csrf_token, supplied_token)
    )


def revoke_session(session: AuthSession) -> None:
    with get_engine().begin() as connection:
        now = _utc_now()
        connection.execute(
            update(web_sessions_table)
            .where(
                web_sessions_table.c.id == session.id,
                web_sessions_table.c.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )


def set_administrator_password(password: str) -> int:
    """Replace the sole administrator verifier and revoke every live session."""
    password_size = len(password.encode("utf-8"))
    if password_size < ADMIN_PASSWORD_MIN_BYTES:
        raise ValueError(
            f"The administrator password must be at least "
            f"{ADMIN_PASSWORD_MIN_BYTES} UTF-8 bytes."
        )
    if password_size > ADMIN_PASSWORD_MAX_BYTES:
        raise ValueError(
            f"The administrator password must be at most "
            f"{ADMIN_PASSWORD_MAX_BYTES} UTF-8 bytes."
        )

    password_hash = PASSWORD_HASHER.hash(password)
    try:
        with get_engine().begin() as connection:
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
                raise RuntimeError(
                    "The active administrator credential is unavailable."
                )

            updated = connection.execute(
                update(password_credentials_table)
                .where(password_credentials_table.c.user_id == user_id)
                .values(password_hash=password_hash, changed_at=now)
            )
            if updated.rowcount != 1:
                raise RuntimeError(
                    "The active administrator credential is unavailable."
                )

            revoked = connection.execute(
                update(web_sessions_table)
                .where(web_sessions_table.c.revoked_at.is_(None))
                .values(revoked_at=now)
            )
            return max(0, revoked.rowcount)
    except SQLAlchemyError as error:
        raise RuntimeError("Could not update the administrator credential.") from error


def verify_seeded_identity() -> None:
    """Fail closed unless the migration's one administrator is usable."""
    try:
        with get_engine().connect() as connection:
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
    except SQLAlchemyError as error:
        raise RuntimeError(
            "The seeded administrator credential is unavailable."
        ) from error

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
    if not valid:
        raise RuntimeError("The seeded administrator credential is missing or invalid.")


def database_ready() -> bool:
    try:
        with get_engine().connect() as connection:
            connection.execute(select(1)).scalar_one()
        return True
    except SQLAlchemyError:
        return False


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


def _bounded_source_key(source: str) -> str:
    value = source.strip() or "unknown"
    if len(value) <= MAX_SOURCE_KEY_LENGTH:
        return value
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


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
    if (blocked_until is not None and blocked_until <= now) or (
        blocked_until is None and window_started_at + THROTTLE_WINDOW <= now
    ):
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
    blocked_until = cast(datetime | None, bucket["blocked_until"])
    if blocked_until is None or blocked_until <= now:
        return None
    seconds = math.ceil((blocked_until - now).total_seconds())
    return max(1, min(seconds, MAX_RETRY_AFTER_SECONDS))


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
    cookie_value = _urlsafe_encode(session_secret + csrf_secret)
    return (
        cookie_value,
        hashlib.sha256(session_secret).digest(),
        hashlib.sha256(csrf_secret).digest(),
        _urlsafe_encode(csrf_secret),
    )


def _decode_session_secrets(
    cookie_value: str | None,
) -> tuple[bytes, bytes, str] | None:
    if cookie_value is None or len(cookie_value) > 128:
        return None
    try:
        raw = _urlsafe_decode(cookie_value)
    except ValueError:
        return None
    if len(raw) != SESSION_SECRET_BYTES + CSRF_SECRET_BYTES:
        return None
    session_secret = raw[:SESSION_SECRET_BYTES]
    csrf_secret = raw[SESSION_SECRET_BYTES:]
    return (
        hashlib.sha256(session_secret).digest(),
        hashlib.sha256(csrf_secret).digest(),
        _urlsafe_encode(csrf_secret),
    )


def _urlsafe_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _urlsafe_decode(value: str) -> bytes:
    try:
        return base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except ValueError as error:
        raise ValueError("Invalid opaque session token.") from error
