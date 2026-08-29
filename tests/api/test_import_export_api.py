from datetime import datetime, timezone

import pytest

import trellmark
from tests.helpers import (
    assert_validation_error,
    child_names_in,
    clear_authentication,
    db_query,
    exported_without_timestamp,
    group_in,
    group_names_in,
    grouped_url_ids_in,
    grouped_urls_in,
    http_json,
)
from tests.postgres import reset_database
from trellmark import storage


def test_get_export_returns_groups_and_urls_without_internal_ids(app):
    base_url, _ = app
    default_url = trellmark.add_url("https://one.example")
    reading = trellmark.add_group("Reading")
    reading_url = trellmark.add_url("https://two.example")
    trellmark.move_url_to_group(reading_url["id"], reading["id"])

    status, payload = http_json(base_url, "/api/export")

    assert status == 200
    assert payload["version"] == 1
    assert payload["exported_at"].endswith("Z")
    assert datetime.fromisoformat(payload["exported_at"].replace("Z", "+00:00"))
    assert payload["groups"] == [
        {
            "name": "default",
            "parent": None,
            "position": 0,
            "nsfw": False,
            "domains": [],
            "urls": [
                {
                    "url": "https://one.example",
                    "title": None,
                    "created_at": default_url["created_at"],
                    "important": False,
                }
            ],
        },
        {
            "name": "Reading",
            "parent": None,
            "position": 1,
            "nsfw": False,
            "domains": [],
            "urls": [
                {
                    "url": "https://two.example",
                    "title": None,
                    "created_at": reading_url["created_at"],
                    "important": False,
                }
            ],
        },
    ]
    assert "id" not in payload["groups"][0]
    assert "id" not in payload["groups"][0]["urls"][0]


def test_post_import_into_fresh_db_restores_exported_document(app):
    base_url, database_url = app
    reading = trellmark.add_group("Reading")
    work = trellmark.add_group("Work")
    default_url = trellmark.add_url("https://one.example")
    reading_url = trellmark.add_url("https://two.example")
    work_url = trellmark.add_url("https://three.example")
    trellmark.move_url_to_group(reading_url["id"], reading["id"])
    trellmark.move_url_to_group(work_url["id"], work["id"])
    http_json(
        base_url,
        "/api/groups/order",
        method="PATCH",
        payload={"parent_id": None, "group_ids": [work["id"], reading["id"], 1]},
    )
    _, exported = http_json(base_url, "/api/export")

    # The cutover empties the target and imports into it; reset_database
    # leaves exactly what a freshly migrated database has.
    reset_database(database_url)
    clear_authentication(base_url)

    status, payload = http_json(
        base_url, "/api/import", method="POST", payload=exported
    )
    _, round_tripped = http_json(base_url, "/api/export")

    assert status == 200
    assert payload["imported"] == 3
    assert payload["skipped"] == 0
    assert group_names_in(payload) == ["Work", "Reading", "default"]
    assert grouped_urls_in(payload, "default") == [default_url["url"]]
    assert grouped_urls_in(payload, "Reading") == [reading_url["url"]]
    assert grouped_urls_in(payload, "Work") == [work_url["url"]]
    assert exported_without_timestamp(round_tripped) == exported_without_timestamp(
        exported
    )


