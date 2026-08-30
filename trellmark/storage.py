from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Literal

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    delete,
    exists,
    func,
    insert,
    literal,
    select,
    update,
)
from sqlalchemy import cast as sql_cast
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import (
    DBAPIError,
    IntegrityError,
    OperationalError,
    ProgrammingError,
    SQLAlchemyError,
)
from sqlalchemy.types import UserDefinedType

from . import config
from .storage_types import (
    DeleteGroupResult as DeleteGroupData,
)
from .storage_types import (
    ExportDocument,
    ExportGroupRecord,
    ExportURLRecord,
    GroupRecord,
    ImportDocument,
    ImportResult,
    ImportURLRecord,
    SiteIconCacheRecord,
    UpdateURLFields,
    URLRecord,
)
from .storage_types import (
    MoveURLGroupResult as MoveURLGroupData,
)
from .url_normalization import domain_for_url, normalize_domains

MOVE_GROUP_NOT_FOUND: Literal["group_not_found"] = "group_not_found"
MOVE_URL_NOT_FOUND: Literal["url_not_found"] = "url_not_found"
MOVE_SOURCE_NOT_FOUND: Literal["source_not_found"] = "source_not_found"
MOVE_SOURCE_REQUIRED: Literal["source_required"] = "source_required"
GROUP_NOT_FOUND: Literal["group_not_found"] = "group_not_found"
DEFAULT_GROUP: Literal["default_group"] = "default_group"
GROUP_NAME_CONFLICT: Literal["name_conflict"] = "name_conflict"
GROUP_HAS_CHILDREN: Literal["group_has_children"] = "group_has_children"
PARENT_NOT_FOUND: Literal["parent_not_found"] = "parent_not_found"
PARENT_IS_SELF_OR_DESCENDANT: Literal["parent_is_self_or_descendant"] = (
    "parent_is_self_or_descendant"
)
DEPTH_EXCEEDED: Literal["depth_exceeded"] = "depth_exceeded"
URL_NOT_FOUND: Literal["url_not_found"] = "url_not_found"
URL_CONFLICT: Literal["url_conflict"] = "url_conflict"
URL_VERSION_CONFLICT: Literal["url_version_conflict"] = "url_version_conflict"

# `update_group` has to tell "leave the parent alone" from "move to the root",
# which `int | None` alone cannot express.
UNCHANGED_PARENT: Literal["unchanged_parent"] = "unchanged_parent"
type ParentUpdate = int | None | Literal["unchanged_parent"]

# The baseline's hierarchy trigger raises with these codes.
# Storage validates first so the API gets a specific error; these are what a
# concurrent writer that beats the validation looks like.
TRIGGER_ERRORS: dict[str, "GroupHierarchyError"] = {
    "GH001": PARENT_NOT_FOUND,
    "GH002": PARENT_IS_SELF_OR_DESCENDANT,
    "GH003": DEPTH_EXCEEDED,
}
# Three levels, root inclusive. The database enforces the same bound.
MAX_GROUP_DEPTH = 3

# Advisory-lock namespace for "the sibling set under this parent", keyed by
# parent id. The namespace only has to be distinct from any other advisory lock
# this database ever takes; the roots use a key no group id can have.
SIBLING_LOCK_NAMESPACE = 7501
ROOT_SIBLING_LOCK_KEY = 0

# Import takes this transaction-scoped gate before any sibling lock. The
# namespace is deliberately distinct from the sibling-set namespace so the
# global operation boundary cannot alias a positional lock.
BOOKMARK_MUTATION_LOCK_NAMESPACE = 7502
BOOKMARK_MUTATION_LOCK_KEY = 0


class BookmarkMutationConflict(RuntimeError):
    """Another bookmark mutation owns the shared PostgreSQL gate."""


type ImportValidator = Callable[[Sequence[GroupRecord]], None]
type ImportStageHook = Callable[[str], None]

IMPORT_MUTATION_STAGES = (
    "group_metadata",
    "hierarchy_detach",
    "hierarchy_attach",
    "sibling_order",
    "url_insert",
    "membership_insert",
    "url_metadata",
)


def _ignore_import_stage(_stage: str) -> None:
    pass


type MoveURLGroupError = Literal[
    "group_not_found", "url_not_found", "source_not_found", "source_required"
]
type MoveURLGroupResult = tuple[MoveURLGroupData, None] | tuple[None, MoveURLGroupError]
type GroupHierarchyError = Literal[
    "parent_not_found", "parent_is_self_or_descendant", "depth_exceeded"
]
type DeleteGroupError = Literal[
    "group_not_found", "default_group", "group_has_children"
]
type AddGroupError = Literal[
    "name_conflict",
    "parent_not_found",
    "parent_is_self_or_descendant",
    "depth_exceeded",
]
type AddGroupResult = tuple[GroupRecord, None] | tuple[None, AddGroupError]
type UpdateGroupError = Literal[
    "group_not_found",
    "default_group",
    "name_conflict",
    "parent_not_found",
    "parent_is_self_or_descendant",
    "depth_exceeded",
]
type UpdateGroupResult = tuple[GroupRecord, None] | tuple[None, UpdateGroupError]
type UpdateURLError = Literal["url_not_found", "url_conflict", "url_version_conflict"]
type UpdateURLResult = tuple[URLRecord, None] | tuple[None, UpdateURLError]
type DeleteGroupStorageResult = (
    tuple[DeleteGroupData, None] | tuple[None, DeleteGroupError]
)
type RowData = RowMapping


metadata = MetaData()


class LTREE(UserDefinedType[str]):
    """The `ltree` column type, which SQLAlchemy does not ship.

    Declaring it keeps `metadata` an honest mirror of the migrated schema and
    lets ancestry predicates be written as Core expressions with bound
    parameters (`column.op("<@")(value)`), rather than as interpolated SQL.
    """

    cache_ok = True

    def get_col_spec(self, **_kw: Any) -> str:
        return "LTREE"


