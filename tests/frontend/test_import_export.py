import json
from datetime import datetime

import pytest
from playwright.sync_api import expect
from sqlalchemy.exc import SQLAlchemyError

from tests.bookmarks import helpers as bookmark_helpers
from tests.helpers import (
    grouped_url_ids_in,
    logical_bookmark_snapshot,
    public_bookmark_snapshot,
)


def _write_import_file(tmp_path, filename, *, group_name="Reading"):
    import_file = tmp_path / filename
    import_file.write_text(
        json.dumps(
            {
                "version": 1,
                "exported_at": "2026-07-03T12:00:00Z",
                "groups": [
                    {
                        "name": group_name,
                        "position": 0,
                        "urls": [
                            {
                                "url": "Example.com/",
                                "created_at": "2026-07-03T12:00:01Z",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return import_file


def test_backup_factory_clear_preserves_new_request_ownership(static_page):
    assert static_page.evaluate("""async () => {
      const {createBackupControls} = await import('/static/shell/backup.js');
      const {createRequestLifetime} = await import('/static/shared/request.js');
      const root = document.createElement('section');
      root.innerHTML = '<button id="export-button"></button>'
        + '<input type="file" id="import-input">'
        + '<button id="import-retry-button"></button>';
      document.body.append(root);
      const privateLifetime = createRequestLifetime();
      const releases = [];
      const statuses = [];
      const replacements = [];
      let finish;
      const completed = new Promise(resolve => { finish = resolve; });
      let requests = 0;
      window.fetch = async () => {
        requests++;
        return new Response(JSON.stringify({groups: [], imported: 1, skipped: 0}),
          {status: 200, headers: {'Content-Type': 'application/json'}});
      };
      const controls = createBackupControls(root, {
        privateLifetime,
        refreshGroups: () => new Promise(resolve => releases.push(resolve)),
        replaceGroups: groups => replacements.push(groups),
        setStatus: message => {
          statuses.push(message);
          if (message) finish();
        },
      });
      const input = root.querySelector('input');
      function choose(name) {
        const transfer = new DataTransfer();
        transfer.items.add(new File(['{}'], name));
        input.files = transfer.files;
        input.dispatchEvent(new Event('change'));
      }
      const tick = () => new Promise(resolve => setTimeout(resolve, 0));
      choose('old.json');
      privateLifetime.invalidate();
      controls.clear();
      const cleared = input.files.length === 0;
      choose('new.json');
      releases[0]();
      await tick();
      const guarded = input.disabled && input.files[0].name === 'new.json'
        && requests === 0 && replacements.length === 0;
      releases[1]();
      await completed;
      await tick();
      const succeeded = !input.disabled && input.files.length === 0
        && replacements.length === 1 && statuses.at(-1) === 'Imported 1, skipped 0.';
      controls.dispose();
      choose('disposed.json');
      await tick();
      return [cleared, guarded, succeeded, requests, releases.length];
    }""") == [True, True, True, 1, 2]


def test_frontend_exports_json_file(app, page):
    base_url, _ = app
    reading = bookmark_helpers.seed_group("Reading")
    saved = bookmark_helpers.seed_url("https://one.example")
    bookmark_helpers.seed_membership(saved["id"], reading["id"])

    page.goto(base_url)

    with page.expect_download() as download_info:
        page.get_by_role("button", name="Export").click()

    download = download_info.value
    payload = json.loads(download.path().read_text(encoding="utf-8"))
    exported_at = datetime.fromisoformat(payload["exported_at"].replace("Z", "+00:00"))
    assert download.suggested_filename == (
        f"trellmark-export-{exported_at:%Y%m%dT%H%M%SZ}.json"
    )
    assert payload["version"] == 1
    assert payload["groups"] == [
        {
            "name": "default",
            "parent": None,
            "position": 0,
            "nsfw": False,
            "domains": [],
            "urls": [],
        },
        {
            "name": "Reading",
            "parent": None,
            "position": 1,
            "nsfw": False,
            "domains": [],
            "urls": [
                {
                    "url": "https://one.example",
                    "title": None,
                    "created_at": saved["created_at"],
                    "important": False,
                }
            ],
        },
    ]
    expect(page.locator("#form-status")).to_have_text("Exported.")


def test_frontend_imports_json_file(app, page, tmp_path):
    base_url, _ = app
    import_file = _write_import_file(tmp_path, "trellmark-import.json")

    page.goto(base_url)
    # Reload restores the existing server session asynchronously. A real user
    # cannot reach this file input while the app view is hidden; wait for the
    # same boundary before Playwright sets a file on the hidden control.
    expect(page.locator("#app-view")).to_be_visible()
    retry = page.locator("#import-retry-button")
    expect(retry).to_have_text("Retry import")
    expect(retry).to_be_hidden()
    expect(retry).to_be_disabled()
    page.locator("#import-input").set_input_files(str(import_file))

    expect(page.locator("#form-status")).to_have_text("Imported 1, skipped 0.")
    expect(page.locator("#url-count")).to_have_text("1 saved")
    expect(page.get_by_role("heading", name="Reading")).to_be_visible()
    expect(page.get_by_role("link", name="example.com")).to_have_attribute(
        "href", "https://example.com"
    )
    assert grouped_url_ids_in(
        {"groups": bookmark_helpers.group_payloads()}, "Reading"
    ) == [bookmark_helpers.url_payloads()[0]["id"]]
    expect(page.locator("#import-input")).to_have_value("")
    expect(retry).to_be_hidden()
    expect(retry).to_be_disabled()


@pytest.mark.parametrize(
    ("status", "code", "message"),
    [
        (
            409,
            "import_conflict",
            "Another bookmark change is in progress. No import changes were saved. "
            "Try again.",
        ),
        (
            500,
            "import_failed",
            "Import failed. No import changes were saved. Try again.",
        ),
    ],
)
def test_frontend_retains_import_state_for_one_manual_retry(
    app, page, tmp_path, status, code, message
):
    base_url, _ = app
    bookmark_helpers.seed_group("Existing")
    import_file = _write_import_file(tmp_path, f"retry-{code}.json")
    import_requests = []
    groups_requests = []

    page.goto(base_url)
    expect(page.get_by_role("heading", name="Existing")).to_be_visible()
    page.on(
        "request",
        lambda request: (
            groups_requests.append(request.url)
            if request.url.endswith("/api/groups")
            else None
        ),
    )

    def fail_once(route):
        import_requests.append(route.request.post_data)
        if len(import_requests) == 1:
            route.fulfill(
                status=status,
                content_type="application/json",
                body=json.dumps({"error": message, "code": code}),
            )
        else:
            route.continue_()

    page.route("**/api/import", fail_once)
    import_input = page.locator("#import-input")
    retry = page.locator("#import-retry-button")

    import_input.set_input_files(str(import_file))

    expect(page.locator("#form-status")).to_have_text(message)
    expect(page.get_by_role("heading", name="Existing")).to_be_visible()
    assert import_input.evaluate("input => input.files.length") == 1
    assert import_input.evaluate("input => input.files[0].name") == import_file.name
    expect(retry).to_be_visible()
    expect(retry).to_be_enabled()
    assert len(import_requests) == 1
    assert groups_requests == []

    retry.evaluate("button => button.click()")
    expect(retry).to_be_disabled()
    retry.evaluate("button => button.click()")

    expect(page.locator("#form-status")).to_have_text("Imported 1, skipped 0.")
    expect(page.get_by_role("heading", name="Reading")).to_be_visible()
    assert len(import_requests) == 2
    assert import_requests[0] == import_requests[1]
    assert groups_requests == []
    expect(import_input).to_have_value("")
    assert import_input.evaluate("input => input.files.length") == 0
    expect(retry).to_be_hidden()
    expect(retry).to_be_disabled()


def test_frontend_invalid_import_requires_a_corrected_file(app, page, tmp_path):
    base_url, _ = app
    bookmark_helpers.seed_group("Existing")
    invalid_file = tmp_path / "invalid-import.json"
    invalid_file.write_text("{", encoding="utf-8")
    corrected_file = _write_import_file(tmp_path, "corrected-import.json")
    import_requests = []

    page.goto(base_url)
    expect(page.get_by_role("heading", name="Existing")).to_be_visible()

    def reject_invalid(route):
        import_requests.append(route.request.post_data)
        if len(import_requests) == 1:
            route.fulfill(
                status=422,
                content_type="application/json",
                body=json.dumps(
                    {"error": "Invalid import file.", "code": "invalid_import"}
                ),
            )
        else:
            route.continue_()

    page.route("**/api/import", reject_invalid)
    import_input = page.locator("#import-input")
    retry = page.locator("#import-retry-button")

    import_input.set_input_files(str(invalid_file))

    expect(page.locator("#form-status")).to_have_text("Invalid import file.")
    expect(page.get_by_role("heading", name="Existing")).to_be_visible()
    assert import_input.evaluate("input => input.files[0].name") == invalid_file.name
    expect(retry).to_be_hidden()
    expect(retry).to_be_disabled()
    assert len(import_requests) == 1

    import_input.set_input_files(str(corrected_file))

    expect(page.locator("#form-status")).to_have_text("Imported 1, skipped 0.")
    assert len(import_requests) == 2
    expect(import_input).to_have_value("")
    expect(retry).to_be_hidden()
    expect(retry).to_be_disabled()


def test_frontend_retries_the_retained_file_after_real_import_rollback(
    app, page, tmp_path, backup_uow_factory
):
    base_url, _ = app
    bookmark_helpers.seed_group("Existing")
    import_file = _write_import_file(tmp_path, "rollback-retry.json")
    direct_before = logical_bookmark_snapshot()
    public_before = public_bookmark_snapshot(base_url)
    requests = []
    stages = []

    def fail_after_url_metadata(stage):
        stages.append(stage)
        if stage == "url_metadata":
            raise SQLAlchemyError("Synthetic late restore failure")

    backup_uow_factory.observer = fail_after_url_metadata
    page.goto(base_url)
    expect(page.get_by_role("heading", name="Existing")).to_be_visible()
    page.on(
        "request",
        lambda request: (
            requests.append(request.post_data)
            if request.url.endswith("/api/import")
            else None
        ),
    )
    import_input = page.locator("#import-input")
    retry = page.locator("#import-retry-button")
    with page.expect_response("**/api/import") as failed_response:
        import_input.set_input_files(str(import_file))

    assert failed_response.value.status == 500
    assert failed_response.value.headers["cache-control"] == "no-store"
    assert failed_response.value.json() == {
        "error": "Import failed. No import changes were saved. Try again.",
        "code": "import_failed",
    }
    expect(page.locator("#form-status")).to_have_text(
        "Import failed. No import changes were saved. Try again."
    )
    assert stages[-1] == "url_metadata"
    assert logical_bookmark_snapshot() == direct_before
    assert public_bookmark_snapshot(base_url) == public_before
    expect(page.get_by_role("heading", name="Existing")).to_be_visible()
    expect(page.get_by_role("heading", name="Reading")).to_have_count(0)
    assert import_input.evaluate("input => input.files[0].name") == import_file.name
    expect(retry).to_be_enabled()
    assert len(requests) == 1

    backup_uow_factory.observer = None
    with page.expect_response("**/api/import") as successful_response:
        retry.click()

    assert successful_response.value.status == 200
    assert successful_response.value.headers["cache-control"] == "no-store"
    expect(page.locator("#form-status")).to_have_text("Imported 1, skipped 0.")
    expect(page.get_by_role("heading", name="Reading")).to_be_visible()
    assert len(requests) == 2 and requests[0] == requests[1]
    expect(import_input).to_have_value("")
    expect(retry).to_be_hidden()
    expect(retry).to_be_disabled()


def test_logout_clears_retained_import_state(app, page, tmp_path):
    base_url, _ = app
    bookmark_helpers.seed_group("Existing")
    import_file = _write_import_file(tmp_path, "logout-import.json")

    page.add_init_script(
        """
        (() => {
          const nativeFetch = window.fetch.bind(window);
          let releaseSession;
          let releaseGroups;
          const sessionGate = new Promise((resolve) => {
            releaseSession = resolve;
          });
          const groupsGate = new Promise((resolve) => {
            releaseGroups = resolve;
          });
          window.__releaseSession = releaseSession;
          window.__releaseGroups = releaseGroups;
          window.__groupsResponseReady = false;
          window.fetch = async (...args) => {
            const input = args[0];
            const url = typeof input === "string" ? input : input.url;
            if (url.endsWith("/api/auth/session")) {
              await sessionGate;
            }
            const response = await nativeFetch(...args);
            if (url.endsWith("/api/groups")) {
              window.__groupsResponseReady = true;
              await groupsGate;
            }
            return response;
          };
        })();
        """
    )
    page.goto(base_url)
    page.route(
        "**/api/import",
        lambda route: route.fulfill(
            status=409,
            content_type="application/json",
            body=json.dumps(
                {
                    "error": "Another bookmark change is in progress. No import "
                    "changes were saved. Try again.",
                    "code": "import_conflict",
                }
            ),
        ),
    )
    import_input = page.locator("#import-input")
    retry = page.locator("#import-retry-button")
    import_input.set_input_files(str(import_file))
    page.wait_for_function("!document.querySelector('#import-retry-button').hidden")

    page.evaluate("window.__releaseSession()")
    expect(page.locator("#app-view")).to_be_visible()
    expect(retry).to_be_visible()
    page.wait_for_function("window.__groupsResponseReady === true")
    page.get_by_role("button", name="Log out").click()

    expect(page.locator("#login-view")).to_be_visible()
    assert import_input.evaluate("input => input.files.length") == 0
    expect(retry).to_be_hidden()
    expect(retry).to_be_disabled()
    page.evaluate("window.__releaseGroups()")
    expect(page.locator("#groups")).to_be_empty()
