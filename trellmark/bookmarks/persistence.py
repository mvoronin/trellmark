from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import TracebackType
from typing import Any

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
from sqlalchemy.engine import Connection, Engine, RowMapping, Transaction
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.types import UserDefinedType

from ..platform.runtime import read_connection
from .domain import (
    MAX_GROUP_DEPTH,
    UNCHANGED_PARENT,
    BookmarkMutationConflict,
    CreateGroup,
    CreateGroupOutcome,
    CreateURL,
    CreateURLOutcome,
    DefaultGroupProtected,
    DeleteGroup,
    DeleteGroupOutcome,
    EditURL,
    EditURLOutcome,
    EmptyURLEdit,
    GroupCreated,
    GroupDeleted,
    GroupDepthExceeded,
    GroupHasChildren,
    GroupHierarchyFailure,
    GroupNameConflict,
    GroupNotFound,
    GroupRecord,
    GroupsReordered,
    GroupUpdated,
    InvalidGroupOrder,
    MoveURL,
    MoveURLOutcome,
    ParentIsSelfOrDescendant,
    ParentNotFound,
    RemoveURL,
    RemoveURLOutcome,
    ReorderGroups,
    ReorderGroupsOutcome,
    SetImportant,
    SetImportantOutcome,
    SetImportantSucceeded,
    SiteIcon,
    SiteIconCacheRecord,
    UnchangedURLField,
    UpdateGroup,
    UpdateGroupOutcome,
    URLConflict,
    URLCreated,
    URLMembershipNotFound,
    URLMoved,
    URLNotFound,
    URLRecord,
    URLRemoved,
    URLSourceRequired,
    URLUpdated,
    URLVersionConflict,
    domain_for_url,
    normalize_domains,
    normalize_url,
    valid_group_order,
)

BOOKMARK_MUTATION_LOCK_NAMESPACE = 7502
BOOKMARK_MUTATION_LOCK_KEY = 0

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

_URL_RECORD_COLUMNS = (
    urls_table.c.id,
    urls_table.c.url,
    urls_table.c.title,
    urls_table.c.created_at,
    urls_table.c.important,
    urls_table.c.version,
)

type EngineFactory = Callable[[], Engine]

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


@dataclass(frozen=True, slots=True)
class PostgresSiteIconCacheRepository:
    """Connection-bound access to site_icon_cache only; never logical user state."""

    connection: Connection

    def read(self, origin: str) -> SiteIconCacheRecord | None:
        row = (
            self.connection.execute(
                select(site_icon_cache_table).where(
                    site_icon_cache_table.c.origin == origin
                )
            )
            .mappings()
            .first()
        )
        return None if row is None else _site_icon_cache_record(row)

    def success(
        self, origin: str, icon: SiteIcon, fetched_at: datetime, retry_after: datetime
    ) -> SiteIconCacheRecord:
        statement = (
            pg_insert(site_icon_cache_table)
            .values(
                origin=origin,
                icon_bytes=icon.data,
                media_type=icon.media_type,
                fetched_at=fetched_at,
                retry_after=retry_after,
            )
            .on_conflict_do_update(
                index_elements=[site_icon_cache_table.c.origin],
                set_={
                    "icon_bytes": icon.data,
                    "media_type": icon.media_type,
                    "fetched_at": fetched_at,
                    "retry_after": retry_after,
                },
            )
            .returning(*site_icon_cache_table.c)
        )
        return _site_icon_cache_record(
            self.connection.execute(statement).mappings().one()
        )

    def failure(self, origin: str, retry_after: datetime) -> SiteIconCacheRecord:
        # Failed revalidation retains successful bytes, media and fetched_at.
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
        return _site_icon_cache_record(
            self.connection.execute(statement).mappings().one()
        )


@dataclass(frozen=True, slots=True)
class PostgresSiteIconCacheQueries:
    engine_factory: EngineFactory

    def read(self, origin: str) -> SiteIconCacheRecord | None:
        connection = self.engine_factory().connect()
        try:
            record = PostgresSiteIconCacheRepository(connection).read(origin)
        except BaseException:
            try:
                connection.close()
            except BaseException:
                # A simultaneous outage during cleanup cannot replace the query
                # failure observed by the worker and the safe HTTP boundary.
                pass
            raise
        connection.close()
        return record