def test_version_1_nested_round_trip_restores_the_complete_tree(app):
    base_url, database_url = app
    engineering = trellmark.add_group("Engineering")
    reading = trellmark.add_group("Reading", domains=["example.com"])
    assert engineering is not None
    assert reading is not None
    backend = trellmark.add_group(
        "Backend",
        domains=["example.com"],
        parent_id=engineering["id"],
    )
    archive = trellmark.add_group(
        "Archive",
        nsfw=True,
        parent_id=engineering["id"],
    )
    assert backend is not None
    assert archive is not None
    deep = trellmark.add_group("Deep", parent_id=backend["id"])
    assert deep is not None

    shared = trellmark.add_url("https://example.com/shared", title="Shared")
    deep_url = trellmark.add_url("https://deep.example/article", title="Deep")
    assert shared is not None
    assert deep_url is not None
    trellmark.move_url_to_group(deep_url["id"], deep["id"])
    trellmark.set_url_important(shared["id"], True)
    trellmark.update_url_created_at(
        shared["id"], datetime(2026, 8, 8, 10, 30, tzinfo=timezone.utc)
    )
    assert trellmark.update_group_order(None, [reading["id"], engineering["id"], 1])
    assert trellmark.update_group_order(
        engineering["id"], [archive["id"], backend["id"]]
    )

    _, exported = http_json(base_url, "/api/export")

    assert exported["version"] == 1
    assert [
        (group["name"], group["parent"], group["position"])
        for group in exported["groups"]
    ] == [
        ("Reading", None, 0),
        ("Engineering", None, 1),
        ("Archive", "Engineering", 0),
        ("Backend", "Engineering", 1),
        ("Deep", "Backend", 0),
        ("default", None, 2),
    ]
    for group in exported["groups"]:
        assert not {"id", "parent_id", "path", "depth"}.intersection(group)

    reset_database(database_url)
    clear_authentication(base_url)

    status, payload = http_json(
        base_url, "/api/import", method="POST", payload=exported
    )
    _, round_tripped = http_json(base_url, "/api/export")

    assert status == 200
    assert payload["imported"] == 2
    assert payload["skipped"] == 0
    assert group_names_in(payload) == ["Reading", "Engineering", "default"]
    assert child_names_in(payload, "Engineering") == ["Archive", "Backend"]
    assert child_names_in(payload, "Backend") == ["Deep"]
    assert grouped_url_ids_in(payload, "Reading") == grouped_url_ids_in(
        payload, "Backend"
    )
    restored_shared = group_in(payload, "Reading")["urls"][0]
    assert restored_shared["title"] == "Shared"
    assert restored_shared["created_at"] == "2026-08-08T10:30:00Z"
    assert restored_shared["important"] is True
    assert group_in(payload, "Archive")["nsfw"] is True
    assert group_in(payload, "Backend")["domains"] == ["example.com"]
    assert exported_without_timestamp(round_tripped) == exported_without_timestamp(
        exported
    )


def test_version_1_round_trip_restores_domains_and_multiple_group_memberships(app):
    base_url, database_url = app
    trellmark.add_group("Reading", domains=["example.com"])
    trellmark.add_group("Work", domains=["example.com"])
    saved = trellmark.add_url("https://example.com/article")
    _, exported = http_json(base_url, "/api/export")

    reset_database(database_url)
    clear_authentication(base_url)

    status, payload = http_json(
        base_url, "/api/import", method="POST", payload=exported
    )
    _, round_tripped = http_json(base_url, "/api/export")

    assert status == 200
    assert payload["imported"] == 1
    assert payload["skipped"] == 0
    assert grouped_url_ids_in(payload, "Reading") == [saved["id"]]
    assert grouped_url_ids_in(payload, "Work") == [saved["id"]]
    assert next(group for group in payload["groups"] if group["name"] == "Reading")[
        "domains"
    ] == ["example.com"]
    assert exported_without_timestamp(round_tripped) == exported_without_timestamp(
        exported
    )


def test_version_1_preserves_wire_order_for_shared_url_metadata(app):
    base_url, _ = app
    status, payload = http_json(
        base_url,
        "/api/import",
        method="POST",
        payload={
            "version": 1,
            "exported_at": "2026-08-08T12:00:00Z",
            "groups": [
                {
                    "name": "First in document",
                    "parent": None,
                    "position": 1,
                    "urls": [
                        {
                            "url": "https://shared.example",
                            "title": "First metadata",
                            "created_at": "2026-08-08T12:00:01Z",
                            "important": True,
                        }
                    ],
                },
                {
                    "name": "Parent",
                    "parent": None,
                    "position": 0,
                    "urls": [],
                },
                {
                    "name": "Child",
                    "parent": "Parent",
                    "position": 0,
                    "urls": [
                        {
                            "url": "https://shared.example",
                            "title": "Later metadata",
                            "created_at": "2026-08-08T12:00:02Z",
                            "important": False,
                        }
                    ],
                },
            ],
        },
    )

    assert status == 200
    first = group_in(payload, "First in document")["urls"][0]
    child = group_in(payload, "Child")["urls"][0]
    assert first["id"] == child["id"]
    assert first["title"] == "First metadata"
    assert first["created_at"] == "2026-08-08T12:00:01Z"
    assert first["important"] is True


