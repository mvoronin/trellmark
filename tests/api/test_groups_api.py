from dataclasses import asdict
from datetime import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from tests.bookmarks import helpers as bookmark_helpers
from tests.bookmarks.helpers import saved_urls, url_group_ids, url_payload, url_payloads
from tests.helpers import (
    assert_validation_error,
    capture_storage_statements,
    db_connection,
    group_names_in,
    group_positions_in,
    grouped_url_ids_in,
    grouped_urls_in,
    http_json,
    run_async,
)
from trellmark.bookmarks.api import DeleteGroupResponse, EditGroupResponse
from trellmark.bookmarks.application import update_group_in_uow
from trellmark.bookmarks.domain import (
    DeleteGroup,
    GroupDeleted,
    GroupUpdated,
    MoveURL,
    UpdateGroup,
    URLMoved,
)
from trellmark.bookmarks.persistence import (
    PostgresGroupQueries,
    PostgresLogicalBookmarkUnitOfWorkFactory,
)
from trellmark.platform import runtime


def test_patch_group_succeeds_if_deleted_before_response_read(app, monkeypatch):
    base_url, _ = app
    group = bookmark_helpers.seed_group("Before deletion")
    original_list = PostgresGroupQueries.list_groups
    deleted = False

    def delete_before_read(queries):
        nonlocal deleted
        if not deleted:
            deleted = True
            with PostgresLogicalBookmarkUnitOfWorkFactory(runtime.get_engine)() as uow:
                outcome = uow.groups.delete_group(DeleteGroup(group["id"], "delete"))
                assert isinstance(outcome, GroupDeleted)
                uow.commit()
        return original_list(queries)

    monkeypatch.setattr(PostgresGroupQueries, "list_groups", delete_before_read)
    status, payload = http_json(
        base_url,
        f"/api/groups/{group['id']}",
        method="PATCH",
        payload={"name": "Committed"},
    )
    assert deleted
    assert status == 200
    assert payload["group"]["id"] == group["id"]
    assert payload["group"]["name"] == "Committed"
    assert group["id"] not in {item["id"] for item in payload["groups"]}


def test_get_groups_returns_empty_default_group(app):
    base_url, _ = app

    status, payload = http_json(base_url, "/api/groups")

    assert status == 200
    assert payload == {
        "groups": [
            {
                "id": 1,
                "name": "default",
                "parent_id": None,
                "position": 0,
                "depth": 1,
                "nsfw": False,
                "domains": [],
                "urls": [],
                "children": [],
            }
        ]
    }


def test_get_groups_returns_saved_urls_in_default_group(app):
    base_url, _ = app
    bookmark_helpers.seed_url("https://one.example")
    bookmark_helpers.seed_url("https://two.example")

    status, payload = http_json(base_url, "/api/groups")

    assert status == 200
    assert [group["name"] for group in payload["groups"]] == ["default"]
    assert grouped_url_ids_in(payload, "default") == [1, 2]
    assert grouped_urls_in(payload, "default") == [
        "https://one.example",
        "https://two.example",
    ]
    created_at = payload["groups"][0]["urls"][0]["created_at"]
    assert created_at.endswith("Z")
    assert datetime.fromisoformat(created_at.replace("Z", "+00:00"))


def test_membership_pointing_at_a_missing_group_is_rejected(app):
    """PostgreSQL makes the orphaned-membership state unreachable.

    Under SQLite this row could be inserted whenever foreign keys happened to
    be off, so reads had to tolerate it. The foreign key is always enforced
    here, so the guarantee is now that the write never lands.
    """
    base_url, _ = app
    saved = bookmark_helpers.seed_url("https://one.example")

    with pytest.raises(IntegrityError):
        with db_connection() as connection:
            connection.execute(
                text("INSERT INTO url_groups (url_id, group_id) VALUES (:url_id, 999)"),
                {"url_id": saved["id"]},
            )

    status, payload = http_json(base_url, "/api/groups")

    assert status == 200
    assert grouped_url_ids_in(payload, "default") == [saved["id"]]


def test_get_groups_does_not_change_flat_url_endpoint(app):
    base_url, _ = app
    bookmark_helpers.seed_url("https://one.example")

    status, payload = http_json(base_url, "/api/urls")

    assert status == 200
    assert payload == {"urls": url_payloads()}


