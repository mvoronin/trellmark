import base64
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

import trellmark
from trellmark.site_icons import PNG_MEDIA_TYPE, SiteIcon

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8A"
    "AQUBAScY42YAAAAASUVORK5CYII="
)


class RecordingTitleFetcher:
    def __init__(self, title):
        self.title = title
        self.calls = []

    async def __call__(self, url):
        self.calls.append(url)
        return self.title


class RecordingIconService:
    def __init__(self, icon=None, refreshed=False):
        self.icon = icon
        self.refreshed = refreshed
        self.get_calls = []
        self.refresh_calls = []

    async def get(self, url):
        self.get_calls.append(url)
        return self.icon

    async def refresh(self, url):
        self.refresh_calls.append(url)
        return self.refreshed

    async def wait_for_idle(self):
        return None


def test_frontend_renders_saved_urls(app, page):
    base_url, _ = app
    trellmark.add_url("https://one.example")
    trellmark.add_url("https://two.example/path")

    page.goto(base_url)

    expect(page.locator("#url-count")).to_have_text("2 saved")
    expect(page.get_by_role("link", name="one.example")).to_have_attribute(
        "href", "https://one.example"
    )
    expect(page.get_by_role("link", name="two.example/path")).to_have_attribute(
        "href", "https://two.example/path"
    )


def test_frontend_renders_saved_title_as_link_text(app, page):
    base_url, _ = app
    trellmark.add_url("https://one.example", title="One Example")

    page.goto(base_url)

    expect(page.get_by_role("link", name="One Example")).to_have_attribute(
        "href", "https://one.example"
    )


def test_frontend_adds_url_and_updates_list(app, page):
    base_url, _ = app

    page.goto(base_url)
    page.get_by_label("URL").fill("example.com")
    page.get_by_role("button", name="Add", exact=True).click()

    expect(page.locator("#form-status")).to_have_text("Saved.")
    expect(page.locator("#url-count")).to_have_text("1 saved")
    expect(page.get_by_role("link", name="example.com")).to_have_attribute(
        "href", "https://example.com"
    )
    assert trellmark.read_urls() == ["https://example.com"]


@pytest.mark.parametrize(
    "title_fetcher",
    [RecordingTitleFetcher("Example Domain")],
    indirect=True,
)
def test_frontend_adds_url_with_backend_title(app, page, title_fetcher):
    base_url, _ = app

    page.goto(base_url)
    page.get_by_label("URL").fill("example.com")
    page.get_by_role("button", name="Add", exact=True).click()

    expect(page.locator("#form-status")).to_have_text("Saved.")
    expect(page.get_by_role("link", name="Example Domain")).to_have_attribute(
        "href", "https://example.com"
    )
    assert title_fetcher.calls == ["https://example.com"]
    assert trellmark.read_url_records()[0]["title"] == "Example Domain"


def test_frontend_edits_url_and_title(app, page):
    base_url, _ = app
    record = trellmark.add_url("https://old.example", title="Old title")

    page.goto(base_url)
    page.get_by_role("button", name="Edit old.example in default", exact=True).click()
    page.get_by_label("Name / title").fill("New title")
    page.locator("#url-edit-dialog").get_by_label("Address", exact=True).fill(
        "new.example/"
    )
    page.locator("#url-edit-dialog").get_by_role("button", name="Save").click()

    expect(page.locator("#form-status")).to_have_text("URL updated.")
    expect(page.get_by_role("link", name="New title")).to_have_attribute(
        "href", "https://new.example"
    )
    assert trellmark.read_url_record_by_id(record["id"])["url"] == (
        "https://new.example"
    )
    assert trellmark.read_url_record_by_id(record["id"])["title"] == "New title"


def test_frontend_failed_url_edit_clears_stale_page_status(app, page):
    base_url, _ = app
    trellmark.add_url("https://old.example")
    trellmark.add_url("https://taken.example")

    page.goto(base_url)
    page.get_by_label("URL").fill("old.example")
    page.get_by_role("button", name="Add", exact=True).click()
    expect(page.locator("#form-status")).to_have_text("This URL is already saved.")

    page.get_by_role("button", name="Edit old.example in default", exact=True).click()
    page.locator("#url-edit-dialog").get_by_label("Address", exact=True).fill(
        "taken.example"
    )
    page.locator("#url-edit-dialog").get_by_role("button", name="Save").click()

    expect(page.locator("#url-edit-status")).to_have_text("This URL is already saved.")
    expect(page.locator("#form-status")).to_be_empty()


