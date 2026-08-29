"""Tests for the fresh Trellmark PostgreSQL baseline."""

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

import trellmark
from tests.helpers import (
    CURRENT_HEAD,
    db_connection,
    db_query,
    table_columns,
    table_indexes,
)


def test_run_migrations_creates_schema_and_exactly_one_default_group(empty_database):
    trellmark.run_migrations()

    assert db_query('SELECT name, position, nsfw FROM "groups"') == [
        ("default", 0, False)
    ]
    assert db_query("SELECT version_num FROM alembic_version ORDER BY version_num") == [
        (CURRENT_HEAD,)
    ]
    assert "group_id" not in table_columns("urls")
    assert table_columns("url_groups") == ["url_id", "group_id"]
    assert table_columns("group_domains") == ["group_id", "domain"]
    assert table_indexes("group_domains")["ix_group_domains_domain"] == ["domain"]
    assert table_indexes("url_groups")["ix_url_groups_group_id"] == ["group_id"]


def test_baseline_seeds_a_disabled_admin_credential(empty_database):
    trellmark.run_migrations()

    assert db_query(
        "SELECT u.username, u.role, u.status, p.password_hash "
        "FROM users AS u JOIN password_credentials AS p ON p.user_id = u.id"
    ) == [("admin", "admin", "active", "!")]


def test_authentication_schema_has_bounded_lookup_and_expiry_indexes(empty_database):
    trellmark.run_migrations()

    assert {
        "users",
        "password_credentials",
        "web_sessions",
        "auth_login_throttle",
    }.issubset(
        {
            table
            for (table,) in db_query(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )
        }
    )
    assert table_indexes("users")["uq_users_username_lower"] == [None]
    session_indexes = table_indexes("web_sessions")
    assert session_indexes["ix_web_sessions_idle_expires_at"] == [
        "idle_expires_at",
        "id",
    ]
    assert session_indexes["ix_web_sessions_absolute_expires_at"] == [
        "absolute_expires_at",
        "id",
    ]
    assert table_indexes("auth_login_throttle")[
        "ix_auth_login_throttle_updated_at"
    ] == ["updated_at"]


def test_baseline_creates_origin_keyed_site_icon_cache(empty_database):
    trellmark.run_migrations()

    assert table_columns("site_icon_cache") == [
        "origin",
        "icon_bytes",
        "media_type",
        "fetched_at",
        "retry_after",
    ]
    assert db_query(
        "SELECT constraint_type FROM information_schema.table_constraints "
        "WHERE table_schema = 'public' AND table_name = 'site_icon_cache' "
        "AND constraint_type = 'PRIMARY KEY'"
    ) == [("PRIMARY KEY",)]


def test_site_icon_cache_rejects_partial_or_oversized_positive_rows(app):
    with pytest.raises(IntegrityError):
        with db_connection() as connection:
            connection.execute(
                text(
                    "INSERT INTO site_icon_cache "
                    "(origin, icon_bytes, media_type, fetched_at, retry_after) "
                    "VALUES ('https://partial.example', :bytes, NULL, now(), now())"
                ),
                {"bytes": b"icon"},
            )

    with pytest.raises(IntegrityError):
        with db_connection() as connection:
            connection.execute(
                text(
                    "INSERT INTO site_icon_cache "
                    "(origin, icon_bytes, media_type, fetched_at, retry_after) "
                    "VALUES ('https://large.example', :bytes, 'image/png', "
                    "now(), now())"
                ),
                {"bytes": b"x" * (256 * 1024 + 1)},
            )


def test_migrated_columns_use_native_postgresql_types(empty_database):
    trellmark.run_migrations()

    assert _column_types("urls") == {
        "id": ("integer", "NO"),
        "url": ("text", "NO"),
        "title": ("text", "YES"),
        "created_at": ("timestamp with time zone", "NO"),
        "important": ("boolean", "NO"),
        "version": ("integer", "NO"),
    }
    assert _column_types("groups") == {
        "id": ("integer", "NO"),
        "name": ("text", "NO"),
        "position": ("integer", "NO"),
        "created_at": ("timestamp with time zone", "NO"),
        "nsfw": ("boolean", "NO"),
        "parent_id": ("integer", "YES"),
        "path": ("USER-DEFINED", "NO"),
    }
    assert _column_defaults("urls") == {"important": "false", "version": "1"}
    assert _column_defaults("groups") == {"nsfw": "false"}


def _column_types(table):
    return {
        name: (data_type, is_nullable)
        for name, data_type, is_nullable in db_query(
            "SELECT column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_name = :table AND table_schema = 'public'",
            table=table,
        )
    }


