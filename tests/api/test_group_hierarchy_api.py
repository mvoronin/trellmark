"""Nested groups: tree reads, parent-aware writes, and every rejection.

The flat behaviour these build on is covered by `test_groups_api.py`; this file
is only about what having a parent changes.
"""

import threading
from dataclasses import asdict

import pytest
from sqlalchemy import event, text

from tests.bookmarks import helpers as bookmark_helpers
from tests.helpers import (
    all_groups_in,
    capture_storage_statements,
    child_names_in,
    group_in,
    group_names_in,
    grouped_url_ids_in,
    http_json,
    run_async,
    stored_groups,
)
from trellmark.bookmarks import domain, persistence
from trellmark.bookmarks.api import GroupsResponse
from trellmark.platform.runtime import get_engine


def _create_group(base_url, name, parent_id=None, **fields):
    return http_json(
        base_url,
        "/api/groups",
        method="POST",
        payload={"name": name, "parent_id": parent_id, **fields},
    )


def _seed_tree(base_url):
    """default / Root -> Child -> Grandchild, and a second root."""
    _, root = _create_group(base_url, "Root")
    _, child = _create_group(base_url, "Child", root["group"]["id"])
    _, grandchild = _create_group(base_url, "Grandchild", child["group"]["id"])
    _, other = _create_group(base_url, "Other")
    return (
        root["group"]["id"],
        child["group"]["id"],
        grandchild["group"]["id"],
        other["group"]["id"],
    )


def test_get_groups_returns_a_nested_tree(app):
    base_url, _ = app
    root_id, child_id, grandchild_id, _ = _seed_tree(base_url)

    status, payload = http_json(base_url, "/api/groups")

    assert status == 200
    assert GroupsResponse.model_validate(payload).model_dump() == payload
    assert group_names_in(payload) == ["default", "Root", "Other"]
    root = group_in(payload, "Root")
    child = group_in(payload, "Child")
    grandchild = group_in(payload, "Grandchild")
    assert (root["parent_id"], root["depth"], root["position"]) == (None, 1, 1)
    assert (child["parent_id"], child["depth"], child["position"]) == (root_id, 2, 0)
    assert (grandchild["parent_id"], grandchild["depth"]) == (child_id, 3)
    assert root["children"] == [child]
    assert child["children"] == [grandchild]
    assert grandchild["children"] == []
    assert grandchild["id"] == grandchild_id


def test_no_response_exposes_the_internal_path(app):
    base_url, _ = app
    _seed_tree(base_url)

    _, payload = http_json(base_url, "/api/groups")

    for group in all_groups_in(payload):
        assert "path" not in group


def test_reading_the_tree_takes_a_bounded_number_of_queries(app, monkeypatch):
    base_url, _ = app
    _seed_tree(base_url)
    for index in range(6):
        bookmark_helpers.seed_url(f"https://{index}.example")
    statements, engine = capture_storage_statements(monkeypatch)

    groups = [
        asdict(group)
        for group in persistence.PostgresGroupQueries(get_engine).list_groups()
    ]
    engine.dispose()

    # Three reads — groups, memberships, domains — however deep the tree is.
    assert len(all_groups_in({"groups": groups})) == 5
    assert len(statements) == 3


def test_post_group_appends_under_its_parent(app):
    base_url, _ = app
    _, root = _create_group(base_url, "Root")
    root_id = root["group"]["id"]

    _, first = _create_group(base_url, "First", root_id)
    status, payload = _create_group(base_url, "Second", root_id)

    assert status == 201
    assert first["group"]["position"] == 0
    assert payload["group"] == {
        "id": payload["group"]["id"],
        "name": "Second",
        "parent_id": root_id,
        "position": 1,
        "depth": 2,
        "nsfw": False,
        "domains": [],
        "urls": [],
        "children": [],
    }
    assert child_names_in(payload, "Root") == ["First", "Second"]
    assert group_names_in(payload) == ["default", "Root"]