def test_post_group_creates_group_at_next_position(app):
    base_url, _ = app

    status, payload = http_json(
        base_url, "/api/groups", method="POST", payload={"name": "Reading"}
    )

    assert status == 201
    assert payload["group"] == {
        "id": 2,
        "name": "Reading",
        "parent_id": None,
        "position": 1,
        "depth": 1,
        "nsfw": False,
        "domains": [],
        "urls": [],
        "children": [],
    }
    assert [group["name"] for group in payload["groups"]] == ["default", "Reading"]
    assert [group["position"] for group in payload["groups"]] == [0, 1]

    status, payload = http_json(
        base_url, "/api/groups", method="POST", payload={"name": "Work"}
    )

    assert status == 201
    assert payload["group"] == {
        "id": 3,
        "name": "Work",
        "parent_id": None,
        "position": 2,
        "depth": 1,
        "nsfw": False,
        "domains": [],
        "urls": [],
        "children": [],
    }
    assert [group["name"] for group in payload["groups"]] == [
        "default",
        "Reading",
        "Work",
    ]
    assert [group["position"] for group in payload["groups"]] == [0, 1, 2]


def test_post_group_stores_nsfw_flag_and_defaults_to_safe(app):
    base_url, _ = app

    status, payload = http_json(
        base_url,
        "/api/groups",
        method="POST",
        payload={"name": "Comics", "nsfw": True},
    )

    assert status == 201
    assert payload["group"]["nsfw"] is True
    assert payload["groups"][0]["nsfw"] is False
    assert bookmark_helpers.group_payloads()[1]["nsfw"] is True


def test_post_group_normalizes_and_deduplicates_domains(app):
    base_url, _ = app

    status, payload = http_json(
        base_url,
        "/api/groups",
        method="POST",
        payload={
            "name": "Reading",
            "domains": ["news.example", " Example.COM. ", "example.com"],
        },
    )

    assert status == 201
    assert payload["group"]["domains"] == ["example.com", "news.example"]
    assert payload["groups"][1]["domains"] == ["example.com", "news.example"]


def test_storage_group_mutations_normalize_domains_and_match_urls(app):
    created = bookmark_helpers.seed_group(
        "Reading",
        domains=[" Z.Example. ", "a.example", "z.example"],
    )

    assert created["domains"] == ["a.example", "z.example"]
    assert bookmark_helpers.group_payloads()[1]["domains"] == created["domains"]
    first_url = bookmark_helpers.seed_url("https://z.example/article")
    assert url_group_ids(first_url["id"]) == [created["id"]]

    updated = update_group_in_uow(
        PostgresLogicalBookmarkUnitOfWorkFactory(runtime.get_engine),
        UpdateGroup(created["id"], domains=(" Y.Example. ", "b.example", "y.example")),
    )

    assert isinstance(updated, GroupUpdated)
    assert updated.record.domains == ("b.example", "y.example")
    assert bookmark_helpers.group_payloads()[1]["domains"] == list(
        updated.record.domains
    )
    second_url = bookmark_helpers.seed_url("https://y.example/article")
    assert url_group_ids(second_url["id"]) == [created["id"]]


def test_storage_group_mutations_reject_invalid_domains_atomically(app):
    with pytest.raises(ValueError, match="Enter valid domains"):
        bookmark_helpers.seed_group("Invalid", domains=["bad domain"])
    assert bookmark_helpers.group_by_name("Invalid") is None

    created = bookmark_helpers.seed_group("Reading", domains=["example.com"])
    with pytest.raises(ValueError, match="Enter valid domains"):
        update_group_in_uow(
            PostgresLogicalBookmarkUnitOfWorkFactory(runtime.get_engine),
            UpdateGroup(created["id"], domains=("bad domain",)),
        )
    assert bookmark_helpers.group_by_name("Reading")["domains"] == ["example.com"]


def test_internal_group_domain_writer_normalizes_before_insert(app):
    group = bookmark_helpers.seed_group("Reading")

    with PostgresLogicalBookmarkUnitOfWorkFactory(runtime.get_engine)() as uow:
        uow.groups.set_group_domains(group["id"], [" Example.COM. ", "example.com"])
        uow.commit()

    assert bookmark_helpers.group_by_name("Reading")["domains"] == ["example.com"]
    saved = bookmark_helpers.seed_url("https://example.com/article")
    assert url_group_ids(saved["id"]) == [group["id"]]


@pytest.mark.parametrize(
    "domains",
    [["https://example.com"], ["example.com/path"], ["bad domain"], [42]],
)
def test_post_group_rejects_invalid_domains(app, domains):
    base_url, _ = app

    status, payload = http_json(
        base_url,
        "/api/groups",
        method="POST",
        payload={"name": "Reading", "domains": domains},
    )

    assert status == 400
    assert payload == {"error": "Enter valid domains."}