def _column_defaults(table):
    """Return literal column defaults, ignoring now() and identity sequences."""
    return {
        name: default
        for name, default in db_query(
            "SELECT column_name, column_default "
            "FROM information_schema.columns "
            "WHERE table_name = :table AND table_schema = 'public' "
            "AND column_default IS NOT NULL",
            table=table,
        )
        if "now()" not in default and "nextval" not in default
    }


def test_ids_are_generated_by_identity_columns(empty_database):
    trellmark.run_migrations()

    assert db_query(
        "SELECT is_identity FROM information_schema.columns "
        "WHERE table_name = 'urls' AND column_name = 'id'"
    ) == [("YES",)]
    assert db_query(
        "SELECT is_identity FROM information_schema.columns "
        "WHERE table_name = 'groups' AND column_name = 'id'"
    ) == [("YES",)]


def test_run_migrations_can_run_more_than_once(empty_database):
    trellmark.run_migrations()
    trellmark.add_url("https://one.example")
    trellmark.run_migrations()

    assert db_query('SELECT COUNT(*) FROM "groups"') == [(1,)]
    assert db_query("SELECT COUNT(*) FROM alembic_version") == [(1,)]
    assert trellmark.read_urls() == ["https://one.example"]


def test_deleting_a_group_cascades_to_memberships_and_domains(app):
    group = trellmark.add_group("Reading", domains=["example.com"])
    saved = trellmark.add_url("https://example.com/x")

    with db_connection() as connection:
        connection.execute(
            text("DELETE FROM groups WHERE id = :id"), {"id": group["id"]}
        )

    assert db_query(
        "SELECT COUNT(*) FROM url_groups WHERE group_id = :id", id=group["id"]
    ) == [(0,)]
    assert db_query(
        "SELECT COUNT(*) FROM group_domains WHERE group_id = :id", id=group["id"]
    ) == [(0,)]
    # The URL itself survives; only its membership in that group went away.
    assert trellmark.read_url_record_by_id(saved["id"]) == saved


def test_deleting_a_url_cascades_to_its_memberships(app):
    saved = trellmark.add_url("https://one.example")

    with db_connection() as connection:
        connection.execute(text("DELETE FROM urls WHERE id = :id"), {"id": saved["id"]})

    assert db_query(
        "SELECT COUNT(*) FROM url_groups WHERE url_id = :id", id=saved["id"]
    ) == [(0,)]


def test_url_uniqueness_is_enforced_by_the_database(app):
    trellmark.add_url("https://one.example")

    with pytest.raises(IntegrityError):
        with db_connection() as connection:
            connection.execute(
                text("INSERT INTO urls (url) VALUES ('https://one.example')")
            )


def test_group_name_uniqueness_is_case_insensitive_in_the_database(app):
    trellmark.add_group("Reading")

    with pytest.raises(IntegrityError):
        with db_connection() as connection:
            connection.execute(
                text("INSERT INTO \"groups\" (name, position) VALUES ('rEaDiNg', 99)")
            )


def test_group_position_uniqueness_is_enforced_by_the_database(app):
    with pytest.raises(IntegrityError):
        with db_connection() as connection:
            connection.execute(
                text("INSERT INTO \"groups\" (name, position) VALUES ('Reading', 0)")
            )


def test_migrations_have_exactly_one_head():
    heads = ScriptDirectory.from_config(trellmark.alembic_config()).get_heads()

    assert list(heads) == [CURRENT_HEAD]


def test_baseline_installs_the_ltree_extension(empty_database):
    trellmark.run_migrations()

    assert db_query("SELECT extname FROM pg_extension WHERE extname = 'ltree'") == [
        ("ltree",)
    ]
    assert db_query(
        "SELECT udt_name, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'groups' AND column_name = 'path'"
    ) == [("ltree", "NO")]


def test_group_path_has_a_gist_index_with_the_ltree_operator_class(empty_database):
    trellmark.run_migrations()

    # The operator class is the point: a GiST index over some other class, or a
    # plain text column, would not make `<@` and `@>` index-usable.
    assert db_query(
        "SELECT access_method.amname, operator_class.opcname "
        "FROM pg_index AS index "
        "JOIN pg_class AS index_relation ON index_relation.oid = index.indexrelid "
        "JOIN pg_am AS access_method ON access_method.oid = index_relation.relam "
        "JOIN pg_opclass AS operator_class "
        "ON operator_class.oid = index.indclass[0] "
        "WHERE index_relation.relname = 'ix_groups_path_gist'"
    ) == [("gist", "gist_ltree_ops")]


