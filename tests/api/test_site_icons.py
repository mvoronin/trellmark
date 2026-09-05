import asyncio
import ipaddress
import threading
from dataclasses import FrozenInstanceError, asdict
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock
from urllib.parse import urlparse

import pytest
from aiohttp import web
from sqlalchemy import event
from sqlalchemy.exc import DBAPIError, OperationalError

from tests.api.test_database_error_boundaries import _api_json_response
from tests.bookmarks import helpers as bookmark_helpers
from tests.bookmarks.helpers import icon_cache_service, make_icon_service, seed_url
from tests.helpers import http_json, run_async
from trellmark.bookmarks import domain, persistence
from trellmark.bookmarks import integrations as site_icons
from trellmark.bookmarks.integrations import (
    MAX_ICON_BYTES,
    SiteIcon,
    fetch_site_icon,
    normalize_site_origin,
    validate_icon,
)
from trellmark.bookmarks.persistence import PostgresSiteIconCacheQueries
from trellmark.platform import network
from trellmark.platform.runtime import get_engine

PNG = b"\x89PNG\r\n\x1a\nsmall-png"
ICO = b"\x00\x00\x01\x00\x01\x00small-ico"


def test_icon_cache_and_metadata_have_bookmarks_owners(database, bookmarks_service):
    from trellmark.bookmarks import application, integrations, persistence

    assert hasattr(integrations, "SiteIconService")
    assert hasattr(domain, "SiteIconCacheRecord")
    assert hasattr(application, "SiteIconGateway")
    assert hasattr(persistence, "PostgresDerivedStateUnitOfWork")
    assert hasattr(bookmarks_service, "refresh_url_metadata")

    record = bookmark_helpers.seed_url(
        "https://metadata-owner.example", title="Existing"
    )
    result = run_async(lambda: bookmarks_service.refresh_url_metadata(record["id"]))
    assert isinstance(result, domain.URLMetadataRefreshed)
    assert result.record.title == "Existing"
    assert result.record.version == record["version"]
    assert result.title_updated is False
    assert result.icon_updated is False


class CacheRecords:
    def get(self, origin):
        record = PostgresSiteIconCacheQueries(get_engine).read(origin)
        return None if record is None else asdict(record)

    def __getitem__(self, origin):
        record = self.get(origin)
        assert record is not None
        return record


def cache_success(origin, icon_bytes, media_type, fetched_at, retry_after):
    service = icon_cache_service()
    return asdict(
        run_async(
            lambda: service.success(
                origin, SiteIcon(icon_bytes, media_type), fetched_at, retry_after
            )
        )
    )


def cache_failure(origin, retry_after):
    return asdict(run_async(lambda: icon_cache_service().failure(origin, retry_after)))


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("https://EXAMPLE.com:443/a", "https://example.com"),
        ("http://example.com:80/a", "http://example.com"),
        ("https://example.com:8443/a", "https://example.com:8443"),
        ("http://example.com/a", "http://example.com"),
        ("http://[2001:db8::1]:8080/a", "http://[2001:db8::1]:8080"),
    ],
)
def test_normalize_site_origin(url, origin):
    assert normalize_site_origin(url) == origin


@pytest.mark.parametrize(
    "url",
    ["", "ftp://example.com/a", "https:///missing", "https://example.com:bad"],
)
def test_normalize_site_origin_rejects_invalid_urls(url):
    with pytest.raises(ValueError, match="valid http or https URL"):
        normalize_site_origin(url)


def test_storage_records_positive_and_negative_cache_state_without_export_drift(app):
    fetched_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    positive_retry = fetched_at + timedelta(days=30)
    negative_retry = fetched_at + timedelta(days=37)

    stored = cache_success(
        "https://example.com",
        PNG,
        "image/png",
        fetched_at,
        positive_retry,
    )
    failed = cache_failure(
        "https://example.com",
        negative_retry,
    )

    assert stored == {
        "origin": "https://example.com",
        "icon_bytes": PNG,
        "media_type": "image/png",
        "fetched_at": fetched_at,
        "retry_after": positive_retry,
    }
    assert failed == {**stored, "retry_after": negative_retry}
    assert CacheRecords().get("https://example.com") == failed

    record = bookmark_helpers.seed_url("https://example.com/article", title="Example")
    assert record is not None
    assert set(record) == {
        "id",
        "url",
        "title",
        "created_at",
        "important",
        "version",
    }
    status, exported = http_json(app[0], "/api/export")
    assert status == 200
    assert set(exported["groups"][0]["urls"][0]) == {
        "url",
        "title",
        "created_at",
        "important",
    }


