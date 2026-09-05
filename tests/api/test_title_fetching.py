import ast
import asyncio
import ipaddress
import threading
from dataclasses import replace
from importlib.util import resolve_name
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock
from urllib import parse

import pytest
from aiohttp import ClientError, web

from tests.bookmarks import helpers as bookmark_helpers
from tests.helpers import run_async
from trellmark.bookmarks import application as bookmark_application
from trellmark.bookmarks import domain
from trellmark.bookmarks import integrations as page_titles
from trellmark.bookmarks.application import BookmarksApplicationService
from trellmark.bookmarks.persistence import PostgresURLQueries
from trellmark.platform import network
from trellmark.platform.network import is_public_address
from trellmark.platform.runtime import get_engine


def test_complete_legacy_module_inventory_has_no_sources_imports_or_facade():
    import trellmark

    root = Path(__file__).resolve().parents[2]
    legacy_names = {
        "handlers",
        "models",
        "storage",
        "storage_types",
        "responses",
        "url_normalization",
        "page_titles",
        "title_text",
        "site_icons",
        "app_keys",
    }
    forbidden = {f"trellmark.{name}" for name in legacy_names}
    for name in legacy_names:
        assert not (root / "trellmark" / f"{name}.py").exists(), name
        assert not (root / "trellmark" / name).exists(), name
        assert not hasattr(trellmark, name), name

    assert trellmark.__all__ == ["create_app", "main"]
    assert {
        name
        for name, value in vars(trellmark).items()
        if not name.startswith("_") and not isinstance(value, ModuleType)
    } == {"create_app", "main"}

    for directory in ("trellmark", "tests", "scripts", "migrations"):
        for path in (root / directory).rglob("*.py"):
            package = ".".join(path.relative_to(root).parent.parts)
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                imported = set()
                if isinstance(node, ast.Import):
                    imported.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if node.level:
                        module = resolve_name("." * node.level + module, package)
                    imported.add(module)
                    imported.update(f"{module}.{alias.name}" for alias in node.names)
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    # Include literal dynamic imports and monkeypatch targets.
                    imported.add(node.value)
                assert not any(
                    name == old or name.startswith(f"{old}.")
                    for name in imported
                    for old in forbidden
                ), (path.relative_to(root), node.lineno, imported)


def test_create_title_service_commits_insert_before_fetch_and_skips_duplicates(
    database, bookmarks_service
):
    assert hasattr(BookmarksApplicationService, "create_url")
    calls = []

    async def fetch(url):
        calls.append(url)
        # Independent PostgreSQL checkout proves the insert is already durable.
        record = PostgresURLQueries(get_engine).url_by_url(url)
        assert record is not None
        assert record.title is None
        assert record.version == 1
        assert get_engine().pool.checkedout() == 0
        outcome = await service.set_important(domain.SetImportant(record.id, True))
        assert isinstance(outcome, domain.SetImportantSucceeded)
        return "Fetched title"

    service = replace(bookmarks_service, title_fetcher=fetch)

    async def exercise():
        outcome = await service.create_url(domain.CreateURL("https://example.com"))
        assert isinstance(outcome, domain.URLCreated)
        assert outcome.record.title == "Fetched title"
        assert outcome.record.version == 2
        assert outcome.record.important is True
        assert await service.url_group_ids(outcome.record.id) == (1,)
        assert await service.url_by_id(outcome.record.id) == outcome.record
        assert await service.create_url(domain.CreateURL(outcome.record.url)) == (
            domain.URLConflict(outcome.record.url)
        )

    run_async(exercise)
    assert calls == ["https://example.com"]


def test_refresh_title_service_revalidates_prefetch_version_without_holding_uow(
    database, bookmarks_service
):
    assert hasattr(BookmarksApplicationService, "refresh_url_title")

    record = bookmark_helpers.seed_url("https://example.com", "Existing title")

    async def fetch(url):
        assert url == record["url"]
        assert get_engine().pool.checkedout() == 0
        result = await service.edit_url(
            domain.EditURL(record["id"], record["version"], title="Manual title")
        )
        assert isinstance(result, domain.URLUpdated)
        return "Fetched title"

    service = replace(bookmarks_service, title_fetcher=fetch)
    result = run_async(lambda: service.refresh_url_title(record["id"]))
    assert result == domain.URLVersionConflict(record["id"], record["version"])
    current = PostgresURLQueries(get_engine).url_by_id(record["id"])
    assert current.title == "Manual title"
    assert current.version == record["version"] + 1


