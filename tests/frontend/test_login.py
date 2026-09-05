import json

import pytest
from playwright.sync_api import expect
from sqlalchemy import text

from tests.bookmarks import helpers as bookmark_helpers
from tests.frontend.test_request_lifetimes import gate_responses, release_response
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


@pytest.mark.parametrize(
    "operation, path", [("listGroups", "/api/groups"), ("exportData", "/api/export")]
)
def test_old_401_after_relogin_preserves_new_csrf_and_current_expiry_once(
    app, page, operation, path
):
    base_url, _ = app
    bookmark_helpers.seed_url("https://session-b.example")
    page.goto(base_url)
    expect(page.get_by_role("link", name="session-b.example")).to_be_visible()
    gate_responses(page, path, status=401, payload={"error": "Unauthorized"})
    page.evaluate(
        """async operation => {
      const api = await import('/static/api/client.js');
      window.expiredErrors = 0;
      window.startExpiredRequest = () => api[operation]().catch(error => {
        if (!(error instanceof api.SessionExpiredError)) throw error;
        window.expiredErrors += 1;
      });
      window.startExpiredRequest();
    }""",
        operation,
    )
    page.wait_for_function("window.requestGates[0]?.ready")
    page.evaluate("window.gatingEnabled = false")
    page.get_by_role("button", name="Log out").click()
    expect(page.locator("#login-view")).to_be_visible()
    _login(page)
    expect(page.get_by_role("link", name="session-b.example")).to_be_visible()
    page.evaluate("""() => {
      window.expiryTransitions = 0;
      new MutationObserver(records => {
        window.expiryTransitions += records.filter(r => r.attributeName === 'hidden').length;
      }).observe(document.querySelector('#app-view'), {attributes: true});
    }""")

    release_response(page, 0)
    page.wait_for_function("window.expiredErrors === 1")

    expect(page.locator("#app-view")).to_be_visible()
    expect(page.locator("#login-view")).to_be_hidden()
    expect(page.get_by_role("link", name="session-b.example")).to_be_visible()
    assert page.evaluate("window.expiryTransitions") == 0
    # This goes through the real adapter and PostgreSQL-backed CSRF boundary.
    page.locator("#url-input").fill("new-session-mutation.example")
    with page.expect_response("**/api/urls") as mutation:
        page.get_by_role("button", name="Add", exact=True).click()
    assert mutation.value.status == 201
    assert mutation.value.request.headers.get("x-csrf-token")
    expect(
        page.get_by_role("link", name="new-session-mutation.example")
    ).to_be_visible()

    page.evaluate("""() => {
      window.gatingEnabled = true;
      window.startExpiredRequest();
      window.startExpiredRequest();
    }""")
    page.wait_for_function(
        "window.requestGates[1]?.ready && window.requestGates[2]?.ready"
    )
    release_response(page, 1)
    release_response(page, 2)
    page.wait_for_function("window.expiredErrors === 3")
    expect(page.locator("#login-view")).to_be_visible()
    expect(page.locator("#login-status")).to_have_text(
        "Your session expired. Log in again."
    )
    expect(page.locator("#groups")).to_be_empty()
    assert page.evaluate("window.expiryTransitions") == 1


@pytest.mark.parametrize("old_operation", ["login", "logout", "getSession"])
def test_adapter_auth_completion_cannot_replace_new_session(static_page, old_operation):
    result = static_page.evaluate(
        """async oldOperation => {
      const api = await import('/static/api/client.js');
      api.establishSession('first-token');
      const requests = [];
      window.fetch = (path, init) => new Promise(resolve => requests.push({path, init, resolve}));
      const old = api[oldOperation]({login: 'old', password: 'synthetic'});
      const current = api.login({login: 'new', password: 'synthetic'});
      requests[1].resolve(new Response(JSON.stringify({authenticated: true, csrf_token: 'new-token'})));
      await current;
      requests[0].resolve(new Response(JSON.stringify({authenticated: true, csrf_token: 'old-token'})));
      await old;
      const mutation = api.createUrl({url: 'example.com'});
      const token = requests[2].init.headers['X-CSRF-Token'];
      requests[2].resolve(new Response('{}'));
      await mutation;
      return token;
    }""",
        old_operation,
    )
    assert result == "new-token"


