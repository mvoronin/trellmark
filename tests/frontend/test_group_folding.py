import pytest
from playwright.sync_api import expect

from tests.bookmarks import helpers as bookmark_helpers


def test_feature_interactions_and_clear_do_not_revive_private_snapshots(static_page):
    result = static_page.evaluate(
        """async () => {
          const {createBookmarksModel} = await import('/static/features/bookmarks/model.js');
          const writes = [];
          const model = createBookmarksModel(() => ({
            getItem: key => key === 'foldedGroups' ? '[1,"2",null,3.5]' : null,
            setItem: (key, value) => writes.push([key, value]),
          }));
          const initial = [...model.ui.foldedGroupIds];
          const previous = model.ui;
          model.unfoldGroup(1);
          model.foldGroup(2);
          const group = {id: 1, name: 'default', parent_id: null, position: 0,
            depth: 1, nsfw: false, domains: [], urls: [], children: []};
          const url = {id: 7, url: 'https://example.com', title: null,
            created_at: '', version: 1, important: false};
          model.replaceGroups([group]);
          model.openGroupEditor(group);
          model.openUrlEditor(url);
          const selected = [model.ui.editingGroup === group, model.ui.editingUrl === url];
          model.startDrag({groupId: 1, parentId: null, pointerId: 4, startX: 0, startY: 0,
            header: document.createElement('div'), section: document.createElement('section')});
          model.moveDrag();
          model.setDropTarget({groupId: 2, before: true});
          const moving = model.ui.activeDrag.moving;
          model.finishDrag();
          model.closeGroupEditor();
          model.closeUrlEditor();
          model.openGroupEditor(group);
          model.openUrlEditor(url);
          model.clearGroups();
          model.setSortMode('domain');
          model.setSafeMode(false);
          return {initial, previous: [...previous.foldedGroupIds],
            folded: [...model.ui.foldedGroupIds], writes, selected, moving,
            groups: model.server.groups.length, group: model.ui.editingGroup,
            url: model.ui.editingUrl, drag: model.ui.activeDrag};
        }"""
    )
    assert result == {
        "initial": [1],
        "previous": [1],
        "folded": [2],
        "writes": [["foldedGroups", "[]"], ["foldedGroups", "[2]"]],
        "selected": [True, True],
        "moving": True,
        "groups": 0,
        "group": None,
        "url": None,
        "drag": None,
    }


@pytest.mark.parametrize("stored", ["not-json", "null", "{}", '["1",null,1.5]'])
def test_malformed_fold_storage_still_allows_folding(app, page, stored):
    base_url, _ = app
    bookmark_helpers.seed_url("https://one.example")
    page.goto(base_url)
    page.evaluate("value => localStorage.setItem('foldedGroups', value)", stored)
    page.reload()
    expect(page.get_by_role("button", name="Toggle default")).to_have_attribute(
        "aria-expanded", "true"
    )
    page.get_by_role("button", name="Toggle default").click()
    expect(page.get_by_role("link", name="one.example")).to_be_hidden()


def test_denied_storage_keeps_fold_and_theme_interactions_working(app, page):
    base_url, _ = app
    bookmark_helpers.seed_url("https://one.example")
    page.add_init_script("""Object.defineProperty(window, 'localStorage', {
      get() { throw new DOMException('synthetic denied storage', 'SecurityError'); }
    });""")
    page.goto(base_url)
    page.get_by_role("button", name="Toggle default").click()
    expect(page.get_by_role("link", name="one.example")).to_be_hidden()
    page.get_by_role("button", name="Toggle default").click()
    expect(page.get_by_role("link", name="one.example")).to_be_visible()
    page.get_by_role("button", name="Dark", exact=True).click()
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")


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
