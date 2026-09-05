import os
import shutil
import subprocess
from pathlib import Path

import pytest

BASE_DIR = Path(__file__).resolve().parents[1]
DEPLOY_DIR = BASE_DIR / "deploy"
CONTAINERFILE = DEPLOY_DIR / "Containerfile"
QUADLET_DIR = DEPLOY_DIR / "quadlet"

PULL_FAILURE_PODMAN_SHIM = """#!/usr/bin/env bash
printf 'podman %s\\n' "$*" >> "$COMMAND_LOG"
if [ "${FAIL_POSTGRES_PULL:-0}" = "1" ] && [ "$1" = "pull" ] && [[ "$*" == *"postgres:18-alpine"* ]]; then
    exit 42
fi
if [ "$1" = "image" ] && [ "$2" = "inspect" ]; then
    printf 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa|[example.invalid/image@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb]\\n'
fi
if [ "${FAIL_RUNTIME_AUDIT:-0}" = "1" ] && [ "$1" = "ps" ]; then
    exit 43
fi
exit 0
"""

SYSTEMCTL_SHIM = """#!/usr/bin/env bash
printf 'systemctl %s\\n' "$*" >> "$COMMAND_LOG"
exit 0
"""


def _deploy_recipe(justfile: str) -> str:
    return justfile.split("deploy:\n", 1)[1].split("\nbackup ", 1)[0]


def test_one_containerfile_installs_locked_dependencies_and_the_complete_app():
    containerfile = CONTAINERFILE.read_text()

    assert "FROM docker.io/library/python:3.14-slim" in containerfile
    assert "COPY --from=ghcr.io/astral-sh/uv:0.12.3" in containerfile
    assert "COPY pyproject.toml uv.lock ./" in containerfile
    assert "uv sync --frozen --no-dev --no-install-project" in containerfile
    assert 'ENV PATH="/app/.venv/bin:$PATH"' in containerfile
    assert "COPY server.py ./" in containerfile
    assert "COPY trellmark ./trellmark" in containerfile
    staging, runtime = containerfile.split(
        "FROM docker.io/library/python:3.14-slim AS runtime", 1
    )
    assert "AS frontend-assets" in staging
    assert "COPY web/index.html ./web/index.html" in staging
    assert "COPY web/static ./web/static" in staging
    assert "RUN rm -rf ./web/static/design" in staging
    assert "web/design.html" not in staging
    assert "COPY --from=frontend-assets /assets/web ./web" in runtime
    assert "COPY web" not in runtime
    assert "rm -rf" not in runtime
    assert 'CMD ["python", "server.py", "--host", "0.0.0.0", "--port", "8000"]' in (
        containerfile
    )
    assert "pip install" not in containerfile
    assert "apt-get" not in containerfile
    assert "apk add" not in containerfile


def test_deploy_tree_contains_no_private_caddy_or_second_application_image():
    tracked_names = {
        path.name.lower() for path in DEPLOY_DIR.rglob("*") if path.is_file()
    }
    containerfile = CONTAINERFILE.read_text().lower()

    assert "containerfile.api" not in tracked_names
    assert "containerfile.web" not in tracked_names
    assert "caddyfile" not in tracked_names
    assert "trellmark-web.container" not in tracked_names
    assert "caddy:" not in containerfile
    assert "caddy run" not in containerfile


