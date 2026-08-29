from datetime import datetime

import pytest

import trellmark
from tests.helpers import (
    RecordingTitleFetcher,
    assert_validation_error,
    grouped_url_ids_in,
    grouped_urls_in,
    http_json,
    urls_in,
)


@pytest.mark.parametrize("value", [None, 42, ["https://example.com"], {"x": 1}])
def test_post_rejects_non_string_url(app, value):
    base_url, _ = app

    status, payload = http_json(
        base_url, "/api/urls", method="POST", payload={"url": value}
    )

    assert_validation_error(status, payload)
    assert trellmark.read_urls() == []


def test_post_url_saves_normalized_url(app):
    base_url, _ = app

    status, payload = http_json(
        base_url, "/api/urls", method="POST", payload={"url": "example.com"}
    )

    assert status == 201
    assert payload["url"]["id"] == 1
    assert payload["url"]["url"] == "https://example.com"
    assert payload["url"]["title"] is None
    assert payload["url"]["created_at"].endswith("Z")
    assert urls_in(payload) == ["https://example.com"]
    assert trellmark.read_urls() == ["https://example.com"]


@pytest.mark.parametrize(
    "title_fetcher",
    [RecordingTitleFetcher("Example Domain")],
    indirect=True,
)
def test_post_url_fetches_and_stores_title(app, title_fetcher):
    base_url, _ = app

    status, payload = http_json(
        base_url, "/api/urls", method="POST", payload={"url": "example.com"}
    )

    assert status == 201
    assert title_fetcher.calls == ["https://example.com"]
    assert payload["url"]["title"] == "Example Domain"
    assert payload["groups"][0]["urls"][0]["title"] == "Example Domain"
    assert trellmark.read_url_records()[0]["title"] == "Example Domain"


@pytest.mark.parametrize(
    "title_fetcher",
    [RecordingTitleFetcher(error=RuntimeError("network unavailable"))],
    indirect=True,
)
def test_post_url_keeps_saved_url_when_title_fetch_fails(app, title_fetcher):
    base_url, _ = app

    status, payload = http_json(
        base_url, "/api/urls", method="POST", payload={"url": "example.com"}
    )

    assert status == 201
    assert title_fetcher.calls == ["https://example.com"]
    assert payload["url"]["title"] is None
    assert trellmark.read_urls() == ["https://example.com"]


def test_post_url_returns_grouped_urls_with_new_url_in_default(app):
    base_url, _ = app

    status, payload = http_json(
        base_url, "/api/urls", method="POST", payload={"url": "example.com"}
    )

    assert status == 201
    assert payload["groups"][0]["name"] == "default"
    assert payload["groups"][0]["position"] == 0
    assert grouped_url_ids_in(payload, "default") == [1]
    assert grouped_urls_in(payload, "default") == ["https://example.com"]
    created_at = payload["groups"][0]["urls"][0]["created_at"]
    assert created_at.endswith("Z")
    assert datetime.fromisoformat(created_at.replace("Z", "+00:00"))


def test_post_url_accepts_form_encoded_requests(app):
    base_url, _ = app

    status, payload = http_json(
        base_url,
        "/api/urls",
        method="POST",
        payload={"url": "https://form.example"},
        content_type="application/x-www-form-urlencoded",
    )

    assert status == 201
    assert urls_in(payload) == ["https://form.example"]
    assert trellmark.read_urls() == ["https://form.example"]


def test_post_duplicate_url_returns_conflict(app):
    base_url, _ = app
    trellmark.add_url("https://example.com")

    status, payload = http_json(
        base_url, "/api/urls", method="POST", payload={"url": "example.com"}
    )

    assert status == 409
    assert payload == {"error": "This URL is already saved."}
    assert trellmark.read_urls() == ["https://example.com"]


@pytest.mark.parametrize(
    "title_fetcher",
    [RecordingTitleFetcher("Should Not Fetch")],
    indirect=True,
)
def test_post_duplicate_url_does_not_fetch_title(app, title_fetcher):
    base_url, _ = app
    trellmark.add_url("https://example.com")

    status, payload = http_json(
        base_url, "/api/urls", method="POST", payload={"url": "example.com"}
    )

    assert status == 409
    assert payload == {"error": "This URL is already saved."}
    assert title_fetcher.calls == []


def test_post_invalid_url_returns_bad_request(app):
    base_url, _ = app

    status, payload = http_json(
        base_url, "/api/urls", method="POST", payload={"url": "ftp://example.com"}
    )

    assert status == 400
    assert payload == {"error": "Enter a valid http or https URL."}
    assert trellmark.read_urls() == []


def test_delete_urls_collection_method_is_not_allowed(app):
    base_url, _ = app
    trellmark.add_url("https://one.example")

    status, payload = http_json(
        base_url, "/api/urls", method="DELETE", payload={"url": "one.example"}
    )

    assert status == 405
    assert payload == {"detail": "Method Not Allowed"}
    assert trellmark.read_urls() == ["https://one.example"]


def test_delete_url_by_id_removes_saved_url(app):
    base_url, _ = app
    trellmark.add_url("https://one.example")
    trellmark.add_url("https://two.example")

    status, payload = http_json(base_url, "/api/urls/1?group_id=1", method="DELETE")

    assert status == 200
    assert payload["url"]["id"] == 1
    assert payload["url"]["url"] == "https://one.example"
    assert urls_in(payload) == ["https://two.example"]
    assert trellmark.read_urls() == ["https://two.example"]


def test_delete_missing_url_id_returns_not_found(app):
    base_url, _ = app
    trellmark.add_url("https://one.example")

    status, payload = http_json(base_url, "/api/urls/999?group_id=1", method="DELETE")

    assert status == 404
    assert payload == {"error": "This URL is not saved."}
    assert trellmark.read_urls() == ["https://one.example"]


def test_delete_url_by_id_requires_group_id(app):
    base_url, _ = app
    trellmark.add_url("https://one.example")

    status, payload = http_json(base_url, "/api/urls/1", method="DELETE")

    assert_validation_error(status, payload)
    assert trellmark.read_urls() == ["https://one.example"]