@pytest.mark.parametrize("parent_id", [None, "omitted"])
def test_post_group_without_a_parent_creates_a_root(app, parent_id):
    base_url, _ = app
    payload = {"name": "Reading"}
    if parent_id is None:
        payload["parent_id"] = None

    status, response = http_json(
        base_url, "/api/groups", method="POST", payload=payload
    )

    assert status == 201
    assert response["group"]["parent_id"] is None
    assert response["group"]["depth"] == 1
    assert group_names_in(response) == ["default", "Reading"]


def test_post_group_returns_not_found_for_a_missing_parent(app):
    base_url, _ = app

    status, payload = _create_group(base_url, "Orphan", 999)

    assert status == 404
    assert payload == {"error": "This group does not exist."}
    assert group_names_in(stored_groups()) == ["default"]


def test_post_group_rejects_a_fourth_level(app):
    base_url, _ = app
    _, _, grandchild_id, _ = _seed_tree(base_url)

    status, payload = _create_group(base_url, "TooDeep", grandchild_id)

    assert status == 400
    assert payload == {"error": "Groups can be nested three levels deep."}
    assert len(all_groups_in(stored_groups())) == 5


def test_post_group_accepts_default_as_a_parent(app):
    base_url, _ = app

    status, payload = _create_group(base_url, "Inbox", 1)

    assert status == 201
    assert payload["group"]["parent_id"] == 1
    assert child_names_in(payload, "default") == ["Inbox"]


def test_patch_group_without_parent_id_leaves_the_parent_alone(app):
    base_url, _ = app
    root_id, child_id, _, _ = _seed_tree(base_url)

    status, payload = http_json(
        base_url,
        f"/api/groups/{child_id}",
        method="PATCH",
        payload={"name": "Renamed"},
    )

    assert status == 200
    assert payload["group"]["parent_id"] == root_id
    assert child_names_in(payload, "Root") == ["Renamed"]


def test_patch_group_with_null_parent_id_moves_the_group_to_the_root(app):
    base_url, _ = app
    _, child_id, grandchild_id, _ = _seed_tree(base_url)

    status, payload = http_json(
        base_url,
        f"/api/groups/{child_id}",
        method="PATCH",
        payload={"parent_id": None},
    )

    assert status == 200
    assert payload["group"]["parent_id"] is None
    assert payload["group"]["depth"] == 1
    # The subtree came with it.
    assert group_names_in(payload) == ["default", "Root", "Other", "Child"]
    assert child_names_in(payload, "Child") == ["Grandchild"]
    assert group_in(payload, "Grandchild")["depth"] == 2
    assert group_in(payload, "Grandchild")["parent_id"] == child_id
    assert grandchild_id == group_in(payload, "Grandchild")["id"]
    assert child_names_in(payload, "Root") == []


def test_patch_group_moves_a_subtree_and_edits_metadata_in_one_request(app):
    base_url, _ = app
    _, child_id, _, other_id = _seed_tree(base_url)

    status, payload = http_json(
        base_url,
        f"/api/groups/{child_id}",
        method="PATCH",
        payload={
            "name": "Moved",
            "nsfw": True,
            "domains": ["moved.example"],
            "parent_id": other_id,
        },
    )

    assert status == 200
    assert payload["group"]["name"] == "Moved"
    assert payload["group"]["nsfw"] is True
    assert payload["group"]["domains"] == ["moved.example"]
    assert payload["group"]["parent_id"] == other_id
    assert child_names_in(payload, "Other") == ["Moved"]
    assert child_names_in(payload, "Moved") == ["Grandchild"]
    assert group_in(payload, "Grandchild")["depth"] == 3


