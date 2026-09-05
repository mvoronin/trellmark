from playwright.sync_api import expect

from tests.bookmarks import helpers as bookmark_helpers
from tests.helpers import grouped_url_ids_in


def test_frontend_renders_groups_and_nested_urls(app, page):
    base_url, _ = app
    reading = bookmark_helpers.seed_group("Reading")
    default_url = bookmark_helpers.seed_url("https://one.example")
    reading_url = bookmark_helpers.seed_url("https://two.example")
    bookmark_helpers.seed_membership(reading_url["id"], reading["id"])

    page.goto(base_url)

    expect(page.locator("#url-count")).to_have_text("2 saved")
    expect(page.get_by_role("heading", name="default")).to_be_visible()
    expect(page.get_by_role("heading", name="Reading")).to_be_visible()

    # Each URL renders under its own group's list.
    default_group = page.locator(
        ".group", has=page.get_by_role("heading", name="default")
    )
    reading_group = page.locator(
        ".group", has=page.get_by_role("heading", name="Reading")
    )
    expect(default_group.get_by_role("link")).to_have_text("one.example")
    expect(reading_group.get_by_role("link")).to_have_text("two.example")
    assert default_url["id"] != reading_url["id"]


def test_frontend_shows_empty_default_group(app, page):
    base_url, _ = app

    page.goto(base_url)

    expect(page.locator("#url-count")).to_have_text("0 saved")
    expect(page.get_by_role("heading", name="default")).to_be_visible()
    expect(page.get_by_text("No URLs yet.")).to_be_visible()


def test_frontend_creates_group(app, page):
    base_url, _ = app

    page.goto(base_url)
    page.get_by_label("New group").fill("Reading")
    page.get_by_role("button", name="Add group").click()

    expect(page.locator("#group-status")).to_have_text("Group added.")
    expect(page.get_by_role("heading", name="Reading")).to_be_visible()
    assert [group["name"] for group in bookmark_helpers.group_payloads()] == [
        "default",
        "Reading",
    ]


def test_frontend_creates_group_domains_and_auto_groups_new_url(app, page):
    base_url, _ = app

    page.goto(base_url)
    page.get_by_label("New group").fill("Reading")
    page.locator("#group-form").get_by_label("Domains", exact=True).fill(
        " Example.COM., news.example "
    )
    page.get_by_role("button", name="Add group").click()
    expect(page.locator("#group-status")).to_have_text("Group added.")
    page.get_by_label("URL").fill("https://example.com/article")
    page.get_by_role("button", name="Add", exact=True).click()

    reading = page.locator(".group", has=page.get_by_role("heading", name="Reading"))
    default = page.locator(".group", has=page.get_by_role("heading", name="default"))
    expect(reading.locator(".group-domains")).to_have_text("example.com, news.example")
    expect(reading.get_by_role("link", name="example.com/article")).to_be_visible()
    expect(default.get_by_role("link")).to_have_count(0)
    expect(page.locator("#url-count")).to_have_text("1 saved")
    assert bookmark_helpers.group_payloads()[1]["domains"] == [
        "example.com",
        "news.example",
    ]


def test_frontend_creates_nsfw_group_and_clears_checkbox(app, page):
    base_url, _ = app

    page.goto(base_url)
    group_form = page.locator("#group-form")
    group_form.get_by_label("New group").fill("Comics")
    group_form.get_by_label("NSFW").check()
    group_form.get_by_role("button", name="Add group").click()

    expect(page.locator("#group-status")).to_have_text("Group added.")
    expect(page.get_by_role("heading", name="Comics")).to_have_count(0)
    expect(group_form.get_by_label("NSFW")).not_to_be_checked()
    assert bookmark_helpers.group_payloads()[1]["nsfw"] is True

    page.get_by_role("button", name="All", exact=True).click()
    expect(page.get_by_role("heading", name="Comics")).to_be_visible()