@pytest.mark.parametrize("concurrent_change", ["edit", "remove"])
def test_create_title_service_preserves_existing_race_behavior(
    bookmarks_service, concurrent_change
):
    async def fetch(url):
        record = await service.url_by_url(url)
        assert record is not None
        if concurrent_change == "edit":
            outcome = await service.edit_url(
                domain.EditURL(
                    record.id, record.version, url="https://new.example", title="Manual"
                )
            )
            assert isinstance(outcome, domain.URLUpdated)
        else:
            outcome = await service.remove_url(domain.RemoveURL(record.id, 1))
            assert isinstance(outcome, domain.URLRemoved)
        return "Fetched"

    service = replace(bookmarks_service, title_fetcher=fetch)

    async def exercise():
        outcome = await service.create_url(domain.CreateURL("https://old.example"))
        assert isinstance(outcome, domain.URLCreated)
        stored = await service.url_by_id(outcome.record.id)
        if concurrent_change == "edit":
            assert stored == outcome.record
            assert outcome.record.url == "https://new.example"
            assert outcome.record.title == "Fetched"
            assert outcome.record.version == 3
        else:
            assert stored is None
            assert outcome.record.url == "https://old.example"
            assert outcome.record.title is None
            assert outcome.record.version == 1

    run_async(exercise)


def test_parse_title_normalizes_html_title():
    assert (
        page_titles.parse_title(b"<html><title>  Example\nTitle &amp; More  </title>")
        == "Example Title & More"
    )
    assert page_titles.parse_title(b"<html><body>No title</body></html>") is None


def test_fetch_url_title_uses_youtube_oembed(monkeypatch):
    requests = []
    video_url = "https://www.youtube.com/shorts/aqz-KE-bpKQ"

    async def resolves_to_public_host(url):
        return True

    monkeypatch.setattr(page_titles, "resolves_to_public_host", resolves_to_public_host)

    async def run():
        async def oembed(request):
            requests.append(dict(request.query))
            return web.json_response({"title": "  Big Buck\nBunny  "})

        application = web.Application()
        application.router.add_get("/oembed", oembed)
        runner = web.AppRunner(application)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()

        try:
            port = runner.addresses[0][1]
            monkeypatch.setattr(
                page_titles,
                "YOUTUBE_OEMBED_URL",
                f"http://127.0.0.1:{port}/oembed",
            )
            return await page_titles.fetch_url_title(video_url)
        finally:
            await runner.cleanup()

    assert run_async(run) == "Big Buck Bunny"
    assert requests == [{"format": "json", "url": video_url}]


def test_fetch_url_title_falls_back_when_youtube_oembed_has_no_title(monkeypatch):
    requests = []

    async def resolves_to_public_host(url):
        return True

    monkeypatch.setattr(page_titles, "resolves_to_public_host", resolves_to_public_host)
    monkeypatch.setattr(page_titles, "_is_youtube_url", lambda url: True)

    async def run():
        async def oembed(request):
            requests.append(request.path)
            return web.json_response({"provider_name": "YouTube"})

        async def page(request):
            requests.append(request.path)
            return web.Response(
                text="<title>HTML title</title>", content_type="text/html"
            )

        application = web.Application()
        application.router.add_get("/oembed", oembed)
        application.router.add_get("/video", page)
        runner = web.AppRunner(application)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()

        try:
            port = runner.addresses[0][1]
            monkeypatch.setattr(
                page_titles,
                "YOUTUBE_OEMBED_URL",
                f"http://127.0.0.1:{port}/oembed",
            )
            return await page_titles.fetch_url_title(f"http://127.0.0.1:{port}/video")
        finally:
            await runner.cleanup()

    assert run_async(run) == "HTML title"
    assert requests == ["/oembed", "/video"]