def test_version_1_name_matching_uses_database_lower_semantics(app):
    base_url, database_url = app
    sharp_s = trellmark.add_group("Straße")
    double_s = trellmark.add_group("STRASSE")
    assert sharp_s is not None
    assert double_s is not None
    sharp_url = trellmark.add_url("https://sharp-s.example")
    double_url = trellmark.add_url("https://double-s.example")
    assert sharp_url is not None
    assert double_url is not None
    trellmark.move_url_to_group(sharp_url["id"], sharp_s["id"])
    trellmark.move_url_to_group(double_url["id"], double_s["id"])
    _, exported = http_json(base_url, "/api/export")

    reset_database(database_url)
    clear_authentication(base_url)
    status, payload = http_json(
        base_url, "/api/import", method="POST", payload=exported
    )

    assert status == 200
    assert grouped_urls_in(payload, "Straße") == ["https://sharp-s.example"]
    assert grouped_urls_in(payload, "STRASSE") == ["https://double-s.example"]


def test_storage_import_rejects_a_missing_parent_before_creating_groups(app):
    before = trellmark.read_group_records()

    with pytest.raises(ValueError, match="Imported parent group does not exist"):
        trellmark.import_saved_data(
            {
                "version": 1,
                "groups": [
                    {
                        "name": "New",
                        "parent": "Missing",
                        "position": 0,
                        "nsfw": False,
                        "domains": [],
                        "urls": [],
                    }
                ],
            }
        )

    assert trellmark.read_group_records() == before


def test_storage_import_reports_a_sibling_set_that_disappears(app, monkeypatch):
    parent = trellmark.add_group("Parent")
    assert parent is not None
    original_read_group_records = storage.read_group_records
    read_count = 0

    def read_changing_groups():
        nonlocal read_count
        read_count += 1
        groups = original_read_group_records()
        if read_count == 2:
            # The post-attach read that builds current_ids_by_parent.
            stored_parent = next(group for group in groups if group["name"] == "Parent")
            stored_parent["children"] = []
        return groups

    monkeypatch.setattr(storage, "read_group_records", read_changing_groups)

    with pytest.raises(RuntimeError, match="Imported sibling set no longer exists"):
        trellmark.import_saved_data(
            {
                "version": 1,
                "groups": [
                    {
                        "name": "New",
                        "parent": "Parent",
                        "position": 0,
                        "nsfw": False,
                        "domains": [],
                        "urls": [],
                    }
                ],
            }
        )


@pytest.mark.parametrize(
    "groups",
    [
        [{"name": "New", "parent": "Missing", "position": 0, "urls": []}],
        [{"name": "New", "parent": "new", "position": 0, "urls": []}],
        [
            {"name": "A", "parent": "B", "position": 0, "urls": []},
            {"name": "B", "parent": "A", "position": 0, "urls": []},
        ],
        [
            {"name": "A", "parent": None, "position": 0, "urls": []},
            {"name": "B", "parent": None, "position": 0, "urls": []},
        ],
        [
            {"name": "A", "parent": None, "position": 0, "urls": []},
            {"name": "B", "parent": "A", "position": 0, "urls": []},
            {"name": "C", "parent": "B", "position": 0, "urls": []},
            {"name": "D", "parent": "C", "position": 0, "urls": []},
        ],
    ],
    ids=["missing-parent", "self-parent", "cycle", "duplicate-position", "depth"],
)
def test_version_1_rejects_an_invalid_hierarchy_before_any_write(app, groups):
    base_url, _ = app
    reading = trellmark.add_group("Reading", domains=["example.com"])
    saved = trellmark.add_url("https://example.com/existing")
    assert reading is not None
    assert saved is not None
    before_groups = trellmark.read_group_records()
    before_urls = trellmark.read_url_records()
    document = {
        "version": 1,
        "exported_at": "2026-08-08T12:00:00Z",
        "groups": groups,
    }

    status, response = http_json(
        base_url, "/api/import", method="POST", payload=document
    )

    assert_validation_error(status, response)
    assert "Invalid import file." in str(response)
    assert trellmark.read_group_records() == before_groups
    assert trellmark.read_url_records() == before_urls