@pytest.mark.parametrize("old_success", [True, False])
@pytest.mark.parametrize("old_first", [True, False])
def test_overlapping_login_attempts_ignore_stale_success_error_and_finally(
    app, unauthenticated_page, old_success, old_first
):
    page = unauthenticated_page
    base_url, _ = app
    page.goto(base_url)
    expect(page.locator("#login-view")).to_be_visible()
    gate_responses(page, "/api/auth/login")
    requests = []

    def login_response(route):
        requests.append(route.request.url)
        succeeds = old_success if len(requests) == 1 else not old_success
        if succeeds:
            route.continue_()
        else:
            route.fulfill(status=401, json={"error": "Invalid login or password."})

    page.route("**/api/auth/login", login_response)
    private_requests = []
    page.on(
        "request",
        lambda req: (
            private_requests.append(req.url)
            if req.url.endswith("/api/groups")
            else None
        ),
    )
    _login(page)
    page.wait_for_function("window.requestGates[0]?.ready")
    page.get_by_label("Password").fill(TEST_PASSWORD)
    # A second submit can be queued before disabling the button is observed.
    page.locator("#login-form").evaluate(
        "el => el.dispatchEvent(new Event('submit', {cancelable: true}))"
    )
    page.wait_for_function("window.requestGates[1]?.ready")
    if old_first:
        release_response(page, 0)
        expect(page.locator("#login-button")).to_be_disabled()
        expect(page.locator("#login-status")).to_have_text("")
        expect(page.locator("#password-input")).to_have_value(TEST_PASSWORD)
        assert private_requests == []
    release_response(page, 1)
    if old_success:
        expect(page.locator("#login-status")).to_have_text("Invalid login or password.")
        expect(page.locator("#password-input")).to_be_focused()
    else:
        expect(page.locator("#app-view")).to_be_visible()
        expect(page.locator("#url-input")).to_be_focused()
    if not old_first:
        release_response(page, 0)
    expect(page.locator("#login-button")).to_be_enabled()
    if old_success:
        expect(page.locator("#login-view")).to_be_visible()
        expect(page.locator("#login-status")).to_have_text("Invalid login or password.")
        expect(page.locator("#password-input")).to_be_focused()
        assert private_requests == []
    else:
        expect(page.locator("#app-view")).to_be_visible()
        expect(page.locator("#url-input")).to_be_focused()
        assert len(private_requests) == 1


@pytest.mark.parametrize("old_status", [200, 500])
def test_late_logout_does_not_clear_new_login(app, page, old_status):
    gate_responses(page, "/api/auth/logout")
    if old_status == 500:
        page.route(
            "**/api/auth/logout",
            lambda route: route.fulfill(
                status=500, json={"error": "Old logout failed"}
            ),
        )
    page.get_by_role("button", name="Log out").click()
    page.wait_for_function("window.requestGates[0]?.ready")
    page.locator("#login-input").evaluate("(el, value) => el.value = value", TEST_LOGIN)
    page.locator("#password-input").evaluate(
        "(el, value) => el.value = value", TEST_PASSWORD
    )
    with page.expect_response("**/api/auth/login"):
        page.locator("#login-form").evaluate(
            "el => el.dispatchEvent(new Event('submit', {cancelable: true}))"
        )
    expect(page.locator("#login-button")).to_be_enabled()
    release_response(page, 0)
    expect(page.locator("#app-view")).to_be_visible()
    expect(page.locator("#login-view")).to_be_hidden()
    expect(page.locator("#form-status")).to_have_text("")
    expect(page.locator("#logout-button")).to_be_enabled()
    page.locator("#url-input").fill("after-late-logout.example")
    page.get_by_role("button", name="Add", exact=True).click()
    expect(page.get_by_role("link", name="after-late-logout.example")).to_be_visible()