@dataclass(slots=True)
class PostgresDerivedStateUnitOfWork:
    """Ungated cache scope, run wholly inside the capacity-2 derived worker.

    Logical tables (groups, urls, url_groups, group_domains) are exposed only
    by PostgresLogicalBookmarkUnitOfWork. This scope binds only the cache port.
    """

    engine_factory: EngineFactory
    cache: PostgresSiteIconCacheRepository = field(init=False)
    _connection: Connection | None = field(init=False, default=None)
    _transaction: Transaction | None = field(init=False, default=None)

    def __enter__(self) -> "PostgresDerivedStateUnitOfWork":
        if self._connection is not None:
            raise RuntimeError("Derived state unit of work is already active.")
        self._connection = self.engine_factory().connect()
        try:
            self._transaction = self._connection.begin()
            self.cache = PostgresSiteIconCacheRepository(self._connection)
            return self
        except BaseException:
            try:
                self._close()
            except BaseException:
                pass
            raise

    def commit(self) -> None:
        transaction = self._transaction
        if transaction is None or not transaction.is_active:
            raise RuntimeError("Derived state unit of work is not active.")
        transaction.commit()

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        try:
            self._close()
        except BaseException:
            # An operation/commit failure keeps its exact identity through cleanup.
            if exception is None:
                raise

    def _close(self) -> None:
        connection, transaction = self._connection, self._transaction
        try:
            if transaction is not None and transaction.is_active:
                transaction.rollback()
        finally:
            try:
                if connection is not None:
                    connection.close()
            finally:
                self._connection = None
                self._transaction = None


@dataclass(frozen=True, slots=True)
class PostgresDerivedStateUnitOfWorkFactory:
    engine_factory: EngineFactory

    def __call__(self) -> PostgresDerivedStateUnitOfWork:
        return PostgresDerivedStateUnitOfWork(self.engine_factory)


def _site_icon_cache_record(row: RowMapping) -> SiteIconCacheRecord:
    icon_bytes, fetched_at = row["icon_bytes"], row["fetched_at"]
    if icon_bytes is not None and not isinstance(icon_bytes, bytes):
        raise RuntimeError("Expected bytes for icon bytes.")
    if fetched_at is not None and not isinstance(fetched_at, datetime):
        raise RuntimeError("Expected a timestamp value for fetched at.")
    retry_after = row["retry_after"]
    if not isinstance(retry_after, datetime):
        raise RuntimeError("Expected a timestamp value for retry after.")
    return SiteIconCacheRecord(
        _string_value(row["origin"], "origin"),
        icon_bytes,
        _optional_string_value(row["media_type"], "media type"),
        fetched_at,
        retry_after,
    )


