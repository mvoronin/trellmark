import json

from playwright.sync_api import expect

import trellmark
from tests.helpers import grouped_url_ids_in


def test_frontend_exports_json_file(app, page):
    base_url, _ = app
    reading = trellmark.add_group("Reading")
    saved = trellmark.add_url("https://one.example")
    trellmark.move_url_to_group(saved["id"], reading["id"])

    page.goto(base_url)

    with page.expect_download() as download_info:
        page.get_by_role("button", name="Export").click()

    download = download_info.value
    payload = json.loads(download.path().read_text(encoding="utf-8"))
    assert download.suggested_filename == "trellmark-export.json"
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
    import_file = tmp_path / "trellmark-import.json"
    import_file.write_text(
        json.dumps(
            {
                "version": 1,
                "exported_at": "2026-07-03T12:00:00Z",
                "groups": [
                    {
                        "name": "Reading",
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

    page.goto(base_url)
    # Reload restores the existing server session asynchronously. A real user
    # cannot reach this file input while the app view is hidden; wait for the
    # same boundary before Playwright sets a file on the hidden control.
    expect(page.locator("#app-view")).to_be_visible()
    page.locator("#import-input").set_input_files(str(import_file))

    expect(page.locator("#form-status")).to_have_text("Imported 1, skipped 0.")
    expect(page.locator("#url-count")).to_have_text("1 saved")
    expect(page.get_by_role("heading", name="Reading")).to_be_visible()
    expect(page.get_by_role("link", name="example.com")).to_have_attribute(
        "href", "https://example.com"
    )
    assert grouped_url_ids_in(
        {"groups": trellmark.read_group_records()}, "Reading"
    ) == [trellmark.read_url_records()[0]["id"]]
