"""Product-neutral public-address checks and bounded response-body reading."""

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

from aiohttp import ClientResponse

READ_CHUNK_BYTES = 65_536
type IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


async def resolves_to_public_host(url: str) -> bool:
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
        if not is_public_address(address):
            return False

    # aiohttp resolves again when connecting, so this intentionally does not
    # close DNS-rebinding TOCTOU. That is acceptable for this personal app.
    return True


def is_public_address(address: IPAddress) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_global and not address.is_multicast


async def read_bounded_body(
    response: ClientResponse,
    limit: int,
) -> bytes | None:
    body = bytearray()
    while len(body) <= limit:
        remaining = limit + 1 - len(body)
        chunk = await response.content.read(min(READ_CHUNK_BYTES, remaining))
        if not chunk:
            return bytes(body)
        body.extend(chunk)
    return None