def test_storage_upserts_a_negative_cache_entry(app):
    retry_after = datetime(2026, 1, 8, tzinfo=timezone.utc)

    assert cache_failure("http://negative.example", retry_after) == {
        "origin": "http://negative.example",
        "icon_bytes": None,
        "media_type": None,
        "fetched_at": None,
        "retry_after": retry_after,
    }


@pytest.mark.parametrize(
    ("media_type", "body", "expected_type"),
    [
        ("image/png", PNG, "image/png"),
        ("image/x-icon", ICO, "image/vnd.microsoft.icon"),
        ("image/vnd.microsoft.icon", ICO, "image/vnd.microsoft.icon"),
        ("image/png", ICO, None),
        ("image/x-icon", PNG, None),
        ("image/svg+xml", b"<svg></svg>", None),
        ("application/octet-stream", PNG, None),
        ("", PNG, None),
    ],
)
def test_validate_icon_requires_matching_mime_and_magic(
    media_type, body, expected_type
):
    icon = validate_icon(media_type, body)
    if expected_type is None:
        assert icon is None
    else:
        assert icon is not None
        assert icon.media_type == expected_type


def test_validate_icon_rejects_oversized_bytes():
    assert validate_icon("image/png", PNG + b"x" * MAX_ICON_BYTES) is None


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "169.254.169.254",
        "192.168.1.1",
        "224.0.0.1",
        "::ffff:127.0.0.1",
    ],
)
def test_fetch_site_icon_applies_shared_policy_to_denied_address_classes(
    monkeypatch,
    address,
):
    checks = []

    async def public(url):
        checks.append(url)
        return network.is_public_address(ipaddress.ip_address(address))

    monkeypatch.setattr(site_icons, "resolves_to_public_host", public)

    assert run_async(lambda: fetch_site_icon("https://blocked.example/path")) is None
    assert checks == [
        "https://blocked.example/path",
        "https://blocked.example/favicon.ico",
    ]


def test_fetch_site_icon_rejects_dns_failure_before_request(monkeypatch):
    async def run():
        async def fail_dns(*_args, **_kwargs):
            raise OSError("DNS failure")

        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "getaddrinfo", fail_dns)
        return await fetch_site_icon("https://does-not-resolve.invalid/path")

    assert run_async(run) is None


def test_fetch_site_icon_uses_three_unique_links_in_order(monkeypatch):
    requests = []

    async def public(_url):
        return True

    monkeypatch.setattr(site_icons, "resolves_to_public_host", public)

    async def run():
        async def page(request):
            requests.append(request.path)
            return web.Response(
                text=(
                    '<link rel="icon" href="/bad">'
                    '<link rel="shortcut icon" href="/bad">'
                    '<link rel="ICON" href="/svg">'
                    '<link rel="icon" href="/good">'
                    '<link rel="icon" href="/ignored">'
                ),
                content_type="text/html",
            )

        async def bad(request):
            requests.append(request.path)
            return web.Response(body=b"not-png", content_type="image/png")

        async def svg(request):
            requests.append(request.path)
            return web.Response(body=b"<svg/>", content_type="image/svg+xml")

        async def good(request):
            requests.append(request.path)
            return web.Response(body=PNG, content_type="image/png")

        async def ignored(request):
            requests.append(request.path)
            return web.Response(body=ICO, content_type="image/x-icon")

        application = web.Application()
        application.router.add_get("/page", page)
        application.router.add_get("/bad", bad)
        application.router.add_get("/svg", svg)
        application.router.add_get("/good", good)
        application.router.add_get("/ignored", ignored)
        runner = web.AppRunner(application)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        try:
            port = runner.addresses[0][1]
            return await fetch_site_icon(f"http://127.0.0.1:{port}/page")
        finally:
            await runner.cleanup()

    assert run_async(run) == SiteIcon(PNG, "image/png")
    assert requests == ["/page", "/bad", "/svg", "/good"]