@pytest.mark.parametrize(
    "title_fetcher",
    [RecordingTitleFetcher("Fresh title")],
    indirect=True,
)
@pytest.mark.parametrize(
    "icon_service",
    [RecordingIconService(icon=SiteIcon(PNG, PNG_MEDIA_TYPE), refreshed=True)],
    indirect=True,
)
def test_frontend_refreshes_url_title_and_icon(
    app,
    page,
    title_fetcher,
    icon_service,
):
    base_url, _ = app
    trellmark.add_url("https://example.com", title="Old title")

    page.goto(base_url)
    page.get_by_role(
        "button",
        name="Refresh title and icon for example.com in default",
        exact=True,
    ).click()

    expect(page.locator("#form-status")).to_have_text("Title and icon refreshed.")
    expect(page.get_by_role("link", name="Fresh title")).to_have_attribute(
        "href", "https://example.com"
    )
    assert title_fetcher.calls == ["https://example.com"]
    assert icon_service.refresh_calls == ["https://example.com"]
    assert trellmark.read_url_records()[0]["title"] == "Fresh title"


def test_frontend_reports_when_refresh_finds_no_title(app, page):
    base_url, _ = app
    trellmark.add_url("https://example.com", title="Existing title")

    page.goto(base_url)
    page.get_by_role(
        "button",
        name="Refresh title and icon for example.com in default",
        exact=True,
    ).click()

    expect(page.locator("#form-status")).to_have_text(
        "No page title or site icon found."
    )
    expect(page.get_by_role("link", name="Existing title")).to_have_attribute(
        "href", "https://example.com"
    )


@pytest.mark.parametrize(
    "icon_service",
    [RecordingIconService(icon=SiteIcon(PNG, PNG_MEDIA_TYPE))],
    indirect=True,
)
def test_frontend_loads_only_lazy_decorative_same_origin_icons(
    app,
    page,
    icon_service,
):
    base_url, _ = app
    first = trellmark.add_url("https://one.example/path")
    second = trellmark.add_url("https://two.example")
    assert first is not None and second is not None
    requested = []
    page.on("request", lambda browser_request: requested.append(browser_request.url))

    page.goto(base_url)

    icons = page.locator(".site-icon-image")
    expect(icons).to_have_count(2)
    expect(icons.first).to_have_attribute("alt", "")
    expect(icons.first).to_have_attribute("loading", "lazy")
    sources = icons.evaluate_all(
        "(elements) => elements.map((element) => element.getAttribute('src'))"
    )
    assert set(sources) == {
        f"/api/urls/{first['id']}/icon",
        f"/api/urls/{second['id']}/icon",
    }
    expect(page.locator(".site-icon.is-loaded")).to_have_count(2)
    expected_origin = urlsplit(base_url).netloc
    assert requested
    assert {urlsplit(url).netloc for url in requested} == {expected_origin}
    assert sorted(icon_service.get_calls) == [
        "https://one.example/path",
        "https://two.example",
    ]


def test_frontend_keeps_local_placeholder_after_icon_error(app, page):
    base_url, _ = app
    trellmark.add_url("https://missing.example")

    page.goto(base_url)

    image = page.locator(".site-icon-image")
    expect(page.locator(".site-icon-placeholder")).to_be_visible()
    expect(page.locator(".site-icon.is-unavailable")).to_have_count(1)
    expect(image).to_be_hidden()


def test_frontend_ignores_url_edit_response_after_dialog_is_reused(app, page):
    base_url, _ = app
    first = trellmark.add_url("https://first.example", title="First title")
    trellmark.add_url("https://second.example", title="Second title")
    assert first is not None

    page.add_init_script(
        r"""
        (() => {
          const originalFetch = window.fetch.bind(window);
          window.urlEditResponseSettled = false;
          window.fetch = (input, init = {}) => {
            const url = typeof input === "string" ? input : input.url;
            const path = new URL(url, window.location.href).pathname;
            if (init.method === "PATCH" && /^\/api\/urls\/\d+$/.test(path)) {
              const request = originalFetch(input, init);
              return new Promise((resolve, reject) => {
                window.releaseUrlEditResponse = () => {
                  request.then(
                    (response) => {
                      resolve(response);
                      setTimeout(() => { window.urlEditResponseSettled = true; }, 0);
                    },
                    reject,
                  );
                };
              });
            }
            return originalFetch(input, init);
          };
        })();
        """
    )

    page.goto(base_url)
    page.get_by_role("button", name="Edit first.example in default", exact=True).click()
    page.get_by_label("Name / title").fill("Changed first title")
    page.locator("#url-edit-dialog").get_by_role("button", name="Save").click()
    expect(page.locator("#url-edit-save")).to_be_disabled()

    page.keyboard.press("Escape")
    expect(page.locator("#url-edit-dialog")).not_to_be_visible()
    page.get_by_role(
        "button", name="Edit second.example in default", exact=True
    ).click()
    expect(page.get_by_label("Name / title")).to_have_value("Second title")

    page.evaluate("window.releaseUrlEditResponse()")
    page.wait_for_function("window.urlEditResponseSettled === true")

    expect(page.locator("#url-edit-dialog")).to_be_visible()
    expect(page.get_by_label("Name / title")).to_have_value("Second title")
    expect(page.locator("#form-status")).to_be_empty()
    assert trellmark.read_url_record_by_id(first["id"])["title"] == (
        "Changed first title"
    )