@dataclass(frozen=True, slots=True)
class PostgresBookmarkRepository:
    connection: Connection

    def create_url(self, command: CreateURL) -> CreateURLOutcome:
        url = command.url
        domain = domain_for_url(url)
        matching_group_ids = (
            list(
                self.connection.execute(
                    select(group_domains_table.c.group_id).where(
                        group_domains_table.c.domain == domain
                    )
                ).scalars()
            )
            if domain is not None
            else []
        )
        if not matching_group_ids:
            default_group_id = self.connection.scalar(
                select(groups_table.c.id).where(groups_table.c.name == "default")
            )
            if default_group_id is None:
                raise RuntimeError("Default group is missing.")
            matching_group_ids = [default_group_id]
        try:
            row = (
                self.connection.execute(
                    insert(urls_table)
                    .values(url=url, title=command.title)
                    .returning(*_URL_RECORD_COLUMNS)
                )
                .mappings()
                .one()
            )
        except IntegrityError as error:
            diagnostic = getattr(error.orig, "diag", None)
            if getattr(diagnostic, "constraint_name", None) == "uq_urls_url":
                return URLConflict(url)
            raise
        record = url_record(row)
        for group_id in matching_group_ids:
            self.add_membership(record.id, _int_value(group_id, "matched group id"))
        return URLCreated(record)

    def list_urls(self) -> tuple[URLRecord, ...]:
        rows = self.connection.execute(
            select(*_URL_RECORD_COLUMNS).order_by(urls_table.c.id)
        ).mappings()
        return tuple(url_record(row) for row in rows)

    def url_by_id(self, url_id: int) -> URLRecord | None:
        row = (
            self.connection.execute(
                select(*_URL_RECORD_COLUMNS).where(urls_table.c.id == url_id)
            )
            .mappings()
            .first()
        )
        return None if row is None else url_record(row)

    def url_by_url(self, url: str) -> URLRecord | None:
        row = (
            self.connection.execute(
                select(*_URL_RECORD_COLUMNS).where(urls_table.c.url == url)
            )
            .mappings()
            .first()
        )
        return None if row is None else url_record(row)

    def url_group_ids(self, url_id: int) -> tuple[int, ...]:
        values = self.connection.execute(
            select(url_groups_table.c.group_id)
            .join(groups_table, url_groups_table.c.group_id == groups_table.c.id)
            .where(url_groups_table.c.url_id == url_id)
            .order_by(groups_table.c.position)
        ).scalars()
        return tuple(_int_value(value, "group id") for value in values)

    def edit_url(self, command: EditURL) -> EditURLOutcome:
        values: dict[str, object] = {}
        canonical_url = None
        if command.url is not None:
            canonical_url = normalize_url(command.url)
            values["url"] = canonical_url
        if command.title is not UnchangedURLField.VALUE:
            values["title"] = command.title
        if not values:
            return EmptyURLEdit(command.url_id)
        values["version"] = urls_table.c.version + 1
        try:
            row = (
                self.connection.execute(
                    update(urls_table)
                    .where(
                        urls_table.c.id == command.url_id,
                        urls_table.c.version == command.expected_version,
                    )
                    .values(**values)
                    .returning(*_URL_RECORD_COLUMNS)
                )
                .mappings()
                .first()
            )
        except IntegrityError as error:
            diagnostic = getattr(error.orig, "diag", None)
            if getattr(diagnostic, "constraint_name", None) == "uq_urls_url":
                if canonical_url is not None:
                    return URLConflict(canonical_url)
            raise
        if row is not None:
            return URLUpdated(url_record(row))
        # The conditional write is authoritative. Only a failed write needs a
        # read to distinguish missing from stale; there is no service pre-check.
        existing = self.connection.execute(
            select(urls_table.c.id).where(urls_table.c.id == command.url_id)
        ).first()
        if existing is None:
            return URLNotFound(command.url_id)
        return URLVersionConflict(command.url_id, command.expected_version)

    def move_url(self, command: MoveURL) -> MoveURLOutcome:
        record = self.url_by_id(command.url_id)
        if record is None:
            return URLNotFound(command.url_id)
        group = self.connection.execute(
            select(groups_table.c.id).where(groups_table.c.id == command.group_id)
        ).first()
        if group is None:
            return GroupNotFound(command.group_id)
        group_ids = self.url_group_ids(command.url_id)
        source = command.source_group_id
        if source is None:
            if len(group_ids) != 1:
                return URLSourceRequired(command.url_id, group_ids)
            source = group_ids[0]
        if source not in group_ids:
            return URLMembershipNotFound(command.url_id, source)
        if source != command.group_id:
            self.add_membership(command.url_id, command.group_id)
            self.connection.execute(
                delete(url_groups_table).where(
                    url_groups_table.c.url_id == command.url_id,
                    url_groups_table.c.group_id == source,
                )
            )
        return URLMoved(record, command.group_id, source)

    def remove_url(self, command: RemoveURL) -> RemoveURLOutcome:
        record = self.url_by_id(command.url_id)
        if record is None:
            return URLNotFound(command.url_id)
        group_ids = self.url_group_ids(command.url_id)
        if command.group_id not in group_ids:
            return URLMembershipNotFound(command.url_id, command.group_id)
        self.connection.execute(
            delete(url_groups_table).where(
                url_groups_table.c.url_id == command.url_id,
                url_groups_table.c.group_id == command.group_id,
            )
        )
        if len(group_ids) == 1:
            self.connection.execute(
                delete(urls_table).where(urls_table.c.id == command.url_id)
            )
        return URLRemoved(record, command.group_id)

    def add_membership(self, url_id: int, group_id: int) -> bool:
        result = self.connection.execute(
            pg_insert(url_groups_table)
            .values(url_id=url_id, group_id=group_id)
            .on_conflict_do_nothing()
            .returning(url_groups_table.c.url_id)
        )
        return result.first() is not None

    def replace_memberships(self, url_id: int, group_id: int) -> None:
        self.connection.execute(
            delete(url_groups_table).where(url_groups_table.c.url_id == url_id)
        )
        self.add_membership(url_id, group_id)

    def set_important(self, command: SetImportant) -> SetImportantOutcome:
        row = (
            self.connection.execute(
                update(urls_table)
                .where(urls_table.c.id == command.url_id)
                .values(important=command.important)
                .returning(*_URL_RECORD_COLUMNS)
            )
            .mappings()
            .first()
        )
        if row is None:
            return URLNotFound(command.url_id)
        return SetImportantSucceeded(url_record(row))


