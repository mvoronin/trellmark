import json

from playwright.sync_api import expect

from tests.bookmarks import helpers as bookmark_helpers


def test_frontend_toggles_url_important(app, page):
    base_url, _ = app
    bookmark_helpers.seed_url("https://one.example")

    page.goto(base_url)
    toggle = page.get_by_role("button", name="Mark one.example important")
    expect(toggle).to_have_attribute("aria-pressed", "false")

    toggle.click()

    toggle = page.get_by_role("button", name="Mark one.example important")
    expect(page.locator("#form-status")).to_have_text("Updated.")
    expect(toggle).to_have_attribute("aria-pressed", "true")
    assert bookmark_helpers.url_payloads()[0]["important"] is True

    page.reload()
    toggle = page.get_by_role("button", name="Mark one.example important")
    expect(toggle).to_have_attribute("aria-pressed", "true")

    toggle.click()

    toggle = page.get_by_role("button", name="Mark one.example important")
    expect(toggle).to_have_attribute("aria-pressed", "false")
    assert bookmark_helpers.url_payloads()[0]["important"] is False


def test_frontend_important_error_restores_toggle(app, page):
    base_url, _ = app
    bookmark_helpers.seed_url("https://one.example")

    page.goto(base_url)
    page.route(
        "**/api/urls/*/important",
        lambda route: route.fulfill(
            status=400,
            content_type="application/json",
            body=json.dumps({"error": "Could not update important flag."}),
        ),
    )

    toggle = page.get_by_role("button", name="Mark one.example important")
    expect(toggle).to_have_attribute("aria-pressed", "false")
    toggle.click()

    toggle = page.get_by_role("button", name="Mark one.example important")
    expect(page.locator("#form-status")).to_have_text(
        "Could not update important flag."
    )
    expect(toggle).to_have_attribute("aria-pressed", "false")
    assert bookmark_helpers.url_payloads()[0]["important"] is False