def test_container_command_runs_the_explicit_cli_with_frozen_startup_options(
    monkeypatch,
):
    import json

    from trellmark import cli
    from trellmark.identity import persistence
    from trellmark.platform import runtime

    assert cli.verify_db_at_head is runtime.verify_db_at_head
    assert cli.verify_seeded_identity is persistence.verify_seeded_identity
    command = next(
        json.loads(line.removeprefix("CMD "))
        for line in CONTAINERFILE.read_text().splitlines()
        if line.startswith("CMD ")
    )
    assert command == ["python", "server.py", "--host", "0.0.0.0", "--port", "8000"]
    events = []
    application = object()
    monkeypatch.setattr(cli, "public_origin", lambda: events.append("origin"))
    monkeypatch.setattr(cli, "verify_db_at_head", lambda: events.append("schema"))
    monkeypatch.setattr(
        cli, "verify_seeded_identity", lambda: events.append("identity")
    )

    def construct():
        events.append("construct")
        return application

    def serve(app, **options):
        assert app is application
        assert options == {
            "host": "0.0.0.0",
            "port": 8000,
            "proxy_headers": True,
            "forwarded_allow_ips": "127.0.0.1,::1",
        }
        events.append("serve")

    monkeypatch.setattr(cli, "create_app", construct)
    monkeypatch.setattr(cli.uvicorn, "run", serve)
    cli.main(command[2:])
    assert events == ["origin", "schema", "identity", "construct", "serve"]


@pytest.mark.parametrize("operation", ["migrate", "revision", "verify-identity"])
def test_maintenance_commands_dispatch_to_explicit_platform_and_identity_owners(
    monkeypatch, operation
):
    from trellmark import cli
    from trellmark.identity import persistence
    from trellmark.platform import runtime

    assert cli.run_migrations is runtime.run_migrations
    assert cli.alembic_config is runtime.alembic_config
    assert cli.verify_db_at_head is runtime.verify_db_at_head
    assert cli.verify_seeded_identity is persistence.verify_seeded_identity
    assert cli.set_administrator_password is persistence.set_administrator_password
    events = []
    configuration = runtime.alembic_config()
    monkeypatch.setattr(cli, "run_migrations", lambda: events.append("migrate"))
    monkeypatch.setattr(cli, "alembic_config", lambda: configuration)
    monkeypatch.setattr(cli, "verify_db_at_head", lambda: events.append("schema"))
    monkeypatch.setattr(
        cli, "verify_seeded_identity", lambda: events.append("identity")
    )

    def revision(config, *, message):
        assert config is configuration
        assert message == "synthetic revision"
        events.append("revision")

    monkeypatch.setattr(cli.command, "revision", revision)
    arguments = [operation]
    if operation == "revision":
        arguments.extend(["--message", "synthetic revision"])
    cli.main(arguments)
    assert events == (
        ["schema", "identity"] if operation == "verify-identity" else [operation]
    )


def test_image_carries_no_database_storage_or_embedded_credentials():
    containerfile = CONTAINERFILE.read_text()

    assert "TRELLMARK_DATABASE_URL=" not in containerfile
    assert "POSTGRES_PASSWORD=" not in containerfile
    assert "TRELLMARK_DB=" not in containerfile
    assert "VOLUME " not in containerfile
    assert 'name = "psycopg-binary"' in (BASE_DIR / "uv.lock").read_text()