def test_patch_group_compacts_the_old_siblings_and_appends_to_the_new(app):
    base_url, _ = app
    _, root = _create_group(base_url, "Root")
    _, other = _create_group(base_url, "Other")
    root_id = root["group"]["id"]
    other_id = other["group"]["id"]
    _, first = _create_group(base_url, "First", root_id)
    _create_group(base_url, "Second", root_id)
    _create_group(base_url, "Third", root_id)
    _create_group(base_url, "Existing", other_id)

    status, payload = http_json(
        base_url,
        f"/api/groups/{first['group']['id']}",
        method="PATCH",
        payload={"parent_id": other_id},
    )

    assert status == 200
    assert child_names_in(payload, "Root") == ["Second", "Third"]
    assert [child["position"] for child in group_in(payload, "Root")["children"]] == [
        0,
        1,
    ]
    assert child_names_in(payload, "Other") == ["Existing", "First"]
    assert [child["position"] for child in group_in(payload, "Other")["children"]] == [
        0,
        1,
    ]


def test_patch_group_rejects_moving_a_group_into_itself(app):
    base_url, _ = app
    _, child_id, _, _ = _seed_tree(base_url)

    status, payload = http_json(
        base_url,
        f"/api/groups/{child_id}",
        method="PATCH",
        payload={"parent_id": child_id},
    )

    assert status == 400
    assert payload == {"error": "A group cannot be moved into itself."}
    assert child_names_in(stored_groups(), "Root") == ["Child"]


def test_patch_group_rejects_moving_a_group_under_its_own_descendant(app):
    base_url, _ = app
    root_id, _, grandchild_id, _ = _seed_tree(base_url)

    status, payload = http_json(
        base_url,
        f"/api/groups/{root_id}",
        method="PATCH",
        payload={"parent_id": grandchild_id},
    )

    assert status == 400
    assert payload == {"error": "A group cannot be moved into itself."}
    assert group_names_in(stored_groups()) == ["default", "Root", "Other"]
    assert child_names_in(stored_groups(), "Child") == ["Grandchild"]


def test_patch_group_rejects_a_move_that_would_make_the_subtree_too_deep(app):
    base_url, _ = app
    _, child_id, _, other_id = _seed_tree(base_url)
    _, other_child = _create_group(base_url, "OtherChild", other_id)

    # Child would only be at level 3 itself, but it carries a grandchild.
    status, payload = http_json(
        base_url,
        f"/api/groups/{child_id}",
        method="PATCH",
        payload={"parent_id": other_child["group"]["id"]},
    )

    assert status == 400
    assert payload == {"error": "Groups can be nested three levels deep."}
    assert child_names_in(stored_groups(), "Root") == ["Child"]
    assert child_names_in(stored_groups(), "OtherChild") == []


def test_patch_group_moves_a_leaf_to_the_deepest_level(app):
    base_url, _ = app
    _, _, _, other_id = _seed_tree(base_url)
    _, other_child = _create_group(base_url, "OtherChild", other_id)
    _, leaf = _create_group(base_url, "Leaf")

    status, payload = http_json(
        base_url,
        f"/api/groups/{leaf['group']['id']}",
        method="PATCH",
        payload={"parent_id": other_child["group"]["id"]},
    )

    assert status == 200
    assert payload["group"]["depth"] == 3
    assert child_names_in(payload, "OtherChild") == ["Leaf"]


def test_patch_group_returns_not_found_for_a_missing_parent(app):
    base_url, _ = app
    _, child_id, _, _ = _seed_tree(base_url)

    status, payload = http_json(
        base_url,
        f"/api/groups/{child_id}",
        method="PATCH",
        payload={"parent_id": 999},
    )

    assert status == 404
    assert payload == {"error": "This group does not exist."}
    assert child_names_in(stored_groups(), "Root") == ["Child"]


def test_patch_group_refuses_to_reparent_default(app):
    base_url, _ = app
    root_id, _, _, _ = _seed_tree(base_url)

    status, payload = http_json(
        base_url,
        "/api/groups/1",
        method="PATCH",
        payload={"parent_id": root_id},
    )

    assert status == 400
    assert payload == {"error": "The default group cannot be edited."}
    assert group_names_in(stored_groups())[0] == "default"
    assert stored_groups()["groups"][0]["parent_id"] is None