def test_fetch_url_title_skips_non_global_destination():
    requests = []

    async def run():
        async def handler(request):
            requests.append(request.path)
            return web.Response(
                text="<title>Internal</title>", content_type="text/html"
            )

        application = web.Application()
        application.router.add_get("/", handler)
        runner = web.AppRunner(application)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()

        try:
            port = runner.addresses[0][1]
            return await page_titles.fetch_url_title(f"http://127.0.0.1:{port}/")
        finally:
            await runner.cleanup()

    assert run_async(run) is None
    assert requests == []


def test_fetch_url_title_checks_redirect_target_before_fetching(monkeypatch):
    requests = []

    async def resolves_to_public_host(url):
        return parse.urlparse(url).path == "/redirect"

    monkeypatch.setattr(page_titles, "resolves_to_public_host", resolves_to_public_host)

    async def run():
        async def redirect(request):
            requests.append(request.path)
            raise web.HTTPFound("/target")

        async def target(request):
            requests.append(request.path)
            return web.Response(
                text="<title>Internal</title>", content_type="text/html"
            )

        application = web.Application()
        application.router.add_get("/redirect", redirect)
        application.router.add_get("/target", target)
        runner = web.AppRunner(application)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()

        try:
            port = runner.addresses[0][1]
            return await page_titles.fetch_url_title(
                f"http://127.0.0.1:{port}/redirect"
            )
        finally:
            await runner.cleanup()

    assert run_async(run) is None
    assert requests == ["/redirect"]


def test_fetch_url_title_reads_past_first_chunk(monkeypatch):
    async def resolves_to_public_host(url):
        return True

    monkeypatch.setattr(page_titles, "resolves_to_public_host", resolves_to_public_host)

    async def run():
        async def handler(request):
            response = web.StreamResponse(
                headers={"Content-Type": "text/html; charset=utf-8"}
            )
            await response.prepare(request)
            await response.write(b"x" * (page_titles.TITLE_READ_CHUNK_BYTES + 1))
            await asyncio.sleep(0.01)
            await response.write(b"<title>Late title</title>")
            await response.write_eof()
            return response

        application = web.Application()
        application.router.add_get("/", handler)
        runner = web.AppRunner(application)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()

        try:
            port = runner.addresses[0][1]
            return await page_titles.fetch_url_title(f"http://127.0.0.1:{port}/")
        finally:
            await runner.cleanup()

    assert run_async(run) == "Late title"


def test_fetch_url_title_stops_at_size_limit(monkeypatch):
    async def resolves_to_public_host(url):
        return True

    monkeypatch.setattr(page_titles, "resolves_to_public_host", resolves_to_public_host)

    async def run():
        async def handler(request):
            prefix = b"x" * page_titles.MAX_TITLE_BYTES
            return web.Response(
                body=prefix + b"<title>Too late</title>",
                content_type="text/html",
            )

        application = web.Application()
        application.router.add_get("/", handler)
        runner = web.AppRunner(application)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()

        try:
            port = runner.addresses[0][1]
            return await page_titles.fetch_url_title(f"http://127.0.0.1:{port}/")
        finally:
            await runner.cleanup()

    assert run_async(run) is None


def test_fetch_url_title_handles_malformed_remote_response(monkeypatch):
    requests = []

    async def resolves_to_public_host(url):
        return True

    monkeypatch.setattr(page_titles, "resolves_to_public_host", resolves_to_public_host)

    async def run():
        async def send_malformed_response(reader, writer):
            request = await reader.read(4096)
            requests.append(request)
            writer.write(b"HTTP/1.1 200 OK\r\nMalformed header\r\n\r\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(send_malformed_response, "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            return await page_titles.fetch_url_title(f"http://127.0.0.1:{port}/")
        finally:
            server.close()
            await server.wait_closed()

    assert run_async(run) is None
    assert len(requests) == 1
    assert requests[0].startswith(b"GET / HTTP/1.1\r\n")


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("8.8.8.8", True),
        ("127.0.0.1", False),
        ("169.254.169.254", False),
        ("192.168.1.1", False),
        ("224.0.0.1", False),
        ("::ffff:127.0.0.1", False),
    ],
)
def test_title_fetch_public_address_guard(address, expected):
    assert is_public_address(ipaddress.ip_address(address)) is expected


