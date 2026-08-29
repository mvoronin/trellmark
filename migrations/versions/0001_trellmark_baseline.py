"""Trellmark PostgreSQL baseline.

The complete schema for a fresh Trellmark installation. This is intentionally
the first and only revision; no earlier migration or export compatibility is
part of this project.

Revision ID: 0001_trellmark_baseline
Revises:
Create Date: 2026-08-29 00:00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001_trellmark_baseline"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

MAX_GROUP_DEPTH = 3
MAX_ICON_BYTES = 256 * 1024

# This is intentionally not a password verifier. A fresh database cannot
# authenticate until the operator runs `server.py set-password`, so no reusable
# credential or verifier from another installation enters the repository.
DISABLED_PASSWORD_SENTINEL = "!"


class LTREE(sa.types.UserDefinedType[str]):
    cache_ok = True

    def get_col_spec(self, **_kw: object) -> str:
        return "LTREE"


PATH_MAINTENANCE_FUNCTION = f"""
CREATE FUNCTION groups_maintain_path() RETURNS trigger AS $$
DECLARE
    parent_path ltree;
    subtree_height integer := 0;
    derived_path ltree;
BEGIN
    IF pg_trigger_depth() > 1 THEN
        RETURN NEW;
    END IF;

    IF NEW.parent_id IS NULL THEN
        derived_path := text2ltree('g_' || NEW.id);
    ELSE
        IF NEW.parent_id = NEW.id THEN
            RAISE EXCEPTION 'group % cannot be its own parent', NEW.id
                USING ERRCODE = 'GH002';
        END IF;

        SELECT path INTO parent_path
        FROM "groups"
        WHERE id = NEW.parent_id
        FOR SHARE;

        IF parent_path IS NULL THEN
            RAISE EXCEPTION 'parent group % does not exist', NEW.parent_id
                USING ERRCODE = 'GH001';
        END IF;

        IF TG_OP = 'UPDATE' AND parent_path <@ OLD.path THEN
            RAISE EXCEPTION 'group % cannot be moved under its own descendant',
                NEW.id
                USING ERRCODE = 'GH002';
        END IF;

        derived_path := parent_path || text2ltree('g_' || NEW.id);
    END IF;

    IF TG_OP = 'UPDATE' THEN
        SELECT COALESCE(MAX(nlevel(path)), nlevel(OLD.path)) - nlevel(OLD.path)
        INTO subtree_height
        FROM "groups"
        WHERE path <@ OLD.path;
    END IF;

    IF nlevel(derived_path) + subtree_height > {MAX_GROUP_DEPTH} THEN
        RAISE EXCEPTION
            'group % would exceed the maximum depth of {MAX_GROUP_DEPTH}', NEW.id
            USING ERRCODE = 'GH003';
    END IF;

    NEW.path := derived_path;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

