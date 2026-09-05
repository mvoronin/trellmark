import json

import pytest
from playwright.sync_api import expect

from tests.bookmarks import helpers as bookmark_helpers
from tests.postgres import TEST_LOGIN, TEST_PASSWORD


@pytest.mark.parametrize("completion_owner", ["disposed", "private-clear"])
def test_drag_factory_cancel_dispose_and_foreign_targets(static_page, completion_owner):
    result = static_page.evaluate(
        """async completionOwner => {
          const { createBookmarkDrag } = await import('/static/features/bookmarks/drag.js');
          const { createBookmarksModel } = await import('/static/features/bookmarks/model.js');
          const { createRequestLifetime } = await import('/static/shared/request.js');
          const model = createBookmarksModel();
          model.replaceGroups([1, 2].map(id => ({id, name: 'equal', position: 0,
            parent_id: null, children: [], urls: [], domains: [], nsfw: false})));
          const root = document.createElement('div');
          const make = id => {
            const section = document.createElement('section');
            const header = document.createElement('header');
            header.className = 'group-header'; header.dataset.groupId = String(id);
            header.dataset.parentId = ''; section.append(header); return {header, section};
          };
          const first = make(1), second = make(2), foreign = make(2);
          root.append(first.section, second.section); document.body.append(root, foreign.section);
          let captured = false, complete, calls = 0, replacements = 0;
          first.header.setPointerCapture = () => { captured = true; };
          first.header.hasPointerCapture = () => captured;
          first.header.releasePointerCapture = () => { captured = false; };
          let target = second.header;
          document.elementFromPoint = () => target;
          second.header.getBoundingClientRect = () => ({top: 0, height: 40});
          const privateLifetime = createRequestLifetime();
          const drag = createBookmarkDrag(root, { model, privateLifetime,
            reorder: () => { calls++; return new Promise(resolve => { complete = resolve; }); },
            replace: () => { replacements++; }, load: async () => {}, status: () => {} });
          drag.makeHeaderDraggable(first.header, first.section, 1, null);
          const send = type => first.header.dispatchEvent(new PointerEvent(type,
            {pointerId: 1, isPrimary: true, button: 0, clientX: type === 'pointerdown' ? 0 : 20, clientY: 35}));
          send('pointerdown'); send('pointermove');
          const moving = captured && first.section.classList.contains('dragging');
          send('pointercancel');
          const cancelled = !captured && model.ui.activeDrag === null && calls === 0;
          target = foreign.header; send('pointerdown'); send('pointermove'); send('pointerup');
          const forbidden = calls === 0 && !foreign.header.classList.contains('drag-over');
          target = second.header; send('pointerdown'); send('pointermove'); send('pointerup');
          if (completionOwner === 'disposed') drag.dispose();
          else { privateLifetime.invalidate(); model.clearGroups(); }
          complete({groups: []}); await Promise.resolve(); await Promise.resolve();
          drag.dispose();
          send('pointerdown');
          return {moving, cancelled, forbidden, calls, replacements, disposed: model.ui.activeDrag === null};
        }""",
        completion_owner,
    )
    assert result == {
        "moving": True,
        "cancelled": True,
        "forbidden": True,
        "calls": 1,
        "replacements": 0,
        "disposed": True,
    }


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


def test_drag_keeps_captured_header_and_folded_descendants_during_movement(app, page):
    base_url, _ = app
    parent = bookmark_helpers.seed_group("Parent")
    child = bookmark_helpers.seed_group("Child", parent_id=parent["id"])
    bookmark_helpers.seed_group("Grandchild", parent_id=child["id"])
    bookmark_helpers.seed_group("Other")
    page.goto(base_url)
    page.get_by_role("button", name="Toggle Parent", exact=True).click()
    source = group_header(page, "Parent")
    source.evaluate("""header => {
      window.capturedHeader = header;
      header.addEventListener('pointerdown', event => { window.dragPointer = event.pointerId; },
        {once: true});
    }""")
    mouse_drag_over(page, source, group_header(page, "Other"), y_fraction=0.85)
    assert source.evaluate("""header => header === window.capturedHeader &&
      header.isConnected && header.hasPointerCapture(window.dragPointer)""")
    expect(page.get_by_role("heading", name="Grandchild", exact=True)).to_be_hidden()
    page.mouse.up()
    expect(page.locator("#form-status")).to_have_text("Reordered.")
    expect(page.locator(".group-name")).to_have_text(
        ["default", "Other", "Parent", "Child", "Grandchild"]
    )
    expect(
        page.get_by_role("button", name="Toggle Parent", exact=True)
    ).to_have_attribute("aria-expanded", "false")


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