def test_frontend_safe_filter_hides_nsfw_groups_and_urls_by_default(app, page):
    base_url, _ = app
    private = bookmark_helpers.seed_group("Private", nsfw=True)
    safe_url = bookmark_helpers.seed_url("https://safe.example")
    private_url = bookmark_helpers.seed_url("https://private.example")
    bookmark_helpers.seed_membership(private_url["id"], private["id"])

    page.goto(base_url)

    expect(page.get_by_role("button", name="Safe", exact=True)).to_have_attribute(
        "aria-pressed", "true"
    )
    expect(page.locator("#url-count")).to_have_text("1 saved")
    expect(page.get_by_role("heading", name="Private")).to_have_count(0)
    expect(page.get_by_role("link", name="private.example")).to_have_count(0)
    expect(page.get_by_role("link", name="safe.example")).to_be_visible()
    assert safe_url["id"] != private_url["id"]

    page.get_by_role("button", name="All", exact=True).click()

    expect(page.locator("#url-count")).to_have_text("2 saved")
    expect(page.get_by_role("heading", name="Private")).to_be_visible()
    expect(page.get_by_role("link", name="private.example")).to_be_visible()


def test_frontend_edits_group_name_and_nsfw_flag(app, page):
    base_url, _ = app
    bookmark_helpers.seed_group("Reading", nsfw=True)

    page.goto(base_url)
    page.get_by_role("button", name="All", exact=True).click()
    page.get_by_role("button", name="Edit Reading").click()
    dialog = page.locator("#group-edit-dialog")
    dialog.get_by_label("Group name").fill("Later")
    dialog.get_by_label("NSFW").uncheck()
    dialog.get_by_label("Domains").fill(" Example.COM.\nnews.example ")
    dialog.get_by_role("button", name="Save").click()

    expect(dialog).not_to_be_visible()
    expect(page.locator("#group-status")).to_have_text("Group updated.")
    expect(page.get_by_role("heading", name="Later")).to_be_visible()
    page.get_by_role("button", name="Safe", exact=True).click()
    expect(page.get_by_role("heading", name="Later")).to_be_visible()
    assert bookmark_helpers.group_payloads()[1]["name"] == "Later"
    assert bookmark_helpers.group_payloads()[1]["nsfw"] is False
    assert bookmark_helpers.group_payloads()[1]["domains"] == [
        "example.com",
        "news.example",
    ]


def test_frontend_default_group_has_no_edit_or_delete_controls(app, page):
    base_url, _ = app

    page.goto(base_url)

    expect(page.get_by_role("button", name="Edit default")).to_have_count(0)
    expect(page.get_by_role("button", name="Delete group default")).to_have_count(0)


def test_frontend_deletes_group_and_moves_links_to_default(app, page):
    base_url, _ = app
    reading = bookmark_helpers.seed_group("Reading")
    url = bookmark_helpers.seed_url("https://one.example")
    bookmark_helpers.seed_membership(url["id"], reading["id"])

    page.goto(base_url)
    page.get_by_role("button", name="Delete group Reading").click()
    dialog = page.locator("#group-delete-dialog")
    expect(dialog).to_contain_text("Reading")
    dialog.get_by_role("button", name="Move links to default").click()

    expect(page.locator("#group-status")).to_have_text(
        "Group deleted; 1 link moved to default."
    )
    expect(page.get_by_role("heading", name="Reading")).to_have_count(0)
    default_group = page.locator(
        ".group", has=page.get_by_role("heading", name="default")
    )
    expect(default_group.get_by_role("link", name="one.example")).to_be_visible()
    assert grouped_url_ids_in(
        {"groups": bookmark_helpers.group_payloads()}, "default"
    ) == [url["id"]]


def test_frontend_deletes_group_and_included_links(app, page):
    base_url, _ = app
    reading = bookmark_helpers.seed_group("Reading")
    url = bookmark_helpers.seed_url("https://one.example")
    bookmark_helpers.seed_membership(url["id"], reading["id"])

    page.goto(base_url)
    page.get_by_role("button", name="Delete group Reading").click()
    page.locator("#group-delete-dialog").get_by_role(
        "button", name="Delete links"
    ).click()

    expect(page.locator("#group-status")).to_have_text("Group and 1 link deleted.")
    expect(page.get_by_role("heading", name="Reading")).to_have_count(0)
    expect(page.get_by_role("link", name="one.example")).to_have_count(0)
    assert bookmark_helpers.saved_urls() == []