SUBTREE_REWRITE_FUNCTION = """
CREATE FUNCTION groups_rewrite_subtree() RETURNS trigger AS $$
BEGIN
    IF pg_trigger_depth() > 1 THEN
        RETURN NULL;
    END IF;

    UPDATE "groups"
    SET path = NEW.path || subpath(path, nlevel(OLD.path))
    WHERE path <@ OLD.path
      AND id <> NEW.id;

    RETURN NULL;
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS ltree")

    op.create_table(
        "groups",
        sa.Column("id", sa.Integer(), sa.Identity(always=False), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("nsfw", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "parent_id",
            sa.Integer(),
            sa.ForeignKey("groups.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("path", LTREE(), nullable=False),
        sa.UniqueConstraint("path", name="uq_groups_path"),
        sa.CheckConstraint(
            f"nlevel(path) BETWEEN 1 AND {MAX_GROUP_DEPTH}",
            name="ck_groups_path_depth",
        ),
    )
    op.create_index(
        "uq_groups_name_lower",
        "groups",
        [sa.text("lower(name)")],
        unique=True,
    )
    op.execute(
        'CREATE UNIQUE INDEX uq_groups_root_position ON "groups" (position) '
        "WHERE parent_id IS NULL"
    )
    op.execute(
        'CREATE UNIQUE INDEX uq_groups_child_position ON "groups" '
        "(parent_id, position) WHERE parent_id IS NOT NULL"
    )
    op.create_index("ix_groups_parent_id_position", "groups", ["parent_id", "position"])
    op.execute(
        'CREATE INDEX ix_groups_path_gist ON "groups" USING GIST (path gist_ltree_ops)'
    )

    op.execute(PATH_MAINTENANCE_FUNCTION)
    op.execute(SUBTREE_REWRITE_FUNCTION)
    op.execute(
        'CREATE TRIGGER groups_maintain_path BEFORE INSERT OR UPDATE ON "groups" '
        "FOR EACH ROW EXECUTE FUNCTION groups_maintain_path()"
    )
    op.execute(
        'CREATE TRIGGER groups_rewrite_subtree AFTER UPDATE ON "groups" '
        "FOR EACH ROW WHEN (OLD.path IS DISTINCT FROM NEW.path) "
        "EXECUTE FUNCTION groups_rewrite_subtree()"
    )
    op.execute("INSERT INTO \"groups\" (name, position) VALUES ('default', 0)")

    op.create_table(
        "urls",
        sa.Column("id", sa.Integer(), sa.Identity(always=False), primary_key=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("important", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.UniqueConstraint("url", name="uq_urls_url"),
    )

    op.create_table(
        "group_domains",
        sa.Column(
            "group_id",
            sa.Integer(),
            sa.ForeignKey("groups.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("domain", sa.Text(), primary_key=True),
    )
    op.create_index("ix_group_domains_domain", "group_domains", ["domain"])

    op.create_table(
        "url_groups",
        sa.Column(
            "url_id",
            sa.Integer(),
            sa.ForeignKey("urls.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "group_id",
            sa.Integer(),
            sa.ForeignKey("groups.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )
    op.create_index("ix_url_groups_group_id", "url_groups", ["group_id"])

    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("role = 'admin'", name="ck_users_role"),
        sa.CheckConstraint("status IN ('active', 'inactive')", name="ck_users_status"),
    )
    op.create_index(
        "uq_users_username_lower",
        "users",
        [sa.text("lower(username)")],
        unique=True,
    )

    op.create_table(
        "password_credentials",
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_table(
        "web_sessions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("secret_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("csrf_secret_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("absolute_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "last_used_at >= created_at AND idle_expires_at > created_at "
            "AND absolute_expires_at > created_at",
            name="ck_web_sessions_lifetimes",
        ),
        sa.UniqueConstraint("secret_hash", name="uq_web_sessions_secret_hash"),
    )
    op.create_index("ix_web_sessions_user_id", "web_sessions", ["user_id"])
    op.create_index(
        "ix_web_sessions_idle_expires_at",
        "web_sessions",
        ["idle_expires_at", "id"],
    )
    op.create_index(
        "ix_web_sessions_absolute_expires_at",
        "web_sessions",
        ["absolute_expires_at", "id"],
    )
    op.create_index(
        "ix_web_sessions_revoked_at",
        "web_sessions",
        ["revoked_at", "id"],
        postgresql_where=sa.text("revoked_at IS NOT NULL"),
    )

    op.create_table(
        "auth_login_throttle",
        sa.Column("scope", sa.Text(), primary_key=True),
        sa.Column("bucket_key", sa.Text(), primary_key=True),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("blocked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "scope IN ('source', 'global')", name="ck_auth_throttle_scope"
        ),
        sa.CheckConstraint("failure_count >= 0", name="ck_auth_throttle_failure_count"),
        sa.CheckConstraint(
            "length(bucket_key) BETWEEN 1 AND 128",
            name="ck_auth_throttle_bucket_key_length",
        ),
    )
    op.create_index(
        "ix_auth_login_throttle_updated_at",
        "auth_login_throttle",
        ["updated_at"],
    )

    connection = op.get_bind()
    user_id = connection.execute(
        sa.text(
            "INSERT INTO users (username, role, status) "
            "VALUES ('admin', 'admin', 'active') RETURNING id"
        )
    ).scalar_one()
    connection.execute(
        sa.text(
            "INSERT INTO password_credentials (user_id, password_hash) "
            "VALUES (:user_id, :password_hash)"
        ),
        {"user_id": user_id, "password_hash": DISABLED_PASSWORD_SENTINEL},
    )

    op.create_table(
        "site_icon_cache",
        sa.Column("origin", sa.Text(), primary_key=True),
        sa.Column("icon_bytes", sa.LargeBinary(), nullable=True),
        sa.Column("media_type", sa.Text(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retry_after", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(icon_bytes IS NULL AND media_type IS NULL AND fetched_at IS NULL) OR "
            "(icon_bytes IS NOT NULL AND media_type IS NOT NULL "
            "AND fetched_at IS NOT NULL)",
            name="ck_site_icon_cache_state",
        ),
        sa.CheckConstraint(
            "media_type IS NULL OR media_type IN "
            "('image/png', 'image/vnd.microsoft.icon')",
            name="ck_site_icon_cache_media_type",
        ),
        sa.CheckConstraint(
            f"icon_bytes IS NULL OR octet_length(icon_bytes) <= {MAX_ICON_BYTES}",
            name="ck_site_icon_cache_size",
        ),
    )


def downgrade() -> None:
    op.drop_table("site_icon_cache")
    op.drop_table("auth_login_throttle")
    op.drop_table("web_sessions")
    op.drop_table("password_credentials")
    op.drop_index("uq_users_username_lower", table_name="users")
    op.drop_table("users")
    op.drop_table("url_groups")
    op.drop_table("group_domains")
    op.drop_table("urls")
    op.execute('DROP TRIGGER IF EXISTS groups_rewrite_subtree ON "groups"')
    op.execute('DROP TRIGGER IF EXISTS groups_maintain_path ON "groups"')
    op.execute("DROP FUNCTION IF EXISTS groups_rewrite_subtree()")
    op.execute("DROP FUNCTION IF EXISTS groups_maintain_path()")
    op.drop_table("groups")
    op.execute("DROP EXTENSION IF EXISTS ltree")
