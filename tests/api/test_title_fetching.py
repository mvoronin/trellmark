import asyncio
import ipaddress
from urllib import parse

import pytest
from aiohttp import web

import trellmark
import trellmark.page_titles as page_titles
from tests.helpers import run_async


def test_parse_title_normalizes_html_title():
    assert (
        trellmark.parse_title(b"<html><title>  Example\nTitle &amp; More  </title>")
        == "Example Title & More"
    )
    assert trellmark.parse_title(b"<html><body>No title</body></html>") is None


def test_fetch_url_title_uses_youtube_oembed(monkeypatch):
    requests = []
    video_url = "https://www.youtube.com/shorts/aqz-KE-bpKQ"

    async def resolves_to_public_host(url):
        return True

    monkeypatch.setattr(
        page_titles, "_resolves_to_public_host", resolves_to_public_host
    )

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
            return await trellmark.fetch_url_title(video_url)
        finally:
            await runner.cleanup()

    assert run_async(run) == "Big Buck Bunny"
    assert requests == [{"format": "json", "url": video_url}]


def test_fetch_url_title_falls_back_when_youtube_oembed_has_no_title(monkeypatch):
    requests = []

    async def resolves_to_public_host(url):
        return True

    monkeypatch.setattr(
        page_titles, "_resolves_to_public_host", resolves_to_public_host
    )
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
            return await trellmark.fetch_url_title(f"http://127.0.0.1:{port}/video")
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
            return await trellmark.fetch_url_title(f"http://127.0.0.1:{port}/")
        finally:
            await runner.cleanup()

    assert run_async(run) is None
    assert requests == []


def test_fetch_url_title_checks_redirect_target_before_fetching(monkeypatch):
    requests = []

    async def resolves_to_public_host(url):
        return parse.urlparse(url).path == "/redirect"

    monkeypatch.setattr(
        page_titles, "_resolves_to_public_host", resolves_to_public_host
    )

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
            return await trellmark.fetch_url_title(f"http://127.0.0.1:{port}/redirect")
        finally:
            await runner.cleanup()

    assert run_async(run) is None
    assert requests == ["/redirect"]


def test_fetch_url_title_reads_past_first_chunk(monkeypatch):
    async def resolves_to_public_host(url):
        return True

    monkeypatch.setattr(
        page_titles, "_resolves_to_public_host", resolves_to_public_host
    )

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
            return await trellmark.fetch_url_title(f"http://127.0.0.1:{port}/")
        finally:
            await runner.cleanup()

    assert run_async(run) == "Late title"


def test_fetch_url_title_stops_at_size_limit(monkeypatch):
    async def resolves_to_public_host(url):
        return True

    monkeypatch.setattr(
        page_titles, "_resolves_to_public_host", resolves_to_public_host
    )

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
            return await trellmark.fetch_url_title(f"http://127.0.0.1:{port}/")
        finally:
            await runner.cleanup()

    assert run_async(run) is None


def test_fetch_url_title_handles_malformed_remote_response(monkeypatch):
    requests = []

    async def resolves_to_public_host(url):
        return True

    monkeypatch.setattr(
        page_titles, "_resolves_to_public_host", resolves_to_public_host
    )

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
            return await trellmark.fetch_url_title(f"http://127.0.0.1:{port}/")
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
    assert page_titles._is_public_address(ipaddress.ip_address(address)) is expected
