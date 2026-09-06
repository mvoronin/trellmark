import pytest
from playwright.sync_api import expect

from tests.bookmarks import helpers as bookmark_helpers
from tests.frontend.test_group_reorder import mouse_drag, mouse_drag_over, touch_drag
from tests.frontend.test_request_lifetimes import gate_responses, release_response


def group(page, name):
    return (
        page.locator(".group")
        .filter(has=page.get_by_role("heading", name=name, exact=True))
        .last
    )


@pytest.mark.parametrize("touch", [False, True])
@pytest.mark.parametrize("folded", [False, True])
def test_drag_url_moves_only_source_membership(app, page, touch, folded):
    base_url, _ = app
    source = bookmark_helpers.seed_group("Source", domains=["example.com"])
    existing = bookmark_helpers.seed_group("Existing", domains=["example.com"])
    parent = bookmark_helpers.seed_group("Parent")
    target = bookmark_helpers.seed_group("Target", parent_id=parent["id"])
    record = bookmark_helpers.seed_url("https://example.com/article")
    page.goto(base_url)
    if folded:
        page.get_by_role("button", name="Toggle Target", exact=True).click()
    source_handle = group(page, "Source").locator(".url-drag-handle")
    destination = group(page, "Target").locator(":scope > .group-header")
    (touch_drag if touch else mouse_drag)(page, source_handle, destination)
    expect(page.locator("#form-status")).to_have_text("Moved.")
    expect(group(page, "Source").get_by_role("link")).to_have_count(0)
    assert set(bookmark_helpers.url_group_ids(record["id"])) == {
        existing["id"],
        target["id"],
    }
    assert source["id"] not in bookmark_helpers.url_group_ids(record["id"])
    page.reload()
    if folded:
        toggle = page.get_by_role("button", name="Toggle Target", exact=True)
        expect(toggle).to_have_attribute("aria-expanded", "false")
        toggle.click()
    expect(group(page, "Target").get_by_role("link")).to_have_count(1)


@pytest.mark.parametrize(
    "cancel", ["escape", "same-group", "outside", "pointercancel", "filter"]
)
def test_url_drag_cancellation_never_moves(app, page, cancel):
    base_url, _ = app
    record = bookmark_helpers.seed_url("https://one.example")
    bookmark_helpers.seed_group("Target")
    memberships = bookmark_helpers.url_group_ids(record["id"])
    page.goto(base_url)
    requests = []
    page.on("request", lambda request: requests.append(request.url))
    source = group(page, "default").locator(".url-drag-handle")
    destination = group(page, "Target").locator(".group-header")
    if cancel == "same-group":
        destination = group(page, "default").locator(".group-header")
    elif cancel == "outside":
        destination = page.locator("#url-count")
    mouse_drag_over(page, source, destination)
    if cancel == "escape":
        page.keyboard.press("Escape")
    elif cancel == "pointercancel":
        source.dispatch_event("pointercancel", {"pointerId": 1})
    elif cancel == "filter":
        page.locator('[data-group-filter="all"]').evaluate("button => button.click()")
    page.mouse.up()
    expect(page.locator(".url-dragging, .drag-over")).to_have_count(0)
    # A subsequent successful operation provides a request-order barrier.
    page.get_by_label("Move one.example").select_option(label="Target")
    expect(page.locator("#form-status")).to_have_text("Moved.")
    assert sum(url.endswith(f"/api/urls/{record['id']}/group") for url in requests) == 1
    assert memberships != bookmark_helpers.url_group_ids(record["id"])


def test_url_drag_failure_retains_link_and_allows_retry(app, page):
    base_url, _ = app
    record = bookmark_helpers.seed_url("https://one.example")
    target = bookmark_helpers.seed_group("Target")
    memberships = bookmark_helpers.url_group_ids(record["id"])
    page.goto(base_url)
    route = f"**/api/urls/{record['id']}/group"
    page.route(
        route,
        lambda route: route.fulfill(
            status=409, content_type="application/json", body='{"error":"Try again."}'
        ),
    )
    mouse_drag(
        page,
        group(page, "default").locator(".url-drag-handle"),
        group(page, "Target").locator(".group-empty"),
    )
    expect(page.locator("#form-status")).to_have_text("Try again.")
    expect(group(page, "default").get_by_role("link")).to_have_count(1)
    assert bookmark_helpers.url_group_ids(record["id"]) == memberships
    page.unroute(route)
    mouse_drag(
        page,
        group(page, "default").locator(".url-drag-handle"),
        group(page, "Target").locator(".group-empty"),
    )
    expect(page.locator("#form-status")).to_have_text("Moved.")
    assert bookmark_helpers.url_group_ids(record["id"]) == [target["id"]]


def test_pending_drag_cannot_repeat_or_restore_content_after_logout(app, page):
    base_url, _ = app
    record = bookmark_helpers.seed_url("https://one.example")
    bookmark_helpers.seed_group("Target")
    page.goto(base_url)
    gate_responses(page, f"/api/urls/{record['id']}/group")
    source = group(page, "default").locator(".url-drag-handle")
    destination = group(page, "Target").locator(".group-header")
    mouse_drag(page, source, destination)
    page.wait_for_function("window.requestGates[0]?.ready")
    mouse_drag(page, source, destination)
    expect(page.locator(".url-dragging, .drag-over")).to_have_count(0)
    assert page.evaluate("window.requestGates.length") == 1
    page.get_by_role("button", name="Log out").click()
    expect(page.locator("#app-view")).to_be_hidden()
    release_response(page, 0)
    expect(page.locator("#groups")).to_be_empty()
    expect(page.locator("#form-status")).not_to_have_text("Moved.")


def test_drag_into_existing_membership_does_not_duplicate_link(app, page):
    base_url, _ = app
    bookmark_helpers.seed_group("Source", domains=["one.example"])
    target = bookmark_helpers.seed_group("Target", domains=["one.example"])
    record = bookmark_helpers.seed_url("https://one.example")
    page.goto(base_url)
    mouse_drag(
        page,
        group(page, "Source").locator(".url-drag-handle"),
        group(page, "Target").locator(".url-main"),
    )
    expect(page.locator("#form-status")).to_have_text("Moved.")
    expect(group(page, "Target").get_by_role("link")).to_have_count(1)
    assert bookmark_helpers.url_group_ids(record["id"]) == [target["id"]]
