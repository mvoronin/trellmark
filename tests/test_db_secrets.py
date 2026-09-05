"""Execute the `just db-secrets` recipe against a mocked podman.

The other deployment tests read the justfile as text, which cannot catch a
recipe that builds a wrong DSN — string matching would happily pass a broken
encoder. These run the real recipe with `podman` replaced by a shim that
records what each secret was given, then parse the captured DSN the way the
application does.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

BASE_DIR = Path(__file__).resolve().parents[1]

PODMAN_SHIM = """#!/usr/bin/env bash
# Records `podman secret create --replace <name> -` payloads to $SECRET_DIR.
if [ "$1" = "secret" ] && [ "$2" = "create" ]; then
    shift 2
    name=""
    for arg in "$@"; do
        case "$arg" in
            --*|-) ;;
            *) name="$arg" ;;
        esac
    done
    cat > "$SECRET_DIR/$name"
    exit 0
fi
exit 0
"""


@pytest.fixture
def run_db_secrets(tmp_path):
    """Return a callable that runs `just db-secrets` with a given password."""
    if shutil.which("just") is None:
        pytest.skip("just is not installed")

    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "podman"
    shim.write_text(PODMAN_SHIM)
    shim.chmod(0o755)

    def run(password: str) -> dict[str, str]:
        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
        env["SECRET_DIR"] = str(secret_dir)
        result = subprocess.run(
            ["just", "db-secrets"],
            cwd=BASE_DIR,
            env=env,
            input=f"{password}\n",
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert password not in result.stdout
        assert password not in result.stderr
        return {
            path.name: path.read_text()
            for path in secret_dir.iterdir()
            if path.is_file()
        }

    return run


@pytest.mark.parametrize(
    "password",
    [
        "plain123",
        "p@ss",  # the '@' that used to shift the userinfo/host split
        "a%41b",  # would decode to 'aAb' unencoded
        "sec%ret",
        "sim/ple",
        "a:b#c?d",
        "ünïcode",
        "back\\slash",
        "  padded  ",  # default IFS in `read` would strip these
        "\ttabbed\t",
    ],
)
def test_db_secrets_dsn_round_trips_the_password(run_db_secrets, password):
    secrets = run_db_secrets(password)

    assert set(secrets) == {"trellmark-db-password", "trellmark-database-url"}
    assert secrets["trellmark-db-password"] == password

    url = make_url(secrets["trellmark-database-url"])
    assert url.password == password
    assert url.username == "trellmark"
    assert url.host == "127.0.0.1"
    assert url.port == 5432
    assert url.database == "trellmark"


@pytest.mark.parametrize("password", ["p@ss", "  padded  "])
def test_db_secrets_dsn_redacts_without_leaking(run_db_secrets, password, monkeypatch):
    """The stored DSN must survive the app's own validation and redaction."""
    from trellmark import config

    secrets = run_db_secrets(password)
    monkeypatch.setattr(config, "DATABASE_URL", secrets["trellmark-database-url"])

    redacted = config.redacted_database_url()

    assert config.database_url().password == password
    assert redacted == "postgresql+psycopg://trellmark:***@127.0.0.1:5432/trellmark"
    assert password.strip() not in redacted


def test_db_secrets_refuses_an_empty_password(tmp_path):
    if shutil.which("just") is None:
        pytest.skip("just is not installed")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "podman"
    shim.write_text(PODMAN_SHIM)
    shim.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["SECRET_DIR"] = str(tmp_path)

    result = subprocess.run(
        ["just", "db-secrets"],
        cwd=BASE_DIR,
        env=env,
        input="\n",
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode != 0
    assert "Refusing to set an empty password" in result.stderr
