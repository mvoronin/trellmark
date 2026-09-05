import base64
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from tests.bookmarks import helpers as bookmark_helpers
from trellmark.bookmarks.domain import SiteIcon
from trellmark.bookmarks.integrations import PNG_MEDIA_TYPE

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8A"
    "AQUBAScY42YAAAAASUVORK5CYII="
)


def test_bookmark_editor_factory_scopes_fields_and_rejects_old_completion(static_page):
    result = static_page.evaluate(
        """async () => {
          const before = document.body.innerHTML;
          const effects = [];
          const originals = [];
          for (const [owner, key] of [
            [Document.prototype, 'querySelector'], [Document.prototype, 'querySelectorAll'],
            [EventTarget.prototype, 'addEventListener'],
            [Storage.prototype, 'getItem'], [Storage.prototype, 'setItem'], [window, 'fetch'],
          ]) {
            originals.push([owner, key, owner[key]]);
            owner[key] = () => { effects.push(key); throw Error(`Import side effect: ${key}`); };
          }
          let createBookmarkEditors;
          try {
            ({ createBookmarkEditors } = await import('/static/features/bookmarks/editors.js'));
          } finally {
            for (const [owner, key, original] of originals) owner[key] = original;
          }
          const inert = document.body.innerHTML === before;
          const { createBookmarksModel } = await import('/static/features/bookmarks/model.js');
          const { createDialogs } = await import('/static/shell/dialogs.js');
          const { createRequestLifetime } = await import('/static/shared/request.js');
          const html = await (await fetch('/index.html')).text();
          const parsed = new DOMParser().parseFromString(html, 'text/html');
          const root = document.createElement('div');
          root.append(...parsed.body.children);
          const decoy = document.createElement('input');
          decoy.id = 'url-edit-title'; decoy.value = 'Outside';
          document.body.append(decoy, root);
          const model = createBookmarksModel();
          const dialogs = createDialogs(root);
          let finish;
          let replacements = 0;
          const editors = createBookmarkEditors(root, {
            model, dialogs, status() {}, privateLifetime: createRequestLifetime(),
            api: { editUrl: () => new Promise(resolve => { finish = resolve; }) },
            refresh: { replace() { replacements++; }, render() {}, async load() {}, async ready() {} },
          });
          const record = { id: 1, url: 'https://example.test', title: 'Original', version: 1 };
          editors.showUrlEditor(record);
          const title = root.querySelector('#url-edit-title');
          title.value = 'Old draft';
          root.querySelector('#url-edit-form').requestSubmit();
          root.querySelector('#url-edit-cancel').click();
          editors.showUrlEditor(record);
          title.value = 'New draft';
          finish({groups: []});
          await new Promise(resolve => setTimeout(resolve, 0));
          const result = { inert, effects, outside: decoy.value, title: title.value, replacements,
            open: dialogs.isOpen('url-edit-dialog'), disabled: root.querySelector('#url-edit-save').disabled };
          editors.clear();
          dialogs.dispose();
          return result;
        }"""
    )
    assert result == {
        "inert": True,
        "effects": [],
        "outside": "Outside",
        "title": "New draft",
        "replacements": 0,
        "open": True,
        "disabled": False,
    }


def test_bookmarks_state_projections_preserve_snapshots_and_equal_keys(static_page):
    result = static_page.evaluate(
        """async () => {
          const { createBookmarksModel, sortUrls, visibleGroups, treeGroups } =
            await import('/static/features/bookmarks/model.js');
          const model = createBookmarksModel();
          const empty = model.server.groups.length;
          const url = id => Object.freeze({id, url: `https://same.example/${id}`,
            title: null, created_at: '2026-09-05T00:00:00Z', important: false, version: 1});
          const first = url(1), second = url(2);
          const group = (id, nsfw, urls, children = []) => Object.freeze({
            id, name: id === 1 ? 'default' : `Group ${id}`, parent_id: null,
            position: id, depth: 1, nsfw, domains: Object.freeze([]),
            urls: Object.freeze(urls), children: Object.freeze(children)});
          const hidden = group(3, true, [second]);
          const root = group(1, false, [first, second], [hidden]);
          const duplicate = group(2, false, [first]);
          const snapshot = Object.freeze([root, duplicate]);
          model.replaceGroups(snapshot);
          model.setSortMode('domain');
          model.setSafeMode(true);
          const ties = sortUrls(root.urls, model.ui.sortMode);
          const safe = visibleGroups(root.children, model.ui.safeMode);
          model.setSafeMode(false);
          const all = visibleGroups(root.children, model.ui.safeMode);
          const unchanged = model.server.groups === snapshot && root.urls[0] === first;
          const memberships = treeGroups(model.server.groups)
            .flatMap(({group}) => group.urls).filter(item => item.id === 1);
          model.replaceGroups([root]);
          const single = treeGroups(model.server.groups).map(({group}) => group.id);
          model.clearGroups();
          model.setSortMode('added-asc');
          model.setSafeMode(true);
          return {empty, ties: ties.map(item => item.id), same: ties[0] === first,
            safe: safe.length, all: all.length, unchanged,
            memberships: memberships.length, sharedIdentity: memberships[0] === memberships[1],
            single, cleared: model.server.groups.length};
        }"""
    )
    assert result == {
        "empty": 0,
        "ties": [1, 2],
        "same": True,
        "safe": 0,
        "all": 1,
        "unchanged": True,
        "memberships": 2,
        "sharedIdentity": True,
        "single": [1, 3],
        "cleared": 0,
    }


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
    bookmark_helpers.seed_url("https://one.example")
    bookmark_helpers.seed_url("https://two.example/path")

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
    bookmark_helpers.seed_url("https://one.example", title="One Example")

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
    assert bookmark_helpers.saved_urls() == ["https://example.com"]


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
    assert bookmark_helpers.url_payloads()[0]["title"] == "Example Domain"