def test_group_hierarchy_constraints_and_indexes_exist(empty_database):
    trellmark.run_migrations()

    assert db_query(
        "SELECT conname FROM pg_constraint "
        "WHERE conrelid = 'groups'::regclass AND contype = 'c' "
        "ORDER BY conname"
    ) == [("ck_groups_path_depth",)]
    assert "uq_groups_position" not in table_indexes("groups")
    assert table_indexes("groups")["ix_groups_parent_id_position"] == [
        "parent_id",
        "position",
    ]
    assert _index_definitions() == {
        "uq_groups_root_position": '("position") WHERE (parent_id IS NULL)',
        "uq_groups_child_position": (
            '(parent_id, "position") WHERE (parent_id IS NOT NULL)'
        ),
    }
    assert db_query(
        "SELECT confdeltype FROM pg_constraint "
        "WHERE conrelid = 'groups'::regclass AND contype = 'f'"
    ) == [("r",)]  # ON DELETE RESTRICT


def _index_definitions():
    """The predicate and columns of each sibling-position unique index."""
    return {
        name: definition.split(" USING btree ")[1]
        for name, definition in db_query(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE tablename = 'groups' AND indexname IN "
            "('uq_groups_root_position', 'uq_groups_child_position')"
        )
    }


def test_default_group_gets_a_root_path(app):
    assert db_query('SELECT name, parent_id, path::text FROM "groups"') == [
        ("default", None, "g_1")
    ]


def _insert_group(name, parent_id=None, position=0, path=None):
    """Insert a group through raw SQL, bypassing every storage guard."""
    with db_connection() as connection:
        return connection.execute(
            text(
                'INSERT INTO "groups" (name, position, parent_id, path) '
                "VALUES (:name, :position, :parent_id, CAST(:path AS ltree)) "
                "RETURNING id, path::text"
            ),
            {
                "name": name,
                "position": position,
                "parent_id": parent_id,
                "path": path,
            },
        ).one()


def _reparent(group_id, parent_id, position=0):
    with db_connection() as connection:
        connection.execute(
            text(
                'UPDATE "groups" SET parent_id = :parent_id, position = :position '
                "WHERE id = :id"
            ),
            {"id": group_id, "parent_id": parent_id, "position": position},
        )


def _sqlstate(error):
    return getattr(error.value.orig, "sqlstate", None)


def test_database_builds_paths_from_ids_without_a_supplied_path(app):
    root_id, root_path = _insert_group("Root", position=1)
    child_id, child_path = _insert_group("Child", parent_id=root_id)
    _, grandchild_path = _insert_group("Grandchild", parent_id=child_id)

    assert root_path == f"g_{root_id}"
    assert child_path == f"g_{root_id}.g_{child_id}"
    assert grandchild_path.startswith(f"g_{root_id}.g_{child_id}.g_")


def test_database_replaces_a_supplied_path_that_disagrees_with_the_parent(app):
    root_id, _ = _insert_group("Root", position=1)

    child_id, child_path = _insert_group("Child", parent_id=root_id, path="g_999")

    assert child_path == f"g_{root_id}.g_{child_id}"


def test_database_rejects_a_fourth_level_on_insert(app):
    root_id, _ = _insert_group("Root", position=1)
    child_id, _ = _insert_group("Child", parent_id=root_id)
    grandchild_id, _ = _insert_group("Grandchild", parent_id=child_id)

    with pytest.raises(DBAPIError) as error:
        _insert_group("TooDeep", parent_id=grandchild_id)

    assert _sqlstate(error) == "GH003"
    assert db_query('SELECT COUNT(*) FROM "groups"') == [(4,)]


def test_database_rejects_a_move_whose_subtree_would_exceed_the_depth(app):
    root_id, _ = _insert_group("Root", position=1)
    child_id, _ = _insert_group("Child", parent_id=root_id)
    _insert_group("Grandchild", parent_id=child_id)
    other_root_id, _ = _insert_group("Other", position=2)
    other_child_id, _ = _insert_group("OtherChild", parent_id=other_root_id)

    # Child is only one level deep itself, but it carries a grandchild, so the
    # destination has to fit the whole subtree.
    with pytest.raises(DBAPIError) as error:
        _reparent(child_id, other_child_id)

    assert _sqlstate(error) == "GH003"
    assert _paths_by_name()["Child"] == f"g_{root_id}.g_{child_id}"


