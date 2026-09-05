import json

from playwright.sync_api import expect
from sqlalchemy import text

from tests.bookmarks import helpers as bookmark_helpers
from tests.helpers import db_connection
from tests.postgres import TEST_LOGIN, TEST_PASSWORD


def _login(page):
    page.get_by_label("Login").fill(TEST_LOGIN)
    page.get_by_label("Password").fill(TEST_PASSWORD)
    page.get_by_role("button", name="Log in").click()


def _populate_private_dom(page, base_url):
    group_name = "Private DOM Group"
    private_domain = "private-dom.example"
    url_title = "Private DOM Title"
    url_value = "https://private-dom-url.example"
    group = bookmark_helpers.seed_group(group_name, domains=[private_domain])
    saved = bookmark_helpers.seed_url(url_value, title=url_title)
    bookmark_helpers.seed_membership(saved["id"], group["id"])

    page.goto(base_url)
    expect(page.get_by_role("link", name=url_title)).to_be_visible()

    page.get_by_role("button", name=f"Edit {group_name}", exact=True).click()
    page.locator("#group-edit-dialog").get_by_role("button", name="Cancel").click()
    page.get_by_role(
        "button", name=f"Edit private-dom-url.example in {group_name}", exact=True
    ).click()
    page.locator("#url-edit-dialog").get_by_role("button", name="Cancel").click()
    page.get_by_role("button", name=f"Delete group {group_name}").click()
    page.locator("#group-delete-dialog").get_by_role("button", name="Cancel").click()
    page.get_by_role(
        "button", name=f"Delete private-dom-url.example from {group_name}", exact=True
    ).click()
    page.locator("#confirm-dialog").get_by_role("button", name="Cancel").click()

    page.locator("#url-input").fill("private-draft-url.example")
    page.locator("#group-input").fill("Private Draft Group")
    page.locator("#group-domains").fill("private-draft-domain.example")
    return [
        group_name,
        private_domain,
        url_title,
        url_value,
        "private-draft-url.example",
        "Private Draft Group",
        "private-draft-domain.example",
    ]


def _assert_private_dom_cleared(page, private_values):
    snapshot = page.evaluate(
        """() => ({
            text: document.body.textContent ?? "",
            values: Array.from(
                document.querySelectorAll("input, textarea, select"),
                (element) => element.value,
            ),
            options: Array.from(
                document.querySelectorAll("option"),
                (option) => option.textContent ?? "",
            ),
        })"""
    )
    serialized = json.dumps(snapshot)
    for private_value in private_values:
        assert private_value not in serialized

    expect(page.locator("#group-parent option")).to_have_count(1)
    expect(page.locator("#group-edit-parent option")).to_have_count(1)
    for selector in [
        "#url-input",
        "#group-input",
        "#group-domains",
        "#group-edit-name",
        "#group-edit-domains",
        "#url-edit-title",
        "#url-edit-url",
    ]:
        expect(page.locator(selector)).to_have_value("")


def test_unauthenticated_startup_shows_only_login_and_fetches_no_private_data(
    app, unauthenticated_page
):
    base_url, _ = app
    bookmark_helpers.seed_url("https://private.example")
    requests = []
    unauthenticated_page.on("request", lambda req: requests.append(req.url))

    unauthenticated_page.goto(base_url)

    expect(unauthenticated_page.locator("#session-pending")).to_be_hidden()
    expect(unauthenticated_page.locator("#login-view")).to_be_visible()
    expect(unauthenticated_page.locator("#app-view")).to_be_hidden()
    expect(unauthenticated_page.get_by_label("Login")).to_be_focused()
    assert not any(url.endswith("/api/groups") for url in requests)
    assert unauthenticated_page.get_by_text("private.example").count() == 0


def test_login_error_is_uniform_and_success_reveals_private_application(
    app, unauthenticated_page
):
    base_url, _ = app
    bookmark_helpers.seed_url("https://private.example")
    unauthenticated_page.goto(base_url)
    unauthenticated_page.get_by_label("Login").fill("unknown")
    unauthenticated_page.get_by_label("Password").fill("wrong")
    unauthenticated_page.get_by_role("button", name="Log in").click()

    expect(unauthenticated_page.locator("#login-status")).to_have_text(
        "Invalid login or password."
    )
    expect(unauthenticated_page.get_by_label("Password")).to_be_focused()

    _login(unauthenticated_page)
    expect(unauthenticated_page.locator("#app-view")).to_be_visible()
    expect(
        unauthenticated_page.get_by_role("link", name="private.example")
    ).to_be_visible()
    storage = unauthenticated_page.evaluate(
        "Object.fromEntries(Object.entries(localStorage))"
    )
    assert not any("csrf" in key.lower() or "session" in key.lower() for key in storage)


def test_reload_uses_server_session_without_another_password(app, unauthenticated_page):
    base_url, _ = app
    unauthenticated_page.goto(base_url)
    _login(unauthenticated_page)
    expect(unauthenticated_page.locator("#app-view")).to_be_visible()

    unauthenticated_page.reload()

    expect(unauthenticated_page.locator("#app-view")).to_be_visible()
    expect(unauthenticated_page.locator("#login-view")).to_be_hidden()


def test_logout_clears_private_dom_and_returns_focus_to_login(app, page):
    base_url, _ = app
    private_values = _populate_private_dom(page, base_url)

    page.get_by_role("button", name="Log out").click()

    expect(page.locator("#login-view")).to_be_visible()
    expect(page.locator("#app-view")).to_be_hidden()
    expect(page.get_by_label("Login")).to_be_focused()
    _assert_private_dom_cleared(page, private_values)


def test_mid_session_401_clears_private_state_once(app, page):
    base_url, _ = app
    private_values = _populate_private_dom(page, base_url)
    with db_connection() as connection:
        connection.execute(text("UPDATE web_sessions SET revoked_at = now()"))

    page.get_by_role("button", name="Add", exact=True).click()

    expect(page.locator("#login-view")).to_be_visible()
    expect(page.locator("#login-status")).to_have_text(
        "Your session expired. Log in again."
    )
    expect(page.locator("#form-status")).to_have_text("Your session has expired.")
    _assert_private_dom_cleared(page, private_values)

    _login(page)

    expect(page.locator("#app-view")).to_be_visible()
    expect(page.locator("#form-status")).to_have_text("")


def test_login_layout_fits_phone_and_wide_viewports(app, unauthenticated_page):
    base_url, _ = app
    for width in (360, 1440):
        unauthenticated_page.set_viewport_size({"width": width, "height": 800})
        unauthenticated_page.goto(base_url)
        expect(unauthenticated_page.locator("#login-form")).to_be_visible()
        box = unauthenticated_page.locator("#login-form").bounding_box()
        assert box is not None
        assert box["x"] >= 0
        assert box["x"] + box["width"] <= width