def test_fetch_site_icon_falls_back_and_checks_redirect_hops(monkeypatch):
    requests = []

    async def public(url):
        return urlparse(url).path != "/private"

    monkeypatch.setattr(site_icons, "resolves_to_public_host", public)

    async def run():
        async def page(request):
            requests.append(request.path)
            return web.Response(text="<title>No icon</title>", content_type="text/html")

        async def favicon(request):
            requests.append(request.path)
            raise web.HTTPFound("/private")

        async def private(request):
            requests.append(request.path)
            return web.Response(body=ICO, content_type="image/x-icon")

        application = web.Application()
        application.router.add_get("/page", page)
        application.router.add_get("/favicon.ico", favicon)
        application.router.add_get("/private", private)
        runner = web.AppRunner(application)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        try:
            port = runner.addresses[0][1]
            return await fetch_site_icon(f"http://127.0.0.1:{port}/page")
        finally:
            await runner.cleanup()

    assert run_async(run) is None
    assert requests == ["/page", "/favicon.ico"]


def test_fetch_site_icon_fallback_accepts_a_valid_ico(monkeypatch):
    async def public(_url):
        return True

    monkeypatch.setattr(site_icons, "resolves_to_public_host", public)

    async def run():
        async def page(_request):
            return web.Response(
                text="<title>No links</title>", content_type="text/html"
            )

        async def favicon(_request):
            return web.Response(body=ICO, content_type="image/x-icon")

        application = web.Application()
        application.router.add_get("/page", page)
        application.router.add_get("/favicon.ico", favicon)
        runner = web.AppRunner(application)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        try:
            port = runner.addresses[0][1]
            return await fetch_site_icon(f"http://127.0.0.1:{port}/page")
        finally:
            await runner.cleanup()

    assert run_async(run) == SiteIcon(ICO, "image/vnd.microsoft.icon")


def test_fetch_site_icon_stops_after_three_redirects(monkeypatch):
    requests = []

    async def public(_url):
        return True

    monkeypatch.setattr(site_icons, "resolves_to_public_host", public)

    async def run():
        async def page(request):
            requests.append(request.path)
            return web.Response(
                text='<link rel="icon" href="/redirect/0">',
                content_type="text/html",
            )

        async def redirect(request):
            requests.append(request.path)
            step = int(request.match_info["step"])
            raise web.HTTPFound(f"/redirect/{step + 1}")

        async def fallback(request):
            requests.append(request.path)
            return web.Response(status=404)

        application = web.Application()
        application.router.add_get("/page", page)
        application.router.add_get("/redirect/{step}", redirect)
        application.router.add_get("/favicon.ico", fallback)
        runner = web.AppRunner(application)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        try:
            port = runner.addresses[0][1]
            return await fetch_site_icon(f"http://127.0.0.1:{port}/page")
        finally:
            await runner.cleanup()

    assert run_async(run) is None
    assert requests == [
        "/page",
        "/redirect/0",
        "/redirect/1",
        "/redirect/2",
        "/redirect/3",
        "/favicon.ico",
    ]


def test_fetch_site_icon_rejects_oversized_response(monkeypatch):
    async def public(_url):
        return True

    monkeypatch.setattr(site_icons, "resolves_to_public_host", public)

    async def run():
        async def page(_request):
            return web.Response(text="", content_type="text/html")

        async def favicon(_request):
            return web.Response(
                body=ICO + b"x" * MAX_ICON_BYTES,
                content_type="image/x-icon",
            )

        application = web.Application()
        application.router.add_get("/page", page)
        application.router.add_get("/favicon.ico", favicon)
        runner = web.AppRunner(application)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        try:
            port = runner.addresses[0][1]
            return await fetch_site_icon(f"http://127.0.0.1:{port}/page")
        finally:
            await runner.cleanup()

    assert run_async(run) is None


def test_fetch_site_icon_has_one_overall_deadline(monkeypatch):
    async def public(_url):
        return True

    monkeypatch.setattr(site_icons, "resolves_to_public_host", public)
    monkeypatch.setattr(site_icons, "ICON_FETCH_TIMEOUT_SECONDS", 0.05)

    async def run():
        async def page(_request):
            await asyncio.sleep(0.2)
            return web.Response(text="", content_type="text/html")

        application = web.Application()
        application.router.add_get("/page", page)
        runner = web.AppRunner(application)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        try:
            port = runner.addresses[0][1]
            started = asyncio.get_running_loop().time()
            result = await fetch_site_icon(f"http://127.0.0.1:{port}/page")
            return result, asyncio.get_running_loop().time() - started
        finally:
            await runner.cleanup()

    result, elapsed = run_async(run)
    assert result is None
    assert elapsed < 0.5