@dataclass(frozen=True, slots=True)
class PostgresURLQueries:
    engine_factory: EngineFactory

    def list_urls(self) -> tuple[URLRecord, ...]:
        with read_connection(self.engine_factory()) as connection:
            return PostgresBookmarkRepository(connection).list_urls()

    def url_by_id(self, url_id: int) -> URLRecord | None:
        with read_connection(self.engine_factory()) as connection:
            return PostgresBookmarkRepository(connection).url_by_id(url_id)

    def url_by_url(self, url: str) -> URLRecord | None:
        with read_connection(self.engine_factory()) as connection:
            return PostgresBookmarkRepository(connection).url_by_url(url)

    def url_group_ids(self, url_id: int) -> tuple[int, ...]:
        with read_connection(self.engine_factory()) as connection:
            return PostgresBookmarkRepository(connection).url_group_ids(url_id)


@dataclass(slots=True)
class PostgresLogicalBookmarkUnitOfWork:
    engine_factory: EngineFactory
    bookmarks: PostgresBookmarkRepository = field(init=False)
    groups: PostgresGroupRepository = field(init=False)
    _connection: Connection | None = field(init=False, default=None)
    _transaction: Transaction | None = field(init=False, default=None)

    def __enter__(self) -> "PostgresLogicalBookmarkUnitOfWork":
        if self._connection is not None:
            raise RuntimeError("Logical bookmark unit of work is already active.")

        connection = self.engine_factory().connect()
        self._connection = connection
        transaction: Transaction | None = None
        try:
            transaction = connection.begin()
            self._transaction = transaction
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
            self.bookmarks = PostgresBookmarkRepository(connection)
            self.groups = PostgresGroupRepository(connection)
            return self
        except BaseException:
            try:
                self._close()
            except BaseException:
                # A failed gate/bind must preserve its original exception even
                # when the same outage also prevents rollback or close.
                pass
            raise

    def commit(self) -> None:
        transaction = self._transaction
        if transaction is None or not transaction.is_active:
            raise RuntimeError("Logical bookmark unit of work is not active.")
        transaction.commit()

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        try:
            self._close()
        except BaseException:
            # Carry the operation/commit error unchanged through the worker to
            # the safe HTTP boundary. Cleanup-only errors still propagate.
            if exception is None:
                raise

    def _close(self) -> None:
        connection = self._connection
        transaction = self._transaction
        try:
            if transaction is not None and transaction.is_active:
                transaction.rollback()
        finally:
            try:
                if connection is not None:
                    connection.close()
            finally:
                self._connection = None
                self._transaction = None


@dataclass(frozen=True, slots=True)
class PostgresLogicalBookmarkUnitOfWorkFactory:
    engine_factory: EngineFactory

    def __call__(self) -> PostgresLogicalBookmarkUnitOfWork:
        return PostgresLogicalBookmarkUnitOfWork(self.engine_factory)


def url_record(row: RowMapping) -> URLRecord:
    created_at = row["created_at"]
    if not isinstance(created_at, datetime):
        raise RuntimeError("Expected a timestamp value for created_at.")
    moment = (
        created_at
        if created_at.tzinfo is not None
        else created_at.replace(tzinfo=timezone.utc)
    )
    return URLRecord(
        id=_int_value(row["id"], "id"),
        url=_string_value(row["url"], "url"),
        title=_optional_string_value(row["title"], "title"),
        created_at=moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        important=bool(row["important"]),
        version=_int_value(row["version"], "version"),
    )


def _int_value(value: object, label: str) -> int:
    if type(value) is not int:
        raise RuntimeError(f"Expected integer value for {label}.")
    return value


