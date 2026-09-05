import asyncio
import codecs
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import cast
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit

from aiohttp import ClientError, ClientResponse, ClientSession, ClientTimeout

from ..platform.network import read_bounded_body, resolves_to_public_host
from .application import SiteIconCache, SiteIconFetcher
from .domain import SiteIcon, SiteIconCacheRecord, clean_title_text

logger = logging.getLogger(__name__)

MAX_TITLE_BYTES = 1_048_576
MAX_OEMBED_BYTES = 65_536
TITLE_READ_CHUNK_BYTES = 65_536
MAX_REDIRECTS = 3
TITLE_FETCH_TIMEOUT_SECONDS = 3
HTML_CONTENT_TYPES = {"text/html", "application/xhtml+xml"}
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
YOUTUBE_OEMBED_URL = "https://www.youtube.com/oembed"

MAX_ICON_BYTES = 256 * 1024
MAX_ICON_CANDIDATES = 3
MAX_ICON_REDIRECTS = 3
ICON_FETCH_TIMEOUT_SECONDS = 3
POSITIVE_TTL = timedelta(days=30)
NEGATIVE_TTL = timedelta(days=7)
MAX_CONCURRENT_ORIGINS = 2
PNG_MEDIA_TYPE = "image/png"
ICO_MEDIA_TYPE = "image/vnd.microsoft.icon"
ICO_SOURCE_MEDIA_TYPES = {"image/x-icon", ICO_MEDIA_TYPE}
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
ICO_MAGIC = b"\x00\x00\x01\x00"


@dataclass(frozen=True, slots=True)
class _FetchedBody:
    data: bytes
    media_type: str
    final_url: str
    charset: str | None


