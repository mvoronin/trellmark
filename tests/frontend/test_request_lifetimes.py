import json

import pytest
from playwright.sync_api import expect

from tests.bookmarks import helpers as bookmark_helpers
from tests.frontend.test_import_export import _write_import_file
from tests.frontend.test_url_list import RecordingTitleFetcher
from tests.postgres import TEST_LOGIN, TEST_PASSWORD


def test_shell_modules_import_without_browser_effects(static_page):
    result = static_page.evaluate("""async () => {
      const effects = [];
      window.fetch = () => { effects.push('fetch'); throw new Error('fetch'); };
      Document.prototype.querySelector = () => { effects.push('query'); };
      Document.prototype.querySelectorAll = () => { effects.push('queryAll'); };
      EventTarget.prototype.addEventListener = () => { effects.push('listener'); };
      Object.defineProperty(window, 'localStorage', {get() {
        effects.push('storage'); throw new Error('storage');
      }});
      await import('/static/shell/session.js');
      await import('/static/shell/view.js');
      await import('/static/shared/theme.js');
      return effects;
    }""")
    assert result == []


@pytest.mark.parametrize("old_success", [True, False])
def test_disposed_shell_cannot_touch_reconstructed_session(static_page, old_success):
    result = static_page.evaluate(
        """async oldSuccess => {
      const html = await (await fetch('/index.html')).text();
      const parsed = new DOMParser().parseFromString(html, 'text/html');
      parsed.querySelectorAll('script').forEach(node => node.remove());
      const root = document.createElement('div');
      root.append(...parsed.body.childNodes);
      document.body.append(root);
      const {createShellSession} = await import('/static/shell/session.js');
      const api = await import('/static/api/client.js');
      let oldResolve, oldReject;
      let loads = 0, clears = 0, errors = 0;
      const options = {
        api, onAuthenticated: () => { loads += 1; },
        onPrivateClear: () => { clears += 1; },
        onPrivateInvalidate: () => {}, onError: () => { errors += 1; },
      };
      const requests = [];
      window.fetch = (path, init) => new Promise((resolve, reject) => {
        requests.push({path, init, resolve});
        if (requests.length === 1) { oldResolve = resolve; oldReject = reject; }
      });
      const submit = () => root.querySelector('#login-form').dispatchEvent(
        new Event('submit', {cancelable: true}));
      const old = createShellSession(root, options);
      submit();
      old.dispose();
      const current = createShellSession(root, options);
      submit();
      requests[1].resolve(new Response(JSON.stringify({authenticated: true, csrf_token: 'new-token'})));
      await new Promise(resolve => setTimeout(resolve, 0));
      old.clear(); old.dispose();
      if (oldSuccess) oldResolve(new Response(JSON.stringify({authenticated: true, csrf_token: 'old-token'})));
      else oldReject(new Error('old failure'));
      await new Promise(resolve => setTimeout(resolve, 0));
      const mutation = api.createUrl({url: 'example.com'});
      const token = requests[2].init.headers['X-CSRF-Token'];
      requests[2].resolve(new Response('{}')); await mutation;
      const state = [loads, clears, errors, token,
        root.querySelector('#app-view').hidden,
        root.querySelector('#login-button').disabled];
      current.dispose();
      submit();
      return [...state, requests.length];
    }""",
        old_success,
    )
    assert result == [1, 0, 0, "new-token", False, False, 3]


def test_theme_controls_own_root_and_dispose_with_optional_storage(static_page):
    result = static_page.evaluate("""async () => {
      const {createThemeControls} = await import('/static/shared/theme.js');
      const root = document.createElement('section');
      const outside = document.createElement('button');
      outside.dataset.themeValue = 'dark'; outside.setAttribute('aria-pressed', 'outside');
      document.body.append(outside, root);
      const toggle = document.createElement('div'); toggle.className = 'theme-toggle';
      root.append(toggle);
      for (const value of ['system', 'light', 'dark']) {
        const button = document.createElement('button');
        button.dataset.themeValue = value; toggle.append(button);
      }
      Object.defineProperty(window, 'localStorage', {get() { throw new Error('denied'); }});
      const first = createThemeControls(root);
      toggle.children[2].click();
      const dark = root.dataset.theme;
      first.dispose(); toggle.children[1].click();
      const disposed = root.dataset.theme;
      const next = createThemeControls(root);
      toggle.children[1].click();
      const light = root.dataset.theme;
      toggle.children[0].click();
      const system = root.hasAttribute('data-theme');
      next.dispose();
      return [dark, disposed, light, system, outside.getAttribute('aria-pressed')];
    }""")
    assert result == ["dark", "dark", "light", False, "outside"]