def test_post_url_adds_exact_domain_to_every_matching_group(app):
    base_url, _ = app
    reading = bookmark_helpers.seed_group("Reading", domains=["example.com"])
    work = bookmark_helpers.seed_group("Work", domains=["example.com"])

    status, payload = http_json(
        base_url,
        "/api/urls",
        method="POST",
        payload={"url": "HTTPS://EXAMPLE.COM:8443/article"},
    )

    assert status == 201
    url_id = payload["url"]["id"]
    assert grouped_url_ids_in(payload, "default") == []
    assert grouped_url_ids_in(payload, "Reading") == [url_id]
    assert grouped_url_ids_in(payload, "Work") == [url_id]
    assert url_group_ids(url_id) == [reading["id"], work["id"]]


def test_post_url_uses_default_when_only_a_subdomain_differs(app):
    base_url, _ = app
    bookmark_helpers.seed_group("Reading", domains=["example.com"])

    status, payload = http_json(
        base_url,
        "/api/urls",
        method="POST",
        payload={"url": "https://www.example.com/article"},
    )

    assert status == 201
    assert grouped_url_ids_in(payload, "default") == [payload["url"]["id"]]
    assert grouped_url_ids_in(payload, "Reading") == []


@pytest.mark.parametrize(
    "url",
    [
        "https://exam_ple.com/x",
        "https://-bad.example/x",
        "https://a..b/x",
    ],
)
def test_post_url_uses_default_for_hostname_not_allowed_as_group_domain(app, url):
    base_url, _ = app
    bookmark_helpers.seed_group("Reading", domains=["example.com"])

    status, payload = http_json(
        base_url,
        "/api/urls",
        method="POST",
        payload={"url": url},
    )

    assert status == 201
    assert grouped_url_ids_in(payload, "default") == [payload["url"]["id"]]
    assert grouped_url_ids_in(payload, "Reading") == []


def test_post_url_matches_ipv6_group_domain(app):
    base_url, _ = app
    ipv6 = bookmark_helpers.seed_group("Local", domains=["::1"])

    status, payload = http_json(
        base_url,
        "/api/urls",
        method="POST",
        payload={"url": "http://[::1]/x"},
    )

    assert status == 201
    assert grouped_url_ids_in(payload, "default") == []
    assert grouped_url_ids_in(payload, "Local") == [payload["url"]["id"]]
    assert url_group_ids(payload["url"]["id"]) == [ipv6["id"]]


@pytest.mark.parametrize("nsfw", [1, "true", None, []])
def test_post_group_rejects_non_boolean_nsfw(app, nsfw):
    base_url, _ = app

    status, response = http_json(
        base_url,
        "/api/groups",
        method="POST",
        payload={"name": "Comics", "nsfw": nsfw},
    )

    assert status == 400
    assert response == {"error": "Enter a valid value."}
    assert group_names_in({"groups": bookmark_helpers.group_payloads()}) == ["default"]


def test_post_group_trims_name_before_storage(app):
    base_url, _ = app

    status, payload = http_json(
        base_url, "/api/groups", method="POST", payload={"name": "  Reading  "}
    )

    assert status == 201
    assert payload["group"]["name"] == "Reading"
    assert bookmark_helpers.group_payloads()[1]["name"] == "Reading"


@pytest.mark.parametrize("payload", [{"name": ""}, {"name": "   "}, {}, {"name": 42}])
def test_post_group_rejects_empty_name(app, payload):
    base_url, _ = app

    status, response = http_json(
        base_url, "/api/groups", method="POST", payload=payload
    )

    assert_validation_error(status, response)
    assert [group["name"] for group in bookmark_helpers.group_payloads()] == ["default"]


def test_post_duplicate_group_returns_conflict(app):
    base_url, _ = app
    http_json(base_url, "/api/groups", method="POST", payload={"name": "Reading"})

    status, payload = http_json(
        base_url, "/api/groups", method="POST", payload={"name": "Reading"}
    )

    assert status == 409
    assert payload == {"error": "This group already exists."}
    assert [group["name"] for group in bookmark_helpers.group_payloads()] == [
        "default",
        "Reading",
    ]


def test_post_group_rejects_case_insensitive_duplicate(app):
    base_url, _ = app
    http_json(base_url, "/api/groups", method="POST", payload={"name": "Reading"})

    status, payload = http_json(
        base_url, "/api/groups", method="POST", payload={"name": "reading"}
    )

    assert status == 409
    assert payload == {"error": "This group already exists."}
    assert [group["name"] for group in bookmark_helpers.group_payloads()] == [
        "default",
        "Reading",
    ]


