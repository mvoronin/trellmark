import os
from functools import cache
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy.engine import URL, make_url

BASE_DIR: Path = Path(__file__).resolve().parent.parent
WEB_DIR: Path = BASE_DIR / "web"
STATIC_DIR: Path = WEB_DIR / "static"
INDEX_FILE: Path = WEB_DIR / "index.html"
MIGRATIONS_DIR: Path = BASE_DIR / "migrations"

DATABASE_URL_ENV = "TRELLMARK_DATABASE_URL"
PUBLIC_ORIGIN_ENV = "TRELLMARK_PUBLIC_ORIGIN"
SUPPORTED_DRIVER = "postgresql+psycopg"

# No default. A missing DSN is missing configuration, not a cue to guess at a
# passwordless localhost database — guessing either connects to whatever
# happens to be there or reports a confusing connection failure, and neither
# tells the operator what is actually wrong.
DATABASE_URL: str | None = os.environ.get(DATABASE_URL_ENV)
PUBLIC_ORIGIN: str | None = os.environ.get(PUBLIC_ORIGIN_ENV)

PRODUCTION_SESSION_COOKIE = "__Host-trellmark_session"
DEVELOPMENT_SESSION_COOKIE = "trellmark_session_dev"


def database_url() -> URL:
    """Return the configured PostgreSQL DSN, rejecting other backends.

    The DSN is the whole database configuration; there is no filesystem path
    and no SQLite fallback. A misconfigured backend has to fail here rather
    than surface later as a confusing dialect error.
    """
    if not DATABASE_URL:
        raise RuntimeError(
            f"{DATABASE_URL_ENV} is not set. Point it at the PostgreSQL "
            "database, for example "
            "postgresql+psycopg://trellmark:PASSWORD@127.0.0.1:5432/trellmark"
        )

    try:
        url = make_url(DATABASE_URL)
    except Exception as error:
        raise RuntimeError(
            f"{DATABASE_URL_ENV} is not a valid database URL."
        ) from error

    if url.get_backend_name() != "postgresql":
        raise RuntimeError(
            f"{DATABASE_URL_ENV} must be a PostgreSQL URL; "
            f"got backend {url.get_backend_name()!r}."
        )
    if url.database is None or not url.database:
        raise RuntimeError(f"{DATABASE_URL_ENV} must name a database.")

    # A host can never contain '@'. Seeing one means an unencoded '@' in the
    # password shifted the userinfo/host split: the password is truncated at
    # that character and its remainder became part of the host. That is not
    # only unusable, it defeats redaction — render_as_string(hide_password=True)
    # masks only the parsed password, so the leftover bytes would be printed in
    # the host position. Reject it here so redaction fails closed.
    #
    # The message deliberately does not echo the host, which holds those bytes.
    if url.host and "@" in url.host:
        raise RuntimeError(
            f"{DATABASE_URL_ENV} is malformed: the host contains '@'. "
            "If the password contains '@', percent-encode it as %40 "
            "(`just db-secrets` does this for you)."
        )

    # Normalize to the one supported driver so a bare `postgresql://` DSN does
    # not silently pull in psycopg2, which is not a dependency of this app.
    return url.set(drivername=SUPPORTED_DRIVER)


def redacted_database_url() -> str:
    """Return the DSN safe for logs and operator-facing errors."""
    if not DATABASE_URL:
        return f"<{DATABASE_URL_ENV} not set>"
    try:
        return database_url().render_as_string(hide_password=True)
    except RuntimeError:
        return "<invalid database URL>"


def public_origin() -> str:
    """Return the single trusted browser origin in canonical form.

    Plain HTTP is intentionally limited to an explicit loopback development
    origin. A LAN hostname or address is a deployment, not development, and
    must use HTTPS before cookies or passwords cross the network.
    """
    return _canonical_public_origin(PUBLIC_ORIGIN)


@cache
def _canonical_public_origin(configured_origin: str | None) -> str:
    if not configured_origin:
        raise RuntimeError(
            f"{PUBLIC_ORIGIN_ENV} is not set. Configure the browser-facing "
            "origin, for example https://urls.example.com."
        )

    try:
        parsed = urlsplit(configured_origin)
        port = parsed.port
    except ValueError as error:
        raise RuntimeError(f"{PUBLIC_ORIGIN_ENV} is not a valid origin.") from error

    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or "*" in configured_origin
    ):
        raise RuntimeError(
            f"{PUBLIC_ORIGIN_ENV} must be one http(s) origin with no "
            "credentials, path, query, fragment, or wildcard."
        )

    host = parsed.hostname.lower()
    if parsed.scheme == "http" and not _is_loopback_host(host):
        raise RuntimeError(
            f"{PUBLIC_ORIGIN_ENV} must use https unless its host is localhost "
            "or a loopback IP address."
        )

    display_host = f"[{host}]" if ":" in host else host
    default_port = 80 if parsed.scheme == "http" else 443
    port_suffix = f":{port}" if port is not None and port != default_port else ""
    return f"{parsed.scheme}://{display_host}{port_suffix}"


def secure_session_cookie() -> bool:
    return public_origin().startswith("https://")


def session_cookie_name() -> str:
    return (
        PRODUCTION_SESSION_COOKIE
        if secure_session_cookie()
        else DEVELOPMENT_SESSION_COOKIE
    )


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False