@pytest.mark.parametrize(
    ("body", "charset", "expected"),
    [
        (b"<title>Malformed</title>", "utf-16", None),
        (b"<title>Malformed</title>", "utf-32", None),
        (("<title>&#" + "9" * 5000 + ";</title>").encode(), "utf-8", None),
        (b"<title>Fallback</title>", "base64_codec", "Fallback"),
        (b"<title>Fallback</title>", "rot_13", "Fallback"),
    ],
    ids=["utf16", "utf32", "numeric-entity", "binary-codec", "transform-codec"],
)
def test_title_adapter_classifies_untrusted_decoding_and_parsing(
    monkeypatch, caplog, body, charset, expected
):
    async def public(_url):
        return True

    monkeypatch.setattr(page_titles, "resolves_to_public_host", public)

    async def exercise():
        async def page(_request):
            return web.Response(
                body=body, headers={"Content-Type": f"text/html; charset={charset}"}
            )

        application = web.Application()
        application.router.add_get("/", page)
        runner = web.AppRunner(application)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        try:
            port = runner.addresses[0][1]
            return await page_titles.fetch_url_title(f"http://127.0.0.1:{port}/")
        finally:
            await runner.cleanup()

    assert run_async(exercise) == expected
    assert caplog.records == []


@pytest.mark.parametrize("operation", ["create", "refresh"])
@pytest.mark.parametrize("failure", [None, RuntimeError("synthetic-private-marker")])
def test_title_fetch_only_anomalies_warn_once_without_explicit_private_arguments(
    bookmarks_service, monkeypatch, caplog, operation, failure
):
    record = bookmark_helpers.seed_url("https://stored.example", "Existing")
    warning = Mock(wraps=bookmark_application.logger.warning)
    monkeypatch.setattr(bookmark_application.logger, "warning", warning)
    calls = []

    async def fetch(url):
        calls.append(url)
        assert get_engine().pool.checkedout() == 0
        if failure is not None:
            raise failure
        return None

    service = replace(bookmarks_service, title_fetcher=fetch)
    if operation == "create":
        outcome = run_async(
            lambda: service.create_url(domain.CreateURL("https://new.example"))
        )
        assert isinstance(outcome, domain.URLCreated)
        assert outcome.record.title is None
        assert outcome.record.version == 1
    else:
        outcome = run_async(lambda: service.refresh_url_title(record["id"]))
        if failure is None:
            assert isinstance(outcome, domain.URLTitleRefreshed)
            assert outcome.title_updated is False
        else:
            assert outcome == domain.URLTitleFetchFailed(record["id"])
        assert bookmark_helpers.url_payload(record["id"]) == record
    assert len(calls) == 1
    if failure is None:
        warning.assert_not_called()
        assert caplog.records == []
    else:
        warning.assert_called_once_with(
            "Unexpected page title fetch failure.", exc_info=True
        )
        assert len(caplog.records) == 1
        logged = caplog.records[0]
        assert logged.levelname == "WARNING"
        assert logged.args == ()
        assert logged.exc_info[1] is failure
        assert logged.exc_info[2] is not None
        assert "synthetic-private-marker" not in logged.msg
        assert calls[0] not in logged.msg


@pytest.mark.parametrize("operation", ["create", "refresh"])
@pytest.mark.parametrize("failure_type", [asyncio.CancelledError, BaseException])
def test_title_fetch_cancellation_passes_through_without_warning_or_update(
    bookmarks_service, caplog, operation, failure_type
):
    record = bookmark_helpers.seed_url("https://stored.example", "Existing")
    failure = failure_type("synthetic cancellation")

    async def fetch(_url):
        assert get_engine().pool.checkedout() == 0
        raise failure

    service = replace(bookmarks_service, title_fetcher=fetch)
    with pytest.raises(failure_type) as caught:
        if operation == "create":
            run_async(
                lambda: service.create_url(domain.CreateURL("https://new.example"))
            )
        else:
            run_async(lambda: service.refresh_url_title(record["id"]))
    assert caught.value is failure
    assert bookmark_helpers.url_payload(record["id"]) == record
    if operation == "create":
        saved = PostgresURLQueries(get_engine).url_by_url("https://new.example")
        assert saved is not None and saved.title is None and saved.version == 1
    assert caplog.records == []