def test_post_group_rejects_default_name_variant(app):
    base_url, _ = app

    status, payload = http_json(
        base_url, "/api/groups", method="POST", payload={"name": "Default"}
    )

    assert status == 409
    assert payload == {"error": "This group already exists."}
    assert [group["name"] for group in bookmark_helpers.group_payloads()] == ["default"]


def test_patch_group_updates_name_and_nsfw(app, bookmarks_service):
    base_url, _ = app
    reading = bookmark_helpers.seed_group("Reading", nsfw=True)
    url = bookmark_helpers.seed_url("https://one.example")
    run_async(lambda: bookmarks_service.move_url(MoveURL(url["id"], reading["id"])))

    status, payload = http_json(
        base_url,
        f"/api/groups/{reading['id']}",
        method="PATCH",
        payload={
            "name": "  Later  ",
            "nsfw": False,
            "domains": [" Example.COM. "],
        },
    )

    assert status == 200
    assert EditGroupResponse.model_validate(payload).model_dump() == payload
    assert payload["group"]["name"] == "Later"
    assert payload["group"]["nsfw"] is False
    assert payload["group"]["domains"] == ["example.com"]
    assert grouped_url_ids_in(payload, "Later") == [url["id"]]
    assert bookmark_helpers.group_payloads()[1]["name"] == "Later"
    assert bookmark_helpers.group_payloads()[1]["domains"] == ["example.com"]


def test_patch_group_allows_case_only_rename(app):
    base_url, _ = app
    reading = bookmark_helpers.seed_group("Reading")

    status, payload = http_json(
        base_url,
        f"/api/groups/{reading['id']}",
        method="PATCH",
        payload={"name": "reading"},
    )

    assert status == 200
    assert payload["group"]["name"] == "reading"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({}, "Enter a valid value."),
        ({"name": ""}, "Enter a group name."),
        ({"name": "   "}, "Enter a group name."),
        ({"name": 42}, "Enter a group name."),
        ({"nsfw": 1}, "Enter a valid value."),
        ({"nsfw": "false"}, "Enter a valid value."),
    ],
)
def test_patch_group_rejects_invalid_payload(app, payload, message):
    base_url, _ = app
    reading = bookmark_helpers.seed_group("Reading")

    status, response = http_json(
        base_url,
        f"/api/groups/{reading['id']}",
        method="PATCH",
        payload=payload,
    )

    assert status == 400
    assert response == {"error": message}
    assert bookmark_helpers.group_payloads()[1]["name"] == "Reading"


def test_patch_group_rejects_case_insensitive_duplicate(app):
    base_url, _ = app
    reading = bookmark_helpers.seed_group("Reading")
    bookmark_helpers.seed_group("Work")

    status, payload = http_json(
        base_url,
        f"/api/groups/{reading['id']}",
        method="PATCH",
        payload={"name": "work"},
    )

    assert status == 409
    assert payload == {"error": "This group already exists."}


def test_patch_group_rejects_default_group(app):
    base_url, _ = app

    status, payload = http_json(
        base_url,
        "/api/groups/1",
        method="PATCH",
        payload={"name": "Home", "nsfw": True},
    )

    assert status == 400
    assert payload == {"error": "The default group cannot be edited."}
    assert bookmark_helpers.group_payloads()[0]["name"] == "default"


def test_patch_group_returns_not_found(app):
    base_url, _ = app

    status, payload = http_json(
        base_url,
        "/api/groups/999",
        method="PATCH",
        payload={"name": "Missing"},
    )

    assert status == 404
    assert payload == {"error": "This group does not exist."}


def test_delete_group_and_its_urls_compacts_positions(app, bookmarks_service):
    base_url, _ = app
    reading = bookmark_helpers.seed_group("Reading")
    work = bookmark_helpers.seed_group("Work")
    url = bookmark_helpers.seed_url("https://one.example")
    run_async(lambda: bookmarks_service.move_url(MoveURL(url["id"], reading["id"])))

    status, payload = http_json(
        base_url,
        f"/api/groups/{reading['id']}",
        method="DELETE",
        payload={"url_action": "delete"},
    )

    assert status == 200
    assert DeleteGroupResponse.model_validate(payload).model_dump() == payload
    assert payload["group_id"] == reading["id"]
    assert payload["url_action"] == "delete"
    assert payload["deleted"] == 1
    assert payload["moved"] == 0
    assert group_names_in(payload) == ["default", "Work"]
    assert group_positions_in(payload) == [0, 1]
    assert saved_urls() == []
    assert work["id"] == payload["groups"][1]["id"]


