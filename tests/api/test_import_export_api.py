import json
import threading
from dataclasses import replace
from datetime import datetime, timezone
from urllib import request

import pytest
from sqlalchemy import event

from tests.bookmarks import helpers as bookmark_helpers
from tests.helpers import (
    _authentication_headers,
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
from trellmark.backup.api import ExportDocument, ImportDocument
from trellmark.backup.application import ImportInvalid, import_document_in_uow
from trellmark.backup.domain import normalize_import_document
from trellmark.backup.persistence import PostgresBackupUnitOfWorkFactory
from trellmark.bookmarks import domain as bookmark_domain
from trellmark.bookmarks.persistence import (
    PostgresGroupRepository,
    PostgresLogicalBookmarkUnitOfWorkFactory,
)
from trellmark.platform import runtime


def assert_invalid_import(status, payload):
    assert status == 422
    assert payload == {"error": "Invalid import file.", "code": "invalid_import"}


def test_export_download_filename_matches_document_timestamp(app):
    base_url, _ = app
    with request.urlopen(
        request.Request(
            base_url + "/api/export", headers=_authentication_headers(base_url)
        ),
        timeout=5,
    ) as response:
        payload = json.load(response)
        disposition = response.headers["Content-Disposition"]
        assert response.headers["Cache-Control"] == "no-store"
    exported_at = datetime.fromisoformat(payload["exported_at"].replace("Z", "+00:00"))

    assert disposition == (
        f'attachment; filename="trellmark-export-{exported_at:%Y%m%dT%H%M%SZ}.json"'
    )


def test_get_export_returns_groups_and_urls_without_internal_ids(app):
    base_url, _ = app
    default_url = bookmark_helpers.seed_url("https://one.example")
    reading = bookmark_helpers.seed_group("Reading")
    reading_url = bookmark_helpers.seed_url("https://two.example")
    bookmark_helpers.seed_membership(reading_url["id"], reading["id"])

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


def test_export_uses_one_readonly_repeatable_snapshot_during_concurrent_write(app):
    base_url, _ = app
    group = bookmark_helpers.seed_group("Before", domains=["before.example"])
    saved = bookmark_helpers.seed_url("https://before.example", title="Before title")
    before = exported_without_timestamp(http_json(base_url, "/api/export")[1])
    engine = runtime.get_engine()
    group_read = threading.Event()
    release_export = threading.Event()
    export_connection = []
    query_scopes = []
    settings = []
    response = {}
    statements = []

    def after_query(connection, _cursor, statement, *_args):
        if (
            not export_connection
            and statement.startswith("SELECT groups.id,")
            and "FROM groups ORDER BY" in statement
        ):
            export_connection.append(connection)
            settings.append(
                tuple(
                    connection.exec_driver_sql(
                        "SELECT current_setting('transaction_isolation'), "
                        "current_setting('transaction_read_only')"
                    ).one()
                )
            )
            group_read.set()
            assert release_export.wait(timeout=5), "export barrier was not released"
        if export_connection and connection is export_connection[0]:
            if statement.startswith("SELECT") and "current_setting" not in statement:
                query_scopes.append((id(connection), id(connection.get_transaction())))
            statements.append(statement)

    def export():
        response["value"] = http_json(base_url, "/api/export")

    event.listen(engine, "after_cursor_execute", after_query)
    worker = threading.Thread(target=export)
    worker.start()
    try:
        assert group_read.wait(timeout=5), "export did not reach the first group query"
        with PostgresLogicalBookmarkUnitOfWorkFactory(runtime.get_engine)() as uow:
            assert uow._connection.get_isolation_level() == "READ COMMITTED"
            assert isinstance(
                uow.groups.update_group(
                    bookmark_domain.UpdateGroup(
                        group["id"], name="After", nsfw=True, domains=("after.example",)
                    )
                ),
                bookmark_domain.GroupUpdated,
            )
            assert isinstance(
                uow.bookmarks.edit_url(
                    bookmark_domain.EditURL(
                        saved["id"], saved["version"], title="After title"
                    )
                ),
                bookmark_domain.URLUpdated,
            )
            uow.commit()
    finally:
        release_export.set()
        worker.join(timeout=5)
        event.remove(engine, "after_cursor_execute", after_query)
    assert not worker.is_alive()
    assert response["value"][0] == 200
    assert exported_without_timestamp(response["value"][1]) == before
    assert settings == [("repeatable read", "on")]
    assert len(query_scopes) == 3 and len(set(query_scopes)) == 1
    assert all("advisory" not in statement.lower() for statement in statements)
    after = exported_without_timestamp(http_json(base_url, "/api/export")[1])
    assert after != before
    changed = next(item for item in after["groups"] if item["name"] == "After")
    assert changed["domains"] == ["after.example"] and changed["nsfw"] is True
    assert changed["urls"][0]["title"] == "After title"
    with engine.connect() as connection:
        assert (
            connection.exec_driver_sql("SHOW transaction_isolation").scalar()
            == "read committed"
        )
        assert (
            connection.exec_driver_sql("SHOW transaction_read_only").scalar() == "off"
        )


def test_post_import_into_fresh_db_restores_exported_document(app):
    base_url, database_url = app
    reading = bookmark_helpers.seed_group("Reading")
    work = bookmark_helpers.seed_group("Work")
    default_url = bookmark_helpers.seed_url("https://one.example")
    reading_url = bookmark_helpers.seed_url("https://two.example")
    work_url = bookmark_helpers.seed_url("https://three.example")
    bookmark_helpers.seed_membership(reading_url["id"], reading["id"])
    bookmark_helpers.seed_membership(work_url["id"], work["id"])
    http_json(
        base_url,
        "/api/groups/order",
        method="PATCH",
        payload={"parent_id": None, "group_ids": [work["id"], reading["id"], 1]},
    )
    _, exported = http_json(base_url, "/api/export")

    assert ExportDocument.model_validate(exported).model_dump() == exported
    assert ImportDocument.model_validate(
        exported
    ).to_domain() == normalize_import_document(exported)

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
    engineering = bookmark_helpers.seed_group("Engineering")
    reading = bookmark_helpers.seed_group("Reading", domains=["example.com"])
    assert engineering is not None
    assert reading is not None
    backend = bookmark_helpers.seed_group(
        "Backend",
        domains=["example.com"],
        parent_id=engineering["id"],
    )
    archive = bookmark_helpers.seed_group(
        "Archive",
        nsfw=True,
        parent_id=engineering["id"],
    )
    assert backend is not None
    assert archive is not None
    deep = bookmark_helpers.seed_group("Deep", parent_id=backend["id"])
    assert deep is not None

    shared = bookmark_helpers.seed_url("https://example.com/shared", title="Shared")
    deep_url = bookmark_helpers.seed_url("https://deep.example/article", title="Deep")
    assert shared is not None
    assert deep_url is not None
    bookmark_helpers.seed_membership(deep_url["id"], deep["id"])
    bookmark_helpers.seed_important(shared["id"], True)
    bookmark_helpers.seed_created_at(
        shared["id"], datetime(2026, 8, 8, 10, 30, tzinfo=timezone.utc)
    )
    assert bookmark_helpers.seed_group_order(
        None, [reading["id"], engineering["id"], 1]
    )
    assert bookmark_helpers.seed_group_order(
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
    bookmark_helpers.seed_group("Reading", domains=["example.com"])
    bookmark_helpers.seed_group("Work", domains=["example.com"])
    saved = bookmark_helpers.seed_url("https://example.com/article")
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
    assert payload["imported"] == 1
    assert payload["skipped"] == 0
    first = group_in(payload, "First in document")["urls"][0]
    child = group_in(payload, "Child")["urls"][0]
    assert first["id"] == child["id"]
    assert first["title"] == "First metadata"
    assert first["created_at"] == "2026-08-08T12:00:01Z"
    assert first["important"] is True
    assert grouped_urls_in(payload, "First in document") == ["https://shared.example"]
    assert grouped_urls_in(payload, "Child") == ["https://shared.example"]


def test_version_1_name_matching_uses_database_lower_semantics(app):
    base_url, database_url = app
    sharp_s = bookmark_helpers.seed_group("Straße")
    double_s = bookmark_helpers.seed_group("STRASSE")
    assert sharp_s is not None
    assert double_s is not None
    sharp_url = bookmark_helpers.seed_url("https://sharp-s.example")
    double_url = bookmark_helpers.seed_url("https://double-s.example")
    assert sharp_url is not None
    assert double_url is not None
    bookmark_helpers.seed_membership(sharp_url["id"], sharp_s["id"])
    bookmark_helpers.seed_membership(double_url["id"], double_s["id"])
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
    before = bookmark_helpers.group_payloads()

    document = normalize_import_document(
        {
            "version": 1,
            "exported_at": "2026-08-30T00:00:00Z",
            "groups": [{"name": "New", "parent": "Missing", "position": 0, "urls": []}],
        }
    )
    assert (
        import_document_in_uow(
            PostgresBackupUnitOfWorkFactory(runtime.get_engine), document
        )
        == ImportInvalid()
    )

    assert bookmark_helpers.group_payloads() == before


def test_storage_import_reports_a_sibling_set_that_disappears(app, monkeypatch):
    parent = bookmark_helpers.seed_group("Parent")
    assert parent is not None
    original_list_groups = PostgresGroupRepository.list_groups
    read_count = 0

    def read_changing_groups(repository):
        nonlocal read_count
        read_count += 1
        groups = original_list_groups(repository)
        if read_count == 2:
            # The post-attach read that builds current_ids_by_parent.
            groups = tuple(
                replace(group, children=()) if group.name == "Parent" else group
                for group in groups
            )
        return groups

    monkeypatch.setattr(PostgresGroupRepository, "list_groups", read_changing_groups)

    with pytest.raises(RuntimeError, match="Imported sibling set no longer exists"):
        import_document_in_uow(
            PostgresBackupUnitOfWorkFactory(runtime.get_engine),
            normalize_import_document(
                {
                    "version": 1,
                    "exported_at": "2026-08-30T00:00:00Z",
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
            ),
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
    reading = bookmark_helpers.seed_group("Reading", domains=["example.com"])
    saved = bookmark_helpers.seed_url("https://example.com/existing")
    assert reading is not None
    assert saved is not None
    before_groups = bookmark_helpers.group_payloads()
    before_urls = bookmark_helpers.url_payloads()
    document = {
        "version": 1,
        "exported_at": "2026-08-08T12:00:00Z",
        "groups": groups,
    }

    status, response = http_json(
        base_url, "/api/import", method="POST", payload=document
    )

    assert_invalid_import(status, response)
    assert "Invalid import file." in str(response)
    assert bookmark_helpers.group_payloads() == before_groups
    assert bookmark_helpers.url_payloads() == before_urls


def test_version_1_rejects_depth_against_existing_ancestors_before_any_write(app):
    base_url, _ = app
    root = bookmark_helpers.seed_group("Root")
    assert root is not None
    child = bookmark_helpers.seed_group("Child", parent_id=root["id"])
    assert child is not None
    grandchild = bookmark_helpers.seed_group("Grandchild", parent_id=child["id"])
    assert grandchild is not None
    before = bookmark_helpers.group_payloads()

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

    assert_invalid_import(status, response)
    assert "Invalid import file." in str(response)
    assert bookmark_helpers.group_payloads() == before


def test_version_1_rejects_a_cycle_through_an_existing_descendant(app):
    base_url, _ = app
    parent = bookmark_helpers.seed_group("Parent")
    assert parent is not None
    child = bookmark_helpers.seed_group("Child", parent_id=parent["id"])
    assert child is not None
    before = bookmark_helpers.group_payloads()

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

    assert_invalid_import(status, response)
    assert "Invalid import file." in str(response)
    assert bookmark_helpers.group_payloads() == before


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
    ancestor = bookmark_helpers.seed_group("Ancestor")
    destination = bookmark_helpers.seed_group("Destination")
    assert ancestor is not None
    assert destination is not None
    descendant = bookmark_helpers.seed_group("Descendant", parent_id=ancestor["id"])
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
    parent = bookmark_helpers.seed_group("Parent")
    assert parent is not None
    nested = bookmark_helpers.seed_group("Nested", parent_id=parent["id"])
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
    parent = bookmark_helpers.seed_group("Parent")
    imported_a = bookmark_helpers.seed_group("Imported A", parent_id=parent["id"])
    untouched = bookmark_helpers.seed_group("Untouched", parent_id=parent["id"])
    imported_b = bookmark_helpers.seed_group("Imported B", parent_id=parent["id"])
    imported_root = bookmark_helpers.seed_group("Imported root")
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
    existing = bookmark_helpers.seed_url("https://example.com")
    reading = bookmark_helpers.seed_group("Reading")
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
    assert bookmark_helpers.saved_urls() == [
        "https://example.com",
        "https://fresh.example",
    ]
    assert grouped_url_ids_in(
        {"groups": bookmark_helpers.group_payloads()}, "Reading"
    ) == [bookmark_helpers.url_payloads()[1]["id"]]
    assert reading["id"] == payload["groups"][0]["id"]
    [(stored_created_at,)] = db_query(
        "SELECT created_at FROM urls WHERE url = :url",
        url="https://fresh.example",
    )
    # Stored as an absolute instant now; the document's 'Z' is the same moment.
    assert stored_created_at == datetime(2026, 7, 3, 12, 0, 2, tzinfo=timezone.utc)
    assert bookmark_helpers.url_payloads()[1]["created_at"] == "2026-07-03T12:00:02Z"
    assert bookmark_helpers.url_payloads()[1]["title"] is None
    assert bookmark_helpers.url_payloads()[1]["important"] is False


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
        None,
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
            "exported_at": "2026-07-03T12:00:00Z",
            "groups": [
                {"name": "Same", "position": 0, "urls": []},
                {"name": "same", "position": 1, "urls": []},
            ],
        },
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

    assert_invalid_import(status, response)
    assert [group["name"] for group in bookmark_helpers.group_payloads()] == ["default"]
    assert bookmark_helpers.saved_urls() == []
