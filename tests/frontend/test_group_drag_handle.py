import pytest
from playwright.sync_api import expect

from tests.bookmarks import helpers as bookmark_helpers
from tests.frontend.test_group_reorder import (
    group_handle,
    group_header,
    mouse_drag,
    mouse_drag_over,
    touch_drag,
)
from tests.postgres import TEST_LOGIN, TEST_PASSWORD


def record_reorders(page):
    requests = []
    page.on(
        "request",
        lambda request: (
            requests.append(request.post_data_json)
            if request.url.endswith("/api/groups/order")
            else None
        ),
    )
    return requests


@pytest.mark.parametrize(
    "region", ["name", "count", "domains", "nsfw-badge", "padding", "across-handle"]
)
def test_header_content_cannot_start_a_group_drag(app, page, region):
    base_url, _ = app
    bookmark_helpers.seed_group("Source", domains=["example.test"], nsfw=True)
    bookmark_helpers.seed_group("Target")
    page.goto(base_url)
    page.get_by_role("button", name="All", exact=True).click()
    original = bookmark_helpers.group_payloads()
    requests = record_reorders(page)
    header = group_header(page, "Source")
    source = (
        header
        if region == "padding"
        else header.locator(
            ".group-name" if region == "across-handle" else f".group-{region}"
        )
    )
    src = source.bounding_box()
    dst = group_header(page, "Target").bounding_box()
    x = src["x"] + (1 if region == "padding" else src["width"] / 2)
    page.mouse.move(x, src["y"] + src["height"] / 2)
    page.mouse.down()
    if region == "across-handle":
        handle = group_handle(page, "Source").bounding_box()
        page.mouse.move(
            handle["x"] + handle["width"] / 2,
            handle["y"] + handle["height"] / 2,
            steps=6,
        )
    page.mouse.move(
        dst["x"] + dst["width"] / 2, dst["y"] + dst["height"] * 0.85, steps=8
    )
    expect(page.locator(".group.dragging, .group-header.drag-over")).to_have_count(0)
    page.mouse.up()
    assert bookmark_helpers.group_payloads() == original

    # A successful reorder provides the request barrier for the negative gesture.
    mouse_drag(page, group_handle(page, "Target"), group_header(page, "Source"))
    expect(page.locator("#form-status")).to_have_text("Reordered.")
    assert len(requests) == 1


