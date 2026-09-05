import json
from urllib import error, request

import pytest

from tests.bookmarks.helpers import seed_url, url_payload
from tests.helpers import (
    RecordingTitleFetcher,
    _authentication_headers,
    http_json,
    run_async,
)
from trellmark.app import create_app
from trellmark.bookmarks.domain import EditURL, SiteIcon, URLUpdated
from trellmark.bookmarks.integrations import ICO_MEDIA_TYPE, PNG_MEDIA_TYPE

PNG = b"\x89PNG\r\n\x1a\napi-test"
ICO = b"\x00\x00\x01\x00api-test"


class RecordingIconService:
    def __init__(
        self,
        *,
        icon: SiteIcon | None = None,
        refreshed: bool = False,
    ) -> None:
        self.icon = icon
        self.refreshed = refreshed
        self.get_calls: list[str] = []
        self.refresh_calls: list[str] = []
        self.waited_for_idle = False

    async def get(self, url: str) -> SiteIcon | None:
        self.get_calls.append(url)
        return self.icon

    async def refresh(self, url: str) -> bool:
        self.refresh_calls.append(url)
        return self.refreshed

    async def wait_for_idle(self) -> None:
        self.waited_for_idle = True


class ConcurrentEditTitleFetcher:
    def __init__(self) -> None:
        self.url_id = 0
        self.service = None

    async def __call__(self, _url: str) -> str:
        assert self.service is not None
        record = await self.service.url_by_id(self.url_id)
        assert record is not None
        outcome = await self.service.edit_url(
            EditURL(self.url_id, record.version, title="Manual title")
        )
        assert isinstance(outcome, URLUpdated)
        return "Fetched title"


def _raw_get(base_url: str, path: str) -> tuple[int, bytes, object]:
    req = request.Request(
        base_url + path,
        headers={"Cookie": _authentication_headers(base_url)["Cookie"]},
    )
    try:
        with request.urlopen(req, timeout=5) as response:
            return response.status, response.read(), response.headers
    except error.HTTPError as response:
        return response.code, response.read(), response.headers


@pytest.mark.parametrize(
    ("body", "media_type"),
    [(PNG, PNG_MEDIA_TYPE), (ICO, ICO_MEDIA_TYPE)],
)
@pytest.mark.parametrize("icon_service", [RecordingIconService], indirect=True)
def test_icon_endpoint_returns_authenticated_canonical_raw_media(
    app,
    icon_service,
    body,
    media_type,
):
    base_url, _ = app
    record = seed_url("https://example.com/path")
    assert record is not None
    icon_service.icon = SiteIcon(body, media_type)

    status, response_body, headers = _raw_get(
        base_url,
        f"/api/urls/{record['id']}/icon",
    )

    assert status == 200
    assert response_body == body
    assert headers["Content-Type"] == media_type
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert icon_service.get_calls == ["https://example.com/path"]


@pytest.mark.parametrize("icon_service", [RecordingIconService], indirect=True)
def test_icon_endpoint_authenticates_before_parsing_or_fetching(app, icon_service):
    base_url, _ = app

    status, payload = http_json(
        base_url,
        "/api/urls/not-an-id/icon",
        authenticate=False,
    )

    assert (status, payload) == (401, {"error": "Authentication required."})
    assert icon_service.get_calls == []


@pytest.mark.parametrize("icon_service", [RecordingIconService], indirect=True)
def test_icon_endpoint_returns_json_404_for_missing_or_unavailable_icon(
    app,
    icon_service,
):
    base_url, _ = app
    record = seed_url("https://example.com")
    assert record is not None

    assert http_json(base_url, "/api/urls/999/icon") == (
        404,
        {"error": "This URL is not saved."},
    )
    status, payload = http_json(
        base_url,
        f"/api/urls/{record['id']}/icon",
    )

    assert (status, payload) == (404, {"error": "No site icon available."})
    assert icon_service.get_calls == ["https://example.com"]


@pytest.mark.parametrize(
    "title_fetcher",
    [RecordingTitleFetcher("Fresh title")],
    indirect=True,
)
@pytest.mark.parametrize("icon_service", [RecordingIconService], indirect=True)
def test_refresh_metadata_forces_title_and_icon_and_returns_current_data(
    app,
    title_fetcher,
    icon_service,
):
    base_url, _ = app
    record = seed_url("https://example.com", title="Old title")
    assert record is not None
    icon_service.refreshed = True

    status, payload = http_json(
        base_url,
        f"/api/urls/{record['id']}/refresh-metadata",
        method="POST",
    )

    assert status == 200
    assert payload["title_updated"] is True
    assert payload["icon_updated"] is True
    assert payload["url"]["title"] == "Fresh title"
    assert payload["groups"][0]["urls"][0]["title"] == "Fresh title"
    assert set(payload["url"]) == {
        "id",
        "url",
        "title",
        "created_at",
        "important",
        "version",
    }
    assert title_fetcher.calls == ["https://example.com"]
    assert icon_service.refresh_calls == ["https://example.com"]


