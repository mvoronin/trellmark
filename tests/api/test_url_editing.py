from dataclasses import asdict

import pytest

from tests.bookmarks import helpers as bookmark_helpers
from tests.bookmarks.helpers import seed_url, url_group_ids, url_payload
from tests.helpers import RecordingTitleFetcher, http_json, run_async
from trellmark.bookmarks.domain import (
    EditURL,
    EmptyURLEdit,
    MoveURL,
    SetImportant,
    SetImportantSucceeded,
    URLUpdated,
)


class ConcurrentEditTitleFetcher:
    def __init__(self):
        self.url_id = None
        self.service = None

    async def __call__(self, url):
        assert self.url_id is not None
        assert self.service is not None
        current = await self.service.url_by_id(self.url_id)
        assert current is not None
        outcome = await self.service.edit_url(
            EditURL(self.url_id, current.version, title="Manual title")
        )
        assert isinstance(outcome, URLUpdated)
        return "Fetched title"


class ConcurrentImportantTitleFetcher:
    def __init__(self):
        self.url_id = None
        self.service = None

    async def __call__(self, url):
        assert self.url_id is not None
        assert self.service is not None
        outcome = await self.service.set_important(SetImportant(self.url_id, True))
        assert isinstance(outcome, SetImportantSucceeded)
        return "Fetched title"


def test_patch_url_updates_normalized_url_and_title_without_moving_groups(
    app, bookmarks_service
):
    base_url, _ = app
    reading = bookmark_helpers.seed_group("Reading")
    record = seed_url("https://old.example", title="Old title")
    assert reading is not None
    assert record is not None
    run_async(lambda: bookmarks_service.move_url(MoveURL(record["id"], reading["id"])))

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
    assert url_group_ids(record["id"]) == [reading["id"]]
    assert url_payload(record["id"]) == payload["url"]


def test_patch_url_can_clear_title_without_changing_url(app):
    base_url, _ = app
    record = seed_url("https://example.com", title="Example")
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
    first = seed_url("https://one.example", title="One")
    second = seed_url("https://two.example", title="Two")
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
    assert url_payload(second["id"]) == second


@pytest.mark.parametrize("url", ["", "ftp://example.com", "not a url"])
def test_patch_url_rejects_invalid_url(app, url):
    base_url, _ = app
    record = seed_url("https://example.com")
    assert record is not None

    status, payload = http_json(
        base_url,
        f"/api/urls/{record['id']}",
        method="PATCH",
        payload={"url": url, "version": record["version"]},
    )

    assert status == 400
    assert "error" in payload
    assert url_payload(record["id"]) == record


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
    record = seed_url("https://example.com")
    assert record is not None

    status, payload = http_json(
        base_url,
        f"/api/urls/{record['id']}",
        method="PATCH",
        payload={"url": None, "version": record["version"]},
    )

    assert status == 400
    assert payload == {"error": "Enter a URL."}
    assert url_payload(record["id"]) == record


def test_patch_url_rejects_stale_version_without_reverting_newer_change(app):
    base_url, _ = app
    record = seed_url("https://example.com", title="Old title")
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
    stored = url_payload(record["id"])
    assert stored is not None
    assert stored["url"] == "https://example.com"
    assert stored["title"] == "New title"
    assert stored["version"] == record["version"] + 1


def test_patch_url_accepts_original_version_after_important_change(
    app, bookmarks_service
):
    base_url, _ = app
    record = seed_url("https://example.com", title="Old title")
    assert record is not None
    outcome = run_async(
        lambda: bookmarks_service.set_important(SetImportant(record["id"], True))
    )
    assert isinstance(outcome, SetImportantSucceeded)
    updated = asdict(outcome.record)
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


def test_edit_url_service_rejects_empty_fields(app, bookmarks_service):
    record = seed_url("https://example.com")
    assert record is not None

    outcome = run_async(
        lambda: bookmarks_service.edit_url(EditURL(record["id"], record["version"]))
    )
    assert outcome == EmptyURLEdit(record["id"])

    assert url_payload(record["id"]) == record


@pytest.mark.parametrize(
    "title_fetcher",
    [RecordingTitleFetcher("Fresh title")],
    indirect=True,
)
def test_refresh_url_title_fetches_and_stores_new_title(app, title_fetcher):
    base_url, _ = app
    record = seed_url("https://example.com", title="Old title")
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
    assert url_payload(record["id"])["title"] == "Fresh title"


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
    record = seed_url("https://example.com", title="Existing title")
    assert record is not None

    status, payload = http_json(
        base_url,
        f"/api/urls/{record['id']}/refresh-title",
        method="POST",
    )

    assert status == 502
    assert payload == {"error": "Could not fetch a page title."}
    assert title_fetcher.calls == ["https://example.com"]
    assert url_payload(record["id"])["title"] == "Existing title"


@pytest.mark.parametrize("endpoint", ["refresh-title", "refresh-metadata"])
def test_refresh_url_title_reports_no_title_without_treating_it_as_failure(
    app, endpoint
):
    base_url, _ = app
    record = seed_url("https://example.com", title="Existing title")
    assert record is not None

    status, payload = http_json(
        base_url,
        f"/api/urls/{record['id']}/{endpoint}",
        method="POST",
    )

    assert status == 200
    assert payload["title_updated"] is False
    assert payload["url"]["title"] == "Existing title"
    assert url_payload(record["id"])["title"] == "Existing title"


@pytest.mark.parametrize(
    "title_fetcher",
    [ConcurrentEditTitleFetcher()],
    indirect=True,
)
@pytest.mark.parametrize("endpoint", ["refresh-title", "refresh-metadata"])
def test_refresh_url_title_rejects_concurrent_edit(
    app, title_fetcher, bookmarks_service, endpoint
):
    base_url, _ = app
    record = seed_url("https://example.com", title="Old title")
    assert record is not None
    title_fetcher.url_id = record["id"]
    title_fetcher.service = bookmarks_service

    status, payload = http_json(
        base_url,
        f"/api/urls/{record['id']}/{endpoint}",
        method="POST",
    )

    assert status == 409
    assert payload == {"error": "This URL was changed. Reload and try again."}
    stored = url_payload(record["id"])
    assert stored is not None
    assert stored["title"] == "Manual title"
    assert stored["version"] == record["version"] + 1


@pytest.mark.parametrize(
    "title_fetcher",
    [ConcurrentImportantTitleFetcher()],
    indirect=True,
)
@pytest.mark.parametrize("endpoint", ["refresh-title", "refresh-metadata"])
def test_refresh_url_title_allows_concurrent_important_change(
    app, title_fetcher, bookmarks_service, endpoint
):
    base_url, _ = app
    record = seed_url("https://example.com", title="Old title")
    assert record is not None
    title_fetcher.url_id = record["id"]
    title_fetcher.service = bookmarks_service

    status, payload = http_json(
        base_url,
        f"/api/urls/{record['id']}/{endpoint}",
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
@pytest.mark.parametrize("endpoint", ["refresh-title", "refresh-metadata"])
def test_refresh_missing_url_returns_not_found_without_fetching(
    app, title_fetcher, endpoint
):
    base_url, _ = app

    status, payload = http_json(
        base_url,
        f"/api/urls/999/{endpoint}",
        method="POST",
    )

    assert status == 404
    assert payload == {"error": "This URL is not saved."}
    assert title_fetcher.calls == []
