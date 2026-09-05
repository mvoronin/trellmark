"""Prove local production image exclusions using layers, filesystem and HTTP.

Run after building an image: uv run python scripts/check_frontend_image.py
--image localhost/trellmark:latest. Only disposable resources created by this
invocation are removed; no existing database, pod, volume or identity is used.
"""

import argparse
import http.cookiejar
import json
import os
import posixpath
import re
import secrets
import signal
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path
from urllib import error, request

POSTGRES_IMAGE = "docker.io/library/postgres:18-alpine"
DESIGN_MARKERS = (b"Design overview", b"/static/design/", b"data-design-state")


def reject_design_files(names: Iterable[str]) -> None:
    for name in names:
        name = posixpath.normpath(name).removeprefix("/")
        if (
            name == "app/web/design.html"
            or name == "app/web/static/design"
            or name.startswith("app/web/static/design/")
        ):
            raise RuntimeError("Design asset found in production image")


def check_runtime_files(files: set[str]) -> None:
    files = {posixpath.normpath(name).removeprefix("/") for name in files}
    reject_design_files(files)
    for name in [
        "index.html",
        "static/main.js",
        "static/styles.css",
        "static/icons.svg",
    ]:
        if f"app/web/{name}" not in files:
            raise RuntimeError(f"Missing production asset: {name}")
    for directory, suffix in [
        ("api", ".js"),
        ("shared", ".js"),
        ("shell", ".js"),
        ("features/bookmarks", ".js"),
        ("fonts", ".woff2"),
    ]:
        if not any(
            name.startswith(f"app/web/static/{directory}/") and name.endswith(suffix)
            for name in files
        ):
            raise RuntimeError(f"Missing production asset tree: {directory}")


def check_image_layers(archive: Path) -> None:
    # Inspect saved layers independently: a later whiteout must not disguise
    # development content copied into an earlier runtime layer. Never extract.
    with tarfile.open(archive) as image:
        manifest_file = image.extractfile("manifest.json")
        if manifest_file is None:
            raise RuntimeError("Image archive has no manifest")
        manifests = json.load(manifest_file)
        if len(manifests) != 1 or not manifests[0].get("Layers"):
            raise RuntimeError("Expected one image with runtime layers")
        for layer_name in manifests[0]["Layers"]:
            layer_file = image.extractfile(layer_name)
            if layer_file is None:
                raise RuntimeError("Image archive has a missing layer")
            with layer_file, tarfile.open(fileobj=layer_file, mode="r|*") as layer:
                reject_design_files(member.name for member in layer)


def check_http_content(path: str, status: int, body: bytes, content_type: str) -> None:
    if any(marker.lower() in body.lower() for marker in DESIGN_MARKERS):
        raise RuntimeError(f"Design content served from {path}")
    if path.startswith("/static/design/"):
        if status != 404:
            raise RuntimeError("Dedicated design asset is accessible")
    elif path in {"/", "/design.html"}:
        if path == "/design.html" and status == 404:
            return
        if (
            status != 200
            or "text/html" not in content_type
            or b'id="login-form"' not in body
            or b"/static/main.js" not in body
        ):
            raise RuntimeError("Application page is missing or replaced")
    elif status != 200 or not body or "javascript" not in content_type:
        raise RuntimeError(f"Production module is not served: {path}")