def test_frontend_group_delete_does_not_claim_shared_url_was_deleted(app, page):
    base_url, _ = app
    first = bookmark_helpers.seed_group("First", domains=["example.com"])
    second = bookmark_helpers.seed_group("Second", domains=["example.com"])
    saved = bookmark_helpers.seed_url("https://example.com/x")

    page.goto(base_url)
    page.get_by_role("button", name="Delete group First").click()
    page.locator("#group-delete-dialog").get_by_role(
        "button", name="Delete links"
    ).click()

    expect(page.locator("#group-status")).to_have_text("Group deleted.")
    second_section = page.locator(
        ".group", has=page.get_by_role("heading", name="Second")
    )
    expect(second_section.get_by_role("link", name="example.com/x")).to_be_visible()
    assert bookmark_helpers.url_group_ids(saved["id"]) == [second["id"]]
    assert first["id"] not in bookmark_helpers.url_group_ids(saved["id"])


def test_frontend_group_delete_requires_explicit_choice(app, page):
    base_url, _ = app
    bookmark_helpers.seed_group("Reading")

    page.goto(base_url)
    page.get_by_role("button", name="Delete group Reading").click()
    page.locator("#group-delete-dialog").get_by_role("button", name="Cancel").click()

    expect(page.get_by_role("heading", name="Reading")).to_be_visible()
    assert [group["name"] for group in bookmark_helpers.group_payloads()] == [
        "default",
        "Reading",
    ]


def test_frontend_shows_error_for_duplicate_group(app, page):
    base_url, _ = app
    bookmark_helpers.seed_group("Reading")

    page.goto(base_url)
    page.get_by_label("New group").fill("Reading")
    page.get_by_role("button", name="Add group").click()

    expect(page.locator("#group-status")).to_have_text("This group already exists.")
    assert [group["name"] for group in bookmark_helpers.group_payloads()] == [
        "default",
        "Reading",
    ]


def test_frontend_moves_url_to_group(app, page):
    base_url, _ = app
    bookmark_helpers.seed_url("https://one.example")
    bookmark_helpers.seed_group("Reading")

    page.goto(base_url)
    page.get_by_label("Move one.example").select_option(label="Reading")

    expect(page.locator("#form-status")).to_have_text("Moved.")
    reading_group = page.locator(
        ".group", has=page.get_by_role("heading", name="Reading")
    )
    default_group = page.locator(
        ".group", has=page.get_by_role("heading", name="default")
    )
    expect(reading_group.get_by_role("link")).to_have_text("one.example")
    expect(default_group.get_by_role("link")).to_have_count(0)
    assert grouped_url_ids_in(
        {"groups": bookmark_helpers.group_payloads()}, "Reading"
    ) == [bookmark_helpers.url_payloads()[0]["id"]]


def test_frontend_move_and_delete_change_only_the_current_group(app, page):
    base_url, _ = app
    source = bookmark_helpers.seed_group("Source", domains=["example.com"])
    existing = bookmark_helpers.seed_group("Existing", domains=["example.com"])
    target = bookmark_helpers.seed_group("Target")
    saved = bookmark_helpers.seed_url("https://example.com/article")

    page.goto(base_url)
    source_section = page.locator(
        ".group", has=page.get_by_role("heading", name="Source")
    )
    source_section.get_by_label("Move example.com/article").select_option(
        label="Target"
    )

    expect(source_section.get_by_role("link")).to_have_count(0)
    existing_section = page.locator(
        ".group", has=page.get_by_role("heading", name="Existing")
    )
    target_section = page.locator(
        ".group", has=page.get_by_role("heading", name="Target")
    )
    expect(
        existing_section.get_by_role("link", name="example.com/article")
    ).to_be_visible()
    expect(
        target_section.get_by_role("link", name="example.com/article")
    ).to_be_visible()
    assert bookmark_helpers.url_group_ids(saved["id"]) == [existing["id"], target["id"]]

    page.get_by_label("Delete immediately").check()
    existing_section.get_by_role("button", name="Delete example.com/article").click()

    expect(existing_section.get_by_role("link")).to_have_count(0)
    expect(
        target_section.get_by_role("link", name="example.com/article")
    ).to_be_visible()
    assert bookmark_helpers.url_group_ids(saved["id"]) == [target["id"]]
    assert source["id"] not in bookmark_helpers.url_group_ids(saved["id"])