def test_frontend_edits_url_and_title(app, page):
    base_url, _ = app
    record = bookmark_helpers.seed_url("https://old.example", title="Old title")

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
    assert bookmark_helpers.url_payload(record["id"])["url"] == ("https://new.example")
    assert bookmark_helpers.url_payload(record["id"])["title"] == "New title"


def test_frontend_failed_url_edit_clears_stale_page_status(app, page):
    base_url, _ = app
    bookmark_helpers.seed_url("https://old.example")
    bookmark_helpers.seed_url("https://taken.example")

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
    bookmark_helpers.seed_url("https://example.com", title="Old title")

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
    assert bookmark_helpers.url_payloads()[0]["title"] == "Fresh title"


def test_frontend_reports_when_refresh_finds_no_title(app, page):
    base_url, _ = app
    bookmark_helpers.seed_url("https://example.com", title="Existing title")

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
    first = bookmark_helpers.seed_url("https://one.example/path")
    second = bookmark_helpers.seed_url("https://two.example")
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
    bookmark_helpers.seed_url("https://missing.example")

    page.goto(base_url)

    image = page.locator(".site-icon-image")
    expect(page.locator(".site-icon-placeholder")).to_be_visible()
    expect(page.locator(".site-icon.is-unavailable")).to_have_count(1)
    expect(image).to_be_hidden()


def test_frontend_ignores_url_edit_response_after_dialog_is_reused(app, page):
    base_url, _ = app
    first = bookmark_helpers.seed_url("https://first.example", title="First title")
    bookmark_helpers.seed_url("https://second.example", title="Second title")
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
    assert bookmark_helpers.url_payload(first["id"])["title"] == ("Changed first title")


def test_frontend_url_control_labels_include_group_context(app, page):
    base_url, _ = app
    bookmark_helpers.seed_group("Reading", domains=["example.com"])
    bookmark_helpers.seed_group("Work", domains=["example.com"])
    bookmark_helpers.seed_url("https://example.com/article")

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
    bookmark_helpers.seed_url("https://example.com")

    page.goto(base_url)
    page.get_by_label("URL").fill("example.com")
    page.get_by_role("button", name="Add", exact=True).click()

    expect(page.locator("#form-status")).to_have_text("This URL is already saved.")
    expect(page.locator("#url-count")).to_have_text("1 saved")
    assert bookmark_helpers.saved_urls() == ["https://example.com"]


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
    bookmark_helpers.seed_url("https://one.example")
    bookmark_helpers.seed_url("https://two.example")

    page.goto(base_url)
    expect(page.locator("#url-count")).to_have_text("2 saved")

    # Default ("Delete immediately" off) opens a confirmation dialog.
    page.get_by_role("button", name="Delete one.example").click()
    page.locator("#confirm-dialog").get_by_role("button", name="Delete").click()

    expect(page.locator("#form-status")).to_have_text("Deleted.")
    expect(page.locator("#url-count")).to_have_text("1 saved")
    expect(page.get_by_role("link", name="one.example")).to_have_count(0)
    assert bookmark_helpers.saved_urls() == ["https://two.example"]


def test_frontend_repeated_confirmation_escape_restores_focus(app, page):
    base_url, _ = app
    bookmark_helpers.seed_url("https://one.example")
    bookmark_helpers.seed_url("https://two.example")
    page.goto(base_url)
    page.get_by_role("button", name="Delete one.example").click()
    page.locator("#confirm-dialog").get_by_role("button", name="Delete").click()
    expect(page.locator("#url-count")).to_have_text("1 saved")
    button = page.get_by_role("button", name="Delete two.example")
    for _ in range(3):
        button.click()
        page.keyboard.press("Escape")
        expect(page.locator("#confirm-dialog")).not_to_be_visible()
        expect(button).to_be_focused()
        expect(page.locator("#url-count")).to_have_text("1 saved")
    button.click()
    page.locator("#confirm-dialog").get_by_role("button", name="Cancel").click()
    expect(button).to_be_focused()
    assert bookmark_helpers.saved_urls() == ["https://two.example"]


def test_dialog_factory_disposes_pending_confirmation(static_page):
    assert static_page.evaluate("""async () => {
      const {createDialogs} = await import('/static/shell/dialogs.js');
      const root = document.createElement('section');
      root.innerHTML = '<dialog id="confirm-dialog"><p id="confirm-url"></p></dialog>';
      document.body.append(root);
      const dialogs = createDialogs(root);
      const first = dialogs.confirmDeletion('https://one.example');
      const duplicate = dialogs.confirmDeletion('https://two.example');
      dialogs.dispose();
      return [await first, await duplicate, root.querySelector('dialog').open];
    }""") == [False, False, False]


def test_frontend_delete_immediately_skips_confirmation(app, page):
    base_url, _ = app
    bookmark_helpers.seed_url("https://one.example")

    page.goto(base_url)
    page.get_by_label("Delete immediately").check()
    page.get_by_role("button", name="Delete one.example").click()

    expect(page.locator("#form-status")).to_have_text("Deleted.")
    expect(page.locator("#url-count")).to_have_text("0 saved")
    assert bookmark_helpers.saved_urls() == []


def test_frontend_sorts_by_domain(app, page):
    base_url, _ = app
    bookmark_helpers.seed_url("https://zebra.example")
    bookmark_helpers.seed_url("https://alpha.example")

    page.goto(base_url)
    page.get_by_label("Sort").select_option("domain")

    links = page.get_by_role("link")
    expect(links.first).to_have_text("alpha.example")
    expect(links.last).to_have_text("zebra.example")