def test_build_refreshes_every_external_source_for_the_single_image():
    justfile = (BASE_DIR / "justfile").read_text()
    build_recipe = justfile.split("build-image:", 1)[1].split(
        "# Build and run a disposable local pod", 1
    )[0]

    assert "sources deploy/Containerfile" in build_recipe
    assert "podman build --pull=always -t {{app_image}} -f deploy/Containerfile ." in (
        build_recipe
    )
    assert "container_images.sh audit" in build_recipe
    assert "Containerfile.api" not in build_recipe
    assert "Containerfile.web" not in build_recipe

    sources = subprocess.run(
        ["bash", "scripts/container_images.sh", "sources", "deploy/Containerfile"],
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert sources == [
        "docker.io/library/python:3.14-slim",
        "ghcr.io/astral-sh/uv:0.12.3",
    ]


def test_containerfile_sources_excludes_local_multistage_references(tmp_path):
    containerfile = tmp_path / "Containerfile"
    containerfile.write_text(
        """\
FROM --platform=linux/amd64 docker.io/library/python:3.14-slim AS Builder
COPY --from=ghcr.io/astral-sh/uv:0.12.3 /uv /bin/uv
FROM builder AS runtime
COPY --from=Builder /app /app
COPY --from=0 /etc/os-release /tmp/os-release
COPY --chown=1000 --from=docker.io/library/busybox:1.37 /bin/true /bin/true
"""
    )

    sources = subprocess.run(
        ["bash", "scripts/container_images.sh", "sources", str(containerfile)],
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()

    assert sources == [
        "docker.io/library/python:3.14-slim",
        "ghcr.io/astral-sh/uv:0.12.3",
        "docker.io/library/busybox:1.37",
    ]


def test_digest_log_covers_build_sources_and_runtime_images():
    justfile = (BASE_DIR / "justfile").read_text()
    audit_script = (BASE_DIR / "scripts" / "container_images.sh").read_text()
    digest_recipe = justfile.split("image-digests:", 1)[1].split(
        "deployed-image-digests:", 1
    )[0]

    assert "sources deploy/Containerfile" in digest_recipe
    assert '"${source_images[@]}" {{postgres_image}} {{app_image}}' in digest_recipe
    assert "{{.ID}}|{{.RepoDigests}}" in audit_script
    assert "{{.Name}}|{{.ImageName}}|{{.Image}}" in audit_script
    assert ".ImageDigest" not in audit_script
    assert "{{if not .IsInfra}}" in audit_script
    assert "xargs" not in audit_script


def test_pod_publishes_only_the_application_port_on_host_loopback():
    pod = (QUADLET_DIR / "trellmark.pod").read_text()
    application = (QUADLET_DIR / "trellmark-app.container").read_text()
    postgres = (QUADLET_DIR / "trellmark-postgres.container").read_text()

    assert "PodName=trellmark" in pod.splitlines()
    assert "PublishPort=127.0.0.1:8901:8000" in pod.splitlines()
    assert "0.0.0.0:" not in pod
    assert "PublishPort=" not in application
    assert "PublishPort=" not in postgres
    assert ":80:80" not in pod
    assert ":443:443" not in pod


def test_readme_places_tls_and_public_routing_outside_this_repository():
    readme = (BASE_DIR / "README.md").read_text()

    assert "The app is published only as `127.0.0.1:8901 -> 8000`." in readme
    assert (
        "Trellmark owns no TLS keys and does not bind host ports 80 or 443." in readme
    )
    assert "The separate infrastructure repository owns the single shared Caddy" in (
        readme
    )
    assert "reverse-proxy the complete request URI to `http://127.0.0.1:8901`" in (
        readme
    )
    assert "must\nreturn `404` for `/internal/*`" in readme


def test_postgres_is_pinned_persistent_named_and_readiness_gated():
    unit = (QUADLET_DIR / "trellmark-postgres.container").read_text()
    volume = (QUADLET_DIR / "trellmark-postgres-data.volume").read_text()

    assert "ContainerName=trellmark-postgres" in unit.splitlines()
    assert "Image=docker.io/library/postgres:18-alpine" in unit
    assert "Pod=trellmark.pod" in unit
    assert "Volume=trellmark-postgres-data.volume:/var/lib/postgresql" in unit
    assert "VolumeName=trellmark-postgres-data" in volume
    assert "HealthCmd=pg_isready" in unit
    assert "HealthInterval=" in unit
    assert "HealthRetries=" in unit
    assert "Notify=healthy" in unit.splitlines()


def test_application_waits_for_postgres_and_reads_configuration_from_secrets():
    unit = (QUADLET_DIR / "trellmark-app.container").read_text()

    assert "ContainerName=trellmark-app" in unit.splitlines()
    assert "Image=localhost/trellmark:latest" in unit
    assert "Pod=trellmark.pod" in unit
    assert "After=trellmark-postgres.service" in unit
    assert "Requires=trellmark-postgres.service" in unit
    assert (
        "Secret=trellmark-database-url,type=env,target=TRELLMARK_DATABASE_URL" in unit
    )
    assert "Secret=trellmark-public-origin,type=env,target=TRELLMARK_PUBLIC_ORIGIN" in (
        unit
    )
    assert "TRELLMARK_DATABASE_URL=postgresql" not in unit


def test_application_health_check_uses_the_pod_private_readiness_endpoint():
    unit = (QUADLET_DIR / "trellmark-app.container").read_text()

    assert "Notify=healthy" in unit
    assert "HealthCmd=python -c" in unit
    assert "http://127.0.0.1:8000/internal/ready" in unit
    assert "http://127.0.0.1:8000/api/health" not in unit
    assert "HealthInterval=30s" in unit
    assert "HealthRetries=3" in unit
    assert "HealthStartPeriod=10s" in unit
    assert "HealthTimeout=5s" in unit
    assert "TimeoutStartSec=180" in unit


def test_no_database_password_is_committed_to_the_deployment():
    for path in sorted(QUADLET_DIR.iterdir()):
        text = path.read_text()
        assert "POSTGRES_PASSWORD=" not in text, path.name
        assert "TRELLMARK_DATABASE_URL=postgresql" not in text, path.name

    postgres = (QUADLET_DIR / "trellmark-postgres.container").read_text()
    assert "Secret=trellmark-db-password,type=env,target=POSTGRES_PASSWORD" in postgres


def test_deploy_validates_all_configuration_before_network_or_downtime():
    recipe = _deploy_recipe((BASE_DIR / "justfile").read_text())
    pull = "podman pull --policy=always {{postgres_image}}"
    stop = "systemctl --user stop trellmark-pod.service"

    assert "set -euo pipefail" in recipe
    for secret in ("db_password_secret", "db_url_secret", "public_origin_secret"):
        check = f"podman secret exists {{{{{secret}}}}}"
        assert check in recipe
        assert recipe.index(check) < recipe.index(pull)
        assert recipe.index(check) < recipe.index(stop)


def test_public_origin_recipe_requires_one_https_origin():
    justfile = (BASE_DIR / "justfile").read_text()
    recipe = justfile.split("public-origin:", 1)[1].split("set-password:", 1)[0]

    assert "https://*)" in recipe
    assert "no path, credentials, query, fragment, or wildcard" in recipe
    assert "podman secret create --replace {{public_origin_secret}} -" in recipe


def test_postgres_refresh_and_candidate_build_precede_every_outage_step():
    recipe = _deploy_recipe((BASE_DIR / "justfile").read_text())
    pull = "podman pull --policy=always {{postgres_image}}"
    build = "podman build --pull=always -t {{deploy_image}} -f deploy/Containerfile ."
    audit = '"${source_images[@]}" {{postgres_image}} {{deploy_image}}'
    install = "cp deploy/quadlet/* {{quadlet_dir}}/"
    stop = "systemctl --user stop trellmark-pod.service"
    mask = "systemctl --user mask --runtime trellmark-app.service"

    for step in (pull, build, audit, install, stop, mask):
        assert step in recipe
    assert recipe.index(pull) < recipe.index(build)
    assert recipe.index(build) < recipe.index(audit)
    assert recipe.index(audit) < recipe.index(install)
    assert recipe.index(install) < recipe.index(stop)
    assert recipe.index(stop) < recipe.index(mask)


def test_deploy_keeps_the_application_down_during_migration():
    recipe = _deploy_recipe((BASE_DIR / "justfile").read_text())
    stop = "systemctl --user stop trellmark-pod.service"
    mask = "systemctl --user mask --runtime trellmark-app.service"
    start_db = "systemctl --user start trellmark-postgres.service"
    migrate = "{{deploy_image}} python server.py migrate"

    assert recipe.index(stop) < recipe.index(mask)
    assert recipe.index(mask) < recipe.index(start_db)
    assert recipe.index(start_db) < recipe.index(migrate)
    assert "trap unmask_app_best_effort EXIT" in recipe


def test_deploy_promotes_the_candidate_only_after_migration_and_identity_check():
    recipe = _deploy_recipe((BASE_DIR / "justfile").read_text())
    migrate = "{{deploy_image}} python server.py migrate"
    verify = "{{deploy_image}} python server.py verify-identity"
    tag = "podman tag {{deploy_image}} {{app_image}}"
    restart = "systemctl --user restart trellmark-pod.service"

    assert "-t {{app_image}}" not in recipe
    for step in (migrate, verify, tag, restart):
        assert step in recipe
    assert recipe.index(migrate) < recipe.index(verify)
    assert recipe.index(verify) < recipe.index(tag)
    assert recipe.index(tag) < recipe.index(restart)


def _shimmed_deploy_environment(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    command_log = tmp_path / "commands.log"
    podman = bin_dir / "podman"
    podman.write_text(PULL_FAILURE_PODMAN_SHIM)
    podman.chmod(0o755)
    systemctl = bin_dir / "systemctl"
    systemctl.write_text(SYSTEMCTL_SHIM)
    systemctl.chmod(0o755)
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["HOME"] = str(tmp_path / "home")
    env["XDG_RUNTIME_DIR"] = str(runtime_dir)
    env["COMMAND_LOG"] = str(command_log)
    return env, command_log


def test_failed_postgres_refresh_never_touches_running_services(tmp_path):
    if shutil.which("just") is None:
        pytest.skip("just is not installed")

    env, command_log = _shimmed_deploy_environment(tmp_path)
    subprocess.run(["systemctl", "probe"], env=env, check=True)
    assert "systemctl probe" in command_log.read_text()
    command_log.write_text("")
    env["FAIL_POSTGRES_PULL"] = "1"
    result = subprocess.run(
        ["just", "deploy"],
        cwd=BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    commands = command_log.read_text()
    assert result.returncode != 0
    assert "podman secret exists trellmark-db-password" in commands
    assert "podman secret exists trellmark-database-url" in commands
    assert "podman secret exists trellmark-public-origin" in commands
    assert "podman pull --policy=always" in commands
    assert "docker.io/library/postgres:18-alpine" in commands
    assert "podman build" not in commands
    assert "podman tag" not in commands
    assert "systemctl " not in commands


def test_post_success_audit_failure_does_not_fail_deploy(tmp_path):
    if shutil.which("just") is None:
        pytest.skip("just is not installed")

    env, command_log = _shimmed_deploy_environment(tmp_path)
    env["FAIL_RUNTIME_AUDIT"] = "1"
    result = subprocess.run(
        ["just", "deploy"],
        cwd=BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    commands = command_log.read_text()
    assert result.returncode == 0, result.stderr
    assert (
        "podman tag localhost/trellmark:deploy-candidate localhost/trellmark:latest"
        in (commands)
    )
    assert "systemctl --user restart trellmark-pod.service" in commands
    assert "podman ps --pod --filter pod=trellmark" in commands
    assert "deployment succeeded, but runtime image audit failed" in result.stderr


def test_deploy_unmasks_strictly_before_restarting_the_pod():
    recipe = _deploy_recipe((BASE_DIR / "justfile").read_text())
    trap = "trap unmask_app_best_effort EXIT"
    unmask = "systemctl --user unmask --runtime trellmark-app.service\n"
    state = "systemctl --user show -p LoadState --value trellmark-app.service"
    restart = "systemctl --user restart trellmark-pod.service"

    assert trap in recipe
    success_path = recipe.split(trap, 1)[1]
    assert unmask in success_path
    assert state in success_path
    assert success_path.index(unmask) < success_path.index(state)
    assert success_path.index(state) < success_path.index(restart)


def test_deploy_waits_for_the_application_and_asserts_it_started():
    recipe = _deploy_recipe((BASE_DIR / "justfile").read_text())
    restart = "systemctl --user restart trellmark-pod.service"
    wait = "systemctl --user start trellmark-app.service"
    active = "systemctl --user is-active --quiet trellmark-app.service"

    assert recipe.index(restart) < recipe.index(wait)
    assert recipe.index(wait) < recipe.index(active)


def test_db_secrets_percent_encodes_the_password_into_the_dsn():
    justfile = (BASE_DIR / "justfile").read_text()
    recipe = justfile.split("db-secrets:", 1)[1].split("public-origin:", 1)[0]

    assert "urlencode()" in recipe
    assert 'encoded_password="$(urlencode "$db_password")"' in recipe
    assert "trellmark:${encoded_password}@127.0.0.1" in recipe
    assert "trellmark:${db_password}@" not in recipe


def test_local_container_run_uses_loopback_and_an_ephemeral_database():
    justfile = (BASE_DIR / "justfile").read_text()
    recipe = justfile.split("run-container: build", 1)[1].split("down-container:", 1)[0]

    assert "--publish 127.0.0.1:{{local_port}}:8000" in recipe
    assert "{{postgres_image}}" in recipe
    assert "--tmpfs /var/lib/postgresql:rw" in recipe
    assert "pg_isready" in recipe
    assert "python server.py migrate" in recipe
    assert "secrets.token_urlsafe(24)" in recipe
    assert "python server.py set-password --password-stdin" in recipe
    assert '--env "TRELLMARK_PUBLIC_ORIGIN=http://127.0.0.1:{{local_port}}"' in recipe
    assert "trellmark-postgres-data" not in recipe
    assert "{{db_url_secret}}" not in recipe
    assert "podman pod rm -f {{local_pod}}" in recipe


def test_set_password_uses_hidden_input_and_the_database_secret():
    justfile = (BASE_DIR / "justfile").read_text()
    recipe = justfile.split("set-password:", 1)[1].split("deploy:", 1)[0]

    assert "podman pod exists trellmark" in recipe
    assert "--interactive --tty --pod trellmark" in recipe
    assert "trellmark-database-url,type=env,target=TRELLMARK_DATABASE_URL" in (
        recipe.replace("{{db_url_secret}}", "trellmark-database-url")
    )
    assert "{{app_image}} python server.py set-password" in recipe
    assert "--password-stdin" not in recipe


def test_backup_and_restore_use_postgresql_tools():
    justfile = (BASE_DIR / "justfile").read_text()

    assert "podman exec trellmark-postgres pg_dump" in justfile
    assert "podman exec --interactive trellmark-postgres pg_restore" in justfile
    assert "urls.db" not in justfile


def test_deploy_builds_committed_frontend_without_a_frontend_toolchain():
    justfile = (BASE_DIR / "justfile").read_text()
    recipe = _deploy_recipe(justfile)

    assert "build: check-api-types build-frontend build-image" in justfile
    assert (
        "podman build --pull=always -t {{deploy_image}} -f deploy/Containerfile ."
        in (recipe)
    )
    assert "npm " not in recipe
    assert "uv run" not in recipe


def test_image_proof_requires_application_assets_and_rejects_design_files():
    from scripts.check_frontend_image import check_runtime_files

    files = {
        "app/web/index.html",
        "app/web/static/main.js",
        "app/web/static/styles.css",
        "app/web/static/icons.svg",
        "app/web/static/api/client.js",
        "app/web/static/shared/dom.js",
        "app/web/static/shell/index.js",
        "app/web/static/features/bookmarks/index.js",
        "app/web/static/fonts/local.woff2",
    }
    check_runtime_files(files)
    for missing in files:
        with pytest.raises(RuntimeError, match="Missing production"):
            check_runtime_files(files - {missing})
    for forbidden in [
        "app/web/design.html",
        "app/web/static/design/main.js",
        "app/web/static/design/frame.css",
    ]:
        with pytest.raises(RuntimeError, match="Design asset"):
            check_runtime_files(files | {forbidden})


@pytest.mark.parametrize("status", [200, 404])
def test_image_http_proof_checks_design_content_even_with_spa_fallback(status):
    from scripts.check_frontend_image import check_http_content

    app_html = b'<form id="login-form"></form><script src="/static/main.js"></script>'
    if status == 200:
        check_http_content("/design.html", status, app_html, "text/html")
    else:
        check_http_content("/design.html", status, b"Not found", "text/html")
    for marker in [b"Design overview", b"/static/design/main.js", b"data-design-state"]:
        with pytest.raises(RuntimeError, match="Design content"):
            check_http_content("/design.html", status, app_html + marker, "text/html")
    with pytest.raises(RuntimeError, match="Application page"):
        check_http_content("/design.html", 200, b"wrong page", "text/html")
    with pytest.raises(RuntimeError, match="Dedicated design"):
        check_http_content(
            "/static/design/main.js", 200, b"export {};", "text/javascript"
        )


def test_image_proof_rejects_design_assets_in_earlier_runtime_layers(tmp_path):
    import io
    import json
    import tarfile

    from scripts.check_frontend_image import check_image_layers

    archive = tmp_path / "image.tar"
    with tarfile.open(archive, "w") as image:
        manifest = json.dumps(
            [{"Layers": ["first/layer.tar", "last/layer.tar"]}]
        ).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest)
        image.addfile(info, io.BytesIO(manifest))
        for layer_name, asset in [
            ("first", "app/web/static/design/main.js"),
            ("last", "app/web/static/.wh.design"),
        ]:
            layer = io.BytesIO()
            with tarfile.open(fileobj=layer, mode="w") as contents:
                contents.addfile(tarfile.TarInfo(asset))
            info = tarfile.TarInfo(f"{layer_name}/layer.tar")
            info.size = len(layer.getvalue())
            image.addfile(info, io.BytesIO(layer.getvalue()))
    with pytest.raises(RuntimeError, match="Design asset"):
        check_image_layers(archive)


def test_image_proof_cleanup_is_owned_bounded_and_continues_after_failure(
    tmp_path, monkeypatch
):
    from scripts import check_frontend_image as proof

    container_id = tmp_path / "container.id"
    pod_id = tmp_path / "pod.id"
    uncreated = tmp_path / "uncreated.id"
    container_id.write_text("a" * 64)
    pod_id.write_text("b" * 64)
    calls = []

    def remove(*arguments, **options):
        calls.append((arguments, options))
        return subprocess.CompletedProcess(arguments, int(arguments[-1] == "b" * 64))

    monkeypatch.setattr(proof, "podman", remove)
    with pytest.raises(RuntimeError, match="Could not remove every"):
        proof.cleanup_resources(
            [("container", container_id), ("pod", pod_id), ("pod", uncreated)]
        )
    assert calls == [
        (("pod", "rm", "--force", "b" * 64), {"timeout": 30, "check": False}),
        (("rm", "--force", "a" * 64), {"timeout": 30, "check": False}),
    ]
    calls.clear()
    pod_id.write_text("trellmark")
    with pytest.raises(RuntimeError, match="Could not remove every"):
        proof.cleanup_resources([("pod", pod_id)])
    assert calls == []


def test_image_proof_diagnostics_do_not_echo_subprocess_secrets(monkeypatch):
    from scripts import check_frontend_image as proof

    def failed(arguments, **options):
        assert options["timeout"] == 120
        return subprocess.CompletedProcess(
            arguments, 1, "synthetic secret", "synthetic secret"
        )

    monkeypatch.setattr(proof.subprocess, "run", failed)
    with pytest.raises(RuntimeError, match=r"Podman run failed \(exit 1\)") as failure:
        proof.podman("run", stdin="synthetic secret")
    assert "synthetic secret" not in str(failure.value)


def test_image_http_readiness_retries_reset_but_preserves_later_failures(monkeypatch):
    from scripts import check_frontend_image as proof

    paths = []
    delays = []

    def get(_opener, _origin, path):
        paths.append(path)
        if len(paths) == 1:
            raise ConnectionResetError("Published port precedes application startup")
        if path == "/internal/ready":
            return 200, b"ready", "text/plain"
        raise RuntimeError("Post-readiness failure remains visible")

    monkeypatch.setattr(proof, "http_get", get)
    monkeypatch.setattr(proof.time, "sleep", delays.append)
    with pytest.raises(RuntimeError, match="Post-readiness failure remains visible"):
        proof.check_http("http://127.0.0.1:8000", "synthetic", [])
    assert paths == ["/internal/ready", "/internal/ready", "/"]
    assert delays == [0.25]
