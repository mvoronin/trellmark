from io import StringIO

import pytest
from sqlalchemy import create_engine, inspect, text

import trellmark
from tests.helpers import CURRENT_HEAD, db_connection, db_query
from tests.postgres import TEST_LOGIN, TEST_PASSWORD
from trellmark import cli, config
from trellmark.identity import (
    LoginRejected,
    create_login_session,
    verify_seeded_identity,
)


def test_create_app_does_not_touch_the_database(postgres_server, monkeypatch):
    """Building the app must not connect, let alone migrate.

    The database is created here but never migrated, so any connection the
    factory made would have to fail or leave a schema behind.
    """
    url = postgres_server.create_database()
    monkeypatch.setattr(
        config, "DATABASE_URL", url.render_as_string(hide_password=False)
    )
    trellmark.dispose_engine()
    try:
        trellmark.create_app()

        assert _table_names(url) == []
    finally:
        trellmark.dispose_engine()
        postgres_server.drop_database(url)


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
        trellmark.verify_db_at_head()

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
    trellmark.dispose_engine()
    try:
        with pytest.raises(RuntimeError, match="has no trellmark schema"):
            trellmark.verify_db_at_head()
    finally:
        trellmark.dispose_engine()
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
    trellmark.dispose_engine()
    try:
        with pytest.raises(RuntimeError, match="Cannot connect to PostgreSQL") as error:
            trellmark.verify_db_at_head()
        assert "secret" not in str(error.value)
    finally:
        trellmark.dispose_engine()


def test_verify_db_at_head_reports_a_stale_revision(database):
    with db_connection() as connection:
        connection.execute(
            text("UPDATE alembic_version SET version_num = '0000_ancient'")
        )
    try:
        with pytest.raises(RuntimeError, match="is at revision 0000_ancient"):
            trellmark.verify_db_at_head()
    finally:
        with db_connection() as connection:
            connection.execute(
                text("UPDATE alembic_version SET version_num = :head"),
                {"head": CURRENT_HEAD},
            )


def test_verify_db_at_head_accepts_current_database(database):
    trellmark.verify_db_at_head()


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
    trellmark.dispose_engine()
    try:
        with pytest.raises(RuntimeError, match="TRELLMARK_DATABASE_URL is not set"):
            trellmark.verify_db_at_head()
        assert config.redacted_database_url() == "<TRELLMARK_DATABASE_URL not set>"
    finally:
        trellmark.dispose_engine()


def test_non_postgresql_database_url_is_rejected(monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", "sqlite:///urls.db")
    trellmark.dispose_engine()
    try:
        with pytest.raises(RuntimeError, match="must be a PostgreSQL URL"):
            trellmark.verify_db_at_head()
    finally:
        trellmark.dispose_engine()


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
    trellmark.dispose_engine()
    try:
        with pytest.raises(RuntimeError, match="the host contains '@'"):
            config.database_url()

        redacted = config.redacted_database_url()
        assert redacted == "<invalid database URL>"
        assert "ss@" not in redacted
        assert "sign" not in redacted
    finally:
        trellmark.dispose_engine()


def test_percent_encoded_password_is_accepted_and_redacted(monkeypatch):
    monkeypatch.setattr(
        config,
        "DATABASE_URL",
        "postgresql+psycopg://trellmark:p%40ss@127.0.0.1:5432/trellmark",
    )
    trellmark.dispose_engine()
    try:
        assert config.database_url().password == "p@ss"
        assert (
            config.redacted_database_url()
            == "postgresql+psycopg://trellmark:***@127.0.0.1:5432/trellmark"
        )
    finally:
        trellmark.dispose_engine()