def test_delete_group_moves_urls_to_default(app, bookmarks_service):
    base_url, _ = app
    reading = bookmark_helpers.seed_group("Reading")
    first = bookmark_helpers.seed_url("https://one.example")
    second = bookmark_helpers.seed_url("https://two.example")
    run_async(lambda: bookmarks_service.move_url(MoveURL(first["id"], reading["id"])))
    run_async(lambda: bookmarks_service.move_url(MoveURL(second["id"], reading["id"])))

    status, payload = http_json(
        base_url,
        f"/api/groups/{reading['id']}",
        method="DELETE",
        payload={"url_action": "move_to_default"},
    )

    assert status == 200
    assert payload["moved"] == 2
    assert payload["deleted"] == 0
    assert group_names_in(payload) == ["default"]
    assert grouped_url_ids_in(payload, "default") == [first["id"], second["id"]]
    assert saved_urls() == ["https://one.example", "https://two.example"]


def test_delete_group_counts_only_urls_actually_deleted(app):
    base_url, _ = app
    first = bookmark_helpers.seed_group("First", domains=["example.com"])
    second = bookmark_helpers.seed_group("Second", domains=["example.com"])
    url = bookmark_helpers.seed_url("https://example.com/x")

    status, payload = http_json(
        base_url,
        f"/api/groups/{first['id']}",
        method="DELETE",
        payload={"url_action": "delete"},
    )

    assert status == 200
    assert payload["deleted"] == 0
    assert grouped_url_ids_in(payload, "Second") == [url["id"]]
    assert url_group_ids(url["id"]) == [second["id"]]
    assert url_payload(url["id"]) == url


def test_delete_group_counts_only_new_default_memberships_as_moved(
    app, bookmarks_service
):
    base_url, _ = app
    first = bookmark_helpers.seed_group("First", domains=["example.com"])
    second = bookmark_helpers.seed_group("Second", domains=["example.com"])
    url = bookmark_helpers.seed_url("https://example.com/x")
    run_async(lambda: bookmarks_service.move_url(MoveURL(url["id"], 1, second["id"])))

    status, payload = http_json(
        base_url,
        f"/api/groups/{first['id']}",
        method="DELETE",
        payload={"url_action": "move_to_default"},
    )

    assert status == 200
    assert payload["moved"] == 0
    assert grouped_url_ids_in(payload, "default") == [url["id"]]
    assert url_group_ids(url["id"]) == [1]


def test_delete_group_moves_all_memberships_with_one_insert(
    app, monkeypatch, bookmarks_service
):
    reading = bookmark_helpers.seed_group("Reading")
    urls = [bookmark_helpers.seed_url(f"https://{index}.example") for index in range(5)]
    for url in urls:
        run_async(lambda: bookmarks_service.move_url(MoveURL(url["id"], reading["id"])))
    statements, engine = capture_storage_statements(monkeypatch)

    outcome = run_async(
        lambda: bookmarks_service.delete_group(
            DeleteGroup(reading["id"], "move_to_default")
        )
    )
    engine.dispose()

    membership_inserts = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("INSERT")
        and "URL_GROUPS" in statement.upper()
    ]
    assert isinstance(outcome, GroupDeleted)
    result = asdict(outcome)
    assert result["moved"] == 5
    assert len(membership_inserts) == 1


def test_delete_group_deletes_all_orphans_with_set_based_queries(
    app, monkeypatch, bookmarks_service
):
    reading = bookmark_helpers.seed_group("Reading")
    urls = [bookmark_helpers.seed_url(f"https://{index}.example") for index in range(5)]
    for url in urls:
        run_async(lambda: bookmarks_service.move_url(MoveURL(url["id"], reading["id"])))
    statements, engine = capture_storage_statements(monkeypatch)

    outcome = run_async(
        lambda: bookmarks_service.delete_group(DeleteGroup(reading["id"], "delete"))
    )
    engine.dispose()

    membership_selects = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("SELECT")
        and "URL_GROUPS" in statement.upper()
    ]
    url_deletes = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("DELETE FROM URLS")
    ]
    assert isinstance(outcome, GroupDeleted)
    result = asdict(outcome)
    assert result["deleted"] == 5
    assert len(membership_selects) == 0
    assert len(url_deletes) == 1


@pytest.mark.parametrize("url_action", ["delete", "move_to_default"])
def test_delete_empty_group_reports_zero_urls(app, url_action):
    base_url, _ = app
    reading = bookmark_helpers.seed_group("Reading")

    status, payload = http_json(
        base_url,
        f"/api/groups/{reading['id']}",
        method="DELETE",
        payload={"url_action": url_action},
    )

    assert status == 200
    assert payload["moved"] == 0
    assert payload["deleted"] == 0