def test_patch_group_order_reorders_one_child_set(app):
    base_url, _ = app
    _, root = _create_group(base_url, "Root")
    root_id = root["group"]["id"]
    _, first = _create_group(base_url, "First", root_id)
    _, second = _create_group(base_url, "Second", root_id)
    _, third = _create_group(base_url, "Third", root_id)

    status, payload = http_json(
        base_url,
        "/api/groups/order",
        method="PATCH",
        payload={
            "parent_id": root_id,
            "group_ids": [
                third["group"]["id"],
                first["group"]["id"],
                second["group"]["id"],
            ],
        },
    )

    assert status == 200
    assert child_names_in(payload, "Root") == ["Third", "First", "Second"]
    assert [child["position"] for child in group_in(payload, "Root")["children"]] == [
        0,
        1,
        2,
    ]
    # The roots were not touched.
    assert group_names_in(payload) == ["default", "Root"]
    assert child_names_in(stored_groups(), "Root") == ["Third", "First", "Second"]


def test_patch_group_order_rejects_ids_from_another_parent(app):
    base_url, _ = app
    root_id, child_id, _, other_id = _seed_tree(base_url)
    _, other_child = _create_group(base_url, "OtherChild", other_id)

    status, payload = http_json(
        base_url,
        "/api/groups/order",
        method="PATCH",
        payload={
            "parent_id": root_id,
            "group_ids": [other_child["group"]["id"]],
        },
    )

    assert status == 400
    assert payload == {"error": "Invalid group order."}
    assert child_names_in(stored_groups(), "Root") == ["Child"]
    assert child_names_in(stored_groups(), "Other") == ["OtherChild"]
    assert group_in(stored_groups(), "Child")["id"] == child_id


def test_patch_group_order_rejects_a_root_list_that_names_a_child(app):
    base_url, _ = app
    root_id, child_id, _, other_id = _seed_tree(base_url)

    status, payload = http_json(
        base_url,
        "/api/groups/order",
        method="PATCH",
        payload={"parent_id": None, "group_ids": [child_id, root_id, other_id, 1]},
    )

    assert status == 400
    assert payload == {"error": "Invalid group order."}
    assert group_names_in(stored_groups()) == ["default", "Root", "Other"]


def test_patch_group_order_rejects_a_missing_parent(app):
    """An empty order must not look valid just because a missing parent is
    childless.

    `_sibling_ids` returns nothing both for a parent with no children and for a
    parent that does not exist, so without an existence check both length and
    membership match and the endpoint reports success for a sibling set that is
    not there.
    """
    base_url, _ = app
    _seed_tree(base_url)

    status, payload = http_json(
        base_url,
        "/api/groups/order",
        method="PATCH",
        payload={"parent_id": 999, "group_ids": []},
    )

    assert status == 400
    assert payload == {"error": "Invalid group order."}


def test_patch_group_order_accepts_an_empty_order_for_a_childless_parent(app):
    base_url, _ = app
    _, leaf = _create_group(base_url, "Leaf")

    status, payload = http_json(
        base_url,
        "/api/groups/order",
        method="PATCH",
        payload={"parent_id": leaf["group"]["id"], "group_ids": []},
    )

    assert status == 200
    assert child_names_in(payload, "Leaf") == []


def test_patch_group_order_rejects_an_incomplete_child_list(app):
    base_url, _ = app
    _, root = _create_group(base_url, "Root")
    root_id = root["group"]["id"]
    _, first = _create_group(base_url, "First", root_id)
    _create_group(base_url, "Second", root_id)

    status, payload = http_json(
        base_url,
        "/api/groups/order",
        method="PATCH",
        payload={"parent_id": root_id, "group_ids": [first["group"]["id"]]},
    )

    assert status == 400
    assert payload == {"error": "Invalid group order."}
    assert child_names_in(stored_groups(), "Root") == ["First", "Second"]