def test_version_1_rejects_depth_against_existing_ancestors_before_any_write(app):
    base_url, _ = app
    root = trellmark.add_group("Root")
    assert root is not None
    child = trellmark.add_group("Child", parent_id=root["id"])
    assert child is not None
    grandchild = trellmark.add_group("Grandchild", parent_id=child["id"])
    assert grandchild is not None
    before = trellmark.read_group_records()

    status, response = http_json(
        base_url,
        "/api/import",
        method="POST",
        payload={
            "version": 1,
            "exported_at": "2026-08-08T12:00:00Z",
            "groups": [
                {
                    "name": "Too deep",
                    "parent": "grandCHILD",
                    "position": 0,
                    "urls": [],
                }
            ],
        },
    )

    assert_validation_error(status, response)
    assert "Invalid import file." in str(response)
    assert trellmark.read_group_records() == before


def test_version_1_rejects_a_cycle_through_an_existing_descendant(app):
    base_url, _ = app
    parent = trellmark.add_group("Parent")
    assert parent is not None
    child = trellmark.add_group("Child", parent_id=parent["id"])
    assert child is not None
    before = trellmark.read_group_records()

    status, response = http_json(
        base_url,
        "/api/import",
        method="POST",
        payload={
            "version": 1,
            "exported_at": "2026-08-08T12:00:00Z",
            "groups": [
                {
                    "name": "Parent",
                    "parent": "Child",
                    "position": 0,
                    "urls": [],
                }
            ],
        },
    )

    assert_validation_error(status, response)
    assert "Invalid import file." in str(response)
    assert trellmark.read_group_records() == before


def test_version_1_allows_the_same_position_under_different_parents(app):
    base_url, _ = app
    status, payload = http_json(
        base_url,
        "/api/import",
        method="POST",
        payload={
            "version": 1,
            "exported_at": "2026-08-08T12:00:00Z",
            "groups": [
                {"name": "A", "parent": None, "position": 0, "urls": []},
                {"name": "B", "parent": None, "position": 1, "urls": []},
                {"name": "A child", "parent": "a", "position": 0, "urls": []},
                {"name": "B child", "parent": "B", "position": 0, "urls": []},
            ],
        },
    )

    assert status == 200
    assert child_names_in(payload, "A") == ["A child"]
    assert child_names_in(payload, "B") == ["B child"]


def test_version_1_reparents_existing_groups_without_changing_identity(app):
    base_url, _ = app
    ancestor = trellmark.add_group("Ancestor")
    destination = trellmark.add_group("Destination")
    assert ancestor is not None
    assert destination is not None
    descendant = trellmark.add_group("Descendant", parent_id=ancestor["id"])
    assert descendant is not None

    status, payload = http_json(
        base_url,
        "/api/import",
        method="POST",
        payload={
            "version": 1,
            "exported_at": "2026-08-08T12:00:00Z",
            "groups": [
                {
                    "name": "Ancestor",
                    "parent": "Descendant",
                    "position": 0,
                    "urls": [],
                },
                {
                    "name": "Descendant",
                    "parent": "Destination",
                    "position": 0,
                    "urls": [],
                },
            ],
        },
    )

    assert status == 200
    assert child_names_in(payload, "Destination") == ["Descendant"]
    assert child_names_in(payload, "Descendant") == ["Ancestor"]
    assert group_in(payload, "Ancestor")["id"] == ancestor["id"]
    assert group_in(payload, "Descendant")["id"] == descendant["id"]


def test_imported_groups_without_parents_move_to_the_root(app):
    base_url, _ = app
    parent = trellmark.add_group("Parent")
    assert parent is not None
    nested = trellmark.add_group("Nested", parent_id=parent["id"])
    assert nested is not None

    status, payload = http_json(
        base_url,
        "/api/import",
        method="POST",
        payload={
            "version": 1,
            "exported_at": "2026-08-08T12:00:00Z",
            "groups": [
                {"name": "Parent", "position": 1, "urls": []},
                {"name": "Nested", "position": 0, "urls": []},
            ],
        },
    )

    assert status == 200
    assert group_names_in(payload) == ["Nested", "Parent", "default"]
    assert group_in(payload, "Nested")["parent_id"] is None
    assert group_in(payload, "Parent")["parent_id"] is None