@pytest.mark.parametrize("payload", [{}, {"url_action": "archive"}, {"url_action": 1}])
def test_delete_group_rejects_invalid_url_action(app, payload):
    base_url, _ = app
    reading = bookmark_helpers.seed_group("Reading")

    status, response = http_json(
        base_url,
        f"/api/groups/{reading['id']}",
        method="DELETE",
        payload=payload,
    )

    assert status == 400
    assert response == {"error": "Choose how to handle this group's URLs."}
    assert group_names_in({"groups": bookmark_helpers.group_payloads()}) == [
        "default",
        "Reading",
    ]


def test_delete_group_rejects_default_group(app):
    base_url, _ = app

    status, payload = http_json(
        base_url,
        "/api/groups/1",
        method="DELETE",
        payload={"url_action": "delete"},
    )

    assert status == 400
    assert payload == {"error": "The default group cannot be deleted."}


def test_delete_group_returns_not_found(app):
    base_url, _ = app

    status, payload = http_json(
        base_url,
        "/api/groups/999",
        method="DELETE",
        payload={"url_action": "delete"},
    )

    assert status == 404
    assert payload == {"error": "This group does not exist."}


def test_delete_group_rolls_back_move_when_group_delete_fails(app, bookmarks_service):
    _, _ = app
    reading = bookmark_helpers.seed_group("Reading")
    url = bookmark_helpers.seed_url("https://one.example")
    run_async(lambda: bookmarks_service.move_url(MoveURL(url["id"], reading["id"])))
    # The session shares one database across tests and TRUNCATE does not drop
    # triggers, so this one has to be removed however the assertions go.
    _create_failing_group_delete_trigger()
    try:
        with pytest.raises(DBAPIError):
            run_async(
                lambda: bookmarks_service.delete_group(
                    DeleteGroup(reading["id"], "move_to_default")
                )
            )

        assert group_names_in({"groups": bookmark_helpers.group_payloads()}) == [
            "default",
            "Reading",
        ]
        assert grouped_url_ids_in(
            {"groups": bookmark_helpers.group_payloads()}, "Reading"
        ) == [url["id"]]
    finally:
        _drop_failing_group_delete_trigger()


def _create_failing_group_delete_trigger():
    with db_connection() as connection:
        connection.execute(
            text(
                """
                CREATE FUNCTION fail_group_delete() RETURNS trigger AS $$
                BEGIN
                    RAISE EXCEPTION 'forced delete failure';
                END;
                $$ LANGUAGE plpgsql
                """
            )
        )
        connection.execute(
            text(
                "CREATE TRIGGER fail_group_delete BEFORE DELETE ON groups "
                "FOR EACH ROW EXECUTE FUNCTION fail_group_delete()"
            )
        )


def _drop_failing_group_delete_trigger():
    with db_connection() as connection:
        connection.execute(text("DROP TRIGGER IF EXISTS fail_group_delete ON groups"))
        connection.execute(text("DROP FUNCTION IF EXISTS fail_group_delete()"))


def test_url_service_move_returns_resolved_source(app, bookmarks_service):
    url = bookmark_helpers.seed_url("https://one.example")
    group = bookmark_helpers.seed_group("Reading")
    source_group_id = url_group_ids(url["id"])[0]

    outcome = run_async(
        lambda: bookmarks_service.move_url(MoveURL(url["id"], group["id"]))
    )

    assert isinstance(outcome, URLMoved)
    result = {"url": asdict(outcome.record), "source_group_id": outcome.source_group_id}
    assert result == {
        "url": url,
        "source_group_id": source_group_id,
    }


def test_patch_url_group_moves_url_to_group(app):
    base_url, _ = app
    url = bookmark_helpers.seed_url("https://one.example")
    group = bookmark_helpers.seed_group("Reading")
    source_group_id = url_group_ids(url["id"])[0]

    status, payload = http_json(
        base_url,
        f"/api/urls/{url['id']}/group",
        method="PATCH",
        payload={"group_id": group["id"]},
    )

    assert status == 200
    assert payload["url"] == url
    assert payload["group_id"] == group["id"]
    assert payload["source_group_id"] == source_group_id
    assert grouped_url_ids_in(payload, "default") == []
    assert grouped_url_ids_in(payload, "Reading") == [url["id"]]
    assert grouped_urls_in(payload, "Reading") == ["https://one.example"]
    assert grouped_url_ids_in(
        {"groups": bookmark_helpers.group_payloads()}, "Reading"
    ) == [url["id"]]