@pytest.mark.parametrize(
    "operations, expected",
    [
        ([], [0, False]),
        (["capture", "capture"], [0, False]),
        (["begin", "capture"], [1, True]),
        (["begin", "begin", "finish-old"], [2, True]),
        (["begin", "begin", "finish-current", "finish-old"], [2, False]),
        (["begin", "invalidate", "finish-old"], [2, False]),
        (["invalidate", "invalidate", "begin"], [3, True]),
    ],
)
def test_monotonic_request_lifetime(static_page, operations, expected):
    result = static_page.evaluate(
        """async (operations) => {
          const {createRequestLifetime} = await import('/static/shared/request.js');
          const lifetime = createRequestLifetime();
          const peer = createRequestLifetime();
          let first;
          for (const operation of operations) {
            if (operation === 'capture') {
              const ticket = lifetime.capture();
              if (!lifetime.isCurrent(ticket)) throw Error('Capture is not current');
            }
            if (operation === 'begin') {
              const ticket = lifetime.begin();
              first ??= ticket;
              if (!lifetime.isCurrent(ticket)) throw Error('Begin is not current');
            }
            if (operation === 'invalidate') lifetime.invalidate();
            if (operation === 'finish-old' && lifetime.finish(first)) {
              throw Error('Old completion accepted');
            }
            if (operation === 'finish-current' && !lifetime.finish(lifetime.capture())) {
              throw Error('Current completion rejected');
            }
          }
          if (peer.capture() !== 0 || peer.inFlight) throw Error('Peer changed');
          return [lifetime.capture(), lifetime.inFlight];
        }""",
        operations,
    )
    assert result == expected


def gate_responses(page, path, *, status=None, payload=None):
    """Hold already received responses; tests explicitly release each promise."""
    page.evaluate(
        """({path, status, payload}) => {
          const nativeFetch = window.fetch.bind(window);
          window.requestGates = [];
          window.gatingEnabled = true;
          window.fetch = async (...args) => {
            if (args[0] !== path || !window.gatingEnabled) return nativeFetch(...args);
            const gate = {ready: false, released: false};
            const wait = new Promise(resolve => { gate.release = resolve; });
            window.requestGates.push(gate);
            const response = status === null
              ? await nativeFetch(...args)
              : new Response(JSON.stringify(payload), {
                  status, headers: {'Content-Type': 'application/json'},
                });
            const body = await response.text();
            gate.ready = true;
            await wait;
            gate.released = true;
            return new Response(body, {status: response.status, headers: response.headers});
          };
        }""",
        {"path": path, "status": status, "payload": payload},
    )


def release_response(page, index):
    page.evaluate("index => window.requestGates[index].release()", index)
    page.wait_for_function("index => window.requestGates[index].released", arg=index)
    # Flush rendering after the explicitly released, buffered response body.
    page.evaluate("() => new Promise(requestAnimationFrame)")


def login_again(page):
    page.get_by_label("Login").fill(TEST_LOGIN)
    page.get_by_label("Password").fill(TEST_PASSWORD)
    page.get_by_role("button", name="Log in", exact=True).click()
    expect(page.locator("#app-view")).to_be_visible()