@pytest.mark.parametrize("distance", [0, 5])
def test_handle_click_or_small_motion_does_not_reorder(app, page, distance):
    base_url, _ = app
    bookmark_helpers.seed_group("Reading")
    page.goto(base_url)
    requests = record_reorders(page)
    handle = group_handle(page, "Reading").bounding_box()
    x, y = handle["x"] + handle["width"] / 2, handle["y"] + handle["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + distance, y)
    expect(page.locator(".group.dragging")).to_have_count(0)
    page.mouse.up()
    mouse_drag(page, group_handle(page, "Reading"), group_header(page, "default"))
    expect(page.locator("#form-status")).to_have_text("Reordered.")
    assert len(requests) == 1


@pytest.mark.parametrize(
    "cancel", ["escape", "pointercancel", "lost-capture", "filter"]
)
def test_group_handle_cancellation_allows_retry(app, page, cancel):
    base_url, _ = app
    bookmark_helpers.seed_group("Reading")
    page.goto(base_url)
    original = bookmark_helpers.group_payloads()
    requests = record_reorders(page)
    header = group_header(page, "Reading")
    header.evaluate("""header => header.addEventListener('pointerdown', event => {
      window.dragPointer = event.pointerId;
    }, {once: true})""")
    mouse_drag_over(page, group_handle(page, "Reading"), group_header(page, "default"))
    expect(page.locator(".group.dragging")).to_have_count(1)
    if cancel == "escape":
        page.keyboard.press("Escape")
    elif cancel == "pointercancel":
        header.dispatch_event(
            "pointercancel", {"pointerId": page.evaluate("window.dragPointer")}
        )
    elif cancel == "lost-capture":
        header.evaluate("header => header.releasePointerCapture(window.dragPointer)")
        target = group_header(page, "default").bounding_box()
        page.mouse.move(target["x"] + target["width"] / 2 + 1, target["y"] + 10)
    else:
        page.get_by_role("button", name="All", exact=True).evaluate(
            "button => button.click()"
        )
    expect(page.locator(".group.dragging, .group-header.drag-over")).to_have_count(0)
    page.mouse.up()
    assert bookmark_helpers.group_payloads() == original
    mouse_drag(page, group_handle(page, "Reading"), group_header(page, "default"))
    expect(page.locator("#form-status")).to_have_text("Reordered.")
    assert len(requests) == 1


def test_group_title_can_be_selected_without_reordering(app, page):
    base_url, _ = app
    bookmark_helpers.seed_group("Selectable reading title")
    page.goto(base_url)
    requests = record_reorders(page)
    bounds = (
        group_header(page, "Selectable reading title")
        .locator(".group-name")
        .evaluate(
            """title => {
          const range = document.createRange(); range.selectNodeContents(title);
          const box = range.getBoundingClientRect();
          return {x: box.x, y: box.y, width: box.width, height: box.height};
        }"""
        )
    )
    y = bounds["y"] + bounds["height"] / 2
    page.mouse.move(bounds["x"] + 1, y)
    page.mouse.down()
    page.mouse.move(bounds["x"] + bounds["width"] - 1, y, steps=10)
    page.mouse.up()
    assert (
        page.evaluate("window.getSelection().toString()") == "SELECTABLE READING TITLE"
    )
    expect(page.locator(".group.dragging")).to_have_count(0)
    mouse_drag(
        page,
        group_handle(page, "Selectable reading title"),
        group_header(page, "default"),
    )
    expect(page.locator("#form-status")).to_have_text("Reordered.")
    assert len(requests) == 1


def test_touch_scrolls_header_content_but_handle_reorders(app, browser):
    base_url, _ = app
    for number in range(12):
        bookmark_helpers.seed_group(f"Reading {number:02}")
    with browser.new_context(
        has_touch=True, viewport={"width": 390, "height": 700}
    ) as context:
        page = context.new_page()
        page.goto(base_url)
        page.get_by_label("Login").fill(TEST_LOGIN)
        page.get_by_label("Password").fill(TEST_PASSWORD)
        page.get_by_role("button", name="Log in").click()
        title = group_header(page, "Reading 04").locator(".group-name")
        title.scroll_into_view_if_needed()
        bounds = title.bounding_box()
        original_scroll = page.evaluate("window.scrollY")
        original_order = bookmark_helpers.group_payloads()
        requests = record_reorders(page)
        x, y = bounds["x"] + bounds["width"] / 2, bounds["y"] + bounds["height"] / 2
        client = context.new_cdp_session(page)
        try:
            client.send(
                "Input.dispatchTouchEvent",
                {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]},
            )
            for distance in [20, 40, 60, 80, 100]:
                client.send(
                    "Input.dispatchTouchEvent",
                    {"type": "touchMove", "touchPoints": [{"x": x, "y": y - distance}]},
                )
            # Assert scrolling while the finger is still down, before any fling.
            page.wait_for_function(
                "start => window.scrollY > start + 20", arg=original_scroll
            )
            client.send(
                "Input.dispatchTouchEvent", {"type": "touchCancel", "touchPoints": []}
            )
        finally:
            client.detach()
        expect(page.locator(".group.dragging, .group-header.drag-over")).to_have_count(
            0
        )
        assert bookmark_helpers.group_payloads() == original_order

        group_header(page, "Reading 04").scroll_into_view_if_needed()
        group_header(page, "Reading 05").scroll_into_view_if_needed()
        scroll_before_drag = page.evaluate("window.scrollY")

        def assert_drag_does_not_pan():
            expect(page.locator(".group.dragging")).to_have_count(1)
            assert page.evaluate("window.scrollY") == scroll_before_drag

        # Check before completion: the success status changes layout on drop.
        touch_drag(
            page,
            group_handle(page, "Reading 05"),
            group_header(page, "Reading 04"),
            before_release=assert_drag_does_not_pan,
        )
        expect(page.locator("#form-status")).to_have_text("Reordered.")
        assert len(requests) == 1


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [360, 1440])
def test_handles_fit_nested_long_group_headers(app, page, theme, width):
    base_url, _ = app
    parent = bookmark_helpers.seed_group("LongGroupTitle" * 4)
    child = bookmark_helpers.seed_group("NestedGroupTitle" * 4, parent_id=parent["id"])
    bookmark_helpers.seed_group("DeepGroupTitle" * 4, parent_id=child["id"])
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(base_url)
    page.locator(f'#app-view [data-theme-value="{theme}"]').click()
    handles = page.locator(".group-drag-handle")
    expect(handles).to_have_count(4)
    for handle in handles.all():
        expect(handle).to_be_visible()
        assert handle.evaluate("""handle => {
          const rect = handle.getBoundingClientRect();
          const minimum = 2 * parseFloat(getComputedStyle(document.documentElement).fontSize);
          const header = handle.parentElement;
          return rect.width >= minimum && rect.height >= minimum &&
            header.firstElementChild === handle && handle.nextElementSibling.matches('.fold-toggle') &&
            getComputedStyle(handle).cursor === 'grab' &&
            !['grab', 'grabbing'].includes(getComputedStyle(header).cursor) &&
            header.scrollWidth <= header.clientWidth + 1;
        }""")
    assert page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    )