def test_patch_url_group_moves_only_the_source_membership(app):
    base_url, _ = app
    source = bookmark_helpers.seed_group("Source", domains=["example.com"])
    bookmark_helpers.seed_group("Existing", domains=["example.com"])
    target = bookmark_helpers.seed_group("Target")
    url = bookmark_helpers.seed_url("https://example.com/article")

    status, payload = http_json(
        base_url,
        f"/api/urls/{url['id']}/group",
        method="PATCH",
        payload={
            "source_group_id": source["id"],
            "group_id": target["id"],
        },
    )

    assert status == 200
    assert payload["source_group_id"] == source["id"]
    assert grouped_url_ids_in(payload, "Source") == []
    assert grouped_url_ids_in(payload, "Existing") == [url["id"]]
    assert grouped_url_ids_in(payload, "Target") == [url["id"]]


def test_patch_url_group_removes_source_when_target_membership_exists(app):
    base_url, _ = app
    source = bookmark_helpers.seed_group("Source", domains=["example.com"])
    target = bookmark_helpers.seed_group("Target", domains=["example.com"])
    url = bookmark_helpers.seed_url("https://example.com/article")

    status, payload = http_json(
        base_url,
        f"/api/urls/{url['id']}/group",
        method="PATCH",
        payload={
            "source_group_id": source["id"],
            "group_id": target["id"],
        },
    )

    assert status == 200
    assert grouped_url_ids_in(payload, "Source") == []
    assert grouped_url_ids_in(payload, "Target") == [url["id"]]
    assert url_group_ids(url["id"]) == [target["id"]]


def test_patch_url_group_requires_source_for_multiple_memberships(app):
    base_url, _ = app
    first = bookmark_helpers.seed_group("First", domains=["example.com"])
    second = bookmark_helpers.seed_group("Second", domains=["example.com"])
    target = bookmark_helpers.seed_group("Target")
    url = bookmark_helpers.seed_url("https://example.com/article")

    status, payload = http_json(
        base_url,
        f"/api/urls/{url['id']}/group",
        method="PATCH",
        payload={"group_id": target["id"]},
    )

    assert status == 400
    assert payload == {"error": "Choose the URL's source group."}
    assert url_group_ids(url["id"]) == [first["id"], second["id"]]


def test_delete_url_by_id_removes_only_requested_group_membership(app):
    base_url, _ = app
    first = bookmark_helpers.seed_group("First", domains=["example.com"])
    second = bookmark_helpers.seed_group("Second", domains=["example.com"])
    url = bookmark_helpers.seed_url("https://example.com/article")

    status, payload = http_json(
        base_url,
        f"/api/urls/{url['id']}?group_id={first['id']}",
        method="DELETE",
    )

    assert status == 200
    assert grouped_url_ids_in(payload, "First") == []
    assert grouped_url_ids_in(payload, "Second") == [url["id"]]
    assert url_payload(url["id"]) == url

    status, payload = http_json(
        base_url,
        f"/api/urls/{url['id']}?group_id={second['id']}",
        method="DELETE",
    )

    assert status == 200
    assert url_payload(url["id"]) is None


def test_delete_url_by_id_requires_group_id(app):
    base_url, _ = app
    first = bookmark_helpers.seed_group("First", domains=["example.com"])
    second = bookmark_helpers.seed_group("Second", domains=["example.com"])
    url = bookmark_helpers.seed_url("https://example.com/article")

    status, payload = http_json(
        base_url,
        f"/api/urls/{url['id']}",
        method="DELETE",
    )

    assert_validation_error(status, payload)
    assert url_group_ids(url["id"]) == [first["id"], second["id"]]


def test_patch_url_group_returns_not_found_for_missing_url(app):
    base_url, _ = app
    group = bookmark_helpers.seed_group("Reading")

    status, payload = http_json(
        base_url,
        "/api/urls/999/group",
        method="PATCH",
        payload={"group_id": group["id"]},
    )

    assert status == 404
    assert payload == {"error": "This URL is not saved."}


def test_patch_url_group_returns_not_found_for_missing_group(app):
    base_url, _ = app
    url = bookmark_helpers.seed_url("https://one.example")

    status, payload = http_json(
        base_url,
        f"/api/urls/{url['id']}/group",
        method="PATCH",
        payload={"group_id": 999},
    )

    assert status == 404
    assert payload == {"error": "This group does not exist."}
    assert grouped_url_ids_in(
        {"groups": bookmark_helpers.group_payloads()}, "default"
    ) == [url["id"]]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"group_id": ""},
        {"group_id": "2"},
        {"group_id": 0},
        {"group_id": -1},
        {"group_id": 1.5},
        {"group_id": True},
    ],
)
def test_patch_url_group_rejects_invalid_group_id_payload(app, payload):
    base_url, _ = app
    url = bookmark_helpers.seed_url("https://one.example")

    status, response = http_json(
        base_url,
        f"/api/urls/{url['id']}/group",
        method="PATCH",
        payload=payload,
    )

    assert_validation_error(status, response)
    assert grouped_url_ids_in(
        {"groups": bookmark_helpers.group_payloads()}, "default"
    ) == [url["id"]]