def test_service_singleflights_equivalent_origins_and_negative_caches(database):
    calls = []
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    cache = CacheRecords()

    async def fetch(url):
        calls.append(url)
        await asyncio.sleep(0.01)
        return None

    service = make_icon_service(fetcher=fetch, clock=lambda: now)

    async def run():
        first, second = await asyncio.gather(
            service.get("https://EXAMPLE.com:443/one"),
            service.get("https://example.com/two"),
        )
        third = await service.get("https://example.com/three")
        return first, second, third

    assert run_async(run) == (None, None, None)
    assert calls == ["https://EXAMPLE.com:443/one"]
    cached = cache.get("https://example.com")
    assert cached is not None
    assert cached["icon_bytes"] is None
    assert cached["retry_after"] == now + timedelta(days=7)


def test_service_keeps_a_success_fresh_for_thirty_days(database):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    cache = CacheRecords()
    calls = []

    async def fetch(url):
        calls.append(url)
        return SiteIcon(PNG, "image/png")

    service = make_icon_service(fetcher=fetch, clock=lambda: now)

    async def run():
        first = await service.get("https://example.com/one")
        second = await service.get("https://example.com/two")
        return first, second

    assert run_async(run) == (
        SiteIcon(PNG, "image/png"),
        SiteIcon(PNG, "image/png"),
    )
    assert calls == ["https://example.com/one"]
    assert cache["https://example.com"]["retry_after"] == now + timedelta(days=30)


def test_service_force_refresh_bypasses_a_fresh_ttl(database):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    CacheRecords()
    cache_success(
        "https://example.com",
        PNG,
        "image/png",
        now,
        now + timedelta(days=30),
    )
    calls = []

    async def fetch(url):
        calls.append(url)
        return SiteIcon(ICO, "image/vnd.microsoft.icon")

    service = make_icon_service(fetcher=fetch, clock=lambda: now)

    assert run_async(lambda: service.refresh("https://example.com/page")) is True
    assert calls == ["https://example.com/page"]


def test_service_revalidates_injected_bytes_before_storage(database):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    cache = CacheRecords()

    async def fetch(_url):
        return SiteIcon(b"<svg>attacker-controlled</svg>", "image/png")

    service = make_icon_service(fetcher=fetch, clock=lambda: now)

    assert run_async(lambda: service.get("https://example.com/page")) is None
    assert cache["https://example.com"] == {
        "origin": "https://example.com",
        "icon_bytes": None,
        "media_type": None,
        "fetched_at": None,
        "retry_after": now + timedelta(days=7),
    }


def test_service_returns_stale_icon_and_preserves_it_when_refresh_fails(database):
    fetched_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    now = fetched_at + timedelta(days=31)
    cache = CacheRecords()
    cache_success(
        "https://example.com",
        PNG,
        "image/png",
        fetched_at,
        now,
    )
    refreshed = asyncio.Event()

    async def fetch(_url):
        refreshed.set()
        return None

    service = make_icon_service(fetcher=fetch, clock=lambda: now)

    async def run():
        icon = await service.get("https://example.com/page")
        await asyncio.wait_for(refreshed.wait(), timeout=1)
        await service.wait_for_idle()
        return icon

    assert run_async(run) == SiteIcon(PNG, "image/png")
    cached = cache.get("https://example.com")
    assert cached is not None
    assert cached["icon_bytes"] == PNG
    assert cached["fetched_at"] == fetched_at
    assert cached["retry_after"] == now + timedelta(days=7)


def test_service_limits_fetches_to_two_origins(database):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    CacheRecords()
    active = 0
    maximum = 0
    two_started = asyncio.Event()
    release = asyncio.Event()

    async def fetch(_url):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        if active == 2:
            two_started.set()
        await release.wait()
        active -= 1
        return SiteIcon(ICO, "image/vnd.microsoft.icon")

    service = make_icon_service(fetcher=fetch, clock=lambda: now)

    async def run():
        tasks = [
            asyncio.create_task(service.get(f"https://site-{index}.example/page"))
            for index in range(3)
        ]
        await asyncio.wait_for(two_started.wait(), timeout=1)
        await asyncio.sleep(0)
        assert active == 2
        release.set()
        return await asyncio.gather(*tasks)

    assert run_async(run) == [
        SiteIcon(ICO, "image/vnd.microsoft.icon"),
        SiteIcon(ICO, "image/vnd.microsoft.icon"),
        SiteIcon(ICO, "image/vnd.microsoft.icon"),
    ]
    assert maximum == 2