class _IconLinkParser(HTMLParser):
    def __init__(self, document_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self._document_url = document_url
        self.candidates: list[str] = []
        self._seen: set[str] = set()

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.lower() != "link" or len(self.candidates) >= MAX_ICON_CANDIDATES:
            return
        attributes = {name.lower(): value for name, value in attrs}
        rel = attributes.get("rel")
        href = attributes.get("href")
        if rel is None or href is None or "icon" not in rel.lower().split():
            return

        candidate = _http_candidate(self._document_url, href)
        if candidate is None or candidate in self._seen:
            return
        self._seen.add(candidate)
        self.candidates.append(candidate)


type Clock = Callable[[], datetime]


def normalize_site_origin(url: str) -> str:
    """Return the normalized HTTP(S) origin used as the durable cache key."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise ValueError("Enter a valid http or https URL.") from error

    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Enter a valid http or https URL.")

    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower()
    display_host = f"[{host}]" if ":" in host else host
    default_port = 80 if scheme == "http" else 443
    suffix = f":{port}" if port is not None and port != default_port else ""
    return f"{scheme}://{display_host}{suffix}"


def validate_icon(media_type: str, body: bytes) -> SiteIcon | None:
    """Accept only bounded PNG/ICO bytes whose declaration matches magic."""
    if len(body) > MAX_ICON_BYTES:
        return None
    declared_type = media_type.split(";", 1)[0].strip().lower()
    if declared_type == PNG_MEDIA_TYPE and body.startswith(PNG_MAGIC):
        return SiteIcon(body, PNG_MEDIA_TYPE)
    if declared_type in ICO_SOURCE_MEDIA_TYPES and body.startswith(ICO_MAGIC):
        return SiteIcon(body, ICO_MEDIA_TYPE)
    return None


async def fetch_site_icon(url: str) -> SiteIcon | None:
    """Discover and fetch one safe icon within one three-second deadline."""
    try:
        origin = normalize_site_origin(url)
    except ValueError:
        return None

    timeout = ClientTimeout(total=ICON_FETCH_TIMEOUT_SECONDS)
    try:
        async with asyncio.timeout(ICON_FETCH_TIMEOUT_SECONDS):
            async with ClientSession(
                timeout=timeout,
                headers={"User-Agent": "trellmark/0.1"},
            ) as session:
                candidates = await _discover_icon_candidates(session, url)
                fallback = f"{origin}/favicon.ico"
                if fallback not in candidates:
                    candidates.append(fallback)

                for candidate in candidates:
                    response = await _fetch_public_body(
                        session,
                        candidate,
                        MAX_ICON_BYTES,
                    )
                    if response is None:
                        continue
                    icon = validate_icon(response.media_type, response.data)
                    if icon is not None:
                        return icon
    except ClientError, TimeoutError:
        return None
    return None


async def _discover_icon_candidates(
    session: ClientSession,
    page_url: str,
) -> list[str]:
    response = await _fetch_public_body(
        session,
        page_url,
        MAX_TITLE_BYTES,
    )
    if response is None or response.media_type not in HTML_CONTENT_TYPES:
        return []

    parser = _IconLinkParser(response.final_url)
    try:
        parser.feed(response.data.decode(response.charset or "utf-8", errors="replace"))
        parser.close()
    except LookupError, ValueError:
        # Invalid HTTP charsets, Unicode and HTML entities are remote document
        # failures. Favicon fallback remains available without an anomaly warning.
        return []
    return parser.candidates


async def _fetch_public_body(
    session: ClientSession,
    url: str,
    limit: int,
) -> _FetchedBody | None:
    for _ in range(MAX_ICON_REDIRECTS + 1):
        if not await resolves_to_public_host(url):
            return None
        try:
            async with session.get(url, allow_redirects=False) as response:
                if response.status in REDIRECT_STATUSES:
                    location = response.headers.get("Location")
                    if not location:
                        return None
                    url = urljoin(str(response.url), location)
                    continue
                if 300 <= response.status < 400 or response.status >= 400:
                    return None

                body = await read_bounded_body(response, limit)
                if body is None:
                    return None
                content_type = response.headers.get("Content-Type", "")
                media_type = content_type.split(";", 1)[0].strip().lower()
                return _FetchedBody(
                    body,
                    media_type,
                    str(response.url),
                    response.charset,
                )
        except ClientError, ValueError:
            return None
    return None


def _http_candidate(document_url: str, href: str) -> str | None:
    try:
        parsed = urlsplit(urljoin(document_url, href))
        parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname.lower()
    display_host = f"[{host}]" if ":" in host else host
    default_port = 80 if scheme == "http" else 443
    suffix = f":{parsed.port}" if parsed.port not in {None, default_port} else ""
    return urlunsplit(
        parsed._replace(
            scheme=scheme,
            netloc=f"{display_host}{suffix}",
            fragment="",
        )
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _cached_icon(record: SiteIconCacheRecord | None) -> SiteIcon | None:
    if record is None:
        return None
    data = record.icon_bytes
    media_type = record.media_type
    if data is None or media_type is None:
        return None
    return SiteIcon(data, media_type)


class SiteIconService:
    """TTL policy, per-origin singleflight, and bounded background refresh."""

    def __init__(
        self,
        *,
        cache: SiteIconCache,
        fetcher: SiteIconFetcher = fetch_site_icon,
        clock: Clock = _utc_now,
    ) -> None:
        self._cache = cache
        self._fetcher = fetcher
        self._clock = clock
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_ORIGINS)
        self._inflight: dict[str, asyncio.Task[SiteIcon | None]] = {}
        self._lookups: dict[str, asyncio.Task[SiteIcon | None]] = {}

    async def get(self, url: str) -> SiteIcon | None:
        origin = normalize_site_origin(url)
        # Cache reads now await a worker. Share that lookup before yielding so
        # the first caller still chooses the URL for this origin's singleflight.
        task = self._lookups.get(origin)
        if task is None:
            task = asyncio.create_task(self._get(origin, url))
            self._lookups[origin] = task
            task.add_done_callback(
                lambda completed, key=origin: self._lookup_done(key, completed)
            )
        return await task

    async def _get(self, origin: str, url: str) -> SiteIcon | None:
        cached = await self._cache.read(origin)
        icon = _cached_icon(cached)
        if cached is not None and cached.retry_after > self._clock():
            return icon

        task = self._refresh_task(origin, url)
        if icon is not None:
            return icon
        return await task

    async def refresh(self, url: str) -> bool:
        """Bypass TTL, sharing an already-running refresh for this origin."""
        origin = normalize_site_origin(url)
        return await self._refresh_task(origin, url) is not None

    async def wait_for_idle(self) -> None:
        """Wait for scheduled stale refreshes, primarily for clean shutdown/tests."""
        while self._inflight or self._lookups:
            await asyncio.gather(
                *tuple(self._inflight.values()),
                *tuple(self._lookups.values()),
                return_exceptions=True,
            )
            # A gather over tasks that are already done may complete without
            # yielding; give their removal callbacks one loop turn.
            await asyncio.sleep(0)

    def _refresh_task(
        self,
        origin: str,
        url: str,
    ) -> asyncio.Task[SiteIcon | None]:
        existing = self._inflight.get(origin)
        if existing is not None:
            return existing

        task = asyncio.create_task(self._refresh(origin, url))
        self._inflight[origin] = task
        task.add_done_callback(
            lambda completed, key=origin: self._refresh_done(key, completed)
        )
        return task

    async def _refresh(self, origin: str, url: str) -> SiteIcon | None:
        async with self._semaphore:
            try:
                fetched = await self._fetcher(url)
            except Exception:
                logger.warning("Unexpected site icon fetch failure.", exc_info=True)
                fetched = None

        now = self._clock()
        icon = (
            None if fetched is None else validate_icon(fetched.media_type, fetched.data)
        )
        if icon is None:
            await self._cache.failure(origin, now + NEGATIVE_TTL)
            return None

        await self._cache.success(
            origin,
            icon,
            now,
            now + POSITIVE_TTL,
        )
        return icon

    def _refresh_done(
        self,
        origin: str,
        completed: asyncio.Task[SiteIcon | None],
    ) -> None:
        if self._inflight.get(origin) is completed:
            del self._inflight[origin]
        if not completed.cancelled():
            completed.exception()

    def _lookup_done(
        self,
        origin: str,
        completed: asyncio.Task[SiteIcon | None],
    ) -> None:
        if self._lookups.get(origin) is completed:
            del self._lookups[origin]
        if not completed.cancelled():
            completed.exception()


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
                    if not await resolves_to_public_host(url):
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
    if not await resolves_to_public_host(YOUTUBE_OEMBED_URL):
        return None

    try:
        async with session.get(
            YOUTUBE_OEMBED_URL,
            params={"format": "json", "url": url},
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                return None
            body = await read_bounded_body(response, MAX_OEMBED_BYTES)
    except ClientError:
        return None

    if body is None:
        return None
    try:
        payload = cast(object, json.loads(body))
    except ValueError, RecursionError:
        # JSON syntax, Unicode decoding, integer limits and excessive nesting
        # are all failures of this bounded untrusted document.
        return None
    if not isinstance(payload, dict):
        return None

    title = cast(dict[object, object], payload).get("title")
    if not isinstance(title, str):
        return None
    return clean_title_text(title)


async def _read_response_title(
    response: ClientResponse,
    charset: str,
) -> str | None:
    try:
        # bytes.decode rejects binary and str-to-str codecs advertised as an
        # HTTP charset, before their incremental decoders can assert or mis-type.
        b"\0".decode(charset, errors="replace")
        decoder = codecs.getincrementaldecoder(charset)(errors="replace")
    except LookupError:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    parser = TitleParser()
    remaining = MAX_TITLE_BYTES
    while remaining > 0:
        chunk = await response.content.read(min(TITLE_READ_CHUNK_BYTES, remaining))
        if not chunk:
            try:
                parser.feed(decoder.decode(b"", final=True))
                parser.close()
            except ValueError:
                # UnicodeDecodeError and malformed numeric entities are remote
                # content failures. Do not contain network/repository anomalies.
                return None
            return parser.title()

        remaining -= len(chunk)
        try:
            parser.feed(decoder.decode(chunk))
        except ValueError:
            return None
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


def parse_title(body: bytes, charset: str = "utf-8") -> str | None:
    try:
        try:
            html = body.decode(charset, errors="replace")
        except LookupError:
            html = body.decode("utf-8", errors="replace")

        parser = TitleParser()
        parser.feed(html)
        return parser.title()
    except ValueError:
        return None