def test_frontend_url_control_labels_include_group_context(app, page):
    base_url, _ = app
    trellmark.add_group("Reading", domains=["example.com"])
    trellmark.add_group("Work", domains=["example.com"])
    trellmark.add_url("https://example.com/article")

    page.goto(base_url)
    controls = page.locator(".url-controls [aria-label]")
    expect(controls).to_have_count(10)
    labels = controls.evaluate_all(
        "(elements) => elements.map((element) => element.getAttribute('aria-label'))"
    )

    assert len(labels) == len(set(labels))
    assert "Edit example.com/article in Reading" in labels
    assert "Edit example.com/article in Work" in labels


def test_frontend_shows_error_for_duplicate_url(app, page):
    base_url, _ = app
    trellmark.add_url("https://example.com")

    page.goto(base_url)
    page.get_by_label("URL").fill("example.com")
    page.get_by_role("button", name="Add", exact=True).click()

    expect(page.locator("#form-status")).to_have_text("This URL is already saved.")
    expect(page.locator("#url-count")).to_have_text("1 saved")
    assert trellmark.read_urls() == ["https://example.com"]


def test_frontend_theme_toggle_pins_and_persists(app, page):
    base_url, _ = app

    page.goto(base_url)
    html = page.locator("html")
    # Default is "system": no attribute is set, color-scheme follows the OS.
    expect(html).not_to_have_attribute("data-theme", "dark")
    expect(page.get_by_role("button", name="System")).to_have_attribute(
        "aria-pressed", "true"
    )

    page.get_by_role("button", name="Dark").click()
    expect(html).to_have_attribute("data-theme", "dark")
    expect(page.get_by_role("button", name="Dark")).to_have_attribute(
        "aria-pressed", "true"
    )

    # The choice survives a reload (applied before paint by the inline script).
    page.reload()
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")

    page.get_by_role("button", name="System").click()
    expect(page.locator("html")).not_to_have_attribute("data-theme", "dark")
    assert page.evaluate("() => localStorage.getItem('theme')") is None


def test_frontend_deletes_url(app, page):
    base_url, _ = app
    trellmark.add_url("https://one.example")
    trellmark.add_url("https://two.example")

    page.goto(base_url)
    expect(page.locator("#url-count")).to_have_text("2 saved")

    # Default ("Delete immediately" off) opens a confirmation dialog.
    page.get_by_role("button", name="Delete one.example").click()
    page.locator("#confirm-dialog").get_by_role("button", name="Delete").click()

    expect(page.locator("#form-status")).to_have_text("Deleted.")
    expect(page.locator("#url-count")).to_have_text("1 saved")
    expect(page.get_by_role("link", name="one.example")).to_have_count(0)
    assert trellmark.read_urls() == ["https://two.example"]


def test_frontend_delete_immediately_skips_confirmation(app, page):
    base_url, _ = app
    trellmark.add_url("https://one.example")

    page.goto(base_url)
    page.get_by_label("Delete immediately").check()
    page.get_by_role("button", name="Delete one.example").click()

    expect(page.locator("#form-status")).to_have_text("Deleted.")
    expect(page.locator("#url-count")).to_have_text("0 saved")
    assert trellmark.read_urls() == []


def test_frontend_sorts_by_domain(app, page):
    base_url, _ = app
    trellmark.add_url("https://zebra.example")
    trellmark.add_url("https://alpha.example")

    page.goto(base_url)
    page.get_by_label("Sort").select_option("domain")

    links = page.get_by_role("link")
    expect(links.first).to_have_text("alpha.example")
    expect(links.last).to_have_text("zebra.example")