def test_database_rejects_a_missing_parent(app):
    with pytest.raises(DBAPIError) as error:
        _insert_group("Orphan", parent_id=999)

    assert _sqlstate(error) == "GH001"


def test_database_rejects_self_parenting(app):
    group_id, _ = _insert_group("Root", position=1)

    with pytest.raises(DBAPIError) as error:
        _reparent(group_id, group_id, position=1)

    assert _sqlstate(error) == "GH002"
    assert _paths_by_name()["Root"] == f"g_{group_id}"


def test_database_rejects_parenting_a_group_under_its_own_descendant(app):
    root_id, _ = _insert_group("Root", position=1)
    child_id, _ = _insert_group("Child", parent_id=root_id)
    grandchild_id, _ = _insert_group("Grandchild", parent_id=child_id)

    with pytest.raises(DBAPIError) as error:
        _reparent(root_id, grandchild_id, position=1)

    assert _sqlstate(error) == "GH002"
    assert _paths_by_name() == {
        "default": "g_1",
        "Root": f"g_{root_id}",
        "Child": f"g_{root_id}.g_{child_id}",
        "Grandchild": f"g_{root_id}.g_{child_id}.g_{grandchild_id}",
    }


def test_moving_a_group_rewrites_its_whole_subtree(app):
    root_id, _ = _insert_group("Root", position=1)
    child_id, _ = _insert_group("Child", parent_id=root_id)
    grandchild_id, _ = _insert_group("Grandchild", parent_id=child_id)
    other_root_id, _ = _insert_group("Other", position=2)

    _reparent(child_id, other_root_id)

    assert _paths_by_name() == {
        "default": "g_1",
        "Root": f"g_{root_id}",
        "Other": f"g_{other_root_id}",
        "Child": f"g_{other_root_id}.g_{child_id}",
        "Grandchild": f"g_{other_root_id}.g_{child_id}.g_{grandchild_id}",
    }


def test_moving_a_group_back_to_the_root_rewrites_its_subtree(app):
    root_id, _ = _insert_group("Root", position=1)
    child_id, _ = _insert_group("Child", parent_id=root_id)
    grandchild_id, _ = _insert_group("Grandchild", parent_id=child_id)

    with db_connection() as connection:
        connection.execute(
            text('UPDATE "groups" SET parent_id = NULL, position = 2 WHERE id = :id'),
            {"id": child_id},
        )

    assert _paths_by_name()["Child"] == f"g_{child_id}"
    assert _paths_by_name()["Grandchild"] == f"g_{child_id}.g_{grandchild_id}"


def test_renaming_a_group_leaves_every_path_alone(app):
    root_id, _ = _insert_group("Root", position=1)
    child_id, _ = _insert_group("Child", parent_id=root_id)

    with db_connection() as connection:
        connection.execute(
            text('UPDATE "groups" SET name = :name WHERE id = :id'),
            {"id": root_id, "name": "Renamed"},
        )

    assert _paths_by_name()["Renamed"] == f"g_{root_id}"
    assert _paths_by_name()["Child"] == f"g_{root_id}.g_{child_id}"


def _paths_by_name():
    return dict(db_query('SELECT name, path::text FROM "groups"'))


def test_sibling_positions_are_unique_per_parent_not_globally(app):
    first_root_id, _ = _insert_group("First", position=1)
    second_root_id, _ = _insert_group("Second", position=2)

    # Different parents may each hold a child at position 0.
    _insert_group("FirstChild", parent_id=first_root_id, position=0)
    _insert_group("SecondChild", parent_id=second_root_id, position=0)

    with pytest.raises(IntegrityError):
        _insert_group("Clashing", parent_id=first_root_id, position=0)


def test_root_positions_stay_unique(app):
    _insert_group("First", position=1)

    with pytest.raises(IntegrityError):
        _insert_group("Second", position=1)


def test_deleting_a_group_with_children_is_restricted(app):
    root_id, _ = _insert_group("Root", position=1)
    _insert_group("Child", parent_id=root_id)

    with pytest.raises(IntegrityError):
        with db_connection() as connection:
            connection.execute(
                text('DELETE FROM "groups" WHERE id = :id'), {"id": root_id}
            )


def test_generated_ids_are_not_reused_after_deletion(app):
    first = trellmark.add_url("https://one.example")
    second = trellmark.add_url("https://two.example")

    assert trellmark.remove_url_by_id(second["id"], 1)
    third = trellmark.add_url("https://three.example")

    assert first["id"] == 1
    assert second["id"] == 2
    assert third["id"] == 3