def test_delete_group_with_children_is_refused(app):
    base_url, _ = app
    root_id, _, _, _ = _seed_tree(base_url)
    url = bookmark_helpers.seed_url("https://one.example")
    bookmark_helpers.seed_membership(url["id"], root_id)

    status, payload = http_json(
        base_url,
        f"/api/groups/{root_id}",
        method="DELETE",
        payload={"url_action": "delete"},
    )

    assert status == 409
    assert payload == {"error": "Move or delete this group's child groups first."}
    assert len(all_groups_in(stored_groups())) == 5
    assert grouped_url_ids_in(stored_groups(), "Root") == [url["id"]]
    assert bookmark_helpers.saved_urls() == ["https://one.example"]


def test_delete_group_compacts_only_its_own_siblings(app):
    base_url, _ = app
    _, root = _create_group(base_url, "Root")
    _create_group(base_url, "Other")
    root_id = root["group"]["id"]
    _, first = _create_group(base_url, "First", root_id)
    _create_group(base_url, "Second", root_id)
    _create_group(base_url, "Third", root_id)

    status, payload = http_json(
        base_url,
        f"/api/groups/{first['group']['id']}",
        method="DELETE",
        payload={"url_action": "delete"},
    )

    assert status == 200
    assert child_names_in(payload, "Root") == ["Second", "Third"]
    assert [child["position"] for child in group_in(payload, "Root")["children"]] == [
        0,
        1,
    ]
    # Root positions were left alone.
    assert group_names_in(payload) == ["default", "Root", "Other"]
    assert [group["position"] for group in payload["groups"]] == [0, 1, 2]


def test_delete_deepest_group_moves_its_urls_to_default(app):
    base_url, _ = app
    _, _, grandchild_id, _ = _seed_tree(base_url)
    url = bookmark_helpers.seed_url("https://one.example")
    bookmark_helpers.seed_membership(url["id"], grandchild_id)

    status, payload = http_json(
        base_url,
        f"/api/groups/{grandchild_id}",
        method="DELETE",
        payload={"url_action": "move_to_default"},
    )

    assert status == 200
    assert payload["moved"] == 1
    assert child_names_in(payload, "Child") == []
    assert grouped_url_ids_in(payload, "default") == [url["id"]]


def test_urls_can_be_moved_into_and_out_of_a_nested_group(app):
    base_url, _ = app
    _, _, grandchild_id, _ = _seed_tree(base_url)
    url = bookmark_helpers.seed_url("https://one.example")

    status, payload = http_json(
        base_url,
        f"/api/urls/{url['id']}/group",
        method="PATCH",
        payload={"group_id": grandchild_id},
    )

    assert status == 200
    assert grouped_url_ids_in(payload, "Grandchild") == [url["id"]]
    assert grouped_url_ids_in(payload, "default") == []
    # A nested group's URLs stay its own: no ancestor picked them up.
    assert grouped_url_ids_in(payload, "Child") == []
    assert grouped_url_ids_in(payload, "Root") == []

    status, payload = http_json(
        base_url,
        f"/api/urls/{url['id']}?group_id={grandchild_id}",
        method="DELETE",
    )

    assert status == 200
    assert bookmark_helpers.saved_urls() == []


def test_domain_rules_match_groups_at_every_depth(app):
    base_url, _ = app
    _, root = _create_group(base_url, "Root", domains=["root.example"])
    _, child = _create_group(
        base_url, "Child", root["group"]["id"], domains=["child.example"]
    )
    _create_group(
        base_url,
        "Grandchild",
        child["group"]["id"],
        domains=["grandchild.example"],
    )

    _, deep = http_json(
        base_url,
        "/api/urls",
        method="POST",
        payload={"url": "https://grandchild.example/x"},
    )

    assert grouped_url_ids_in(deep, "Grandchild") == [deep["url"]["id"]]
    # Rules are not inherited in either direction.
    assert grouped_url_ids_in(deep, "Child") == []
    assert grouped_url_ids_in(deep, "Root") == []
    assert grouped_url_ids_in(deep, "default") == []

    _, unmatched = http_json(
        base_url,
        "/api/urls",
        method="POST",
        payload={"url": "https://nowhere.example/x"},
    )

    assert grouped_url_ids_in(unmatched, "default") == [unmatched["url"]["id"]]