def test_version_1_orders_imported_groups_before_unmentioned_siblings(app):
    base_url, _ = app
    parent = trellmark.add_group("Parent")
    imported_a = trellmark.add_group("Imported A", parent_id=parent["id"])
    untouched = trellmark.add_group("Untouched", parent_id=parent["id"])
    imported_b = trellmark.add_group("Imported B", parent_id=parent["id"])
    imported_root = trellmark.add_group("Imported root")
    assert parent is not None
    assert imported_a is not None
    assert untouched is not None
    assert imported_b is not None
    assert imported_root is not None

    status, payload = http_json(
        base_url,
        "/api/import",
        method="POST",
        payload={
            "version": 1,
            "exported_at": "2026-08-08T12:00:00Z",
            "groups": [
                {
                    "name": "Imported A",
                    "parent": "PARENT",
                    "position": 1,
                    "urls": [],
                },
                {
                    "name": "Imported root",
                    "parent": None,
                    "position": 0,
                    "urls": [],
                },
                {
                    "name": "Imported B",
                    "parent": "parent",
                    "position": 0,
                    "urls": [],
                },
            ],
        },
    )

    assert status == 200
    assert group_names_in(payload) == ["Imported root", "default", "Parent"]
    assert child_names_in(payload, "Parent") == [
        "Imported B",
        "Imported A",
        "Untouched",
    ]


def test_post_import_normalizes_urls_and_skips_duplicates(app):
    base_url, _ = app
    existing = trellmark.add_url("https://example.com")
    reading = trellmark.add_group("Reading")
    document = {
        "version": 1,
        "exported_at": "2026-07-03T12:00:00Z",
        "groups": [
            {
                "name": "Reading",
                "position": 0,
                "urls": [
                    {
                        "url": "HTTPS://Example.com/",
                        "created_at": "2026-07-03T12:00:01Z",
                    },
                    {
                        "url": "Fresh.example/",
                        "created_at": "2026-07-03T12:00:02Z",
                    },
                ],
            }
        ],
    }

    status, payload = http_json(
        base_url, "/api/import", method="POST", payload=document
    )

    assert status == 200
    assert payload["imported"] == 1
    assert payload["skipped"] == 1
    assert grouped_url_ids_in(payload, "default") == [existing["id"]]
    assert grouped_urls_in(payload, "Reading") == ["https://fresh.example"]
    assert trellmark.read_urls() == ["https://example.com", "https://fresh.example"]
    assert grouped_url_ids_in(
        {"groups": trellmark.read_group_records()}, "Reading"
    ) == [trellmark.read_url_records()[1]["id"]]
    assert reading["id"] == payload["groups"][0]["id"]
    [(stored_created_at,)] = db_query(
        "SELECT created_at FROM urls WHERE url = :url",
        url="https://fresh.example",
    )
    # Stored as an absolute instant now; the document's 'Z' is the same moment.
    assert stored_created_at == datetime(2026, 7, 3, 12, 0, 2, tzinfo=timezone.utc)
    assert trellmark.read_url_records()[1]["created_at"] == "2026-07-03T12:00:02Z"
    assert trellmark.read_url_records()[1]["title"] is None
    assert trellmark.read_url_records()[1]["important"] is False


def test_post_import_uses_default_for_non_domain_hostname(app):
    base_url, _ = app
    document = {
        "version": 1,
        "exported_at": "2026-07-03T12:00:00Z",
        "groups": [
            {
                "name": "default",
                "position": 0,
                "urls": [
                    {
                        "url": "https://exam_ple.com/x",
                        "created_at": "2026-07-03T12:00:01Z",
                    }
                ],
            }
        ],
    }

    status, payload = http_json(
        base_url, "/api/import", method="POST", payload=document
    )

    assert status == 200
    assert payload["imported"] == 1
    assert grouped_urls_in(payload, "default") == ["https://exam_ple.com/x"]


def test_post_import_restores_important(app):
    base_url, _ = app
    document = {
        "version": 1,
        "exported_at": "2026-07-03T12:00:00Z",
        "groups": [
            {
                "name": "default",
                "position": 0,
                "urls": [
                    {
                        "url": "https://one.example",
                        "created_at": "2026-07-03T12:00:01Z",
                        "important": True,
                    },
                    {
                        "url": "https://two.example",
                        "created_at": "2026-07-03T12:00:02Z",
                        "important": False,
                    },
                ],
            }
        ],
    }

    status, payload = http_json(
        base_url, "/api/import", method="POST", payload=document
    )

    assert status == 200
    assert payload["imported"] == 2
    assert [record["important"] for record in payload["groups"][0]["urls"]] == [
        True,
        False,
    ]
    _, exported = http_json(base_url, "/api/export")
    assert exported["version"] == 1
    assert [record["important"] for record in exported["groups"][0]["urls"]] == [
        True,
        False,
    ]