@pytest.mark.parametrize(
    ("charset", "body"),
    [
        ("unknown-codec", b"no links"),
        ("base64_codec", b"no links"),
        ("utf-16", b"\x00"),
        ("utf-32", b"\x00"),
        ("utf-8", b'<link rel="icon" href="&#' + b"9" * 5000 + b';">'),
    ],
)
def test_icon_discovery_classifies_remote_decoding_and_parser_failures(
    monkeypatch, charset, body, caplog
):
    async def public(_url):
        return True

    monkeypatch.setattr(site_icons, "resolves_to_public_host", public)

    async def exercise():
        async def page(_request):
            return web.Response(
                body=body, headers={"Content-Type": f"text/html; charset={charset}"}
            )

        async def fallback(_request):
            return web.Response(body=ICO, content_type="image/x-icon")

        application = web.Application()
        application.router.add_get("/page", page)
        application.router.add_get("/favicon.ico", fallback)
        runner = web.AppRunner(application)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        try:
            port = runner.addresses[0][1]
            return await fetch_site_icon(f"http://127.0.0.1:{port}/page")
        finally:
            await runner.cleanup()

    assert run_async(exercise) == SiteIcon(ICO, "image/vnd.microsoft.icon")
    assert caplog.records == []


@pytest.mark.parametrize("positive", [True, False])
def test_icon_cache_rolls_back_by_default_and_commits_explicitly(database, positive):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    factory = persistence.PostgresDerivedStateUnitOfWorkFactory(get_engine)
    queries = persistence.PostgresSiteIconCacheQueries(get_engine)
    origin = "https://rollback-cache.example"
    for commit in (False, True):
        with factory() as uow:
            assert not hasattr(uow, "groups") and not hasattr(uow, "bookmarks")
            if positive:
                record = uow.cache.success(origin, SiteIcon(PNG, "image/png"), now, now)
            else:
                record = uow.cache.failure(origin, now)
            assert queries.read(origin) is None
            if commit:
                uow.commit()
        assert uow._connection is None and uow._transaction is None
        assert queries.read(origin) == (record if commit else None)
    with pytest.raises(FrozenInstanceError):
        record.retry_after = now


@pytest.mark.parametrize(
    ("operation", "failure_point"),
    [
        (operation, point)
        for operation in ("read", "success", "failure")
        for point in ("connect", "begin", "sql", "sql_close", "close")
        if (operation, point) != ("read", "begin")
    ],
)
def test_icon_cache_worker_contains_the_complete_scope_and_preserves_errors(
    database, monkeypatch, operation, failure_point
):
    failure = RuntimeError("synthetic cache lifecycle failure")
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    engine = get_engine()
    threads = []

    def observe(*args):
        threads.append(threading.get_ident())
        if failure_point in {"sql", "sql_close"}:
            raise failure

    event.listen(engine, "before_cursor_execute", observe)
    original_connect = engine.connect

    class Connection:
        def __init__(self):
            self.connection = original_connect()

        def begin(self):
            threads.append(threading.get_ident())
            if failure_point == "begin":
                raise failure
            return self.connection.begin()

        def execute(self, *args, **kwargs):
            return self.connection.execute(*args, **kwargs)

        def close(self):
            threads.append(threading.get_ident())
            self.connection.close()
            if failure_point == "close":
                raise failure
            if failure_point == "sql_close":
                raise RuntimeError("synthetic cleanup failure")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    def connect():
        threads.append(threading.get_ident())
        if failure_point == "connect":
            raise failure
        return Connection()

    monkeypatch.setattr(engine, "connect", connect)
    service = icon_cache_service()

    async def exercise():
        event_loop_thread = threading.get_ident()
        try:
            if operation == "read":
                await service.read("https://lifecycle-cache.example")
            elif operation == "success":
                await service.success(
                    "https://lifecycle-cache.example",
                    SiteIcon(PNG, "image/png"),
                    now,
                    now,
                )
            else:
                await service.failure("https://lifecycle-cache.example", now)
        except RuntimeError as caught:
            assert caught is failure
        else:
            pytest.fail("cache failure was swallowed")
        assert threads and len(set(threads)) == 1
        assert threads[0] != event_loop_thread
        assert engine.pool.checkedout() == 0

    try:
        run_async(exercise)
    finally:
        event.remove(engine, "before_cursor_execute", observe)


