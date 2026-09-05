from playwright.sync_api import expect

from tests.bookmarks import helpers as bookmark_helpers


def test_frontend_folds_and_unfolds_group(app, page):
    base_url, _ = app
    bookmark_helpers.seed_url("https://one.example")

    page.goto(base_url)
    link = page.get_by_role("link", name="one.example")
    expect(link).to_be_visible()

    page.get_by_role("button", name="Toggle default").click()
    expect(link).to_be_hidden()

    page.get_by_role("button", name="Toggle default").click()
    expect(link).to_be_visible()


def test_frontend_fold_state_survives_reload(app, page):
    base_url, _ = app
    bookmark_helpers.seed_url("https://one.example")

    page.goto(base_url)
    page.get_by_role("button", name="Toggle default").click()
    expect(page.get_by_role("link", name="one.example")).to_be_hidden()

    page.reload()
    expect(page.get_by_role("button", name="Toggle default")).to_have_attribute(
        "aria-expanded", "false"
    )
    expect(page.get_by_role("link", name="one.example")).to_be_hidden()


def test_frontend_fold_is_keyed_by_group(app, page):
    base_url, _ = app
    bookmark_helpers.seed_url("https://one.example")
    reading = bookmark_helpers.seed_group("Reading")
    moved = bookmark_helpers.seed_url("https://two.example")
    bookmark_helpers.seed_membership(moved["id"], reading["id"])

    page.goto(base_url)
    page.get_by_role("button", name="Toggle default", exact=True).click()

    expect(page.get_by_role("link", name="one.example")).to_be_hidden()
    expect(page.get_by_role("link", name="two.example")).to_be_visible()
    expect(page.get_by_role("button", name="Toggle Reading")).to_have_attribute(
        "aria-expanded", "true"
    )


def test_frontend_new_group_starts_unfolded(app, page):
    base_url, _ = app
    bookmark_helpers.seed_url("https://one.example")

    page.goto(base_url)
    page.get_by_role("button", name="Toggle default", exact=True).click()

    page.get_by_label("New group").fill("Reading")
    page.get_by_role("button", name="Add group").click()

    expect(page.get_by_role("heading", name="Reading")).to_be_visible()
    expect(page.get_by_role("button", name="Toggle Reading")).to_have_attribute(
        "aria-expanded", "true"
    )
