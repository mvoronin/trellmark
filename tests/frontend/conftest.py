import functools
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

import pytest
from playwright.sync_api import expect, sync_playwright

from tests.postgres import TEST_LOGIN, TEST_PASSWORD

WEB_ROOT = Path(__file__).resolve().parents[2] / "web"


class StaticWebHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/__test_blank__":
            body = b"<!doctype html><html><body></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if (
            not Path(self.translate_path(self.path))
            .resolve()
            .is_relative_to(WEB_ROOT.resolve())
        ):
            self.send_error(404)
            return
        super().do_GET()

    def do_HEAD(self):
        if (
            not Path(self.translate_path(self.path))
            .resolve()
            .is_relative_to(WEB_ROOT.resolve())
        ):
            self.send_error(404)
            return
        super().do_HEAD()

    def list_directory(self, path):
        self.send_error(404)
        return None

    def log_message(self, format, *args):
        pass


@pytest.fixture(scope="session")
def static_web_server():
    """Serve only browser files on ephemeral loopback, without app or database."""
    handler = functools.partial(StaticWebHandler, directory=str(WEB_ROOT))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(f"{base_url}/__test_blank__", timeout=5) as response:
            assert response.status == 200
        yield base_url
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.fixture
def static_page(unauthenticated_page, static_web_server):
    unauthenticated_page.goto(f"{static_web_server}/__test_blank__")
    return unauthenticated_page


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        yield browser
        browser.close()


@pytest.fixture
def unauthenticated_page(browser):
    page = browser.new_page()
    yield page
    page.close()


@pytest.fixture
def page(unauthenticated_page, app):
    base_url, _ = app
    unauthenticated_page.goto(base_url)
    unauthenticated_page.get_by_label("Login").fill(TEST_LOGIN)
    unauthenticated_page.get_by_label("Password").fill(TEST_PASSWORD)
    unauthenticated_page.get_by_role("button", name="Log in").click()
    expect(unauthenticated_page.locator("#app-view")).to_be_visible()
    return unauthenticated_page