groups_table = Table(
    "groups",
    metadata,
    Column("id", Integer, Identity(always=False), primary_key=True),
    Column("name", Text, nullable=False),
    Column("position", Integer, nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Column("nsfw", Boolean, nullable=False, server_default=literal(False)),
    # Direct parent for relational work; `path` is the indexed materialized
    # ancestry. A migration-owned trigger keeps the two in agreement and builds
    # `path` from ids, so nothing here ever writes it.
    Column(
        "parent_id",
        Integer,
        ForeignKey("groups.id", ondelete="RESTRICT"),
        nullable=True,
    ),
    Column("path", LTREE, nullable=False),
    UniqueConstraint("path", name="uq_groups_path"),
    CheckConstraint("nlevel(path) BETWEEN 1 AND 3", name="ck_groups_path_depth"),
)
# Case-insensitive group-name uniqueness as a functional index, so no
# PostgreSQL extension (citext) has to be installed to enforce it.
Index("uq_groups_name_lower", func.lower(groups_table.c.name), unique=True)
# Positions are unique per sibling set, not globally. Two partial indexes
# rather than one on (parent_id, position): PostgreSQL treats every NULL parent
# as distinct, which would leave root positions unconstrained.
Index(
    "uq_groups_root_position",
    groups_table.c.position,
    unique=True,
    postgresql_where=groups_table.c.parent_id.is_(None),
)
Index(
    "uq_groups_child_position",
    groups_table.c.parent_id,
    groups_table.c.position,
    unique=True,
    postgresql_where=groups_table.c.parent_id.is_not(None),
)
Index(
    "ix_groups_parent_id_position",
    groups_table.c.parent_id,
    groups_table.c.position,
)
Index(
    "ix_groups_path_gist",
    groups_table.c.path,
    postgresql_using="gist",
    postgresql_ops={"path": "gist_ltree_ops"},
)

urls_table = Table(
    "urls",
    metadata,
    Column("id", Integer, Identity(always=False), primary_key=True),
    Column("url", Text, nullable=False, unique=True),
    Column("title", Text, nullable=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Column("important", Boolean, nullable=False, server_default=literal(False)),
    Column("version", Integer, nullable=False, server_default=literal(1)),
)

site_icon_cache_table = Table(
    "site_icon_cache",
    metadata,
    Column("origin", Text, primary_key=True),
    Column("icon_bytes", LargeBinary, nullable=True),
    Column("media_type", Text, nullable=True),
    Column("fetched_at", DateTime(timezone=True), nullable=True),
    Column("retry_after", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "(icon_bytes IS NULL AND media_type IS NULL AND fetched_at IS NULL) OR "
        "(icon_bytes IS NOT NULL AND media_type IS NOT NULL AND fetched_at IS NOT NULL)",
        name="ck_site_icon_cache_state",
    ),
    CheckConstraint(
        "media_type IS NULL OR media_type IN ('image/png', 'image/vnd.microsoft.icon')",
        name="ck_site_icon_cache_media_type",
    ),
    CheckConstraint(
        "icon_bytes IS NULL OR octet_length(icon_bytes) <= 262144",
        name="ck_site_icon_cache_size",
    ),
)

group_domains_table = Table(
    "group_domains",
    metadata,
    Column(
        "group_id",
        Integer,
        ForeignKey("groups.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("domain", Text, primary_key=True),
)
Index("ix_group_domains_domain", group_domains_table.c.domain)

url_groups_table = Table(
    "url_groups",
    metadata,
    Column(
        "url_id",
        Integer,
        ForeignKey("urls.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "group_id",
        Integer,
        ForeignKey("groups.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)
Index("ix_url_groups_group_id", url_groups_table.c.group_id)

alembic_version_table = Table(
    "alembic_version",
    metadata,
    Column("version_num", Text, primary_key=True),
)


_engine: Engine | None = None
_engine_url: str | None = None


def get_engine() -> Engine:
    """Return the process-lifetime engine, building it on first use.

    One bounded pool per process, rather than the throwaway NullPool engine the
    SQLite path used: a network database makes connection setup expensive, and
    pre-ping keeps a connection the server has since closed from surfacing as a
    request error.
    """
    global _engine, _engine_url
    url = config.database_url()
    rendered = url.render_as_string(hide_password=False)
    if _engine is not None and _engine_url == rendered:
        return _engine

    dispose_engine()
    _engine = create_engine(
        url,
        future=True,
        pool_size=5,
        max_overflow=5,
        pool_pre_ping=True,
        pool_recycle=1800,
    )
    _engine_url = rendered
    return _engine


def dispose_engine() -> None:
    """Close pooled connections and forget the engine.

    Called on application shutdown, and by tests between databases so a pooled
    connection never outlives the database it was opened against.
    """
    global _engine, _engine_url
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _engine_url = None


def get_connection() -> Connection:
    return get_engine().connect()


@contextmanager
def _bookmark_mutation() -> Generator[Connection]:
    """Yield one connection whose outer transaction owns the bookmark gate."""
    with get_connection() as connection:
        with connection.begin():
            acquired = connection.scalar(
                select(
                    func.pg_try_advisory_xact_lock(
                        BOOKMARK_MUTATION_LOCK_NAMESPACE,
                        BOOKMARK_MUTATION_LOCK_KEY,
                    )
                )
            )
            if acquired is not True:
                raise BookmarkMutationConflict
            yield connection


def alembic_config() -> Config:
    # No sqlalchemy.url is set here: migrations/env.py reads the DSN from
    # trellmark.config directly, so a password never passes through ConfigParser
    # (where a '%' would need escaping) or lands in alembic.ini.
    return Config(str(config.BASE_DIR / "alembic.ini"))


def run_migrations() -> None:
    command.upgrade(alembic_config(), "head")


def verify_db_at_head() -> None:
    """Fail with a distinct, actionable message for each startup failure mode.

    Connectivity, authentication, a missing schema, and a stale revision are
    four different operator problems; collapsing them into one "not migrated"
    string sends the operator to the wrong fix.
    """
    expected_heads = set(ScriptDirectory.from_config(alembic_config()).get_heads())
    if not expected_heads:
        return

    target = config.redacted_database_url()
    try:
        with get_connection() as connection:
            current_heads = set(
                connection.execute(
                    select(alembic_version_table.c.version_num)
                ).scalars()
            )
    except OperationalError as error:
        raise RuntimeError(
            f"Cannot connect to PostgreSQL at {target}: {_db_error_text(error)}"
        ) from error
    except ProgrammingError as error:
        raise RuntimeError(
            f"PostgreSQL at {target} has no trellmark schema; "
            "run `just migrate` (or `python server.py migrate`)."
        ) from error
    except SQLAlchemyError as error:
        raise RuntimeError(
            f"PostgreSQL at {target} is unusable: {_db_error_text(error)}"
        ) from error

    if current_heads != expected_heads:
        raise RuntimeError(
            f"PostgreSQL at {target} is at revision "
            f"{', '.join(sorted(current_heads)) or '(none)'} but this build "
            f"expects {', '.join(sorted(expected_heads))}; run `just migrate`."
        )


def _db_error_text(error: SQLAlchemyError) -> str:
    """Render a database error without leaking the DSN password.

    Driver errors embed the connection string, so the raw text is not safe to
    print. Prefer the DBAPI cause, which carries the server's message only.
    """
    cause = getattr(error, "orig", None)
    text = str(cause) if cause is not None else error.__class__.__name__
    return " ".join(text.split()) or error.__class__.__name__


def read_urls() -> list[str]:
    with get_connection() as connection:
        rows = connection.execute(
            select(urls_table.c.url).order_by(urls_table.c.id)
        ).scalars()
        return [_string_value(row, "url") for row in rows]


def read_url_records() -> list[URLRecord]:
    """Return saved URLs with their creation time as ISO 8601 UTC strings.

    created_at is stored as TIMESTAMPTZ, an absolute instant; `_iso_utc`
    renders it in UTC with a trailing 'Z' so the frontend parses it
    unambiguously and shows it in the viewer's local time.
    """
    with get_connection() as connection:
        rows = (
            connection.execute(_url_record_select().order_by(urls_table.c.id))
            .mappings()
            .all()
        )
    return [_url_record(row) for row in rows]


def read_url_record(url: str) -> URLRecord | None:
    with get_connection() as connection:
        row = (
            connection.execute(_url_record_select().where(urls_table.c.url == url))
            .mappings()
            .first()
        )
    return _url_record(row) if row else None


def read_url_record_by_id(url_id: int) -> URLRecord | None:
    with get_connection() as connection:
        row = _url_record_by_id(connection, url_id)
    return _url_record(row) if row else None


def read_url_group_ids(url_id: int) -> list[int]:
    with get_connection() as connection:
        values = connection.execute(
            select(url_groups_table.c.group_id)
            .join(groups_table, url_groups_table.c.group_id == groups_table.c.id)
            .where(url_groups_table.c.url_id == url_id)
            .order_by(groups_table.c.position)
        ).scalars()
        return [_int_value(value, "group id") for value in values]


def read_site_icon_cache(origin: str) -> SiteIconCacheRecord | None:
    """Read private icon cache state without joining it into URL records."""
    with get_connection() as connection:
        row = (
            connection.execute(
                select(site_icon_cache_table).where(
                    site_icon_cache_table.c.origin == origin
                )
            )
            .mappings()
            .first()
        )
    return _site_icon_cache_record(row) if row is not None else None


def upsert_site_icon_success(
    origin: str,
    icon_bytes: bytes,
    media_type: str,
    fetched_at: datetime,
    retry_after: datetime,
) -> SiteIconCacheRecord:
    """Store one validated positive value for an origin."""
    statement = (
        pg_insert(site_icon_cache_table)
        .values(
            origin=origin,
            icon_bytes=icon_bytes,
            media_type=media_type,
            fetched_at=fetched_at,
            retry_after=retry_after,
        )
        .on_conflict_do_update(
            index_elements=[site_icon_cache_table.c.origin],
            set_={
                "icon_bytes": icon_bytes,
                "media_type": media_type,
                "fetched_at": fetched_at,
                "retry_after": retry_after,
            },
        )
        .returning(*site_icon_cache_table.c)
    )
    with get_connection() as connection:
        with connection.begin():
            row = connection.execute(statement).mappings().one()
    return _site_icon_cache_record(row)


def upsert_site_icon_failure(
    origin: str,
    retry_after: datetime,
) -> SiteIconCacheRecord:
    """Record a failed lookup while retaining any prior successful bytes."""
    statement = (
        pg_insert(site_icon_cache_table)
        .values(
            origin=origin,
            icon_bytes=None,
            media_type=None,
            fetched_at=None,
            retry_after=retry_after,
        )
        .on_conflict_do_update(
            index_elements=[site_icon_cache_table.c.origin],
            set_={"retry_after": retry_after},
        )
        .returning(*site_icon_cache_table.c)
    )
    with get_connection() as connection:
        with connection.begin():
            row = connection.execute(statement).mappings().one()
    return _site_icon_cache_record(row)


def read_group_records() -> list[GroupRecord]:
    """Return the group forest: roots in sibling order, each with `children`.

    Three queries and one pass of nesting in Python, however deep the tree is.
    Ordering by `position` alone is enough because rows are then bucketed by
    parent, so every bucket comes out in sibling order.
    """
    with get_connection() as connection:
        return _read_group_records_on(connection)


def _read_group_records_on(connection: Connection) -> list[GroupRecord]:
    groups = (
        connection.execute(_group_record_select().order_by(groups_table.c.position))
        .mappings()
        .all()
    )
    url_rows = (
        connection.execute(
            select(
                urls_table.c.id,
                url_groups_table.c.group_id,
                urls_table.c.url,
                urls_table.c.title,
                urls_table.c.important,
                urls_table.c.version,
                urls_table.c.created_at,
            )
            .select_from(
                url_groups_table.join(
                    urls_table, url_groups_table.c.url_id == urls_table.c.id
                ).join(groups_table, url_groups_table.c.group_id == groups_table.c.id)
            )
            .order_by(urls_table.c.id)
        )
        .mappings()
        .all()
    )
    domain_rows = (
        connection.execute(
            select(group_domains_table.c.group_id, group_domains_table.c.domain)
            .join(groups_table, group_domains_table.c.group_id == groups_table.c.id)
            .order_by(groups_table.c.position, group_domains_table.c.domain)
        )
        .mappings()
        .all()
    )

    urls_by_group: dict[int, list[URLRecord]] = {
        _row_int(group, "id"): [] for group in groups
    }
    for row in url_rows:
        group_urls = urls_by_group.get(_row_int(row, "group_id"))
        if group_urls is not None:
            group_urls.append(_url_record(row))

    domains_by_group: dict[int, list[str]] = {
        _row_int(group, "id"): [] for group in groups
    }
    for row in domain_rows:
        group_domains = domains_by_group.get(_row_int(row, "group_id"))
        if group_domains is not None:
            group_domains.append(_row_str(row, "domain"))

    roots: list[GroupRecord] = []
    children_by_parent: dict[int, list[GroupRecord]] = {}
    records_by_id: dict[int, GroupRecord] = {}
    for row in groups:
        group_id = _row_int(row, "id")
        record = _group_record(
            row,
            domains_by_group[group_id],
            urls_by_group[group_id],
        )
        records_by_id[group_id] = record
        parent_id = record["parent_id"]
        if parent_id is None:
            roots.append(record)
        else:
            children_by_parent.setdefault(parent_id, []).append(record)

    for parent_id, children in children_by_parent.items():
        records_by_id[parent_id]["children"] = children

    return roots


def flatten_group_records(groups: Sequence[GroupRecord]) -> list[GroupRecord]:
    """Every group in the forest, parents before their own children."""
    flattened: list[GroupRecord] = []
    for group in groups:
        flattened.append(group)
        flattened.extend(flatten_group_records(group["children"]))
    return flattened


def find_group_record(
    groups: Sequence[GroupRecord],
    group_id: int,
) -> GroupRecord | None:
    for group in flatten_group_records(groups):
        if group["id"] == group_id:
            return group
    return None


def export_saved_data(exported_at: str) -> ExportDocument:
    groups: list[ExportGroupRecord] = []
    stored_groups = flatten_group_records(read_group_records())
    names_by_id = {group["id"]: group["name"] for group in stored_groups}
    for group in stored_groups:
        urls: list[ExportURLRecord] = [
            {
                "url": record["url"],
                "title": record["title"],
                "created_at": record["created_at"],
                "important": record["important"],
            }
            for record in group["urls"]
        ]
        groups.append(
            {
                "name": group["name"],
                "parent": (
                    None
                    if group["parent_id"] is None
                    else names_by_id[group["parent_id"]]
                ),
                "position": group["position"],
                "nsfw": group["nsfw"],
                "domains": group["domains"],
                "urls": urls,
            }
        )

    return {
        "version": 1,
        "exported_at": exported_at,
        "groups": groups,
    }


def import_saved_data(
    document: ImportDocument,
    *,
    validate_against: ImportValidator,
    after_stage: ImportStageHook = _ignore_import_stage,
) -> ImportResult:
    with _bookmark_mutation() as connection:
        existing_groups = _read_group_records_on(connection)
        validate_against(existing_groups)
        return _import_saved_data_on(
            connection,
            document,
            existing_groups,
            after_stage=after_stage,
        )


def _import_saved_data_on(
    connection: Connection,
    document: ImportDocument,
    existing_groups: Sequence[GroupRecord],
    *,
    after_stage: ImportStageHook,
) -> ImportResult:
    stored_groups = flatten_group_records(existing_groups)
    groups_by_name = {group["name"].lower(): group for group in stored_groups}
    group_ids_by_name = {key: group["id"] for key, group in groups_by_name.items()}
    document_group_keys = {group["name"].lower() for group in document["groups"]}

    # The supplied validator owns the supported document contract. This
    # defensive check keeps direct storage callers from creating a missing
    # parent when they provide a weaker callback.
    for group in document["groups"]:
        parent_key = None if group["parent"] is None else group["parent"].lower()
        if (
            parent_key is not None
            and parent_key not in document_group_keys
            and parent_key not in groups_by_name
        ):
            raise ValueError("Imported parent group does not exist.")

    for group in document["groups"]:
        key = group["name"].lower()
        group_record = groups_by_name.get(key)
        metadata_mutated = group_record is None
        if group_record is None:
            group_record, create_error = _create_group_on(
                connection,
                group["name"],
                nsfw=group["nsfw"],
                domains=group["domains"],
            )
            if group_record is None:
                raise RuntimeError(f"Group could not be created: {create_error}.")
        groups_by_name[key] = group_record
        group_ids_by_name[key] = group_record["id"]
        if group_record["nsfw"] != group["nsfw"]:
            _set_group_nsfw_on(connection, group_record["id"], group["nsfw"])
            metadata_mutated = True
        if group_record["domains"] != group["domains"]:
            _set_group_domains_on(connection, group_record["id"], group["domains"])
            metadata_mutated = True
        if metadata_mutated:
            after_stage("group_metadata")

    desired_parent_ids: dict[str, int | None] = {}
    for group in document["groups"]:
        key = group["name"].lower()
        parent_key = None if group["parent"] is None else group["parent"].lower()
        parent_id = None if parent_key is None else group_ids_by_name.get(parent_key)
        if parent_key is not None and parent_id is None:
            raise RuntimeError("Imported parent group does not exist.")
        desired_parent_ids[key] = parent_id

    # Make the imported portion of the forest independent before rebuilding
    # its requested edges. Moving every imported non-root to the root first
    # means a legal final hierarchy cannot fail because an old descendant is
    # temporarily still attached, or because two existing groups trade places.
    for group in sorted(groups_by_name.values(), key=lambda record: -record["depth"]):
        key = group["name"].lower()
        if key not in desired_parent_ids or group["parent_id"] is None:
            continue
        updated, error = _update_group_on(connection, group["id"], parent_id=None)
        if updated is None:
            raise RuntimeError(f"Imported group could not be detached: {error}.")
        after_stage("hierarchy_detach")

    for group in document["groups"]:
        key = group["name"].lower()
        parent_id = desired_parent_ids[key]
        if parent_id is None:
            continue
        updated, error = _update_group_on(
            connection,
            group_ids_by_name[key],
            parent_id=parent_id,
        )
        if updated is None:
            raise RuntimeError(f"Imported group could not be attached: {error}.")
        after_stage("hierarchy_attach")

    current_groups = flatten_group_records(_read_group_records_on(connection))
    current_ids_by_parent: dict[int | None, list[int]] = {}
    for group in current_groups:
        current_ids_by_parent.setdefault(group["parent_id"], []).append(group["id"])

    imported_by_parent: dict[int | None, list[tuple[int, int]]] = {}
    for group in document["groups"]:
        key = group["name"].lower()
        imported_by_parent.setdefault(desired_parent_ids[key], []).append(
            (group["position"], group_ids_by_name[key])
        )

    for parent_id, positioned_ids in imported_by_parent.items():
        imported_ids = [
            group_id for _, group_id in sorted(positioned_ids, key=lambda item: item[0])
        ]
        imported_id_set = set(imported_ids)
        current_sibling_ids = current_ids_by_parent.get(parent_id)
        if current_sibling_ids is None:
            raise RuntimeError("Imported sibling set no longer exists.")
        full_order = imported_ids + [
            group_id
            for group_id in current_sibling_ids
            if group_id not in imported_id_set
        ]
        if not _update_group_order_on(connection, parent_id, full_order):
            raise RuntimeError("Imported group order could not be restored.")
        after_stage("sibling_order")

    imported = 0
    skipped = 0
    imported_url_ids: dict[str, int] = {}
    for group in document["groups"]:
        group_id = group_ids_by_name[group["name"].lower()]
        for record in group["urls"]:
            already_imported_id = imported_url_ids.get(record["url"])
            if already_imported_id is not None:
                _add_url_to_group_on(connection, already_imported_id, group_id)
                after_stage("membership_insert")
                continue

            inserted = _insert_import_url_on(connection, record)
            if not inserted:
                skipped += 1
                continue
            after_stage("url_insert")

            _set_url_group_on(connection, inserted["id"], group_id)
            after_stage("membership_insert")
            imported_url_ids[record["url"]] = inserted["id"]
            _set_url_created_at_on(connection, inserted["id"], record["created_at"])
            _set_url_important_on(connection, inserted["id"], record["important"])
            after_stage("url_metadata")
            imported += 1

    return {
        "imported": imported,
        "skipped": skipped,
        "groups": _read_group_records_on(connection),
    }


def _insert_import_url_on(
    connection: Connection,
    record: ImportURLRecord,
) -> URLRecord | None:
    row = (
        connection.execute(
            pg_insert(urls_table)
            .values(url=record["url"], title=record["title"])
            .on_conflict_do_nothing(index_elements=[urls_table.c.url])
            .returning(*urls_table.c)
        )
        .mappings()
        .first()
    )
    return _url_record(row) if row is not None else None


def _group_record(
    row: RowData,
    domains: list[str],
    urls: list[URLRecord],
) -> GroupRecord:
    parent_id = row["parent_id"]
    return {
        "id": _row_int(row, "id"),
        "name": _row_str(row, "name"),
        "parent_id": None if parent_id is None else _int_value(parent_id, "parent id"),
        "position": _row_int(row, "position"),
        "depth": _row_int(row, "depth"),
        "nsfw": bool(row["nsfw"]),
        "domains": domains,
        "urls": urls,
        "children": [],
    }


def _url_record(row: RowData) -> URLRecord:
    return {
        "id": _row_int(row, "id"),
        "url": _row_str(row, "url"),
        "title": _row_optional_str(row, "title"),
        "created_at": _iso_utc(row["created_at"]),
        "important": bool(row["important"]),
        "version": _row_int(row, "version"),
    }


def _site_icon_cache_record(row: RowData) -> SiteIconCacheRecord:
    icon_bytes = row["icon_bytes"]
    media_type = row["media_type"]
    fetched_at = row["fetched_at"]
    return {
        "origin": _row_str(row, "origin"),
        "icon_bytes": (
            None if icon_bytes is None else _bytes_value(icon_bytes, "icon bytes")
        ),
        "media_type": (
            None if media_type is None else _string_value(media_type, "media type")
        ),
        "fetched_at": (
            None if fetched_at is None else _datetime_value(fetched_at, "fetched at")
        ),
        "retry_after": _datetime_value(row["retry_after"], "retry after"),
    }


def _iso_utc(value: object) -> str:
    """Render a stored TIMESTAMPTZ as the API's second-precision UTC string.

    The database keeps an absolute instant; the API contract is an ISO 8601
    string ending in 'Z' so the frontend parses it unambiguously and renders it
    in the viewer's local time.
    """
    if not isinstance(value, datetime):
        raise RuntimeError("Expected a timestamp value for created_at.")
    moment = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _url_record_select() -> Any:
    return select(
        urls_table.c.id,
        urls_table.c.url,
        urls_table.c.title,
        urls_table.c.important,
        urls_table.c.version,
        urls_table.c.created_at,
    )


def _url_record_by_id(connection: Connection, url_id: int) -> RowData | None:
    return (
        connection.execute(_url_record_select().where(urls_table.c.id == url_id))
        .mappings()
        .first()
    )


def _group_record_select() -> Any:
    # `depth` is derived from the materialized path rather than stored twice.
    return select(
        groups_table.c.id,
        groups_table.c.name,
        groups_table.c.parent_id,
        groups_table.c.position,
        groups_table.c.nsfw,
        func.nlevel(groups_table.c.path).label("depth"),
    )


def _ltree(path: str) -> Any:
    """Bind a path as `ltree`.

    Without the cast the parameter reaches PostgreSQL untyped, and `<@` is
    overloaded enough that it cannot resolve which operator was meant.
    """
    return sql_cast(literal(path), LTREE)


def _is_descendant_of(column: Any, path: str) -> Any:
    """`column <@ path` — GiST-indexed, so no ancestry walk in Python."""
    return column.op("<@", is_comparison=True)(_ltree(path))


def _subtree_height(connection: Connection, path: str) -> int:
    """Levels below `path`, counting the group itself as 0."""
    height = connection.scalar(
        select(
            func.max(func.nlevel(groups_table.c.path)) - func.nlevel(_ltree(path))
        ).where(_is_descendant_of(groups_table.c.path, path))
    )
    return _int_value(height, "subtree height")


def _sibling_filter(parent_id: int | None) -> Any:
    # `IS NOT DISTINCT FROM` so the roots (parent_id IS NULL) are one sibling
    # set like any other, instead of comparing NULL with `=` and matching none.
    return groups_table.c.parent_id.is_not_distinct_from(parent_id)


def _lock_sibling_sets(connection: Connection, *parent_ids: int | None) -> None:
    """Take every named sibling set for the rest of the transaction.

    Positions are only contiguous if the operations that renumber them run one
    at a time. Appending reads the sibling count and writes it as the new
    position; deleting, moving out, and reordering renumber the survivors. Two
    of those at once leave a gap, and the next append then picks a position
    that is already taken and loses to the unique index — surfacing as a
    nonsense name conflict. So every path that writes a position takes its
    set's lock, without exception: an advisory lock coordinates nothing with a
    caller that does not ask for it.

    It is advisory rather than a row lock because a set may still be empty, and
    because PostgreSQL does not lock the gap a new row would occupy — locking
    the rows that happen to exist would not stop a concurrent insert.

    Keys are taken in sorted order so a move from A to B and a move from B to A
    acquire the pair the same way round instead of deadlocking on each other.
    """
    keys = sorted(
        {
            ROOT_SIBLING_LOCK_KEY if parent_id is None else parent_id
            for parent_id in parent_ids
        }
    )
    for key in keys:
        connection.execute(
            select(func.pg_advisory_xact_lock(SIBLING_LOCK_NAMESPACE, key))
        )


def _sibling_ids(connection: Connection, parent_id: int | None) -> list[int]:
    values = connection.execute(
        select(groups_table.c.id)
        .where(_sibling_filter(parent_id))
        .order_by(groups_table.c.position)
    ).scalars()
    return [_int_value(value, "sibling id") for value in values]


def _validate_parent(
    connection: Connection,
    parent_id: int | None,
    *,
    moved_path: str | None = None,
) -> GroupHierarchyError | None:
    """Check a destination before writing, so the API gets a specific error.

    The baseline trigger rejects the same moves; this
    exists to name the reason, not to replace it.
    """
    if parent_id is None:
        return None

    # FOR UPDATE: the destination must not move or disappear between this check
    # and the write that depends on it.
    parent = (
        connection.execute(
            select(
                func.nlevel(groups_table.c.path).label("depth"),
                (
                    _is_descendant_of(groups_table.c.path, moved_path)
                    if moved_path is not None
                    else literal(False)
                ).label("inside_subtree"),
            )
            .where(groups_table.c.id == parent_id)
            .with_for_update()
        )
        .mappings()
        .first()
    )
    if parent is None:
        return PARENT_NOT_FOUND

    # `<@` is reflexive, so this covers "the group itself" as well as any
    # descendant of it.
    if parent["inside_subtree"]:
        return PARENT_IS_SELF_OR_DESCENDANT
    # The whole subtree has to fit, not just the group being moved: a group one
    # level deep that carries a grandchild needs two levels below the target.
    height = _subtree_height(connection, moved_path) if moved_path is not None else 0
    if _row_int(parent, "depth") + 1 + height > MAX_GROUP_DEPTH:
        return DEPTH_EXCEEDED
    return None


def _hierarchy_error(error: DBAPIError) -> GroupHierarchyError | None:
    """Translate a trigger rejection that beat the storage-level validation."""
    sqlstate = getattr(getattr(error, "orig", None), "sqlstate", None)
    if not isinstance(sqlstate, str):
        return None
    return TRIGGER_ERRORS.get(sqlstate)


def _read_group_domains(connection: Connection, group_id: int) -> list[str]:
    domains = connection.execute(
        select(group_domains_table.c.domain)
        .where(group_domains_table.c.group_id == group_id)
        .order_by(group_domains_table.c.domain)
    ).scalars()
    return [_string_value(domain, "domain") for domain in domains]


def _insert_group_domains(
    connection: Connection,
    group_id: int,
    domains: Sequence[str],
) -> None:
    normalized_domains = normalize_domains(domains)
    if normalized_domains:
        connection.execute(
            insert(group_domains_table),
            [{"group_id": group_id, "domain": domain} for domain in normalized_domains],
        )


def _replace_group_domains(
    connection: Connection,
    group_id: int,
    domains: Sequence[str],
) -> None:
    connection.execute(
        delete(group_domains_table).where(group_domains_table.c.group_id == group_id)
    )
    _insert_group_domains(connection, group_id, domains)


def _case_insensitive_match(column: Any, value: str) -> Any:
    return func.lower(column) == func.lower(value)


def _row_int(row: RowData, key: str) -> int:
    return _int_value(row[key], key)


def _row_str(row: RowData, key: str) -> str:
    return _string_value(row[key], key)


def _row_optional_str(row: RowData, key: str) -> str | None:
    value = row[key]
    if value is None:
        return None
    return _string_value(value, key)


def _int_value(value: object, label: str) -> int:
    if type(value) is not int:
        raise RuntimeError(f"Expected integer value for {label}.")
    return value


def _string_value(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise RuntimeError(f"Expected string value for {label}.")
    return value


def _bytes_value(value: object, label: str) -> bytes:
    if not isinstance(value, bytes):
        raise RuntimeError(f"Expected bytes for {label}.")
    return value


def _datetime_value(value: object, label: str) -> datetime:
    if not isinstance(value, datetime):
        raise RuntimeError(f"Expected a timestamp value for {label}.")
    return value


def _primary_key_id(primary_key: Sequence[object] | None) -> int:
    if not primary_key:
        raise RuntimeError("Insert did not return a primary key.")
    return _int_value(primary_key[0], "primary key")


def read_group_record_by_name(name: str) -> GroupRecord | None:
    with get_connection() as connection:
        row = (
            connection.execute(
                _group_record_select().where(
                    _case_insensitive_match(groups_table.c.name, name)
                )
            )
            .mappings()
            .first()
        )
        domains = _read_group_domains(connection, _row_int(row, "id")) if row else []
    return _group_record(row, domains, []) if row else None


def create_group(
    name: str,
    nsfw: bool = False,
    domains: Sequence[str] = (),
    parent_id: int | None = None,
) -> AddGroupResult:
    """Insert a group under `parent_id`, or at the root, with the reason on
    failure.

    The new group is appended after its new siblings.
    """
    try:
        with _bookmark_mutation() as connection:
            return _create_group_on(
                connection,
                name,
                nsfw=nsfw,
                domains=domains,
                parent_id=parent_id,
            )
    except IntegrityError:
        return None, GROUP_NAME_CONFLICT
    except DBAPIError as error:
        hierarchy_error = _hierarchy_error(error)
        if hierarchy_error is None:
            raise
        return None, hierarchy_error


def _create_group_on(
    connection: Connection,
    name: str,
    *,
    nsfw: bool = False,
    domains: Sequence[str] = (),
    parent_id: int | None = None,
) -> AddGroupResult:
    normalized_domains = normalize_domains(domains)
    duplicate = connection.execute(
        select(groups_table.c.id).where(
            _case_insensitive_match(groups_table.c.name, name)
        )
    ).first()
    if duplicate:
        return None, GROUP_NAME_CONFLICT

    # Before `_validate_parent`, which takes row locks: set locks first
    # everywhere keeps the acquisition order consistent between this and a
    # concurrent move.
    _lock_sibling_sets(connection, parent_id)
    parent_error = _validate_parent(connection, parent_id)
    if parent_error is not None:
        return None, parent_error

    result = connection.execute(
        insert(groups_table).values(
            name=name,
            parent_id=parent_id,
            position=len(_sibling_ids(connection, parent_id)),
            nsfw=nsfw,
        )
    )
    group_id = _primary_key_id(result.inserted_primary_key)
    _insert_group_domains(connection, group_id, normalized_domains)
    row = (
        connection.execute(_group_record_select().where(groups_table.c.id == group_id))
        .mappings()
        .first()
    )
    stored_domains = _read_group_domains(connection, group_id)
    if row is None:
        raise RuntimeError("Inserted group could not be read.")
    return _group_record(row, stored_domains, []), None


def add_group(
    name: str,
    nsfw: bool = False,
    domains: Sequence[str] = (),
    parent_id: int | None = None,
) -> GroupRecord | None:
    """Insert a group, returning its record or None if it could not be created.

    The seeding form, used by tests and by JSON import. `create_group` is the
    same operation with the reason attached, which is what the API needs to
    pick a status code.
    """
    record, _ = create_group(name, nsfw=nsfw, domains=domains, parent_id=parent_id)
    return record


def update_group(
    group_id: int,
    name: str | None = None,
    nsfw: bool | None = None,
    domains: Sequence[str] | None = None,
    parent_id: ParentUpdate = UNCHANGED_PARENT,
) -> UpdateGroupResult:
    """Update editable group metadata and/or the group's parent.

    Metadata edits and a move are one transaction: either every requested
    change and every rewritten descendant path commits, or none does.
    """
    try:
        with _bookmark_mutation() as connection:
            return _update_group_on(
                connection,
                group_id,
                name=name,
                nsfw=nsfw,
                domains=domains,
                parent_id=parent_id,
            )
    except IntegrityError:
        return None, GROUP_NAME_CONFLICT
    except DBAPIError as error:
        hierarchy_error = _hierarchy_error(error)
        if hierarchy_error is None:
            raise
        return None, hierarchy_error


def _update_group_on(
    connection: Connection,
    group_id: int,
    *,
    name: str | None = None,
    nsfw: bool | None = None,
    domains: Sequence[str] | None = None,
    parent_id: ParentUpdate = UNCHANGED_PARENT,
) -> UpdateGroupResult:
    normalized_domains = normalize_domains(domains) if domains is not None else None
    group = (
        connection.execute(
            select(
                groups_table.c.name,
                groups_table.c.parent_id,
                sql_cast(groups_table.c.path, Text).label("path"),
            )
            .where(groups_table.c.id == group_id)
            .with_for_update()
        )
        .mappings()
        .first()
    )
    if not group:
        return None, GROUP_NOT_FOUND
    if _row_str(group, "name").lower() == "default":
        return None, DEFAULT_GROUP

    values: dict[str, object] = {}
    if name is not None:
        duplicate = connection.execute(
            select(groups_table.c.id).where(
                groups_table.c.id != group_id,
                _case_insensitive_match(groups_table.c.name, name),
            )
        ).first()
        if duplicate:
            return None, GROUP_NAME_CONFLICT
        values["name"] = name
    if nsfw is not None:
        values["nsfw"] = nsfw

    stored_parent = group["parent_id"]
    old_parent_id = (
        None if stored_parent is None else _int_value(stored_parent, "parent id")
    )
    destination: int | None = old_parent_id
    moving = False
    if parent_id != UNCHANGED_PARENT:
        destination = parent_id
        moving = destination != old_parent_id

    if moving:
        moved_path = _row_str(group, "path")
        _lock_sibling_sets(connection, old_parent_id, destination)
        connection.execute(
            select(groups_table.c.id)
            .where(_is_descendant_of(groups_table.c.path, moved_path))
            .with_for_update()
        ).all()
        parent_error = _validate_parent(
            connection,
            destination,
            moved_path=moved_path,
        )
        if parent_error is not None:
            return None, parent_error
        values["parent_id"] = destination
        values["position"] = len(_sibling_ids(connection, destination))

    if values:
        connection.execute(
            update(groups_table).where(groups_table.c.id == group_id).values(**values)
        )
    if moving:
        _write_group_order(connection, _sibling_ids(connection, old_parent_id))
    if normalized_domains is not None:
        _replace_group_domains(connection, group_id, normalized_domains)
    updated = (
        connection.execute(_group_record_select().where(groups_table.c.id == group_id))
        .mappings()
        .first()
    )
    updated_domains = _read_group_domains(connection, group_id)

    if updated is None:
        raise RuntimeError("Updated group could not be read.")
    return _group_record(updated, updated_domains, []), None


def delete_group(
    group_id: int,
    url_action: Literal["delete", "move_to_default"],
) -> DeleteGroupStorageResult:
    """Delete a leaf group and handle its URLs atomically.

    A group with children is refused: promoting or deleting a subtree on the
    user's behalf would be a bigger decision than the one they made.
    """
    with _bookmark_mutation() as connection:
        return _delete_group_on(connection, group_id, url_action)


def _delete_group_on(
    connection: Connection,
    group_id: int,
    url_action: Literal["delete", "move_to_default"],
) -> DeleteGroupStorageResult:
    group = (
        connection.execute(
            select(groups_table.c.name, groups_table.c.parent_id)
            .where(groups_table.c.id == group_id)
            .with_for_update()
        )
        .mappings()
        .first()
    )
    if not group:
        return None, GROUP_NOT_FOUND
    if _row_str(group, "name").lower() == "default":
        return None, DEFAULT_GROUP

    stored_parent = group["parent_id"]
    parent_id = (
        None if stored_parent is None else _int_value(stored_parent, "parent id")
    )
    has_children = connection.execute(
        select(groups_table.c.id).where(groups_table.c.parent_id == group_id).limit(1)
    ).first()
    if has_children:
        return None, GROUP_HAS_CHILDREN

    # The bookmark gate is already held before this positional lock.
    _lock_sibling_sets(connection, parent_id)

    moved = 0
    deleted_count = 0
    if url_action == "move_to_default":
        default_group_id = connection.scalar(
            select(groups_table.c.id).where(
                _case_insensitive_match(groups_table.c.name, "default")
            )
        )
        if default_group_id is None:
            raise RuntimeError("Default group is missing.")
        default_id = _int_value(default_group_id, "default group id")
        # RETURNING rather than rowcount: SQLAlchemy only pre-caches rowcount
        # for UPDATE/DELETE, so an INSERT reports -1 once its cursor is closed.
        move_result = connection.execute(
            pg_insert(url_groups_table)
            .from_select(
                ["url_id", "group_id"],
                select(
                    url_groups_table.c.url_id,
                    literal(default_id),
                ).where(url_groups_table.c.group_id == group_id),
            )
            .on_conflict_do_nothing()
            .returning(url_groups_table.c.url_id)
        )
        moved = len(move_result.all())
        connection.execute(
            delete(url_groups_table).where(url_groups_table.c.group_id == group_id)
        )
    else:
        delete_result = connection.execute(
            delete(urls_table).where(
                exists(
                    select(url_groups_table.c.url_id).where(
                        url_groups_table.c.url_id == urls_table.c.id,
                        url_groups_table.c.group_id == group_id,
                    )
                ),
                ~exists(
                    select(url_groups_table.c.url_id).where(
                        url_groups_table.c.url_id == urls_table.c.id,
                        url_groups_table.c.group_id != group_id,
                    )
                ),
            )
        )
        deleted_count = delete_result.rowcount
        connection.execute(
            delete(url_groups_table).where(url_groups_table.c.group_id == group_id)
        )

    connection.execute(
        delete(group_domains_table).where(group_domains_table.c.group_id == group_id)
    )
    connection.execute(delete(groups_table).where(groups_table.c.id == group_id))
    # Only the deleted group's own siblings close the gap; every other sibling
    # set is numbered independently.
    _write_group_order(connection, _sibling_ids(connection, parent_id))

    return (
        {
            "group_id": group_id,
            "url_action": url_action,
            "moved": moved,
            "deleted": deleted_count,
        },
        None,
    )


def _set_group_nsfw_on(
    connection: Connection,
    group_id: int,
    nsfw: bool,
) -> None:
    connection.execute(
        update(groups_table).where(groups_table.c.id == group_id).values(nsfw=nsfw)
    )


def _set_group_domains(  # pyright: ignore[reportUnusedFunction]
    group_id: int,
    domains: Sequence[str],
) -> None:
    """Replace group domains through the legacy internal transaction boundary."""
    with _bookmark_mutation() as connection:
        _set_group_domains_on(connection, group_id, domains)


def _set_group_domains_on(
    connection: Connection,
    group_id: int,
    domains: Sequence[str],
) -> None:
    _replace_group_domains(connection, group_id, domains)


def update_url_created_at(url_id: int, created_at: datetime) -> None:
    with _bookmark_mutation() as connection:
        _set_url_created_at_on(connection, url_id, created_at)


def _set_url_created_at_on(
    connection: Connection,
    url_id: int,
    created_at: datetime,
) -> None:
    connection.execute(
        update(urls_table)
        .where(urls_table.c.id == url_id)
        .values(created_at=created_at)
    )


def update_url_title(url_id: int, title: str) -> URLRecord | None:
    with _bookmark_mutation() as connection:
        return _update_url_title_on(connection, url_id, title)


def _update_url_title_on(
    connection: Connection,
    url_id: int,
    title: str,
) -> URLRecord | None:
    result = connection.execute(
        update(urls_table)
        .where(urls_table.c.id == url_id)
        .values(title=title, version=urls_table.c.version + 1)
    )
    if result.rowcount != 1:
        return None
    updated = _url_record_by_id(connection, url_id)
    return _url_record(updated) if updated is not None else None


def update_url_record(
    url_id: int,
    *,
    expected_version: int,
    fields: UpdateURLFields,
) -> UpdateURLResult:
    """Conditionally update submitted URL fields using optimistic locking."""
    if not fields:
        raise ValueError("At least one URL field is required.")

    try:
        with _bookmark_mutation() as connection:
            return _update_url_record_on(
                connection,
                url_id,
                expected_version=expected_version,
                fields=fields,
            )
    except IntegrityError:
        return None, URL_CONFLICT


def _update_url_record_on(
    connection: Connection,
    url_id: int,
    *,
    expected_version: int,
    fields: UpdateURLFields,
) -> UpdateURLResult:
    values: dict[str, object] = dict(fields)
    values["version"] = urls_table.c.version + 1
    result = connection.execute(
        update(urls_table)
        .where(
            urls_table.c.id == url_id,
            urls_table.c.version == expected_version,
        )
        .values(**values)
    )
    if result.rowcount != 1:
        existing = connection.execute(
            select(urls_table.c.id).where(urls_table.c.id == url_id)
        ).first()
        if existing is None:
            return None, URL_NOT_FOUND
        return None, URL_VERSION_CONFLICT
    updated = _url_record_by_id(connection, url_id)
    if updated is None:
        raise RuntimeError("Updated URL could not be read.")
    return _url_record(updated), None


def set_url_important(url_id: int, important: bool) -> URLRecord | None:
    with _bookmark_mutation() as connection:
        return _set_url_important_on(connection, url_id, important)


def _set_url_important_on(
    connection: Connection,
    url_id: int,
    important: bool,
) -> URLRecord | None:
    result = connection.execute(
        update(urls_table).where(urls_table.c.id == url_id).values(important=important)
    )
    if result.rowcount != 1:
        return None
    updated = _url_record_by_id(connection, url_id)
    return _url_record(updated) if updated is not None else None


def add_url(url: str, title: str | None = None) -> URLRecord | None:
    """Insert a URL, returning its record or None if it already existed."""
    try:
        with _bookmark_mutation() as connection:
            return _add_url_on(connection, url, title=title)
    except IntegrityError:
        return None


def _add_url_on(
    connection: Connection,
    url: str,
    *,
    title: str | None = None,
) -> URLRecord:
    domain = domain_for_url(url)
    matching_group_ids = (
        list(
            connection.execute(
                select(group_domains_table.c.group_id).where(
                    group_domains_table.c.domain == domain
                )
            ).scalars()
        )
        if domain is not None
        else []
    )
    if not matching_group_ids:
        default_group_id = connection.scalar(
            select(groups_table.c.id).where(groups_table.c.name == "default")
        )
        if default_group_id is None:
            raise RuntimeError("Default group is missing.")
        matching_group_ids = [default_group_id]
    result = connection.execute(insert(urls_table).values(url=url, title=title))
    url_id = _primary_key_id(result.inserted_primary_key)
    for group_id_value in matching_group_ids:
        _insert_url_group(
            connection,
            url_id,
            _int_value(group_id_value, "matched group id"),
        )
    row = _url_record_by_id(connection, url_id)
    if row is None:
        raise RuntimeError("Inserted URL could not be read.")
    return _url_record(row)


def move_url_to_group(
    url_id: int,
    group_id: int,
    source_group_id: int | None = None,
) -> MoveURLGroupResult:
    """Move one URL membership from a source group to a target group.

    The source may be omitted only when the URL currently belongs to exactly
    one group. Existing memberships in every other group remain untouched.
    """
    with _bookmark_mutation() as connection:
        return _move_url_to_group_on(
            connection,
            url_id,
            group_id,
            source_group_id=source_group_id,
        )


def _move_url_to_group_on(
    connection: Connection,
    url_id: int,
    group_id: int,
    *,
    source_group_id: int | None = None,
) -> MoveURLGroupResult:
    row = _url_record_by_id(connection, url_id)
    if not row:
        return None, MOVE_URL_NOT_FOUND

    group = connection.execute(
        select(groups_table.c.id).where(groups_table.c.id == group_id)
    ).first()
    if not group:
        return None, MOVE_GROUP_NOT_FOUND

    current_group_ids = list(
        connection.execute(
            select(url_groups_table.c.group_id).where(
                url_groups_table.c.url_id == url_id
            )
        ).scalars()
    )
    if source_group_id is None:
        if len(current_group_ids) != 1:
            return None, MOVE_SOURCE_REQUIRED
        source_group_id = _int_value(current_group_ids[0], "source group id")
    if source_group_id not in current_group_ids:
        return None, MOVE_SOURCE_NOT_FOUND
    if source_group_id == group_id:
        return {
            "url": _url_record(row),
            "source_group_id": source_group_id,
        }, None

    _insert_url_group(connection, url_id, group_id)
    connection.execute(
        delete(url_groups_table).where(
            url_groups_table.c.url_id == url_id,
            url_groups_table.c.group_id == source_group_id,
        )
    )
    return {
        "url": _url_record(row),
        "source_group_id": source_group_id,
    }, None


def _insert_url_group(connection: Connection, url_id: int, group_id: int) -> bool:
    result = connection.execute(
        pg_insert(url_groups_table)
        .values(url_id=url_id, group_id=group_id)
        .on_conflict_do_nothing()
        .returning(url_groups_table.c.url_id)
    )
    return result.first() is not None


def _add_url_to_group_on(
    connection: Connection,
    url_id: int,
    group_id: int,
) -> None:
    _insert_url_group(connection, url_id, group_id)


def _set_url_group_on(
    connection: Connection,
    url_id: int,
    group_id: int,
) -> None:
    connection.execute(
        delete(url_groups_table).where(url_groups_table.c.url_id == url_id)
    )
    _insert_url_group(connection, url_id, group_id)


def update_group_order(parent_id: int | None, group_ids: Sequence[int]) -> bool:
    """Reorder one sibling set, returning False if the order is invalid.

    The list must name every direct child of `parent_id` exactly once and
    nothing else; `None` addresses the roots. Reordering is sibling-scoped, so
    an ID from another parent is as invalid as an unknown one.
    """
    try:
        with _bookmark_mutation() as connection:
            return _update_group_order_on(connection, parent_id, group_ids)
    except IntegrityError:
        return False


def _update_group_order_on(
    connection: Connection,
    parent_id: int | None,
    group_ids: Sequence[int],
) -> bool:
    _lock_sibling_sets(connection, parent_id)
    if parent_id is not None:
        parent = connection.execute(
            select(groups_table.c.id).where(groups_table.c.id == parent_id)
        ).first()
        if parent is None:
            return False

    existing_ids = _sibling_ids(connection, parent_id)
    if len(group_ids) != len(existing_ids):
        return False
    if set(group_ids) != set(existing_ids):
        return False

    _write_group_order(connection, group_ids)
    return True


def _write_group_order(
    connection: Connection,
    group_ids: Sequence[int],
) -> None:
    for position, group_id in enumerate(group_ids):
        connection.execute(
            update(groups_table)
            .where(groups_table.c.id == group_id)
            .values(position=-(position + 1))
        )
    for position, group_id in enumerate(group_ids):
        connection.execute(
            update(groups_table)
            .where(groups_table.c.id == group_id)
            .values(position=position)
        )


def remove_url_by_id(url_id: int, group_id: int) -> URLRecord | None:
    """Remove a URL from one group, deleting the URL after its last membership."""
    with _bookmark_mutation() as connection:
        return _remove_url_by_id_on(connection, url_id, group_id)


def _remove_url_by_id_on(
    connection: Connection,
    url_id: int,
    group_id: int,
) -> URLRecord | None:
    row = _url_record_by_id(connection, url_id)
    if row is None:
        return None
    record = _url_record(row)

    current_group_ids = list(
        connection.execute(
            select(url_groups_table.c.group_id).where(
                url_groups_table.c.url_id == url_id
            )
        ).scalars()
    )
    if group_id not in current_group_ids:
        return None

    connection.execute(
        delete(url_groups_table).where(
            url_groups_table.c.url_id == url_id,
            url_groups_table.c.group_id == group_id,
        )
    )
    if len(current_group_ids) == 1:
        connection.execute(delete(urls_table).where(urls_table.c.id == url_id))
    return record