def _string_value(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise RuntimeError(f"Expected string value for {label}.")
    return value


def _optional_string_value(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _string_value(value, label)


SIBLING_LOCK_NAMESPACE = 7501
ROOT_SIBLING_LOCK_KEY = 0
type RowData = RowMapping


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


def _primary_key_id(primary_key: Sequence[object] | None) -> int:
    if not primary_key:
        raise RuntimeError("Insert did not return a primary key.")
    return _int_value(primary_key[0], "primary key")


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


def _validate_parent(
    connection: Connection,
    parent_id: int | None,
    *,
    moved_path: str | None = None,
) -> GroupHierarchyFailure | None:
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
        return ParentNotFound(parent_id)

    # `<@` is reflexive, so this covers "the group itself" as well as any
    # descendant of it.
    if parent["inside_subtree"]:
        return ParentIsSelfOrDescendant(parent_id)
    # The whole subtree has to fit, not just the group being moved: a group one
    # level deep that carries a grandchild needs two levels below the target.
    height = _subtree_height(connection, moved_path) if moved_path is not None else 0
    if _row_int(parent, "depth") + 1 + height > MAX_GROUP_DEPTH:
        return GroupDepthExceeded()
    return None


def _read_group_records_on(connection: Connection) -> tuple[GroupRecord, ...]:
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
            group_urls.append(url_record(row))

    domains_by_group: dict[int, list[str]] = {
        _row_int(group, "id"): [] for group in groups
    }
    for row in domain_rows:
        group_domains = domains_by_group.get(_row_int(row, "group_id"))
        if group_domains is not None:
            group_domains.append(_row_str(row, "domain"))

    rows_by_parent: dict[int | None, list[RowMapping]] = {}
    for row in groups:
        parent = row["parent_id"]
        parent_id = None if parent is None else _int_value(parent, "parent id")
        rows_by_parent.setdefault(parent_id, []).append(row)

    def build(parent_id: int | None) -> tuple[GroupRecord, ...]:
        return tuple(
            _group_record(
                row,
                domains_by_group[_row_int(row, "id")],
                urls_by_group[_row_int(row, "id")],
                children=build(_row_int(row, "id")),
            )
            for row in rows_by_parent.get(parent_id, ())
        )

    return build(None)


def _group_record(
    row: RowMapping,
    domains: Sequence[str],
    urls: Sequence[URLRecord],
    *,
    children: tuple[GroupRecord, ...] = (),
) -> GroupRecord:
    parent_id = row["parent_id"]
    return GroupRecord(
        id=_row_int(row, "id"),
        name=_row_str(row, "name"),
        parent_id=None if parent_id is None else _int_value(parent_id, "parent id"),
        position=_row_int(row, "position"),
        depth=_row_int(row, "depth"),
        nsfw=bool(row["nsfw"]),
        domains=tuple(domains),
        urls=tuple(urls),
        children=children,
    )


def _group_database_failure(
    error: DBAPIError,
    *,
    name: str | None,
    parent_id: int | None,
) -> GroupNameConflict | GroupHierarchyFailure:
    if isinstance(error, IntegrityError):
        constraint_name = getattr(
            getattr(error.orig, "diag", None), "constraint_name", None
        )
        if constraint_name == "uq_groups_name_lower" and name is not None:
            return GroupNameConflict(name)
        raise error
    sqlstate = getattr(error.orig, "sqlstate", None)
    match sqlstate:
        case "GH001":
            return ParentNotFound(parent_id)
        case "GH002":
            return ParentIsSelfOrDescendant(parent_id)
        case "GH003":
            return GroupDepthExceeded()
        case _:
            raise error


@dataclass(frozen=True, slots=True)
class PostgresGroupQueries:
    engine_factory: EngineFactory

    def list_groups(self) -> tuple[GroupRecord, ...]:
        with read_connection(self.engine_factory()) as connection:
            # Visibility and membership must come from the same snapshot even
            # when an import commits between the forest's separate queries.
            connection.execution_options(
                isolation_level="REPEATABLE READ", postgresql_readonly=True
            )
            return PostgresGroupRepository(connection).list_groups()

    def group_by_name(self, name: str) -> GroupRecord | None:
        with read_connection(self.engine_factory()) as connection:
            connection.execution_options(
                isolation_level="REPEATABLE READ", postgresql_readonly=True
            )
            return PostgresGroupRepository(connection).group_by_name(name)


@dataclass(frozen=True, slots=True)
class PostgresGroupRepository:
    connection: Connection

    def list_groups(self) -> tuple[GroupRecord, ...]:
        return _read_group_records_on(self.connection)

    def group_by_name(self, name: str) -> GroupRecord | None:
        row = (
            self.connection.execute(
                _group_record_select().where(
                    _case_insensitive_match(groups_table.c.name, name)
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        return _group_record(
            row, _read_group_domains(self.connection, _row_int(row, "id")), ()
        )

    def create_group(self, command: CreateGroup) -> CreateGroupOutcome:
        try:
            return self._create_group(command)
        except DBAPIError as error:
            return _group_database_failure(
                error, name=command.name, parent_id=command.parent_id
            )

    def update_group(self, command: UpdateGroup) -> UpdateGroupOutcome:
        try:
            return self._update_group(command)
        except DBAPIError as error:
            parent_id = (
                None if command.parent_id == UNCHANGED_PARENT else command.parent_id
            )
            return _group_database_failure(
                error, name=command.name, parent_id=parent_id
            )

    def _create_group(self, command: CreateGroup) -> CreateGroupOutcome:
        connection = self.connection
        name, nsfw, domains, parent_id = (
            command.name,
            command.nsfw,
            command.domains,
            command.parent_id,
        )
        normalized_domains = normalize_domains(domains)
        # Before `_validate_parent`, which takes row locks: set locks first
        # everywhere keeps the acquisition order consistent between this and a
        # concurrent move.
        _lock_sibling_sets(connection, parent_id)
        parent_error = _validate_parent(connection, parent_id)
        if parent_error is not None:
            return parent_error

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
            connection.execute(
                _group_record_select().where(groups_table.c.id == group_id)
            )
            .mappings()
            .first()
        )
        stored_domains = _read_group_domains(connection, group_id)
        if row is None:
            raise RuntimeError("Inserted group could not be read.")
        return GroupCreated(_group_record(row, stored_domains, []))

    def _update_group(self, command: UpdateGroup) -> UpdateGroupOutcome:
        connection = self.connection
        group_id, name, nsfw, domains, parent_id = (
            command.group_id,
            command.name,
            command.nsfw,
            command.domains,
            command.parent_id,
        )
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
            return GroupNotFound(group_id)
        if _row_str(group, "name").lower() == "default":
            return DefaultGroupProtected(group_id, "edit")

        values: dict[str, object] = {}
        if name is not None:
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
                return parent_error
            values["parent_id"] = destination
            values["position"] = len(_sibling_ids(connection, destination))

        if values:
            connection.execute(
                update(groups_table)
                .where(groups_table.c.id == group_id)
                .values(**values)
            )
        if moving:
            _write_group_order(connection, _sibling_ids(connection, old_parent_id))
        if normalized_domains is not None:
            _replace_group_domains(connection, group_id, normalized_domains)
        updated = (
            connection.execute(
                _group_record_select().where(groups_table.c.id == group_id)
            )
            .mappings()
            .first()
        )
        if updated is None:
            raise RuntimeError("Updated group could not be read.")
        return GroupUpdated(
            _group_record(updated, _read_group_domains(connection, group_id), ())
        )

    def delete_group(self, command: DeleteGroup) -> DeleteGroupOutcome:
        connection = self.connection
        group_id, url_action = command.group_id, command.url_action
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
            return GroupNotFound(group_id)
        if _row_str(group, "name").lower() == "default":
            return DefaultGroupProtected(group_id, "delete")

        stored_parent = group["parent_id"]
        parent_id = (
            None if stored_parent is None else _int_value(stored_parent, "parent id")
        )
        has_children = connection.execute(
            select(groups_table.c.id)
            .where(groups_table.c.parent_id == group_id)
            .limit(1)
        ).first()
        if has_children:
            return GroupHasChildren(group_id)

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
            delete(group_domains_table).where(
                group_domains_table.c.group_id == group_id
            )
        )
        connection.execute(delete(groups_table).where(groups_table.c.id == group_id))
        # Only the deleted group's own siblings close the gap; every other sibling
        # set is numbered independently.
        _write_group_order(connection, _sibling_ids(connection, parent_id))

        return GroupDeleted(group_id, url_action, moved, deleted_count)

    def reorder_groups(self, command: ReorderGroups) -> ReorderGroupsOutcome:
        connection = self.connection
        parent_id, group_ids = command.parent_id, command.group_ids
        _lock_sibling_sets(connection, parent_id)
        if parent_id is not None:
            parent = connection.execute(
                select(groups_table.c.id).where(groups_table.c.id == parent_id)
            ).first()
            if parent is None:
                return InvalidGroupOrder(parent_id, group_ids)
        if not valid_group_order(group_ids, _sibling_ids(connection, parent_id)):
            return InvalidGroupOrder(parent_id, group_ids)
        _write_group_order(connection, group_ids)
        return GroupsReordered(parent_id, group_ids)

    def set_group_nsfw(self, group_id: int, nsfw: bool) -> None:
        self.connection.execute(
            update(groups_table).where(groups_table.c.id == group_id).values(nsfw=nsfw)
        )

    def set_group_domains(self, group_id: int, domains: Sequence[str]) -> None:
        _replace_group_domains(self.connection, group_id, domains)
