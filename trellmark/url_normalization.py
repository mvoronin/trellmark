import ipaddress
import re
from collections.abc import Sequence
from urllib.parse import urlparse, urlunparse

DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def normalize_url(value: str) -> str:
    url = value.strip()
    if not url:
        raise ValueError("Enter a URL.")

    if "\n" in url or "\r" in url:
        raise ValueError("URL cannot contain line breaks.")

    if any(char.isspace() for char in url):
        raise ValueError("URL cannot contain spaces.")

    if "://" not in url:
        url = f"https://{url}"

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Enter a valid http or https URL.")

    # Canonicalize so equivalent URLs dedupe: scheme and host are
    # case-insensitive, and a bare "/" path is equivalent to no path. A
    # trailing slash deeper in the path (e.g. /docs/ vs /docs) is left alone,
    # since those can be distinct resources.
    path = "" if parsed.path == "/" else parsed.path
    return urlunparse(
        parsed._replace(
            scheme=parsed.scheme.lower(),
            netloc=parsed.netloc.lower(),
            path=path,
        )
    )


def normalize_domain(value: str) -> str:
    """Return a canonical hostname suitable for exact URL matching."""
    domain = value.strip().rstrip(".").lower()
    if not domain or any(char.isspace() for char in domain):
        raise ValueError("Enter valid domains.")

    try:
        return str(ipaddress.ip_address(domain))
    except ValueError:
        pass

    if any(char in domain for char in "/:@?#"):
        raise ValueError("Enter valid domains.")

    try:
        ascii_domain = domain.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise ValueError("Enter valid domains.") from error

    if len(ascii_domain) > 253 or any(
        not DOMAIN_LABEL.fullmatch(label) for label in ascii_domain.split(".")
    ):
        raise ValueError("Enter valid domains.")
    return ascii_domain


def normalize_domains(values: Sequence[str]) -> list[str]:
    """Normalize, deduplicate, and sort domain rules."""
    return sorted({normalize_domain(value) for value in values})


def domain_for_url(url: str) -> str | None:
    """Return a matchable URL hostname, or None for a non-domain hostname.

    URL validation deliberately accepts a wider set of HTTP host strings than
    group domain rules. Those URLs remain saveable and simply fall back to the
    default group.
    """
    hostname = urlparse(url).hostname
    if hostname is None:
        raise ValueError("Enter a valid http or https URL.")
    try:
        return normalize_domain(hostname)
    except ValueError:
        return None