@pytest.mark.parametrize("stale_status", [200, 409, 500])
def test_old_import_cannot_finish_a_new_sessions_import(
    app, page, tmp_path, stale_status
):
    base_url, _ = app
    bookmark_helpers.seed_group("Existing")
    page.goto(base_url)
    expect(page.get_by_role("heading", name="Existing")).to_be_visible()
    old_file = _write_import_file(tmp_path, "old.json", group_name="Old Import")
    new_file = _write_import_file(tmp_path, "new.json", group_name="New Import")
    gate_responses(page, "/api/import")
    if stale_status != 200:
        page.route(
            "**/api/import",
            lambda route: route.fulfill(
                status=stale_status,
                content_type="application/json",
                body=json.dumps(
                    {
                        "error": "Old import failure",
                        "code": "import_conflict"
                        if stale_status == 409
                        else "import_failed",
                    }
                ),
            ),
            times=1,
        )
    page.locator("#import-input").set_input_files(str(old_file))
    page.wait_for_function("window.requestGates[0]?.ready")
    page.get_by_role("button", name="Log out").click()
    expect(page.locator("#login-view")).to_be_visible()
    login_again(page)
    page.locator("#import-input").set_input_files(str(new_file))
    page.wait_for_function("window.requestGates[1]?.ready")

    release_response(page, 0)

    expect(page.locator("#import-input")).to_be_disabled()
    assert (
        page.locator("#import-input").evaluate("el => el.files[0].name") == "new.json"
    )
    expect(page.locator("#import-retry-button")).to_be_disabled()
    expect(page.locator("#form-status")).to_have_text("")
    release_response(page, 1)
    expect(page.get_by_role("heading", name="New Import")).to_be_visible()
    expect(page.locator("#form-status")).to_have_text(
        "Imported 0, skipped 1." if stale_status == 200 else "Imported 1, skipped 0."
    )
    expect(page.locator("#import-input")).to_be_enabled()
    expect(page.locator("#import-input")).to_have_value("")


@pytest.mark.parametrize("old_status", [201, 500])
def test_old_create_cannot_restore_snapshot_or_finish_new_create(app, page, old_status):
    base_url, _ = app
    page.goto(base_url)
    expect(page.locator("#app-view")).to_be_visible()
    gate_responses(page, "/api/urls")
    if old_status == 500:
        page.route(
            "**/api/urls",
            lambda route: route.fulfill(status=500, json={"error": "Old save failed"}),
            times=1,
        )
    page.locator("#url-input").fill("old.example")
    page.get_by_role("button", name="Add", exact=True).click()
    page.wait_for_function("window.requestGates[0]?.ready")
    page.get_by_role("button", name="Log out").click()
    expect(page.locator("#login-view")).to_be_visible()
    login_again(page)
    page.locator("#url-input").fill("new.example")
    page.get_by_role("button", name="Add", exact=True).click()
    page.wait_for_function("window.requestGates[1]?.ready")
    page.locator("#group-input").focus()

    release_response(page, 0)

    expect(page.locator("#url-input")).to_have_value("new.example")
    expect(page.locator("#save-button")).to_be_disabled()
    expect(page.locator("#group-input")).to_be_focused()
    expect(page.locator("#form-status")).to_have_text("")
    release_response(page, 1)
    expect(page.get_by_role("link", name="new.example", exact=True)).to_be_visible()
    expect(page.locator("#save-button")).to_be_enabled()
    expect(page.locator("#form-status")).to_have_text("Saved.")


@pytest.mark.parametrize("old_status", [200, 500])
def test_old_editor_completion_cannot_change_reopened_editor(app, page, old_status):
    base_url, _ = app
    record = bookmark_helpers.seed_url("https://editor.example", "Original")
    page.goto(base_url)
    expect(page.get_by_role("link", name="Original", exact=True)).to_be_visible()
    gate_responses(page, f"/api/urls/{record['id']}")
    if old_status == 500:
        page.route(
            f"**/api/urls/{record['id']}",
            lambda route: route.fulfill(status=500, json={"error": "Old edit failed"}),
            times=1,
        )
    page.get_by_role(
        "button", name="Edit editor.example in default", exact=True
    ).click()
    page.locator("#url-edit-title").fill("Old editor")
    page.locator("#url-edit-save").click()
    page.wait_for_function("window.requestGates[0]?.ready")
    page.locator("#url-edit-cancel").click()
    page.get_by_role(
        "button", name="Edit editor.example in default", exact=True
    ).click()
    page.locator("#url-edit-title").fill("New editor draft")
    page.locator("#url-edit-save").click()
    page.wait_for_function("window.requestGates[1]?.ready")

    release_response(page, 0)

    expect(page.locator("#url-edit-dialog")).to_be_visible()
    expect(page.locator("#url-edit-title")).to_have_value("New editor draft")
    expect(page.locator("#url-edit-save")).to_be_disabled()
    expect(page.locator("#url-edit-status")).to_have_text("")
    release_response(page, 1)
    expect(page.locator("#url-edit-save")).to_be_enabled()
    if old_status == 200:
        expect(page.locator("#url-edit-status")).to_have_text(
            "This URL was changed. Reload and try again."
        )
    else:
        expect(
            page.get_by_role("link", name="New editor draft", exact=True)
        ).to_be_visible()


