"""Framework-free Identity commands, session values, and security policies."""

import base64
import hashlib
import hmac
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

PASSWORD_MEMORY_KIB = 19_456
PASSWORD_TIME_COST = 2
PASSWORD_PARALLELISM = 1
PASSWORD_HASH_BYTES = 32
PASSWORD_SALT_BYTES = 16
ADMIN_PASSWORD_MIN_BYTES = 20
ADMIN_PASSWORD_MAX_BYTES = 1_024

SESSION_SECRET_BYTES = 32
CSRF_SECRET_BYTES = 32
SESSION_IDLE_LIFETIME = timedelta(minutes=30)
SESSION_ABSOLUTE_LIFETIME = timedelta(hours=24)
SESSION_LAST_USE_COALESCE = timedelta(minutes=5)
SESSION_PRUNE_LIMIT = 100

THROTTLE_WINDOW = timedelta(minutes=10)
SOURCE_FAILURE_LIMIT = 10
SOURCE_BLOCK_LIFETIME = timedelta(minutes=15)
GLOBAL_FAILURE_LIMIT = 100
GLOBAL_BLOCK_LIFETIME = timedelta(minutes=5)
GLOBAL_BUCKET_KEY = "instance"
MAX_SOURCE_KEY_LENGTH = 128
MAX_RETRY_AFTER_SECONDS = 15 * 60
THROTTLE_PRUNE_LIMIT = 100


@dataclass(frozen=True, slots=True)
class LoginCommand:
    source: str = field(repr=False)
    login: str = field(repr=False)
    password: str = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", bounded_source_key(self.source))
        object.__setattr__(self, "login", self.login.strip().lower())


@dataclass(frozen=True, slots=True)
class AuthSession:
    id: int
    user_id: int
    login: str = field(repr=False)
    csrf_token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class SessionCommand:
    cookie_value: str | None = field(repr=False)
    touch: bool = True


@dataclass(frozen=True, slots=True)
class RotatePasswordCommand:
    password: str = field(repr=False)

    def __post_init__(self) -> None:
        validate_administrator_password(self.password)


@dataclass(frozen=True, slots=True)
class RevokeSessionCommand:
    session_id: int


@dataclass(frozen=True, slots=True)
class LoginCreated:
    session: AuthSession
    cookie_value: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class LoginFailureRecorded:
    """A rejected attempt whose throttle accounting completed successfully."""


@dataclass(frozen=True, slots=True)
class LoginThrottled:
    """A blocked attempt whose housekeeping completed successfully."""

    retry_after: int


type LoginOutcome = LoginCreated | LoginFailureRecorded | LoginThrottled


@dataclass(frozen=True, slots=True)
class SessionAuthenticated:
    session: AuthSession


@dataclass(frozen=True, slots=True)
class SessionMissing:
    pass


type SessionOutcome = SessionAuthenticated | SessionMissing


@dataclass(frozen=True, slots=True)
class PasswordRotated:
    revoked_sessions: int


@dataclass(frozen=True, slots=True)
class SessionRevoked:
    pass


@dataclass(frozen=True, slots=True)
class IdentityCleaned:
    pass


@dataclass(frozen=True, slots=True)
class SeededIdentityValid:
    pass


@dataclass(frozen=True, slots=True)
class SeededIdentityInvalid:
    pass


type SeededIdentityOutcome = SeededIdentityValid | SeededIdentityInvalid


def csrf_matches(session: AuthSession, supplied_token: str | None) -> bool:
    return (
        supplied_token is not None
        and supplied_token.isascii()
        and hmac.compare_digest(session.csrf_token, supplied_token)
    )


def bounded_source_key(source: str) -> str:
    value = source.strip() or "unknown"
    if len(value) <= MAX_SOURCE_KEY_LENGTH:
        return value
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def validate_administrator_password(password: str) -> None:
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


def retry_after_seconds(blocked_until: datetime | None, now: datetime) -> int | None:
    if blocked_until is None or blocked_until <= now:
        return None
    seconds = math.ceil((blocked_until - now).total_seconds())
    return max(1, min(seconds, MAX_RETRY_AFTER_SECONDS))


def throttle_window_expired(
    window_started_at: datetime, blocked_until: datetime | None, now: datetime
) -> bool:
    return (blocked_until is not None and blocked_until <= now) or (
        blocked_until is None and window_started_at + THROTTLE_WINDOW <= now
    )


def decode_session_secrets(
    cookie_value: str | None,
) -> tuple[bytes, bytes, str] | None:
    if cookie_value is None or len(cookie_value) > 128:
        return None
    try:
        raw = urlsafe_decode(cookie_value)
    except ValueError:
        return None
    if len(raw) != SESSION_SECRET_BYTES + CSRF_SECRET_BYTES:
        return None
    session_secret = raw[:SESSION_SECRET_BYTES]
    csrf_secret = raw[SESSION_SECRET_BYTES:]
    return (
        hashlib.sha256(session_secret).digest(),
        hashlib.sha256(csrf_secret).digest(),
        urlsafe_encode(csrf_secret),
    )


def urlsafe_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def urlsafe_decode(value: str) -> bytes:
    try:
        return base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except ValueError as error:
        raise ValueError("Invalid opaque session token.") from error