def test_icon_fetch_only_anomaly_warns_and_cancellation_escapes(database, monkeypatch):
    warning = Mock()
    monkeypatch.setattr(site_icons.logger, "warning", warning)
    anomaly = RuntimeError("synthetic fetch anomaly")
    cancellation = asyncio.CancelledError("synthetic cancellation")

    async def fail(_url):
        raise anomaly

    service = make_icon_service(fetcher=fail)
    assert run_async(lambda: service.refresh("https://anomaly.example")) is False
    warning.assert_called_once_with(
        "Unexpected site icon fetch failure.", exc_info=True
    )
    warning.reset_mock()

    async def cancel(_url):
        raise cancellation

    service = make_icon_service(fetcher=cancel)

    async def exercise():
        try:
            await service.refresh("https://cancel.example")
        except asyncio.CancelledError as caught:
            assert caught is cancellation
        else:
            pytest.fail("fetch cancellation was swallowed")

    run_async(exercise)
    assert (
        persistence.PostgresSiteIconCacheQueries(get_engine).read(
            "https://cancel.example"
        )
        is None
    )
    warning.assert_not_called()


@pytest.fixture
def observed_icon_database_boundary(monkeypatch):
    from trellmark import app as app_module

    observed = []
    original = app_module.database_exception_handler

    async def capture(request, error):
        observed.append(error)
        return await original(request, error)

    monkeypatch.setattr(app_module, "database_exception_handler", capture)
    return observed


@pytest.mark.parametrize("endpoint", ["icon", "refresh-metadata"])
@pytest.mark.parametrize("failure_type", [DBAPIError, OperationalError])
def test_icon_persistence_failure_reaches_http_boundary_unchanged(
    observed_icon_database_boundary, app, monkeypatch, endpoint, failure_type, caplog
):
    base_url, _ = app
    record = seed_url("https://failure-cache.example", "Existing")
    failure = failure_type(
        "synthetic cache statement",
        {},
        RuntimeError("synthetic failure"),
        hide_parameters=True,
    )
    original = persistence.PostgresSiteIconCacheRepository.failure

    def fail_after_write(self, origin, retry_after):
        original(self, origin, retry_after)
        raise failure

    monkeypatch.setattr(
        persistence.PostgresSiteIconCacheRepository, "failure", fail_after_write
    )
    status, payload, cache_control, _ = _api_json_response(
        base_url,
        f"/api/urls/{record['id']}/{endpoint}",
        method="GET" if endpoint == "icon" else "POST",
    )
    expected = (
        (503, "Service unavailable.")
        if failure_type is OperationalError
        else (500, "Internal server error.")
    )
    assert (status, payload) == (expected[0], {"error": expected[1]})
    assert cache_control == "no-store"
    assert observed_icon_database_boundary == [failure]
    assert (
        persistence.PostgresSiteIconCacheQueries(get_engine).read(
            "https://failure-cache.example"
        )
        is None
    )
    assert caplog.records == []


def test_icon_singleflight_reserves_first_url_before_async_cache_lookup(database):
    cache = icon_cache_service()
    calls = []

    async def exercise():
        started = asyncio.Event()
        release = asyncio.Event()

        class Cache:
            async def read(self, origin):
                calls.append(("read", origin))
                started.set()
                await release.wait()
                return await cache.read(origin)

            success = cache.success
            failure = cache.failure

        async def fetch(url):
            calls.append(("fetch", url))
            return SiteIcon(PNG, "image/png")

        service = site_icons.SiteIconService(cache=Cache(), fetcher=fetch)
        first = asyncio.create_task(service.get("https://EXAMPLE.com:443/first"))
        await asyncio.wait_for(started.wait(), timeout=5)
        second = asyncio.create_task(service.get("https://example.com/second"))
        draining = asyncio.create_task(service.wait_for_idle())
        await asyncio.sleep(0)
        assert not draining.done()
        assert len(service._lookups) == 1 and service._inflight == {}
        release.set()
        assert await asyncio.gather(first, second) == [SiteIcon(PNG, "image/png")] * 2
        await draining
        assert service._lookups == {} and service._inflight == {}

    run_async(exercise)
    assert calls == [
        ("read", "https://example.com"),
        ("fetch", "https://EXAMPLE.com:443/first"),
    ]


