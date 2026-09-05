from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect


def test_design_uses_local_components_and_resets_isolated_state(static_page, browser):
    origin = f"{urlsplit(static_page.url).scheme}://{urlsplit(static_page.url).netloc}"
    requests = []
    static_page.on("request", lambda request: requests.append(request.url))
    static_page.goto(f"{origin}/design.html")
    expect(static_page.get_by_role("heading", name="Design overview")).to_be_visible()
    reading = static_page.locator("#groups > .group").filter(
        has=static_page.get_by_role("heading", name="Reading", exact=True)
    )
    toggle = reading.get_by_role("button", name="Toggle Reading", exact=True)
    toggle.click()
    expect(toggle).to_have_attribute("aria-expanded", "false")
    toggle.click()
    important = reading.locator(".important-toggle").first
    expect(important).to_have_attribute("aria-pressed", "false")
    important.click()
    expect(important).to_have_attribute("aria-pressed", "true")
    reading.locator(".edit-url-button").first.click()
    expect(static_page.locator("#url-edit-title")).to_be_focused()
    static_page.locator("#url-edit-title").fill("Page-local draft")
    static_page.locator("#url-edit-save").click()
    expect(reading.get_by_role("link", name="Page-local draft")).to_be_visible()
    other = browser.new_page()
    try:
        other.goto(f"{origin}/design.html")
        expect(other.locator("#groups .important-toggle").first).to_have_attribute(
            "aria-pressed", "false"
        )
        expect(other.get_by_role("link", name="Page-local draft")).to_have_count(0)
    finally:
        other.close()
    static_page.reload()
    expect(reading.get_by_role("link", name="A quiet reading list")).to_be_visible()
    expect(important).to_have_attribute("aria-pressed", "false")
    assert all(urlsplit(url).netloc == urlsplit(origin).netloc for url in requests)
    assert all(not urlsplit(url).path.startswith("/api/") for url in requests)


def test_design_dialog_ids_and_repeated_focus_are_unique(static_page):
    origin = f"{urlsplit(static_page.url).scheme}://{urlsplit(static_page.url).netloc}"
    static_page.goto(f"{origin}/design.html")
    edit = static_page.locator("#groups .edit-url-button").first
    for _ in range(3):
        edit.click()
        expect(static_page.locator("#url-edit-title")).to_be_focused()
        static_page.keyboard.press("Escape")
        expect(static_page.locator("#url-edit-dialog")).not_to_be_visible()
        expect(edit).to_be_focused()
    assert static_page.evaluate(
        """() => {
          const ids = [...document.querySelectorAll('[id]')].map(node => node.id);
          return ids.length === new Set(ids).size &&
            [...document.querySelectorAll('[for], [aria-controls]')].every(node =>
              document.getElementById(node.getAttribute('for') || node.getAttribute('aria-controls')));
        }"""
    )


STATES = [
    "interactive",
    "empty",
    "long-title",
    "icons",
    "important",
    "safe",
    "all",
    "hierarchy",
    "folding",
    "drag",
    "authentication",
    "dialogs",
]


