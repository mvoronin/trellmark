import json

from playwright.sync_api import expect

from tests.bookmarks import helpers as bookmark_helpers
from tests.postgres import TEST_LOGIN, TEST_PASSWORD


def group_header(page, name):
    return page.locator(
        ".group-header", has=page.get_by_role("heading", name=name, exact=True)
    )


def _drag_points(source, target, y_fraction):
    """Start and end points for dragging source onto target's upper region.

    The reorder logic inserts before the target when the drop lands in its top
    half, so `y_fraction` well below 0.5 lands the source first.
    """
    src = source.bounding_box()
    dst = target.bounding_box()
    start = (src["x"] + src["width"] / 2, src["y"] + src["height"] / 2)
    end = (dst["x"] + dst["width"] / 2, dst["y"] + dst["height"] * y_fraction)
    return start, end


def mouse_drag_over(page, source, target, y_fraction=0.15):
    """Drag source onto target and hold there, button still down.

    Leaving the button down is what lets a test observe drop indicators: they
    are cleared on pointerup, so they only exist mid-drag.
    """
    (sx, sy), (ex, ey) = _drag_points(source, target, y_fraction)
    page.mouse.move(sx, sy)
    page.mouse.down()
    # Two steps: cross the drag threshold, then settle onto the target.
    page.mouse.move((sx + ex) / 2, (sy + ey) / 2, steps=6)
    page.mouse.move(ex, ey, steps=6)


def mouse_drag(page, source, target, y_fraction=0.15):
    mouse_drag_over(page, source, target, y_fraction)
    page.mouse.up()


def touch_drag(page, source, target, y_fraction=0.15):
    (sx, sy), (ex, ey) = _drag_points(source, target, y_fraction)
    client = page.context.new_cdp_session(page)
    client.send(
        "Input.dispatchTouchEvent",
        {
            "type": "touchStart",
            "touchPoints": [{"x": sx, "y": sy}],
        },
    )
    client.send(
        "Input.dispatchTouchEvent",
        {
            "type": "touchMove",
            "touchPoints": [{"x": (sx + ex) / 2, "y": (sy + ey) / 2}],
        },
    )
    client.send(
        "Input.dispatchTouchEvent",
        {
            "type": "touchMove",
            "touchPoints": [{"x": ex, "y": ey}],
        },
    )
    client.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    client.detach()


def test_frontend_reorders_groups_by_drag(app, page):
    base_url, _ = app
    reading = bookmark_helpers.seed_group("Reading")
    bookmark_helpers.seed_group("Work")
    pinned = bookmark_helpers.seed_url("https://one.example")
    bookmark_helpers.seed_membership(pinned["id"], reading["id"])

    page.goto(base_url)
    expect(page.locator(".group-name")).to_have_text(["default", "Reading", "Work"])

    # Drop Work onto the top of the default header so it lands first.
    mouse_drag(page, group_header(page, "Work"), group_header(page, "default"))

    expect(page.locator("#form-status")).to_have_text("Reordered.")
    expect(page.locator(".group-name")).to_have_text(["Work", "default", "Reading"])
    # URL membership is untouched by the move.
    reading_group = page.locator(
        ".group", has=page.get_by_role("heading", name="Reading")
    )
    expect(reading_group.get_by_role("link")).to_have_text("one.example")

    page.reload()
    expect(page.locator(".group-name")).to_have_text(["Work", "default", "Reading"])
    assert [group["name"] for group in bookmark_helpers.group_payloads()] == [
        "Work",
        "default",
        "Reading",
    ]


def test_frontend_reorders_groups_by_touch(app, browser):
    base_url, _ = app
    bookmark_helpers.seed_group("Reading")
    bookmark_helpers.seed_group("Work")

    context = browser.new_context(has_touch=True)
    try:
        page = context.new_page()
        page.goto(base_url)
        page.get_by_label("Login").fill(TEST_LOGIN)
        page.get_by_label("Password").fill(TEST_PASSWORD)
        page.get_by_role("button", name="Log in").click()
        expect(page.locator(".group-name")).to_have_text(["default", "Reading", "Work"])

        touch_drag(page, group_header(page, "Work"), group_header(page, "default"))

        expect(page.locator("#form-status")).to_have_text("Reordered.")
        expect(page.locator(".group-name")).to_have_text(["Work", "default", "Reading"])
        assert [group["name"] for group in bookmark_helpers.group_payloads()] == [
            "Work",
            "default",
            "Reading",
        ]
    finally:
        context.close()


def test_frontend_reorder_error_reloads_server_order(app, page):
    base_url, _ = app
    bookmark_helpers.seed_group("Reading")

    page.goto(base_url)
    page.route(
        "**/api/groups/order",
        lambda route: route.fulfill(
            status=400,
            content_type="application/json",
            body=json.dumps({"error": "Invalid group order."}),
        ),
    )

    mouse_drag(page, group_header(page, "Reading"), group_header(page, "default"))

    expect(page.locator("#form-status")).to_have_text("Invalid group order.")
    # The rejected move is dropped and the server's order is shown.
    expect(page.locator(".group-name")).to_have_text(["default", "Reading"])
    assert [group["name"] for group in bookmark_helpers.group_payloads()] == [
        "default",
        "Reading",
    ]


def test_frontend_reorders_only_within_the_dragged_groups_parent(app, page):
    base_url, _ = app
    parent = bookmark_helpers.seed_group("Parent")
    first = bookmark_helpers.seed_group("First", parent_id=parent["id"])
    second = bookmark_helpers.seed_group("Second", parent_id=parent["id"])
    bookmark_helpers.seed_group("Other")

    requests = []
    page.on(
        "request",
        lambda request: (
            requests.append(request.post_data_json)
            if request.url.endswith("/api/groups/order")
            else None
        ),
    )
    page.goto(base_url)

    # A sibling drop marks its target. Asserting that here is what gives the
    # cross-parent check below something to be the absence of.
    mouse_drag_over(page, group_header(page, "Second"), group_header(page, "First"))
    expect(page.locator(".group-header.drag-over .group-name")).to_have_text("First")
    page.mouse.up()

    expect(page.locator("#form-status")).to_have_text("Reordered.")
    assert requests[-1] == {
        "parent_id": parent["id"],
        "group_ids": [second["id"], first["id"]],
    }

    request_count = len(requests)
    mouse_drag_over(page, group_header(page, "Second"), group_header(page, "Other"))
    # Checked mid-drag: pointerup clears every indicator, so the same assertion
    # after the drop cannot tell "never marked" from "marked, then cleared".
    expect(page.locator(".group-header.drag-over")).to_have_count(0)
    page.mouse.up()

    # "Nothing was sent" is not directly observable — request events reach the
    # driver asynchronously, so checking the list right after the drop passes
    # even when the frontend did send one. Follow the rejected drop with a
    # sibling drop that must send, then assert it is the only request that
    # arrived: anything the cross-parent drop queued precedes it in the stream.
    with page.expect_request("**/api/groups/order") as pending:
        mouse_drag(page, group_header(page, "First"), group_header(page, "Second"))
    delivered = pending.value.post_data_json

    assert delivered == {
        "parent_id": parent["id"],
        "group_ids": [first["id"], second["id"]],
    }
    assert requests[request_count:] == [delivered]
    expect(page.locator(".group-name")).to_have_text(
        ["default", "Parent", "First", "Second", "Other"]
    )
