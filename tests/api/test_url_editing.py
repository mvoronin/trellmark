import pytest

import trellmark
from tests.helpers import RecordingTitleFetcher, http_json


class ConcurrentEditTitleFetcher:
    def __init__(self):
        self.url_id = None

    async def __call__(self, url):
        assert self.url_id is not None
        current = trellmark.read_url_record_by_id(self.url_id)
        assert current is not None
        updated, error = trellmark.update_url_record(
            self.url_id,
            expected_version=current["version"],
            fields={"title": "Manual title"},
        )
        assert error is None
        assert updated is not None
        return "Fetched title"


class ConcurrentImportantTitleFetcher:
    def __init__(self):
        self.url_id = None

    async def __call__(self, url):
        assert self.url_id is not None
        updated = trellmark.set_url_important(self.url_id, True)
        assert updated is not None
        return "Fetched title"


def test_patch_url_updates_normalized_url_and_title_without_moving_groups(app):
    base_url, _ = app
    reading = trellmark.add_group("Reading")
    record = trellmark.add_url("https://old.example", title="Old title")
    assert reading is not None
    assert record is not None
    trellmark.move_url_to_group(record["id"], reading["id"])

    status, payload = http_json(
        base_url,
        f"/api/urls/{record['id']}",
        method="PATCH",
        payload={
            "url": "NEW.example/",
            "title": "  New   title  ",
            "version": record["version"],
        },
    )

    assert status == 200
    assert payload["url"]["url"] == "https://new.example"
    assert payload["url"]["title"] == "New title"
    assert trellmark.read_url_group_ids(record["id"]) == [reading["id"]]
    assert trellmark.read_url_record_by_id(record["id"]) == payload["url"]


def test_patch_url_can_clear_title_without_changing_url(app):
    base_url, _ = app
    record = trellmark.add_url("https://example.com", title="Example")
    assert record is not None

    status, payload = http_json(
        base_url,
        f"/api/urls/{record['id']}",
        method="PATCH",
        payload={"title": "   ", "version": record["version"]},
    )

    assert status == 200
    assert payload["url"]["url"] == "https://example.com"
    assert payload["url"]["title"] is None


def test_patch_url_rejects_duplicate_without_changing_record(app):
    base_url, _ = app
    first = trellmark.add_url("https://one.example", title="One")
    second = trellmark.add_url("https://two.example", title="Two")
    assert first is not None
    assert second is not None

    status, payload = http_json(
        base_url,
        f"/api/urls/{second['id']}",
        method="PATCH",
        payload={
            "url": "ONE.example",
            "title": "Changed",
            "version": second["version"],
        },
    )

    assert status == 409
    assert payload == {"error": "This URL is already saved."}
    assert trellmark.read_url_record_by_id(second["id"]) == second


@pytest.mark.parametrize("url", ["", "ftp://example.com", "not a url"])
def test_patch_url_rejects_invalid_url(app, url):
    base_url, _ = app
    record = trellmark.add_url("https://example.com")
    assert record is not None

    status, payload = http_json(
        base_url,
        f"/api/urls/{record['id']}",
        method="PATCH",
        payload={"url": url, "version": record["version"]},
    )

    assert status == 400
    assert "error" in payload
    assert trellmark.read_url_record_by_id(record["id"]) == record


def test_patch_missing_url_returns_not_found(app):
    base_url, _ = app

    status, payload = http_json(
        base_url,
        "/api/urls/999",
        method="PATCH",
        payload={"title": "Missing", "version": 1},
    )

    assert status == 404
    assert payload == {"error": "This URL is not saved."}


def test_patch_url_rejects_null_url_with_specific_error(app):
    base_url, _ = app
    record = trellmark.add_url("https://example.com")
    assert record is not None

    status, payload = http_json(
        base_url,
        f"/api/urls/{record['id']}",
        method="PATCH",
        payload={"url": None, "version": record["version"]},
    )

    assert status == 400
    assert payload == {"error": "Enter a URL."}
    assert trellmark.read_url_record_by_id(record["id"]) == record


def test_patch_url_rejects_stale_version_without_reverting_newer_change(app):
    base_url, _ = app
    record = trellmark.add_url("https://example.com", title="Old title")
    assert record is not None

    status, first_payload = http_json(
        base_url,
        f"/api/urls/{record['id']}",
        method="PATCH",
        payload={"title": "New title", "version": record["version"]},
    )
    assert status == 200
    assert first_payload["url"]["version"] == record["version"] + 1

    status, payload = http_json(
        base_url,
        f"/api/urls/{record['id']}",
        method="PATCH",
        payload={"url": "changed.example", "version": record["version"]},
    )

    assert status == 409
    assert payload == {"error": "This URL was changed. Reload and try again."}
    stored = trellmark.read_url_record_by_id(record["id"])
    assert stored is not None
    assert stored["url"] == "https://example.com"
    assert stored["title"] == "New title"
    assert stored["version"] == record["version"] + 1


