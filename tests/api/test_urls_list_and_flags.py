from dataclasses import asdict
from datetime import datetime

import pytest

from tests.bookmarks import helpers as bookmark_helpers
from tests.bookmarks.helpers import url_payload, url_payloads
from tests.helpers import (
    assert_validation_error,
    http_json,
    run_async,
    url_ids_in,
    urls_in,
)
from trellmark.bookmarks.domain import SetImportant, SetImportantSucceeded, URLNotFound


def test_get_urls_returns_saved_urls(app):
    base_url, _ = app
    bookmark_helpers.seed_url("https://one.example")
    bookmark_helpers.seed_url("https://two.example")

    status, payload = http_json(base_url, "/api/urls")

    assert status == 200
    assert urls_in(payload) == ["https://one.example", "https://two.example"]
    assert url_ids_in(payload) == [1, 2]
    assert all("created_at" in record for record in payload["urls"])


def test_read_url_records_includes_url_id(app):
    bookmark_helpers.seed_url("https://one.example")

    records = url_payloads()

    assert records[0]["id"] == 1
    assert records[0]["url"] == "https://one.example"
    assert records[0]["title"] is None
    assert records[0]["created_at"].endswith("Z")
    assert records[0]["important"] is False
    assert records[0]["version"] == 1


def test_read_group_records_includes_nested_url_id(app):
    bookmark_helpers.seed_url("https://one.example")

    groups = bookmark_helpers.group_payloads()

    assert groups[0]["urls"][0]["id"] == 1
    assert groups[0]["urls"][0]["url"] == "https://one.example"
    assert groups[0]["urls"][0]["title"] is None
    assert groups[0]["urls"][0]["important"] is False
    assert groups[0]["urls"][0]["version"] == 1


def test_set_url_important_updates_flat_and_grouped_records(app, bookmarks_service):
    record = bookmark_helpers.seed_url("https://one.example")

    outcome = run_async(
        lambda: bookmarks_service.set_important(SetImportant(record["id"], True))
    )
    assert isinstance(outcome, SetImportantSucceeded)
    updated = asdict(outcome.record)

    assert updated["important"] is True
    assert updated["version"] == record["version"]
    assert url_payloads()[0]["important"] is True
    assert bookmark_helpers.group_payloads()[0]["urls"][0]["important"] is True

    outcome = run_async(
        lambda: bookmarks_service.set_important(SetImportant(record["id"], False))
    )
    assert isinstance(outcome, SetImportantSucceeded)
    updated = asdict(outcome.record)

    assert updated["important"] is False
    assert updated["version"] == record["version"]
    assert url_payloads()[0]["important"] is False
    assert bookmark_helpers.group_payloads()[0]["urls"][0]["important"] is False


def test_set_url_important_returns_not_found_for_missing_id(app, bookmarks_service):
    assert run_async(
        lambda: bookmarks_service.set_important(SetImportant(999, True))
    ) == URLNotFound(999)


def test_non_content_metadata_changes_do_not_increment_url_version(
    app, bookmarks_service
):
    record = bookmark_helpers.seed_url("https://one.example")

    bookmark_helpers.seed_created_at(record["id"], "2026-08-02 12:00:00")
    outcome = run_async(
        lambda: bookmarks_service.set_important(SetImportant(record["id"], True))
    )
    assert isinstance(outcome, SetImportantSucceeded)

    updated = url_payload(record["id"])
    assert updated["version"] == record["version"]


def test_get_urls_includes_iso_utc_timestamp(app):
    base_url, _ = app
    bookmark_helpers.seed_url("https://one.example")

    _, payload = http_json(base_url, "/api/urls")

    created_at = payload["urls"][0]["created_at"]
    assert created_at.endswith("Z")
    # Parseable ISO 8601 UTC, e.g. "2026-06-14T12:34:56Z".
    assert datetime.fromisoformat(created_at.replace("Z", "+00:00"))


def test_patch_url_important_sets_and_clears_flag(app):
    base_url, _ = app
    url = bookmark_helpers.seed_url("https://one.example")

    status, payload = http_json(
        base_url,
        f"/api/urls/{url['id']}/important",
        method="PATCH",
        payload={"important": True},
    )

    assert status == 200
    assert payload["url"]["id"] == url["id"]
    assert payload["url"]["important"] is True
    assert payload["groups"][0]["urls"][0]["important"] is True
    assert url_payload(url["id"])["important"] is True

    status, payload = http_json(
        base_url,
        f"/api/urls/{url['id']}/important",
        method="PATCH",
        payload={"important": False},
    )

    assert status == 200
    assert payload["url"]["id"] == url["id"]
    assert payload["url"]["important"] is False
    assert payload["groups"][0]["urls"][0]["important"] is False
    assert url_payload(url["id"])["important"] is False


def test_patch_url_important_returns_not_found_for_missing_url(app):
    base_url, _ = app

    status, payload = http_json(
        base_url,
        "/api/urls/999/important",
        method="PATCH",
        payload={"important": True},
    )

    assert status == 404
    assert payload == {"error": "This URL is not saved."}


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"important": ""},
        {"important": "true"},
        {"important": 1},
        {"important": None},
    ],
)
def test_patch_url_important_rejects_invalid_payload(app, payload):
    base_url, _ = app
    url = bookmark_helpers.seed_url("https://one.example")

    status, response = http_json(
        base_url,
        f"/api/urls/{url['id']}/important",
        method="PATCH",
        payload=payload,
    )

    assert_validation_error(status, response)
    assert url_payload(url["id"])["important"] is False