def test_url_uniqueness_remains_global_after_move(app):
    base_url, _ = app
    url = bookmark_helpers.seed_url("https://one.example")
    group = bookmark_helpers.seed_group("Reading")
    http_json(
        base_url,
        f"/api/urls/{url['id']}/group",
        method="PATCH",
        payload={"group_id": group["id"]},
    )

    status, payload = http_json(
        base_url, "/api/urls", method="POST", payload={"url": "one.example"}
    )

    assert status == 409
    assert payload == {"error": "This URL is already saved."}
    assert grouped_url_ids_in(
        {"groups": bookmark_helpers.group_payloads()}, "Reading"
    ) == [url["id"]]


def test_patch_group_order_reorders_groups(app, bookmarks_service):
    base_url, _ = app
    url = bookmark_helpers.seed_url("https://one.example")
    default_group = bookmark_helpers.group_payloads()[0]
    reading = bookmark_helpers.seed_group("Reading")
    work = bookmark_helpers.seed_group("Work")
    run_async(lambda: bookmarks_service.move_url(MoveURL(url["id"], reading["id"])))

    status, payload = http_json(
        base_url,
        "/api/groups/order",
        method="PATCH",
        payload={
            "parent_id": None,
            "group_ids": [work["id"], default_group["id"], reading["id"]],
        },
    )

    assert status == 200
    assert group_names_in(payload) == ["Work", "default", "Reading"]
    assert group_positions_in(payload) == [0, 1, 2]
    assert grouped_url_ids_in(payload, "Reading") == [url["id"]]

    stored_groups = {"groups": bookmark_helpers.group_payloads()}
    assert group_names_in(stored_groups) == ["Work", "default", "Reading"]
    assert group_positions_in(stored_groups) == [0, 1, 2]

    runtime.run_migrations()
    reloaded_groups = {"groups": bookmark_helpers.group_payloads()}
    assert group_names_in(reloaded_groups) == ["Work", "default", "Reading"]
    assert group_positions_in(reloaded_groups) == [0, 1, 2]


@pytest.mark.parametrize(
    "group_ids",
    [
        [1, 2],
        [1, 2, 2],
        [1, 2, 3, 999],
    ],
)
def test_patch_group_order_rejects_incomplete_duplicate_or_unknown_ids(app, group_ids):
    base_url, _ = app
    bookmark_helpers.seed_group("Reading")
    bookmark_helpers.seed_group("Work")

    status, payload = http_json(
        base_url,
        "/api/groups/order",
        method="PATCH",
        payload={"parent_id": None, "group_ids": group_ids},
    )

    assert status == 400
    assert payload == {"error": "Invalid group order."}
    stored_groups = {"groups": bookmark_helpers.group_payloads()}
    assert group_names_in(stored_groups) == ["default", "Reading", "Work"]
    assert group_positions_in(stored_groups) == [0, 1, 2]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        # parent_id is required: a bare list does not say which sibling set it
        # means.
        {"group_ids": [1, 2]},
        {"parent_id": None, "group_ids": ""},
        {"parent_id": None, "group_ids": 1},
        {"parent_id": None, "group_ids": [1, "2"]},
        {"parent_id": None, "group_ids": [1, 1.5]},
        {"parent_id": None, "group_ids": [1, True]},
        {"parent_id": None, "group_ids": [1, 0]},
        {"parent_id": None, "group_ids": [1, -1]},
        {"parent_id": 0, "group_ids": [1, 2]},
        {"parent_id": "1", "group_ids": [1, 2]},
    ],
)
def test_patch_group_order_rejects_invalid_payload(app, payload):
    base_url, _ = app
    bookmark_helpers.seed_group("Reading")

    status, response = http_json(
        base_url,
        "/api/groups/order",
        method="PATCH",
        payload=payload,
    )

    assert_validation_error(status, response)
    stored_groups = {"groups": bookmark_helpers.group_payloads()}
    assert group_names_in(stored_groups) == ["default", "Reading"]
    assert group_positions_in(stored_groups) == [0, 1]