class TitleWorkflowProbe:
    """Record real UoW/query lifetimes; injected errors do not define DB semantics."""

    def __init__(self, service, failure_at=None):
        self.service = service
        self.failure_at = failure_at
        self.failure = RuntimeError("synthetic persistence failure")
        self.events = []
        self.threads = {}
        self.units = 0

    def hit(self, event):
        self.events.append(event)
        self.threads[event] = threading.get_ident()
        if event == self.failure_at:
            raise self.failure

    def url_by_id(self, url_id):
        record = self.service.url_queries.url_by_id(url_id)
        self.hit("query")
        return record

    def factory(self):
        self.units += 1
        label = f"uow{self.units}"
        self.hit(f"{label}:factory")
        inner = self.service.logical_uow_factory()
        probe = self

        class Repository:
            def create_url(self, command):
                result = inner.bookmarks.create_url(command)
                probe.hit(f"{label}:insert")
                return result

            def url_by_id(self, url_id):
                result = inner.bookmarks.url_by_id(url_id)
                probe.hit(f"{label}:revalidate")
                return result

            def edit_url(self, command):
                result = inner.bookmarks.edit_url(command)
                probe.hit(f"{label}:conditional-write")
                return result

        class UnitOfWork:
            def __enter__(self):
                probe.hit(f"{label}:enter")
                inner.__enter__()
                probe.hit(f"{label}:gate")
                self.bookmarks = Repository()
                return self

            def commit(self):
                probe.hit(f"{label}:commit")
                inner.commit()

            def __exit__(self, *args):
                inner.__exit__(*args)
                probe.hit(f"{label}:close")

        return UnitOfWork()

    async def fetch(self, _url):
        assert get_engine().pool.checkedout() == 0
        self.hit("fetch")
        return "Fetched"

    def build(self):
        return replace(
            self.service,
            logical_uow_factory=self.factory,
            url_queries=self,
            title_fetcher=self.fetch,
        )


@pytest.mark.parametrize("operation", ["create", "refresh"])
def test_title_exact_worker_query_fetch_and_uow_sequence(bookmarks_service, operation):
    record = bookmark_helpers.seed_url("https://stored.example", "Existing")
    probe = TitleWorkflowProbe(bookmarks_service)
    service = probe.build()
    if operation == "create":
        outcome = run_async(
            lambda: service.create_url(domain.CreateURL("https://new.example"))
        )
        assert isinstance(outcome, domain.URLCreated)
        assert probe.events == [
            "uow1:factory",
            "uow1:enter",
            "uow1:gate",
            "uow1:insert",
            "uow1:commit",
            "uow1:close",
            "fetch",
            "uow2:factory",
            "uow2:enter",
            "uow2:gate",
            "uow2:revalidate",
            "uow2:conditional-write",
            "uow2:commit",
            "uow2:close",
        ]
    else:
        outcome = run_async(lambda: service.refresh_url_title(record["id"]))
        assert isinstance(outcome, domain.URLTitleRefreshed)
        assert outcome.title_updated is True
        assert probe.events == [
            "query",
            "fetch",
            "uow1:factory",
            "uow1:enter",
            "uow1:gate",
            "uow1:conditional-write",
            "uow1:commit",
            "uow1:close",
        ]
    assert outcome.record.title == "Fetched"
    assert outcome.record.version == 2
    for unit in range(1, probe.units + 1):
        threads = {
            thread
            for event, thread in probe.threads.items()
            if event.startswith(f"uow{unit}:")
        }
        assert len(threads) == 1
        assert probe.threads["fetch"] not in threads


