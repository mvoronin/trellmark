import asyncio
import threading

import anyio
import pytest
from starlette.datastructures import Headers

import trellmark
from trellmark import config


class _Response:
    def __init__(self, messages):
        start = next(
            message for message in messages if message["type"] == "http.response.start"
        )
        self.status_code = start["status"]
        self.headers = Headers(raw=start["headers"])
        self.content = b"".join(
            message.get("body", b"")
            for message in messages
            if message["type"] == "http.response.body"
        )


class _SyncASGIClient:
    """Run each exchange in its own thread, independent of pytest event loops."""

    def __init__(self, application):
        self.application = application

    def get(self, path, headers=None):
        return self._request(path, headers=headers)

    def head(self, path, headers=None):
        return self._request(path, headers=headers, method="HEAD")

    def _request(self, path, headers=None, method="GET"):
        result = []
        errors = []

        async def exchange():
            messages = []
            request_pending = True

            async def receive():
                nonlocal request_pending
                if request_pending:
                    request_pending = False
                    return {"type": "http.request", "body": b"", "more_body": False}
                return {"type": "http.disconnect"}

            async def send(message):
                messages.append(message)

            raw_headers = [
                (name.lower().encode(), value.encode())
                for name, value in (headers or {}).items()
            ]
            await self.application(
                {
                    "type": "http",
                    "asgi": {"version": "3.0", "spec_version": "2.4"},
                    "http_version": "1.1",
                    "method": method,
                    "scheme": "http",
                    "path": path,
                    "raw_path": path.encode(),
                    "query_string": b"",
                    "root_path": "",
                    "headers": raw_headers,
                    "client": ("127.0.0.1", 12345),
                    "server": ("testserver", 80),
                },
                receive,
                send,
            )
            return _Response(messages)

        def run_exchange():
            try:
                result.append(asyncio.run(asyncio.wait_for(exchange(), timeout=2)))
            except BaseException as error:  # re-raised on the test thread below
                errors.append(error)

        thread = threading.Thread(target=run_exchange)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive(), "ASGI exchange did not finish"
        if errors:
            raise errors[0]
        return result[0]


@pytest.fixture
def static_app(tmp_path, monkeypatch):
    site = tmp_path / "web"
    static = site / "static"
    fonts = static / "fonts"
    fonts.mkdir(parents=True)
    (site / "index.html").write_text("current app shell")
    assets = {
        "styles.css": "body { color: black; }",
        "app.js": "export const app = true;\n",
        "api.js": "export const api = true;\n",
        "icons.svg": "<svg></svg>",
    }
    for name, content in assets.items():
        (static / name).write_text(content)
    (fonts / "app.woff2").write_bytes(b"representative-font")

    monkeypatch.setattr(config, "INDEX_FILE", site / "index.html")
    monkeypatch.setattr(config, "STATIC_DIR", static)

    async def run_sync_inline(function, *args, **_kwargs):
        return function(*args)

    # These focused tests drive ASGI without a server. Keep filesystem calls
    # inline so Starlette does not create a nested worker pool for each
    # short-lived exchange thread.
    monkeypatch.setattr(anyio.to_thread, "run_sync", run_sync_inline)

    monkeypatch.setattr(config, "PUBLIC_ORIGIN", "http://127.0.0.1:8000")
    application = trellmark.create_app()
    yield _SyncASGIClient(application), site


def test_fastapi_serves_the_app_shell_and_static_tree(static_app):
    client, _ = static_app

    for path in ("/", "/index.html", "/some/spa/path"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.content == b"current app shell", path
        assert response.headers["Cache-Control"] == "no-cache", path
        assert response.headers.get("ETag"), path
        assert response.headers.get("Last-Modified"), path

    paths = [
        "/static/styles.css",
        "/static/app.js",
        "/static/api.js",
        "/static/icons.svg",
        "/static/fonts/app.woff2",
    ]
    for path in paths:
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers["Cache-Control"] == "no-cache", path
        assert response.headers.get("ETag"), path
        assert response.headers.get("Last-Modified"), path


def test_reserved_private_namespaces_never_fall_back_to_the_spa(static_app):
    client, _ = static_app

    response = client.get("/internal/not-a-route")
    assert response.status_code == 404
    assert response.headers["Cache-Control"] == "no-store"
    assert b"current app shell" not in response.content

    response = client.get("/static/not-a-file")
    assert response.status_code == 404
    assert response.headers["Cache-Control"] == "no-cache"
    assert b"current app shell" not in response.content

    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert b"current app shell" not in response.content


def test_static_files_revalidate_and_changed_content_invalidates_the_etag(static_app):
    client, site = static_app
    path = "/static/app.js"

    response = client.get(path)
    etag = response.headers["ETag"]
    assert response.content == b"export const app = true;\n"

    response = client.get(path, headers={"If-None-Match": etag})
    assert response.status_code == 304
    assert response.headers["Cache-Control"] == "no-cache"

    (site / "static" / "app.js").write_text("export const app = 'deployed update';\n")
    response = client.get(path, headers={"If-None-Match": etag})
    assert response.status_code == 200
    assert response.content == b"export const app = 'deployed update';\n"
    assert response.headers["ETag"] != etag
    assert response.headers["Cache-Control"] == "no-cache"


def test_dynamic_and_internal_responses_are_never_stored(static_app):
    client, _ = static_app

    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"

    response = client.get("/internal/not-a-route")
    assert response.status_code == 404
    assert response.headers["Cache-Control"] == "no-store"


def test_head_requests_preserve_static_cache_metadata_without_a_body(static_app):
    client, _ = static_app

    for path in ("/", "/static/app.js"):
        response = client.head(path)
        assert response.status_code == 200
        assert response.content == b""
        assert response.headers["Cache-Control"] == "no-cache"
        assert response.headers.get("ETag")
        assert response.headers.get("Last-Modified")


def test_vendored_fonts_include_their_complete_ofl_notices():
    font_dir = config.STATIC_DIR / "fonts"
    licenses = {
        "GolosText-OFL.txt": "Copyright 2019 The Golos Text Project Authors",
        "PTSansNarrow-OFL.txt": "Copyright (c) 2010, ParaType Ltd.",
    }

    for filename, copyright_notice in licenses.items():
        license_text = (font_dir / filename).read_text(encoding="utf-8")
        assert license_text.startswith(copyright_notice)
        assert "SIL OPEN FONT LICENSE Version 1.1" in license_text
        assert "5) The Font Software" in license_text
        assert "TERMINATION" in license_text
        assert "DISCLAIMER" in license_text