@pytest.mark.parametrize(
    "title_fetcher",
    [RecordingTitleFetcher(error=RuntimeError("network unavailable"))],
    indirect=True,
)
@pytest.mark.parametrize("icon_service", [RecordingIconService], indirect=True)
def test_refresh_metadata_reports_independent_failure_flags(
    app,
    title_fetcher,
    icon_service,
):
    base_url, _ = app
    record = seed_url("https://example.com", title="Existing title")
    assert record is not None
    icon_service.refreshed = True

    status, payload = http_json(
        base_url,
        f"/api/urls/{record['id']}/refresh-metadata",
        method="POST",
    )

    assert status == 200
    assert payload["title_updated"] is False
    assert payload["icon_updated"] is True
    assert payload["url"]["title"] == "Existing title"
    assert title_fetcher.calls == ["https://example.com"]
    assert icon_service.refresh_calls == ["https://example.com"]


@pytest.mark.parametrize(
    "title_fetcher",
    [RecordingTitleFetcher("Must not fetch")],
    indirect=True,
)
@pytest.mark.parametrize("icon_service", [RecordingIconService], indirect=True)
def test_refresh_metadata_enforces_boundary_before_route_work(
    app,
    title_fetcher,
    icon_service,
):
    base_url, _ = app
    cookie = _authentication_headers(base_url)["Cookie"]

    assert http_json(
        base_url,
        "/api/urls/not-an-id/refresh-metadata",
        method="POST",
        authenticate=False,
    ) == (401, {"error": "Authentication required."})
    assert http_json(
        base_url,
        "/api/urls/not-an-id/refresh-metadata",
        method="POST",
        authenticate=False,
        headers={"Cookie": cookie},
    ) == (403, {"error": "Request origin not allowed."})
    assert http_json(
        base_url,
        "/api/urls/not-an-id/refresh-metadata",
        method="POST",
        authenticate=False,
        headers={"Cookie": cookie, "Origin": base_url},
    ) == (403, {"error": "CSRF validation failed."})

    assert title_fetcher.calls == []
    assert icon_service.refresh_calls == []


@pytest.mark.parametrize(
    "title_fetcher",
    [ConcurrentEditTitleFetcher()],
    indirect=True,
)
@pytest.mark.parametrize("icon_service", [RecordingIconService], indirect=True)
def test_refresh_metadata_keeps_optimistic_title_conflict_behavior(
    app,
    title_fetcher,
    icon_service,
    bookmarks_service,
):
    base_url, _ = app
    record = seed_url("https://example.com", title="Old title")
    assert record is not None
    title_fetcher.url_id = record["id"]
    title_fetcher.service = bookmarks_service

    status, payload = http_json(
        base_url,
        f"/api/urls/{record['id']}/refresh-metadata",
        method="POST",
    )

    assert (status, payload) == (
        409,
        {"error": "This URL was changed. Reload and try again."},
    )
    assert url_payload(record["id"])["title"] == "Manual title"
    assert icon_service.refresh_calls == ["https://example.com"]


def test_icon_service_is_drained_by_application_lifespan():
    service = RecordingIconService()
    application = create_app(icon_service=service)

    async def enter_and_exit_lifespan() -> None:
        async with application.router.lifespan_context(application):
            pass

    run_async(enter_and_exit_lifespan)

    assert service.waited_for_idle is True


def test_openapi_describes_raw_icon_media_without_url_record_drift():
    schema = create_app().openapi()
    icon_responses = schema["paths"]["/api/urls/{url_id}/icon"]["get"]["responses"]

    assert set(icon_responses["200"]["content"]) == {
        PNG_MEDIA_TYPE,
        ICO_MEDIA_TYPE,
    }
    assert set(schema["components"]["schemas"]["URLRecord"]["properties"]) == {
        "id",
        "url",
        "title",
        "created_at",
        "important",
        "version",
    }
    metadata_schema = schema["components"]["schemas"]["RefreshURLMetadataResponse"]
    assert {"title_updated", "icon_updated"} <= set(metadata_schema["properties"])


def test_icon_contract_does_not_enter_url_records_or_exports(app):
    base_url, _ = app
    seed_url("https://example.com")

    _, groups = http_json(base_url, "/api/groups")
    _, exported = http_json(base_url, "/api/export")

    assert "icon" not in json.dumps(groups).lower()
    assert "icon" not in json.dumps(exported).lower()