def test_post_import_restores_title(app):
    base_url, _ = app
    document = {
        "version": 1,
        "exported_at": "2026-07-03T12:00:00Z",
        "groups": [
            {
                "name": "default",
                "position": 0,
                "urls": [
                    {
                        "url": "https://one.example",
                        "title": " One   Example ",
                        "created_at": "2026-07-03T12:00:01Z",
                        "important": True,
                    },
                    {
                        "url": "https://two.example",
                        "title": None,
                        "created_at": "2026-07-03T12:00:02Z",
                        "important": False,
                    },
                ],
            }
        ],
    }

    status, payload = http_json(
        base_url, "/api/import", method="POST", payload=document
    )

    assert status == 200
    assert payload["imported"] == 2
    assert [record["title"] for record in payload["groups"][0]["urls"]] == [
        "One Example",
        None,
    ]
    _, exported = http_json(base_url, "/api/export")
    assert exported["version"] == 1
    assert [record["title"] for record in exported["groups"][0]["urls"]] == [
        "One Example",
        None,
    ]


def test_post_import_restores_nsfw(app):
    base_url, _ = app
    document = {
        "version": 1,
        "exported_at": "2026-07-03T12:00:00Z",
        "groups": [
            {
                "name": "Comics",
                "position": 0,
                "nsfw": True,
                "urls": [],
            }
        ],
    }

    status, payload = http_json(
        base_url, "/api/import", method="POST", payload=document
    )

    assert status == 200
    assert payload["groups"][0]["name"] == "Comics"
    assert payload["groups"][0]["nsfw"] is True
    _, exported = http_json(base_url, "/api/export")
    assert exported["version"] == 1
    assert exported["groups"][0]["nsfw"] is True


def test_post_import_defaults_group_nsfw_to_false(app):
    base_url, _ = app
    document = {
        "version": 1,
        "exported_at": "2026-07-03T12:00:00Z",
        "groups": [
            {
                "name": "Reading",
                "position": 0,
                "urls": [],
            }
        ],
    }

    status, payload = http_json(
        base_url, "/api/import", method="POST", payload=document
    )

    assert status == 200
    assert payload["groups"][0]["nsfw"] is False


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"version": 2, "groups": []},
        {"version": 1, "groups": "not a list"},
        {"version": 1, "groups": []},
        {"version": 1, "groups": [{"name": "default", "position": 0, "urls": []}]},
        {"version": 1, "groups": [{"name": "", "position": 0, "urls": []}]},
        {"version": 1, "groups": [{"name": "default", "position": True, "urls": []}]},
        {"version": 1, "groups": [{"name": "default", "position": 0, "urls": {}}]},
        {
            "version": 1,
            "groups": [
                {
                    "name": "default",
                    "position": 0,
                    "urls": [
                        {
                            "url": "ftp://example.com",
                            "created_at": "2026-07-03T12:00:00Z",
                        }
                    ],
                }
            ],
        },
        {
            "version": 2,
            "exported_at": "2026-07-03T12:00:00Z",
            "groups": [
                {
                    "name": "default",
                    "position": 0,
                    "urls": [
                        {
                            "url": "https://example.com",
                            "created_at": "2026-07-03T12:00:00Z",
                            "important": 1,
                        }
                    ],
                }
            ],
        },
        {
            "version": 3,
            "exported_at": "2026-07-03T12:00:00Z",
            "groups": [
                {
                    "name": "default",
                    "position": 0,
                    "urls": [
                        {
                            "url": "https://example.com",
                            "title": 42,
                            "created_at": "2026-07-03T12:00:00Z",
                            "important": False,
                        }
                    ],
                }
            ],
        },
        {
            "version": 4,
            "exported_at": "2026-07-03T12:00:00Z",
            "groups": [
                {
                    "name": "default",
                    "position": 0,
                    "nsfw": 1,
                    "urls": [],
                }
            ],
        },
    ],
)
def test_post_import_rejects_malformed_file(app, payload):
    base_url, _ = app

    status, response = http_json(
        base_url, "/api/import", method="POST", payload=payload
    )

    assert_validation_error(status, response)
    assert [group["name"] for group in trellmark.read_group_records()] == ["default"]
    assert trellmark.read_urls() == []
