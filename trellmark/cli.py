import argparse
import getpass
import sys
from collections.abc import Callable, Sequence
from typing import cast

import uvicorn
from alembic import command

from .app import create_app
from .config import public_origin
from .identity.persistence import set_administrator_password, verify_seeded_identity
from .platform.runtime import alembic_config, run_migrations, verify_db_at_head

type CommandHandler = Callable[[argparse.Namespace], None]
FORWARDED_ALLOW_IPS = "127.0.0.1,::1"


def main(argv: Sequence[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0].startswith("-"):
        args = ["serve", *args]

    parser = argparse.ArgumentParser(description="Manage Trellmark.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve", help="run the Trellmark web app")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", default=8000, type=int)
    serve_parser.set_defaults(func=serve)

    migrate_parser = subparsers.add_parser("migrate", help="apply database migrations")
    migrate_parser.set_defaults(func=migrate)

    revision_parser = subparsers.add_parser(
        "revision", help="create an Alembic revision"
    )
    revision_parser.add_argument("-m", "--message", required=True)
    revision_parser.set_defaults(func=revision)

    password_parser = subparsers.add_parser(
        "set-password",
        help="replace the administrator password and revoke all sessions",
    )
    password_parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the password from standard input instead of a hidden prompt",
    )
    password_parser.set_defaults(func=set_password)

    identity_parser = subparsers.add_parser(
        "verify-identity",
        help=argparse.SUPPRESS,
    )
    identity_parser.set_defaults(func=verify_identity)

    parsed = parser.parse_args(args)
    handler = cast(CommandHandler, getattr(parsed, "func"))
    handler(parsed)


def serve(args: argparse.Namespace) -> None:
    try:
        public_origin()
        verify_db_at_head()
        verify_seeded_identity()
    except RuntimeError as error:
        raise SystemExit(str(error)) from error
    uvicorn.run(
        create_app(),
        host=_namespace_str(args, "host"),
        port=_namespace_int(args, "port"),
        proxy_headers=True,
        forwarded_allow_ips=FORWARDED_ALLOW_IPS,
    )


def migrate(_args: argparse.Namespace) -> None:
    run_migrations()


def revision(args: argparse.Namespace) -> None:
    command.revision(alembic_config(), message=_namespace_str(args, "message"))


def set_password(args: argparse.Namespace) -> None:
    try:
        verify_db_at_head()
        password = _read_new_password(bool(getattr(args, "password_stdin", False)))
        revoked_sessions = set_administrator_password(password)
    except (RuntimeError, ValueError) as error:
        raise SystemExit(str(error)) from error
    suffix = "session" if revoked_sessions == 1 else "sessions"
    print(f"Administrator password updated; revoked {revoked_sessions} {suffix}.")


def verify_identity(_args: argparse.Namespace) -> None:
    """Let deployment automation detect an uninitialized administrator."""
    try:
        verify_db_at_head()
        verify_seeded_identity()
    except RuntimeError as error:
        raise SystemExit(str(error)) from error


def _read_new_password(password_stdin: bool) -> str:
    if password_stdin:
        password = sys.stdin.read().removesuffix("\n").removesuffix("\r")
        if "\n" in password or "\r" in password:
            raise ValueError("Standard input must contain exactly one password line.")
        return password
    password = getpass.getpass("New administrator password: ")
    confirmation = getpass.getpass("Confirm administrator password: ")
    if password != confirmation:
        raise ValueError("Administrator passwords do not match.")
    return password


def _namespace_str(args: argparse.Namespace, name: str) -> str:
    value = getattr(args, name)
    if not isinstance(value, str):
        raise SystemExit(f"Invalid {name}.")
    return value


def _namespace_int(args: argparse.Namespace, name: str) -> int:
    value = getattr(args, name)
    if type(value) is not int:
        raise SystemExit(f"Invalid {name}.")
    return value