def test_patch_url_accepts_original_version_after_important_change(app):
    base_url, _ = app
    record = trellmark.add_url("https://example.com", title="Old title")
    assert record is not None
    updated = trellmark.set_url_important(record["id"], True)
    assert updated is not None
    assert updated["version"] == record["version"]

    status, payload = http_json(
        base_url,
        f"/api/urls/{record['id']}",
        method="PATCH",
        payload={"title": "New title", "version": record["version"]},
    )

    assert status == 200
    assert payload["url"]["title"] == "New title"
    assert payload["url"]["important"] is True
    assert payload["url"]["version"] == record["version"] + 1


def test_update_url_record_rejects_empty_fields(app):
    record = trellmark.add_url("https://example.com")
    assert record is not None

    with pytest.raises(ValueError, match="At least one URL field is required"):
        trellmark.update_url_record(
            record["id"],
            expected_version=record["version"],
            fields={},
        )

    assert trellmark.read_url_record_by_id(record["id"]) == record


@pytest.mark.parametrize(
    "title_fetcher",
    [RecordingTitleFetcher("Fresh title")],
    indirect=True,
)
def test_refresh_url_title_fetches_and_stores_new_title(app, title_fetcher):
    base_url, _ = app
    record = trellmark.add_url("https://example.com", title="Old title")
    assert record is not None

    status, payload = http_json(
        base_url,
        f"/api/urls/{record['id']}/refresh-title",
        method="POST",
    )

    assert status == 200
    assert title_fetcher.calls == ["https://example.com"]
    assert payload["title_updated"] is True
    assert payload["url"]["title"] == "Fresh title"
    assert payload["groups"][0]["urls"][0]["title"] == "Fresh title"
    assert trellmark.read_url_record_by_id(record["id"])["title"] == "Fresh title"


@pytest.mark.parametrize(
    "title_fetcher",
    [RecordingTitleFetcher(error=RuntimeError("network unavailable"))],
    indirect=True,
)
def test_refresh_url_title_reports_failure_and_keeps_existing_title(
    app,
    title_fetcher,
):
    base_url, _ = app
    record = trellmark.add_url("https://example.com", title="Existing title")
    assert record is not None

    status, payload = http_json(
        base_url,
        f"/api/urls/{record['id']}/refresh-title",
        method="POST",
    )

    assert status == 502
    assert payload == {"error": "Could not fetch a page title."}
    assert title_fetcher.calls == ["https://example.com"]
    assert trellmark.read_url_record_by_id(record["id"])["title"] == "Existing title"


def test_refresh_url_title_reports_no_title_without_treating_it_as_failure(app):
    base_url, _ = app
    record = trellmark.add_url("https://example.com", title="Existing title")
    assert record is not None

    status, payload = http_json(
        base_url,
        f"/api/urls/{record['id']}/refresh-title",
        method="POST",
    )

    assert status == 200
    assert payload["title_updated"] is False
    assert payload["url"]["title"] == "Existing title"
    assert trellmark.read_url_record_by_id(record["id"])["title"] == "Existing title"


@pytest.mark.parametrize(
    "title_fetcher",
    [ConcurrentEditTitleFetcher()],
    indirect=True,
)
def test_refresh_url_title_rejects_concurrent_edit(app, title_fetcher):
    base_url, _ = app
    record = trellmark.add_url("https://example.com", title="Old title")
    assert record is not None
    title_fetcher.url_id = record["id"]

    status, payload = http_json(
        base_url,
        f"/api/urls/{record['id']}/refresh-title",
        method="POST",
    )

    assert status == 409
    assert payload == {"error": "This URL was changed. Reload and try again."}
    stored = trellmark.read_url_record_by_id(record["id"])
    assert stored is not None
    assert stored["title"] == "Manual title"
    assert stored["version"] == record["version"] + 1


@pytest.mark.parametrize(
    "title_fetcher",
    [ConcurrentImportantTitleFetcher()],
    indirect=True,
)
def test_refresh_url_title_allows_concurrent_important_change(app, title_fetcher):
    base_url, _ = app
    record = trellmark.add_url("https://example.com", title="Old title")
    assert record is not None
    title_fetcher.url_id = record["id"]

    status, payload = http_json(
        base_url,
        f"/api/urls/{record['id']}/refresh-title",
        method="POST",
    )

    assert status == 200
    assert payload["title_updated"] is True
    assert payload["url"]["title"] == "Fetched title"
    assert payload["url"]["important"] is True
    assert payload["url"]["version"] == record["version"] + 1


@pytest.mark.parametrize(
    "title_fetcher",
    [RecordingTitleFetcher("Should not fetch")],
    indirect=True,
)
def test_refresh_missing_url_returns_not_found_without_fetching(app, title_fetcher):
    base_url, _ = app

    status, payload = http_json(
        base_url,
        "/api/urls/999/refresh-title",
        method="POST",
    )

    assert status == 404
    assert payload == {"error": "This URL is not saved."}
    assert title_fetcher.calls == []
