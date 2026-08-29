from playwright.sync_api import expect

import trellmark
from tests.helpers import child_names_in, group_in, stored_groups

# Figure spaces, one indent per level below the root. Must match optionLabel()
# in web/src/app.ts.
INDENT = "\u2007\u2007"


def header_for(page, name):
    return page.locator(
        ".group-header", has=page.get_by_role("heading", name=name, exact=True)
    )


def option_labels(select, expected_count):
    r"""Raw option text, indentation included.

    Read through textContent rather than asserted with `to_have_text`, which
    normalizes whitespace before comparing: U+2007 matches `\s`, so every depth
    would collapse to the same string and any indentation would pass.
    """
    options = select.locator("option")
    expect(options).to_have_count(expected_count)
    return options.evaluate_all("options => options.map(option => option.textContent)")


def test_frontend_renders_three_levels_in_tree_order_with_direct_counts(app, page):
    base_url, _ = app
    parent = trellmark.add_group("Parent")
    child = trellmark.add_group("Child", parent_id=parent["id"])
    grandchild = trellmark.add_group("Grandchild", parent_id=child["id"])

    trellmark.add_url("https://default.example")
    child_url = trellmark.add_url("https://child.example")
    trellmark.move_url_to_group(child_url["id"], child["id"])
    grandchild_url = trellmark.add_url("https://grandchild.example")
    trellmark.move_url_to_group(grandchild_url["id"], grandchild["id"])

    page.goto(base_url)

    expect(page.locator(".group-name")).to_have_text(
        ["default", "Parent", "Child", "Grandchild"]
    )
    expect(page.locator('.group[data-depth="1"]')).to_have_count(2)
    expect(page.locator('.group[data-depth="2"]')).to_have_count(1)
    expect(page.locator('.group[data-depth="3"]')).to_have_count(1)
    expect(header_for(page, "Parent").locator(".group-count")).to_have_text("0")
    expect(header_for(page, "Child").locator(".group-count")).to_have_text("1")
    expect(header_for(page, "Grandchild").locator(".group-count")).to_have_text("1")
    expect(page.locator("#url-count")).to_have_text("3 saved")

    child_section = header_for(page, "Child").locator("..")
    child_link = child_section.get_by_role("link", name="child.example", exact=True)
    grandchild_header = header_for(page, "Grandchild")
    assert child_link.bounding_box()["y"] < grandchild_header.bounding_box()["y"]

    move = page.get_by_label("Move child.example from Child")
    assert option_labels(move, 4) == [
        "default",
        "Parent",
        f"{INDENT}— Child",
        f"{INDENT * 2}— Grandchild",
    ]


def test_frontend_parent_fold_hides_subtree_and_preserves_child_fold(app, page):
    base_url, _ = app
    parent = trellmark.add_group("Parent")
    child = trellmark.add_group("Child", parent_id=parent["id"])
    saved = trellmark.add_url("https://child.example")
    trellmark.move_url_to_group(saved["id"], child["id"])

    page.goto(base_url)
    page.get_by_role("button", name="Toggle Child").click()
    page.get_by_role("button", name="Toggle Parent").click()

    expect(header_for(page, "Child")).to_be_hidden()
    page.get_by_role("button", name="Toggle Parent").click()
    expect(header_for(page, "Child")).to_be_visible()
    expect(page.get_by_role("button", name="Toggle Child")).to_have_attribute(
        "aria-expanded", "false"
    )
    expect(page.get_by_role("link", name="child.example")).to_be_hidden()

    page.reload()
    expect(page.get_by_role("button", name="Toggle Parent")).to_have_attribute(
        "aria-expanded", "true"
    )
    expect(page.get_by_role("button", name="Toggle Child")).to_have_attribute(
        "aria-expanded", "false"
    )


def test_frontend_safe_filter_hides_an_nsfw_groups_safe_descendants(app, page):
    base_url, _ = app
    visible = trellmark.add_group("Visible", domains=["shared.example"])
    private = trellmark.add_group("Private", nsfw=True, domains=["shared.example"])
    trellmark.add_group("Safe child", parent_id=private["id"])
    saved = trellmark.add_url("https://shared.example/article")

    page.goto(base_url)

    expect(header_for(page, "Private")).to_have_count(0)
    expect(header_for(page, "Safe child")).to_have_count(0)
    expect(page.get_by_role("link", name="shared.example/article")).to_have_count(1)
    expect(page.locator("#url-count")).to_have_text("1 saved")
    assert visible["id"] in trellmark.read_url_group_ids(saved["id"])

    page.get_by_role("button", name="All", exact=True).click()
    expect(header_for(page, "Private")).to_be_visible()
    expect(header_for(page, "Safe child")).to_be_visible()
    expect(page.get_by_role("link", name="shared.example/article")).to_have_count(2)
    expect(page.locator("#url-count")).to_have_text("1 saved")


def test_frontend_parent_selects_create_and_reparent_groups(app, page):
    base_url, _ = app
    parent = trellmark.add_group("Parent")
    child = trellmark.add_group("Child", parent_id=parent["id"])
    grandchild = trellmark.add_group("Grandchild", parent_id=child["id"])
    other = trellmark.add_group("Other")

    page.goto(base_url)

    create_parent = page.locator("#group-parent")
    assert option_labels(create_parent, 5) == [
        "Root",
        "default",
        "Parent",
        f"{INDENT}— Child",
        "Other",
    ]
    create_parent.select_option(str(parent["id"]))
    page.get_by_label("New group").fill("Created child")
    page.get_by_role("button", name="Add group").click()

    expect(page.locator("#group-status")).to_have_text("Group added.")
    assert "Created child" in child_names_in(stored_groups(), "Parent")
    expect(header_for(page, "Created child")).to_be_visible()

    page.get_by_role("button", name="Edit Child").click()
    edit_parent = page.locator("#group-edit-parent")
    edit_values = edit_parent.locator("option").evaluate_all(
        "options => options.map(option => option.value)"
    )
    assert str(child["id"]) not in edit_values
    assert str(grandchild["id"]) not in edit_values
    assert str(other["id"]) in edit_values

    edit_parent.select_option(str(other["id"]))
    page.locator("#group-edit-dialog").get_by_role("button", name="Save").click()

    expect(page.locator("#group-status")).to_have_text("Group updated.")
    assert child_names_in(stored_groups(), "Other") == ["Child"]
    assert group_in(stored_groups(), "Child")["depth"] == 2


def test_frontend_create_parent_select_falls_back_to_root_when_choice_is_deleted(
    app, page
):
    base_url, _ = app
    parent = trellmark.add_group("Parent")

    page.goto(base_url)
    create_parent = page.locator("#group-parent")
    create_parent.select_option(str(parent["id"]))

    page.get_by_role("button", name="Delete group Parent").click()
    page.locator("#group-delete-dialog").get_by_role(
        "button", name="Delete links"
    ).click()
    expect(page.locator("#group-status")).to_have_text("Group deleted.")

    # The pending choice is gone, so the select has to land on a real option: an
    # id matching none of them leaves selectedIndex at -1, where the control
    # renders blank while still reading back as Root.
    assert option_labels(create_parent, 2) == ["Root", "default"]
    assert create_parent.evaluate("select => select.selectedIndex") == 0
    expect(create_parent).to_have_value("")