def podman(
    *arguments: str,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
    timeout: int = 120,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["podman", *arguments],
            input=stdin,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Podman {arguments[0]} timed out") from None
    if check and result.returncode:
        # Do not reflect subprocess arguments or diagnostics containing the
        # synthetic database DSN, credentials or session response.
        raise RuntimeError(f"Podman {arguments[0]} failed (exit {result.returncode})")
    return result


def cleanup_resources(resources: list[tuple[str, Path]]) -> None:
    failed = False
    for kind, id_file in reversed(resources):
        if not id_file.exists():
            continue
        identifier = id_file.read_text().strip()
        if not re.fullmatch(r"[a-f0-9]{64}", identifier):
            failed = True
            continue
        arguments = ["pod", "rm"] if kind == "pod" else ["rm"]
        try:
            result = podman(*arguments, "--force", identifier, timeout=30, check=False)
            failed = failed or result.returncode != 0
        except OSError, RuntimeError:
            failed = True
    if failed:
        raise RuntimeError("Could not remove every script-created image proof resource")


def http_get(
    opener: request.OpenerDirector, origin: str, path: str
) -> tuple[int, bytes, str]:
    try:
        response = opener.open(origin + path, timeout=3)
    except error.HTTPError as failure:
        response = failure
    with response:
        return (
            response.status,
            response.read(),
            response.headers.get("Content-Type", ""),
        )


def check_http(origin: str, password: str, modules: list[str]) -> None:
    opener = request.build_opener(
        request.ProxyHandler({}),
        request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
    )
    deadline = time.monotonic() + 60
    while True:
        try:
            if http_get(opener, origin, "/internal/ready")[0] == 200:
                break
        except error.URLError, TimeoutError, ConnectionError:
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError("Disposable application did not become ready within 60s")
        time.sleep(0.25)
    for path in [
        "/",
        "/design.html",
        "/static/design/main.js",
        "/static/design/fixtures.js",
        "/static/design/frame.css",
        *modules,
    ]:
        status, body, content_type = http_get(opener, origin, path)
        check_http_content(path, status, body, content_type)
    if http_get(opener, origin, "/api/groups")[0] != 401:
        raise RuntimeError("Production private read lacks authentication")
    login = request.Request(
        origin + "/api/auth/login",
        data=json.dumps({"login": "admin", "password": password}).encode(),
        headers={"Content-Type": "application/json", "Origin": origin},
    )
    with opener.open(login, timeout=10) as response:
        if response.status != 200 or not json.load(response).get("csrf_token"):
            raise RuntimeError("Synthetic production login failed")
    status, body, _ = http_get(opener, origin, "/api/groups")
    if status != 200 or not isinstance(json.loads(body).get("groups"), list):
        raise RuntimeError("Authenticated production read failed")


def check_image(image: str) -> None:
    # Resolve once, so a concurrent retag cannot mix inspected and run images.
    image_id = podman("image", "inspect", "--format", "{{.Id}}", image).stdout.strip()
    if not re.fullmatch(r"sha256:[a-f0-9]{64}|[a-f0-9]{64}", image_id):
        raise RuntimeError("Expected one existing local image")
    with tempfile.TemporaryDirectory(prefix="trellmark-image-proof-") as directory:
        temporary = Path(directory)
        resources: list[tuple[str, Path]] = []
        try:
            archive = temporary / "image.tar"
            podman(
                "save",
                "--format",
                "docker-archive",
                "--output",
                str(archive),
                image_id,
                timeout=180,
            )
            check_image_layers(archive)
            container_file = temporary / "container.id"
            resources.append(("container", container_file))
            podman("create", "--pull=never", "--cidfile", str(container_file), image_id)
            filesystem = temporary / "filesystem.tar"
            podman(
                "export",
                "--output",
                str(filesystem),
                container_file.read_text().strip(),
                timeout=180,
            )
            with tarfile.open(filesystem) as exported:
                files = {
                    posixpath.normpath(member.name).removeprefix("/")
                    for member in exported
                    if member.isfile()
                }
            check_runtime_files(files)
            modules = sorted(
                "/" + name.removeprefix("app/web/")
                for name in files
                if name.startswith("app/web/static/") and name.endswith(".js")
            )
            print(
                "Production runtime layers and filesystem exclude design assets.",
                flush=True,
            )

            pod_file = temporary / "pod.id"
            resources.append(("pod", pod_file))
            podman(
                "pod",
                "create",
                "--pod-id-file",
                str(pod_file),
                "--publish",
                "127.0.0.1::8000",
            )
            pod_id = pod_file.read_text().strip()
            environment = dict(os.environ)
            environment["POSTGRES_PASSWORD"] = secrets.token_urlsafe(24)
            environment["TRELLMARK_DATABASE_URL"] = (
                f"postgresql+psycopg://trellmark:{environment['POSTGRES_PASSWORD']}@127.0.0.1:5432/trellmark"
            )
            database = podman(
                "run",
                "--detach",
                "--pod",
                pod_id,
                "--env",
                "POSTGRES_USER=trellmark",
                "--env",
                "POSTGRES_DB=trellmark",
                "--env",
                "POSTGRES_PASSWORD",
                "--tmpfs",
                "/var/lib/postgresql:rw",
                POSTGRES_IMAGE,
                env=environment,
            ).stdout.strip()
            deadline = time.monotonic() + 60
            # The image initializes through a temporary Unix-socket-only server.
            # Wait for TCP so readiness cannot pass just before that server exits.
            while podman(
                "exec",
                database,
                "pg_isready",
                "--host=127.0.0.1",
                "--username=trellmark",
                "--dbname=trellmark",
                "--quiet",
                timeout=5,
                check=False,
            ).returncode:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "Disposable PostgreSQL did not become ready within 60s"
                    )
                time.sleep(0.25)
            version = podman(
                "exec",
                database,
                "psql",
                "--username=trellmark",
                "--dbname=trellmark",
                "--tuples-only",
                "--no-align",
                "--command",
                "SHOW server_version_num",
            ).stdout.strip()
            if not version.isdigit() or not 180000 <= int(version) < 190000:
                raise RuntimeError("Image proof requires PostgreSQL 18")
            infra = podman(
                "pod", "inspect", "--format", "{{.InfraContainerID}}", pod_id
            ).stdout.strip()
            binding = podman("port", infra, "8000/tcp").stdout.strip()
            if not re.fullmatch(r"127\.0\.0\.1:[0-9]+", binding):
                raise RuntimeError("Image proof port is not exclusively loopback")
            origin = "http://" + binding
            environment["TRELLMARK_PUBLIC_ORIGIN"] = origin
            common = [
                "--pod",
                pod_id,
                "--pull=never",
                "--env",
                "TRELLMARK_DATABASE_URL",
                "--env",
                "TRELLMARK_PUBLIC_ORIGIN",
            ]
            podman(
                "run",
                "--rm",
                *common,
                image_id,
                "python",
                "server.py",
                "migrate",
                env=environment,
            )
            password = secrets.token_urlsafe(32)
            podman(
                "run",
                "--rm",
                "--interactive",
                *common,
                image_id,
                "python",
                "server.py",
                "set-password",
                "--password-stdin",
                stdin=password + "\n",
                env=environment,
            )
            podman("run", "--detach", *common, image_id, env=environment)
            check_http(origin, password, modules)
            print(
                "Production login, modules, authenticated read and design HTTP exclusion passed.",
                flush=True,
            )
        finally:
            cleanup_resources(resources)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify a local production image using runtime layers, exported files and disposable PostgreSQL 18 HTTP checks."
    )
    parser.add_argument(
        "--image",
        required=True,
        help="existing local image tag or ID; application image is never pulled",
    )
    args = parser.parse_args()

    def terminate(_signal: int, _frame: object) -> None:
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        check_image(args.image)
    except KeyboardInterrupt:
        raise SystemExit(
            "Image proof interrupted; owned-resource cleanup attempted."
        ) from None
    except (OSError, RuntimeError, tarfile.TarError, ValueError, KeyError) as failure:
        # Runtime errors are deliberately sanitized at the operation boundary.
        message = (
            str(failure)
            if isinstance(failure, RuntimeError)
            else type(failure).__name__
        )
        raise SystemExit(f"Image proof failed: {message}") from None
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    main()
