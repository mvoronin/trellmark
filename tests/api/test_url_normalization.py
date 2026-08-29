import pytest

import trellmark


def test_normalize_url_adds_https_scheme():
    assert trellmark.normalize_url("example.com") == "https://example.com"


def test_normalize_url_accepts_http_and_https():
    assert (
        trellmark.normalize_url("http://example.com/path") == "http://example.com/path"
    )
    assert (
        trellmark.normalize_url("https://example.com/path")
        == "https://example.com/path"
    )


def test_normalize_url_canonicalizes_host_and_root_slash():
    assert trellmark.normalize_url("https://EXAMPLE.com/") == "https://example.com"
    assert trellmark.normalize_url("HTTPS://Example.Com") == "https://example.com"


def test_normalize_url_preserves_non_root_trailing_slash():
    # /docs and /docs/ can be distinct resources, so the deeper slash stays.
    assert (
        trellmark.normalize_url("https://example.com/docs/")
        == "https://example.com/docs/"
    )
    assert (
        trellmark.normalize_url("https://example.com/docs")
        == "https://example.com/docs"
    )


@pytest.mark.parametrize(
    "value", ["", "   ", "ftp://example.com", "not a url", "example.com\nx"]
)
def test_normalize_url_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        trellmark.normalize_url(value)