def test_import_and_editor_lifetimes_do_not_supersede_each_other(app, page, tmp_path):
    base_url, _ = app
    bookmark_helpers.seed_url("https://independent.example", "Original")
    page.goto(base_url)
    expect(page.get_by_role("link", name="Original", exact=True)).to_be_visible()
    gate_responses(page, "/api/import")
    import_file = _write_import_file(tmp_path, "independent.json")
    page.locator("#import-input").set_input_files(str(import_file))
    page.wait_for_function("window.requestGates[0]?.ready")
    page.get_by_role(
        "button", name="Edit independent.example in default", exact=True
    ).click()
    page.locator("#url-edit-title").fill("Independent edit")
    page.locator("#url-edit-save").click()
    expect(page.locator("#url-edit-dialog")).to_be_hidden()
    expect(page.locator("#form-status")).to_have_text("URL updated.")
    expect(page.locator("#import-input")).to_be_disabled()
    release_response(page, 0)
    expect(page.locator("#form-status")).to_have_text("Imported 1, skipped 0.")
    expect(page.locator("#import-input")).to_be_enabled()
    expect(page.get_by_role("heading", name="Reading")).to_be_visible()


@pytest.mark.parametrize("old_status", [200, 500])
def test_old_group_load_cannot_restore_snapshot_or_error_after_relogin(
    app, page, old_status
):
    base_url, _ = app
    bookmark_helpers.seed_group("Existing")
    page.goto(base_url)
    expect(page.get_by_role("heading", name="Existing")).to_be_visible()
    gate_responses(
        page,
        "/api/groups",
        status=500 if old_status == 500 else None,
        payload={"error": "Old group load failed"},
    )
    # A failed edit requests the authoritative group snapshot through loadGroups.
    page.route(
        "**/api/groups/*",
        lambda route: route.fulfill(status=500, json={"error": "Edit failed"}),
        times=1,
    )
    page.get_by_role("button", name="Edit Existing", exact=True).click()
    page.locator("#group-edit-save").click()
    page.wait_for_function("window.requestGates[0]?.ready")
    page.locator("#group-edit-cancel").click()
    page.evaluate("window.gatingEnabled = false")
    page.get_by_role("button", name="Log out").click()
    expect(page.locator("#login-view")).to_be_visible()
    bookmark_helpers.seed_group("New Session Group")
    login_again(page)
    expect(page.get_by_role("heading", name="New Session Group")).to_be_visible()
    release_response(page, 0)
    page.locator("#sort-select").select_option("domain")
    page.locator('[data-group-filter="all"]').click()
    page.locator('[data-group-filter="safe"]').click()
    expect(page.get_by_role("heading", name="New Session Group")).to_be_visible()
    expect(page.locator("#form-status")).to_have_text("")
    expect(page.locator("#group-status")).to_have_text("")