@pytest.mark.parametrize("cancelled", [False, True])
def test_failed_or_cancelled_icon_lookup_drains_and_allows_retry(database, cancelled):
    cache = icon_cache_service()
    failure = (
        asyncio.CancelledError("synthetic lookup cancellation")
        if cancelled
        else RuntimeError("synthetic lookup failure")
    )
    reads = 0

    class Cache:
        async def read(self, origin):
            nonlocal reads
            reads += 1
            if reads == 1:
                raise failure
            return await cache.read(origin)

        success = cache.success
        failure = cache.failure

    async def fetch(_url):
        return SiteIcon(PNG, "image/png")

    service = site_icons.SiteIconService(cache=Cache(), fetcher=fetch)

    async def exercise():
        try:
            await service.get("https://lookup-retry.example")
        except BaseException as caught:
            assert caught is failure
        else:
            pytest.fail("lookup failure did not propagate")
        await service.wait_for_idle()
        assert service._lookups == {} and service._inflight == {}
        assert await service.get("https://lookup-retry.example") == SiteIcon(
            PNG, "image/png"
        )
        await service.wait_for_idle()
        assert service._lookups == {} and service._inflight == {}

    run_async(exercise)
    assert reads == 2


@pytest.mark.parametrize(
    ("failure_point", "cleanup_failure"),
    [
        (point, None)
        for point in (
            None,
            "connect",
            "begin",
            "bind",
            "write",
            "commit",
            "rollback",
            "close",
        )
    ]
    + [
        (point, cleanup)
        for point in ("write", "commit", "cancel")
        for cleanup in ("rollback", "close")
    ],
)
def test_derived_icon_uow_lifecycle_keeps_primary_failure_identity(
    monkeypatch, failure_point, cleanup_failure
):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = domain.SiteIconCacheRecord(
        "https://lifecycle.example", None, None, None, now
    )
    primary = (
        asyncio.CancelledError("synthetic worker cancellation")
        if failure_point == "cancel"
        else RuntimeError("synthetic lifecycle failure")
    )
    events = []
    threads = []

    class Lifecycle:
        is_active = True

        def record(self, stage):
            events.append(stage)
            threads.append(threading.get_ident())
            if stage == failure_point or (
                stage == "write" and failure_point == "cancel"
            ):
                raise primary
            if stage == cleanup_failure:
                raise RuntimeError("synthetic cleanup failure")

        def connect(self):
            self.record("connect")
            return self

        def begin(self):
            self.record("begin")
            return self

        def commit(self):
            self.record("commit")
            self.is_active = False

        def rollback(self):
            self.record("rollback")
            self.is_active = False

        def close(self):
            self.record("close")

    class Repository:
        def __init__(self, connection):
            connection.record("bind")
            self.connection = connection

        def failure(self, _origin, _retry_after):
            self.connection.record("write")
            return result

    lifecycle = Lifecycle()
    units = []

    def factory():
        unit = persistence.PostgresDerivedStateUnitOfWork(lambda: lifecycle)
        units.append(unit)
        return unit

    monkeypatch.setattr(persistence, "PostgresSiteIconCacheRepository", Repository)
    cache = icon_cache_service(factory=factory)

    async def exercise():
        loop_thread = threading.get_ident()
        try:
            if failure_point == "rollback":

                def rollback_work():
                    with factory() as uow:
                        uow.cache.failure(result.origin, now)

                await cache.work_runner.run(rollback_work)
            else:
                actual = await cache.failure(result.origin, now)
                assert actual == result
        except BaseException as caught:
            assert caught is primary
        else:
            assert failure_point is None
        assert len(units) == 1
        assert units[0]._connection is None and units[0]._transaction is None
        assert len(set(threads)) == 1 and threads[0] != loop_thread

    run_async(exercise)
    if failure_point == "connect":
        assert events == ["connect"]
    else:
        assert events[-1] == "close"
    assert "gate" not in events
    if failure_point in ("bind", "write", "cancel", "rollback"):
        assert "commit" not in events
    if failure_point in ("bind", "write", "cancel", "commit", "rollback"):
        assert "rollback" in events