@pytest.mark.parametrize(
    ("operation", "failure_at"),
    [
        ("create", f"uow1:{stage}")
        for stage in ("factory", "enter", "insert", "commit", "close")
    ]
    + [
        ("create", f"uow2:{stage}")
        for stage in (
            "factory",
            "enter",
            "revalidate",
            "conditional-write",
            "commit",
            "close",
        )
    ]
    + [("refresh", "query")]
    + [
        ("refresh", f"uow1:{stage}")
        for stage in ("factory", "enter", "conditional-write", "commit", "close")
    ],
)
def test_title_persistence_errors_cross_service_unchanged(
    bookmarks_service, caplog, operation, failure_at
):
    record = bookmark_helpers.seed_url("https://stored.example", "Existing")
    probe = TitleWorkflowProbe(bookmarks_service, failure_at)
    service = probe.build()
    with pytest.raises(RuntimeError) as caught:
        if operation == "create":
            run_async(
                lambda: service.create_url(domain.CreateURL("https://new.example"))
            )
        else:
            run_async(lambda: service.refresh_url_title(record["id"]))
    assert caught.value is probe.failure
    assert probe.events.count(failure_at) == 1
    assert probe.events.count("fetch") <= 1
    assert get_engine().pool.checkedout() == 0
    assert caplog.records == []
    if failure_at.endswith((":conditional-write", ":commit")):
        if operation == "refresh":
            assert bookmark_helpers.url_payload(record["id"]) == record
        elif failure_at.startswith("uow2"):
            saved = PostgresURLQueries(get_engine).url_by_url("https://new.example")
            assert saved is not None and saved.title is None and saved.version == 1
    if operation == "create" and failure_at in {"uow1:insert", "uow1:commit"}:
        assert PostgresURLQueries(get_engine).url_by_url("https://new.example") is None


@pytest.mark.parametrize(
    ("status", "content_type", "body", "expected"),
    [
        (301, "text/html", b"<title>Ignored</title>", None),
        (304, "text/html", b"", None),
        (404, "text/html", b"<title>Ignored</title>", None),
        (500, "text/html", b"<title>Ignored</title>", None),
        (200, "application/json", b"<title>Ignored</title>", None),
        (200, "application/octet-stream", b"<title>Ignored</title>", None),
        (200, "", b"<title>Accepted</title>", "Accepted"),
        (200, "application/xhtml+xml", b"<title>Accepted</title>", "Accepted"),
        (
            200,
            "text/html; charset=unknown-charset",
            b"<title>Fallback</title>",
            "Fallback",
        ),
        (200, "text/html", b"<title>Bad \xff byte</title>", "Bad \ufffd byte"),
        (
            200,
            "text/html",
            b"<TITLE> First &amp; Best </TITLE><title>Second</title>",
            "First & Best",
        ),
        (200, "text/html", b"<title>  \n </title>", None),
    ],
)
def test_title_adapter_http_content_and_normalization_are_silent(
    monkeypatch, caplog, status, content_type, body, expected
):
    async def public(_url):
        return True

    monkeypatch.setattr(page_titles, "resolves_to_public_host", public)

    async def exercise():
        async def page(_request):
            return web.Response(
                status=status, body=body, headers={"Content-Type": content_type}
            )

        app = web.Application()
        app.router.add_get("/", page)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        try:
            return await page_titles.fetch_url_title(
                f"http://127.0.0.1:{runner.addresses[0][1]}/"
            )
        finally:
            await runner.cleanup()

    assert run_async(exercise) == expected
    assert caplog.records == []


@pytest.mark.parametrize(
    "body",
    [
        b"not-json",
        b"\xff",
        b"[]",
        b'{"title": 42}',
        b'{"title": "  "}',
        b"x" * 65_537,
        b'{"title":' + b"9" * 5000 + b"}",
        b"[" * 2000 + b"]" * 2000,
    ],
    ids=[
        "malformed-json",
        "invalid-unicode",
        "wrong-shape",
        "wrong-title-type",
        "empty-title",
        "oversized",
        "numeric-limit",
        "nesting-limit",
    ],
)
def test_title_oembed_failures_silently_fall_back_to_html(monkeypatch, caplog, body):
    requests = []

    async def public(_url):
        return True

    monkeypatch.setattr(page_titles, "resolves_to_public_host", public)
    monkeypatch.setattr(page_titles, "_is_youtube_url", lambda _url: True)

    async def exercise():
        async def oembed(request):
            requests.append(request.path)
            return web.Response(body=body, content_type="application/json")

        async def page(request):
            requests.append(request.path)
            return web.Response(
                text="<title>Fallback</title>", content_type="text/html"
            )

        app = web.Application()
        app.router.add_get("/oembed", oembed)
        app.router.add_get("/page", page)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        try:
            base = f"http://127.0.0.1:{runner.addresses[0][1]}"
            monkeypatch.setattr(page_titles, "YOUTUBE_OEMBED_URL", f"{base}/oembed")
            return await page_titles.fetch_url_title(f"{base}/page")
        finally:
            await runner.cleanup()

    assert run_async(exercise) == "Fallback"
    assert requests == ["/oembed", "/page"]
    assert caplog.records == []