@pytest.mark.parametrize("width", [360, 1440])
def test_design_exposes_every_rare_state_and_local_failure(static_page, width):
    static_page.set_viewport_size({"width": width, "height": 900})
    origin = f"{urlsplit(static_page.url).scheme}://{urlsplit(static_page.url).netloc}"
    requests = []
    static_page.on("request", lambda request: requests.append(request.url))
    static_page.goto(f"{origin}/design.html")
    for state in STATES:
        expect(static_page.locator(f'[data-design-state="{state}"]')).to_be_visible()
    expect(
        static_page.locator('[data-design-state="empty"] .group-empty')
    ).to_be_visible()
    expect(
        static_page.locator('[data-design-state="safe"] .group-nsfw-badge')
    ).to_have_count(0)
    expect(
        static_page.locator('[data-design-state="all"] .group-nsfw-badge')
    ).to_be_visible()
    for depth in [1, 2, 3]:
        expect(
            static_page.locator(
                f'[data-design-state="hierarchy"] .group[data-depth="{depth}"]'
            )
        ).to_be_visible()
    expect(
        static_page.locator('[data-design-state="folding"] [aria-expanded="false"]')
    ).to_be_visible()
    expect(
        static_page.locator('[data-design-state="folding"] [aria-expanded="true"]')
    ).to_be_visible()
    expect(
        static_page.locator('[data-design-state="drag"] .group.dragging')
    ).to_be_visible()
    expect(static_page.locator('[data-design-state="drag"] .drag-over')).to_be_visible()
    icons = static_page.locator('[data-design-state="icons"]')
    icons.scroll_into_view_if_needed()
    expect(icons.locator(".site-icon.is-unavailable")).to_have_count(2)
    expect(icons.locator(".site-icon-image[hidden]")).to_have_count(2)
    expect(icons.locator(".site-icon.is-loaded")).to_have_count(1)
    expect(
        static_page.locator('[data-design-state="important"] [aria-pressed="true"]')
    ).to_have_count(1)
    expect(
        static_page.locator('[data-design-state="important"] [aria-pressed="false"]')
    ).to_have_count(1)
    for theme in ["dark", "light"]:
        static_page.locator(f'#app-view [data-theme-value="{theme}"]').click()
        expect(static_page.locator("html")).to_have_attribute("data-theme", theme)
    static_page.locator("#groups .refresh-metadata-button").first.click()
    expect(static_page.locator("#form-status")).to_have_text(
        "No page title or site icon found."
    )
    static_page.locator("#export-button").click()
    static_page.locator("#import-retry-button").click()
    static_page.locator("#groups .url-text a").first.click()
    static_page.locator("#groups .edit-url-button").first.click()
    static_page.locator("#url-edit-url").fill("javascript:alert(1)")
    static_page.locator("#url-edit-save").click()
    expect(static_page.locator("#url-edit-status")).to_have_text(
        "Use an HTTP or HTTPS example URL."
    )
    static_page.locator("#url-edit-cancel").click()
    assert all(urlsplit(url).netloc == urlsplit(origin).netloc for url in requests)
    assert all(not urlsplit(url).path.startswith("/api/") for url in requests)
    assert not any(urlsplit(url).path.startswith("/html/") for url in requests)


@pytest.mark.parametrize(
    "dialog_id,focus_id",
    [
        ("url-edit-dialog", "url-edit-title"),
        ("group-edit-dialog", "group-edit-name"),
        ("confirm-dialog", None),
        ("group-delete-dialog", None),
    ],
)
def test_design_opens_each_real_dialog_with_focus_and_linked_ids(
    static_page, dialog_id, focus_id
):
    origin = f"{urlsplit(static_page.url).scheme}://{urlsplit(static_page.url).netloc}"
    static_page.goto(f"{origin}/design.html")
    launch = static_page.locator(f'[data-design-dialog="{dialog_id}"]')
    for _ in range(2):
        launch.click()
        dialog = static_page.locator(f"#{dialog_id}")
        expect(dialog).to_be_visible()
        assert dialog.evaluate("node => node.contains(document.activeElement)")
        if focus_id:
            expect(static_page.locator(f"#{focus_id}")).to_be_focused()
        # Native modal traversal may visit browser chrome when wrapping. Start
        # at the first control to test in-dialog order and inert background.
        dialog.locator("input, textarea, select, button").first.focus()
        static_page.keyboard.press("Tab")
        assert dialog.evaluate("node => node.contains(document.activeElement)")
        static_page.keyboard.press("Shift+Tab")
        static_page.locator("#url-input").evaluate("node => node.focus()")
        assert dialog.evaluate("node => node.contains(document.activeElement)")
        static_page.keyboard.press("Escape")
        expect(dialog).not_to_be_visible()
        expect(launch).to_be_focused()
    ids = static_page.locator("[id]").evaluate_all(
        "nodes => nodes.map(node => node.id)"
    )
    assert len(ids) == len(set(ids))