def _hold_sibling_lock(connection, parent_id):
    connection.execute(
        text("SELECT pg_advisory_xact_lock(:namespace, :key)"),
        {
            "namespace": persistence.SIBLING_LOCK_NAMESPACE,
            "key": persistence.ROOT_SIBLING_LOCK_KEY
            if parent_id is None
            else parent_id,
        },
    )


def test_group_sibling_locks_take_unique_keys_in_sorted_order(database):
    engine = get_engine()
    attempted = []

    def capture(_connection, _cursor, statement, parameters, _context, _many):
        if "pg_advisory_xact_lock" in statement:
            namespace, key = parameters.values()
            assert namespace == persistence.SIBLING_LOCK_NAMESPACE
            attempted.append(key)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with engine.connect() as connection:
            with connection.begin():
                persistence._lock_sibling_sets(connection, 3, None, 2, 3)
                locks = (
                    connection.execute(
                        text(
                            "SELECT objid FROM pg_locks WHERE pid = pg_backend_pid() "
                            "AND locktype = 'advisory' AND classid = 7501 AND granted "
                            "ORDER BY objid"
                        )
                    )
                    .scalars()
                    .all()
                )
                assert locks == [0, 2, 3]
        assert attempted == [0, 2, 3]
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    with engine.connect() as connection:
        for key in (0, 2, 3):
            assert (
                connection.scalar(
                    text("SELECT pg_try_advisory_xact_lock(7501, :key)"), {"key": key}
                )
                is True
            )


def _run_in_thread(work):
    outcome = {}

    def target():
        try:
            outcome["value"] = work()
        except BaseException as error:  # pragma: no cover - surfaced by asserts
            outcome["error"] = error

    thread = threading.Thread(target=target)
    thread.start()
    return thread, outcome


def _assert_waits_for_lock(locked_parent_id, work, message):
    """Run `work` while another connection holds a sibling set's lock.

    Every write that renumbers a set has to queue behind the others; holding
    the lock from a second connection stands in for the concurrent writer
    without needing a real race to be deterministic.
    """
    with get_engine().connect() as blocker:
        with blocker.begin():
            _hold_sibling_lock(blocker, locked_parent_id)
            thread, outcome = _run_in_thread(work)
            thread.join(timeout=0.5)

            assert thread.is_alive(), message

    thread.join(timeout=5)
    assert not thread.is_alive()
    assert "error" not in outcome
    return outcome["value"]


@pytest.mark.parametrize("parent", ["root", "child"])
def test_appending_to_a_sibling_set_waits_for_its_lock(app, parent, bookmarks_service):
    """Two appends to one sibling set must not both read the same position.

    Appending reads the sibling count and writes it as the new position, so
    without serialization both writers pick the same number and one loses to
    the position unique index — surfacing as a bogus name conflict.
    """
    base_url, _ = app
    _, root = _create_group(base_url, "Root")
    parent_id = None if parent == "root" else root["group"]["id"]
    existing = len(
        group_in(stored_groups(), "Root")["children"]
        if parent_id
        else stored_groups()["groups"]
    )

    outcome = _assert_waits_for_lock(
        parent_id,
        lambda: run_async(
            lambda: bookmarks_service.create_group(
                domain.CreateGroup("Appended", parent_id=parent_id)
            )
        ),
        "the append did not wait for the lock",
    )

    assert isinstance(outcome, domain.GroupCreated)
    record = asdict(outcome.record)
    assert record["parent_id"] == parent_id
    assert record["position"] == existing


