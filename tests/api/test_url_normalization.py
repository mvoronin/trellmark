import pytest

from trellmark.bookmarks.domain import normalize_url


def test_normalize_url_adds_https_scheme():
    assert normalize_url("example.com") == "https://example.com"


def test_normalize_url_accepts_http_and_https():
    assert normalize_url("http://example.com/path") == "http://example.com/path"
    assert normalize_url("https://example.com/path") == "https://example.com/path"


def test_normalize_url_canonicalizes_host_and_root_slash():
    assert normalize_url("https://EXAMPLE.com/") == "https://example.com"
    assert normalize_url("HTTPS://Example.Com") == "https://example.com"


def test_normalize_url_preserves_non_root_trailing_slash():
    # /docs and /docs/ can be distinct resources, so the deeper slash stays.
    assert normalize_url("https://example.com/docs/") == "https://example.com/docs/"
    assert normalize_url("https://example.com/docs") == "https://example.com/docs"


@pytest.mark.parametrize(
    "value", ["", "   ", "ftp://example.com", "not a url", "example.com\nx"]
)
def test_normalize_url_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        normalize_url(value)
