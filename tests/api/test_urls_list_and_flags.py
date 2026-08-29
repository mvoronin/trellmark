from datetime import datetime

import pytest

import trellmark
from tests.helpers import (
    assert_validation_error,
    http_json,
    url_ids_in,
    urls_in,
)


def test_get_urls_returns_saved_urls(app):
    base_url, _ = app
    trellmark.add_url("https://one.example")
    trellmark.add_url("https://two.example")

    status, payload = http_json(base_url, "/api/urls")

    assert status == 200
    assert urls_in(payload) == ["https://one.example", "https://two.example"]
    assert url_ids_in(payload) == [1, 2]
    assert all("created_at" in record for record in payload["urls"])


def test_read_url_records_includes_url_id(app):
    trellmark.add_url("https://one.example")

    records = trellmark.read_url_records()

    assert records[0]["id"] == 1
    assert records[0]["url"] == "https://one.example"
    assert records[0]["title"] is None
    assert records[0]["created_at"].endswith("Z")
    assert records[0]["important"] is False
    assert records[0]["version"] == 1


def test_read_group_records_includes_nested_url_id(app):
    trellmark.add_url("https://one.example")

    groups = trellmark.read_group_records()

    assert groups[0]["urls"][0]["id"] == 1
    assert groups[0]["urls"][0]["url"] == "https://one.example"
    assert groups[0]["urls"][0]["title"] is None
    assert groups[0]["urls"][0]["important"] is False
    assert groups[0]["urls"][0]["version"] == 1


def test_set_url_important_updates_flat_and_grouped_records(app):
    record = trellmark.add_url("https://one.example")

    updated = trellmark.set_url_important(record["id"], True)

    assert updated["important"] is True
    assert updated["version"] == record["version"]
    assert trellmark.read_url_records()[0]["important"] is True
    assert trellmark.read_group_records()[0]["urls"][0]["important"] is True

    updated = trellmark.set_url_important(record["id"], False)

    assert updated["important"] is False
    assert updated["version"] == record["version"]
    assert trellmark.read_url_records()[0]["important"] is False
    assert trellmark.read_group_records()[0]["urls"][0]["important"] is False


def test_set_url_important_returns_none_for_missing_id(app):
    assert trellmark.set_url_important(999, True) is None


def test_non_content_metadata_changes_do_not_increment_url_version(app):
    record = trellmark.add_url("https://one.example")

    trellmark.update_url_created_at(record["id"], "2026-08-02 12:00:00")
    trellmark.set_url_important(record["id"], True)

    updated = trellmark.read_url_record_by_id(record["id"])
    assert updated["version"] == record["version"]


def test_get_urls_includes_iso_utc_timestamp(app):
    base_url, _ = app
    trellmark.add_url("https://one.example")

    _, payload = http_json(base_url, "/api/urls")

    created_at = payload["urls"][0]["created_at"]
    assert created_at.endswith("Z")
    # Parseable ISO 8601 UTC, e.g. "2026-06-14T12:34:56Z".
    assert datetime.fromisoformat(created_at.replace("Z", "+00:00"))


def test_patch_url_important_sets_and_clears_flag(app):
    base_url, _ = app
    url = trellmark.add_url("https://one.example")

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
    assert trellmark.read_url_record_by_id(url["id"])["important"] is True

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
    assert trellmark.read_url_record_by_id(url["id"])["important"] is False


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
    url = trellmark.add_url("https://one.example")

    status, response = http_json(
        base_url,
        f"/api/urls/{url['id']}/important",
        method="PATCH",
        payload=payload,
    )

    assert_validation_error(status, response)
    assert trellmark.read_url_record_by_id(url["id"])["important"] is False