def test_deleting_a_group_waits_for_its_sibling_set(app, bookmarks_service):
    """Deleting compacts the survivors, so it renumbers the set like an append.

    Left unlocked, a delete that renumbers while an append picks a position
    leaves a gap — and the append after that collides with the row already
    sitting at the position the gap made it choose.
    """
    base_url, _ = app
    _, first = _create_group(base_url, "First")
    _create_group(base_url, "Second")

    outcome = _assert_waits_for_lock(
        None,
        lambda: run_async(
            lambda: bookmarks_service.delete_group(
                domain.DeleteGroup(first["group"]["id"], "delete")
            )
        ),
        "the delete did not wait for the lock",
    )

    assert isinstance(outcome, domain.GroupDeleted)
    result = asdict(outcome)
    assert result["group_id"] == first["group"]["id"]
    assert group_names_in(stored_groups()) == ["default", "Second"]
    assert [group["position"] for group in stored_groups()["groups"]] == [0, 1]


def test_moving_a_group_out_waits_for_the_set_it_leaves(app, bookmarks_service):
    """A move renumbers two sets, and the one it leaves is the easy one to miss.

    The destination here is a different set entirely, so locking only the
    destination would let this run straight through while the source is being
    renumbered by someone else.
    """
    base_url, _ = app
    _, source = _create_group(base_url, "Source")
    _, target = _create_group(base_url, "Target")
    _, first = _create_group(base_url, "First", source["group"]["id"])
    _create_group(base_url, "Second", source["group"]["id"])

    outcome = _assert_waits_for_lock(
        source["group"]["id"],
        lambda: run_async(
            lambda: bookmarks_service.update_group(
                domain.UpdateGroup(
                    first["group"]["id"], parent_id=target["group"]["id"]
                )
            )
        ),
        "the move did not wait for the lock on the set it left",
    )

    assert isinstance(outcome, domain.GroupUpdated)
    record = asdict(outcome.record)
    assert record["parent_id"] == target["group"]["id"]
    assert child_names_in(stored_groups(), "Source") == ["Second"]
    assert group_in(stored_groups(), "Second")["position"] == 0
    assert child_names_in(stored_groups(), "Target") == ["First"]


def test_reordering_waits_for_the_sibling_set(app, bookmarks_service):
    base_url, _ = app
    _, first = _create_group(base_url, "First")
    _, second = _create_group(base_url, "Second")

    outcome = _assert_waits_for_lock(
        None,
        lambda: run_async(
            lambda: bookmarks_service.reorder_groups(
                domain.ReorderGroups(
                    None, (second["group"]["id"], first["group"]["id"], 1)
                )
            )
        ),
        "the reorder did not wait for the lock",
    )

    assert isinstance(outcome, domain.GroupsReordered)
    assert group_names_in(stored_groups()) == ["Second", "First", "default"]


def test_a_trigger_rejection_surfaces_as_a_clean_api_error(app, monkeypatch):
    """What a writer that beat the storage-level validation looks like.

    Storage checks the destination before writing, so the trigger normally
    never fires. Disabling that check is the only way to see the last line of
    defence from the outside — and its message must still be the API's, not
    PostgreSQL's.
    """
    base_url, _ = app
    _, _, grandchild_id, _ = _seed_tree(base_url)
    _, leaf = _create_group(base_url, "Leaf")
    monkeypatch.setattr(persistence, "_validate_parent", lambda *_args, **_kwargs: None)

    status, payload = http_json(
        base_url,
        f"/api/groups/{leaf['group']['id']}",
        method="PATCH",
        payload={"parent_id": grandchild_id},
    )

    assert status == 400
    assert payload == {"error": "Groups can be nested three levels deep."}
    assert child_names_in(stored_groups(), "Grandchild") == []


def test_storage_rejects_a_fourth_level_even_when_called_directly(
    app, bookmarks_service
):
    _, _ = app
    root = bookmark_helpers.seed_group("Root")
    child = bookmark_helpers.seed_group("Child", parent_id=root["id"])
    grandchild = bookmark_helpers.seed_group("Grandchild", parent_id=child["id"])

    outcome = run_async(
        lambda: bookmarks_service.create_group(
            domain.CreateGroup("TooDeep", parent_id=grandchild["id"])
        )
    )

    assert outcome == domain.GroupDepthExceeded()
    assert bookmark_helpers.group_by_name("TooDeep") is None