@pytest.mark.parametrize(
    "title_fetcher", [RecordingTitleFetcher("Fresh title")], indirect=True
)
@pytest.mark.parametrize("operation", ["metadata", "important", "move", "delete"])
@pytest.mark.parametrize("mutation_fails", [False, True])
@pytest.mark.parametrize("logout_failure", ["http", "network"])
def test_failed_logout_preserves_pending_row_operation_ownership(
    app,
    page,
    operation,
    mutation_fails,
    logout_failure,
):
    base_url, _ = app
    record = bookmark_helpers.seed_url("https://pending.example", "Original title")
    destination = bookmark_helpers.seed_group("Destination")
    default_id = next(
        group["id"]
        for group in bookmark_helpers.group_payloads()
        if group["name"] == "default"
    )
    page.goto(base_url)
    expect(page.get_by_role("link", name="Original title", exact=True)).to_be_visible()
    path = {
        "metadata": f"/api/urls/{record['id']}/refresh-metadata",
        "important": f"/api/urls/{record['id']}/important",
        "move": f"/api/urls/{record['id']}/group",
        "delete": f"/api/urls/{record['id']}?group_id={default_id}",
    }[operation]
    gate_responses(
        page,
        path,
        status=500 if mutation_fails else None,
        payload={"error": "Pending action failed"},
    )
    if logout_failure == "http":
        page.route(
            "**/api/auth/logout",
            lambda route: route.fulfill(status=500, json={"error": "Logout failed"}),
        )
    else:
        page.route("**/api/auth/logout", lambda route: route.abort("failed"))
    control = {
        "metadata": page.locator(".refresh-metadata-button"),
        "important": page.locator(".important-toggle"),
        "move": page.locator(".move-select"),
        "delete": page.locator(".delete-button"),
    }[operation]
    if operation == "move":
        control.select_option(str(destination["id"]))
    else:
        if operation == "delete":
            page.locator("#delete-immediately").check()
        control.click()
    page.wait_for_function("window.requestGates[0]?.ready")
    expect(control).to_be_disabled()
    page.get_by_role("button", name="Log out").click()
    expect(page.locator("#form-status")).to_have_text(
        "Logout failed" if logout_failure == "http" else "Failed to fetch"
    )
    expect(page.locator("#logout-button")).to_be_enabled()
    expect(page.locator("#app-view")).to_be_visible()
    release_response(page, 0)
    if mutation_fails:
        expect(page.locator("#form-status")).to_have_text("Pending action failed")
        expect(control).to_be_enabled()
        assert bookmark_helpers.url_payload(record["id"])["title"] == "Original title"
        assert bookmark_helpers.url_group_ids(record["id"]) == [default_id]
    elif operation == "delete":
        expect(page.locator(".url-item")).to_have_count(0)
        assert bookmark_helpers.url_payload(record["id"]) is None
    else:
        expect(control).to_be_enabled()
        if operation == "metadata":
            expect(
                page.get_by_role("link", name="Fresh title", exact=True)
            ).to_be_visible()
            assert bookmark_helpers.url_payload(record["id"])["title"] == "Fresh title"
        elif operation == "important":
            expect(control).to_have_attribute("aria-pressed", "true")
            assert bookmark_helpers.url_payload(record["id"])["important"] is True
        else:
            expect(control).to_have_value(str(destination["id"]))
            assert bookmark_helpers.url_group_ids(record["id"]) == [destination["id"]]
    # A new real CSRF-backed mutation remains usable after either pending result.
    page.evaluate("window.gatingEnabled = false")
    page.locator("#group-input").fill("After failed logout")
    page.locator("#group-button").click()
    expect(
        page.get_by_role("heading", name="After failed logout", exact=True)
    ).to_be_visible()


def test_current_401_during_pending_logout_expires_once_and_ignores_late_failure(
    app, page
):
    gate_responses(
        page, "/api/auth/logout", status=500, payload={"error": "Late logout failure"}
    )
    page.get_by_role("button", name="Log out").click()
    page.wait_for_function("window.requestGates[0]?.ready")
    page.route(
        "**/api/groups",
        lambda route: route.fulfill(status=401, json={"error": "Expired"}),
    )
    page.evaluate("""async () => {
      window.expiryTransitions = 0;
      const login = document.querySelector('#login-view');
      new MutationObserver(records => {
        window.expiryTransitions += records.filter(record => record.attributeName === 'hidden').length;
      }).observe(login, {attributes: true});
      const api = await import('/static/api/client.js');
      await Promise.allSettled([api.listGroups(), api.listGroups()]);
    }""")
    expect(page.locator("#login-view")).to_be_visible()
    expect(page.locator("#login-status")).to_have_text(
        "Your session expired. Log in again."
    )
    release_response(page, 0)
    expect(page.locator("#app-view")).to_be_hidden()
    expect(page.locator("#login-input")).to_be_focused()
    expect(page.locator("#login-status")).to_have_text(
        "Your session expired. Log in again."
    )
    assert page.evaluate("window.expiryTransitions") == 1
