import asyncio
import codecs
import ipaddress
import json
import socket
from html.parser import HTMLParser
from typing import cast
from urllib.parse import urljoin, urlparse

from aiohttp import ClientError, ClientResponse, ClientSession, ClientTimeout

from .title_text import clean_title_text

MAX_TITLE_BYTES = 1_048_576
MAX_OEMBED_BYTES = 65_536
TITLE_READ_CHUNK_BYTES = 65_536
MAX_REDIRECTS = 3
TITLE_FETCH_TIMEOUT_SECONDS = 3
HTML_CONTENT_TYPES = {"text/html", "application/xhtml+xml"}
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
YOUTUBE_OEMBED_URL = "https://www.youtube.com/oembed"
type IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class TitleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._in_title = False
        self._found_title = False
        self._parts: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.lower() == "title" and not self._found_title:
            self._in_title = True

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title" and self._in_title:
            self._in_title = False
            self._found_title = True

    def title(self) -> str | None:
        return clean_title_text("".join(self._parts))

    @property
    def found_title(self) -> bool:
        return self._found_title


async def fetch_url_title(url: str) -> str | None:
    timeout = ClientTimeout(total=TITLE_FETCH_TIMEOUT_SECONDS)
    try:
        async with asyncio.timeout(TITLE_FETCH_TIMEOUT_SECONDS):
            async with ClientSession(
                timeout=timeout,
                headers={"User-Agent": "trellmark/0.1"},
            ) as session:
                if _is_youtube_url(url):
                    title = await _fetch_youtube_oembed_title(session, url)
                    if title is not None:
                        return title

                for _ in range(MAX_REDIRECTS + 1):
                    if not await _resolves_to_public_host(url):
                        return None

                    async with session.get(url, allow_redirects=False) as response:
                        if response.status in REDIRECT_STATUSES:
                            location = response.headers.get("Location")
                            if not location:
                                return None
                            url = urljoin(str(response.url), location)
                            continue

                        if 300 <= response.status < 400 or response.status >= 400:
                            return None
                        content_type = response.headers.get("Content-Type", "")
                        media_type = content_type.split(";", 1)[0].strip().lower()
                        if media_type and media_type not in HTML_CONTENT_TYPES:
                            return None

                        charset = response.charset or "utf-8"
                        return await _read_response_title(response, charset)
    except ClientError, TimeoutError:
        return None

    return None


async def _fetch_youtube_oembed_title(
    session: ClientSession,
    url: str,
) -> str | None:
    if not await _resolves_to_public_host(YOUTUBE_OEMBED_URL):
        return None

    try:
        async with session.get(
            YOUTUBE_OEMBED_URL,
            params={"format": "json", "url": url},
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                return None
            body = await _read_limited_body(response, MAX_OEMBED_BYTES)
    except ClientError:
        return None

    if body is None:
        return None
    try:
        payload = cast(object, json.loads(body))
    except json.JSONDecodeError, UnicodeDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    title = cast(dict[object, object], payload).get("title")
    if not isinstance(title, str):
        return None
    return clean_title_text(title)


async def _read_limited_body(
    response: ClientResponse,
    limit: int,
) -> bytes | None:
    body = bytearray()
    while len(body) <= limit:
        remaining = limit + 1 - len(body)
        chunk = await response.content.read(min(TITLE_READ_CHUNK_BYTES, remaining))
        if not chunk:
            return bytes(body)
        body.extend(chunk)
    return None


async def read_limited_body(
    response: ClientResponse,
    limit: int,
) -> bytes | None:
    """Shared bounded reader for other server-side metadata fetchers."""
    return await _read_limited_body(response, limit)


async def _read_response_title(
    response: ClientResponse,
    charset: str,
) -> str | None:
    try:
        decoder = codecs.getincrementaldecoder(charset)(errors="replace")
    except LookupError:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    parser = TitleParser()
    remaining = MAX_TITLE_BYTES
    while remaining > 0:
        chunk = await response.content.read(min(TITLE_READ_CHUNK_BYTES, remaining))
        if not chunk:
            parser.feed(decoder.decode(b"", final=True))
            parser.close()
            return parser.title()

        remaining -= len(chunk)
        parser.feed(decoder.decode(chunk))
        if parser.found_title:
            return parser.title()

    return None


def _is_youtube_url(url: str) -> bool:
    try:
        host = urlparse(url).hostname
    except ValueError:
        return False
    if host is None:
        return False
    host = host.lower()
    return (
        host == "youtu.be"
        or host.endswith(".youtu.be")
        or host == "youtube.com"
        or host.endswith(".youtube.com")
        or host == "youtube-nocookie.com"
        or host.endswith(".youtube-nocookie.com")
    )


async def _resolves_to_public_host(url: str) -> bool:
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return False

    if parsed.scheme not in {"http", "https"} or not host:
        return False

    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host,
            port,
            type=socket.SOCK_STREAM,
        )
    except OSError:
        return False

    if not infos:
        return False

    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if not _is_public_address(address):
            return False

    # aiohttp resolves again when connecting, so this intentionally does not
    # close DNS-rebinding TOCTOU. That is acceptable for this personal app.
    return True


async def resolves_to_public_host(url: str) -> bool:
    """Apply the title fetcher's public-address policy to an outbound hop."""
    return await _resolves_to_public_host(url)


def _is_public_address(address: IPAddress) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_global and not address.is_multicast


def parse_title(body: bytes, charset: str = "utf-8") -> str | None:
    try:
        html = body.decode(charset, errors="replace")
    except LookupError:
        html = body.decode("utf-8", errors="replace")

    parser = TitleParser()
    parser.feed(html)
    return parser.title()
