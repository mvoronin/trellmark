import asyncio
import ipaddress
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import pytest
from aiohttp import web

import trellmark
import trellmark.page_titles as page_titles
import trellmark.site_icons as site_icons
from tests.helpers import run_async
from trellmark import storage
from trellmark.site_icons import (
    MAX_ICON_BYTES,
    SiteIcon,
    SiteIconService,
    fetch_site_icon,
    normalize_site_origin,
    validate_icon,
)

PNG = b"\x89PNG\r\n\x1a\nsmall-png"
ICO = b"\x00\x00\x01\x00\x01\x00small-ico"


def _install_memory_cache(monkeypatch):
    cache = {}

    def read(origin):
        return cache.get(origin)

    def success(origin, icon_bytes, media_type, fetched_at, retry_after):
        record = {
            "origin": origin,
            "icon_bytes": icon_bytes,
            "media_type": media_type,
            "fetched_at": fetched_at,
            "retry_after": retry_after,
        }
        cache[origin] = record
        return record

    def failure(origin, retry_after):
        record = cache.get(origin) or {
            "origin": origin,
            "icon_bytes": None,
            "media_type": None,
            "fetched_at": None,
            "retry_after": retry_after,
        }
        record = {**record, "retry_after": retry_after}
        cache[origin] = record
        return record

    monkeypatch.setattr(storage, "read_site_icon_cache", read)
    monkeypatch.setattr(storage, "upsert_site_icon_success", success)
    monkeypatch.setattr(storage, "upsert_site_icon_failure", failure)
    return cache


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

    stored = storage.upsert_site_icon_success(
        "https://example.com",
        PNG,
        "image/png",
        fetched_at,
        positive_retry,
    )
    failed = storage.upsert_site_icon_failure(
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
    assert storage.read_site_icon_cache("https://example.com") == failed

    record = trellmark.add_url("https://example.com/article", title="Example")
    assert record is not None
    assert set(record) == {
        "id",
        "url",
        "title",
        "created_at",
        "important",
        "version",
    }
    exported = storage.export_saved_data("2026-01-01T00:00:00Z")
    assert set(exported["groups"][0]["urls"][0]) == {
        "url",
        "title",
        "created_at",
        "important",
    }


def test_storage_upserts_a_negative_cache_entry(app):
    retry_after = datetime(2026, 1, 8, tzinfo=timezone.utc)

    assert storage.upsert_site_icon_failure("http://negative.example", retry_after) == {
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
        return page_titles._is_public_address(ipaddress.ip_address(address))

    monkeypatch.setattr(page_titles, "resolves_to_public_host", public)

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

    monkeypatch.setattr(page_titles, "_resolves_to_public_host", public)

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

    monkeypatch.setattr(page_titles, "_resolves_to_public_host", public)

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

    monkeypatch.setattr(page_titles, "_resolves_to_public_host", public)

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

    monkeypatch.setattr(page_titles, "_resolves_to_public_host", public)

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

    monkeypatch.setattr(page_titles, "_resolves_to_public_host", public)

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

    monkeypatch.setattr(page_titles, "_resolves_to_public_host", public)
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


def test_service_singleflights_equivalent_origins_and_negative_caches(monkeypatch):
    calls = []
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    cache = _install_memory_cache(monkeypatch)

    async def fetch(url):
        calls.append(url)
        await asyncio.sleep(0.01)
        return None

    service = SiteIconService(fetcher=fetch, clock=lambda: now)

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


def test_service_keeps_a_success_fresh_for_thirty_days(monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    cache = _install_memory_cache(monkeypatch)
    calls = []

    async def fetch(url):
        calls.append(url)
        return SiteIcon(PNG, "image/png")

    service = SiteIconService(fetcher=fetch, clock=lambda: now)

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


def test_service_force_refresh_bypasses_a_fresh_ttl(monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    _install_memory_cache(monkeypatch)
    storage.upsert_site_icon_success(
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

    service = SiteIconService(fetcher=fetch, clock=lambda: now)

    assert run_async(lambda: service.refresh("https://example.com/page")) is True
    assert calls == ["https://example.com/page"]


def test_service_revalidates_injected_bytes_before_storage(monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    cache = _install_memory_cache(monkeypatch)

    async def fetch(_url):
        return SiteIcon(b"<svg>attacker-controlled</svg>", "image/png")

    service = SiteIconService(fetcher=fetch, clock=lambda: now)

    assert run_async(lambda: service.get("https://example.com/page")) is None
    assert cache["https://example.com"] == {
        "origin": "https://example.com",
        "icon_bytes": None,
        "media_type": None,
        "fetched_at": None,
        "retry_after": now + timedelta(days=7),
    }


def test_service_returns_stale_icon_and_preserves_it_when_refresh_fails(monkeypatch):
    fetched_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    now = fetched_at + timedelta(days=31)
    cache = _install_memory_cache(monkeypatch)
    storage.upsert_site_icon_success(
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

    service = SiteIconService(fetcher=fetch, clock=lambda: now)

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


def test_service_limits_fetches_to_two_origins(monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    _install_memory_cache(monkeypatch)
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

    service = SiteIconService(fetcher=fetch, clock=lambda: now)

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