@pytest.mark.parametrize(
    ("url", "addresses", "expected"),
    [
        ("ftp://public.example", ["8.8.8.8"], False),
        ("https://", ["8.8.8.8"], False),
        ("https://[invalid", ["8.8.8.8"], False),
        ("https://public.example:99999", ["8.8.8.8"], False),
        ("https://public.example", [], False),
        ("https://public.example", ["invalid-address"], False),
        ("https://public.example", ["8.8.8.8", "127.0.0.1"], False),
        ("https://public.example", ["8.8.8.8", "::ffff:127.0.0.1"], False),
        ("https://public.example", ["8.8.8.8"], True),
        ("https://public.example", None, False),
    ],
)
def test_title_dns_scheme_and_every_resolved_address_are_checked(
    monkeypatch, caplog, url, addresses, expected
):
    async def exercise():
        async def resolve(_host, _port, **_kwargs):
            if addresses is None:
                raise OSError("synthetic DNS failure")
            return [(0, 0, 0, "", (address, 443)) for address in addresses]

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
        return await network.resolves_to_public_host(url)

    assert run_async(exercise) is expected
    assert caplog.records == []


@pytest.mark.parametrize(
    "failure", [ClientError("synthetic network failure"), TimeoutError()]
)
def test_title_adapter_known_network_failures_are_silent(monkeypatch, caplog, failure):
    async def resolve(_url):
        raise failure

    monkeypatch.setattr(page_titles, "resolves_to_public_host", resolve)
    assert (
        run_async(lambda: page_titles.fetch_url_title("https://public.example")) is None
    )
    assert caplog.records == []


@pytest.mark.parametrize(
    "failure", [RuntimeError("synthetic adapter bug"), asyncio.CancelledError()]
)
def test_title_adapter_does_not_contain_anomalies_or_cancellation(
    monkeypatch, caplog, failure
):
    async def resolve(_url):
        raise failure

    monkeypatch.setattr(page_titles, "resolves_to_public_host", resolve)
    with pytest.raises(type(failure)) as caught:
        run_async(lambda: page_titles.fetch_url_title("https://public.example"))
    assert caught.value is failure
    assert caplog.records == []


def test_title_adapter_total_deadline_includes_dns(monkeypatch, caplog):
    finished = []

    async def resolve(_url):
        try:
            await asyncio.Event().wait()
        finally:
            finished.append(True)

    monkeypatch.setattr(page_titles, "resolves_to_public_host", resolve)
    monkeypatch.setattr(page_titles, "TITLE_FETCH_TIMEOUT_SECONDS", 0.01)
    assert (
        run_async(lambda: page_titles.fetch_url_title("https://public.example")) is None
    )
    assert finished == [True]
    assert caplog.records == []


def test_title_redirect_limit_revalidates_every_hop(monkeypatch, caplog):
    checked, fetched = [], []

    async def public(url):
        checked.append(parse.urlparse(url).path)
        return True

    monkeypatch.setattr(page_titles, "resolves_to_public_host", public)

    async def exercise():
        async def redirect(request):
            fetched.append(request.path)
            raise web.HTTPFound(f"/{int(request.match_info['hop']) + 1}")

        app = web.Application()
        app.router.add_get("/{hop}", redirect)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        try:
            return await page_titles.fetch_url_title(
                f"http://127.0.0.1:{runner.addresses[0][1]}/0"
            )
        finally:
            await runner.cleanup()

    assert run_async(exercise) is None
    assert checked == fetched == ["/0", "/1", "/2", "/3"]
    assert caplog.records == []
