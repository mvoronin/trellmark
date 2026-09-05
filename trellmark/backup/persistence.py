"""Backup-owned root transaction and connection lifetime."""

from collections.abc import Callable
from dataclasses import dataclass, field
from types import TracebackType

from sqlalchemy import func, select
from sqlalchemy.engine import Connection, Engine, Transaction

from ..bookmarks.backup import (
    PostgresBookmarkBackupContributor,
    PostgresBookmarkSnapshotContributor,
)
from ..bookmarks.domain import BookmarkMutationConflict
from ..bookmarks.persistence import (
    BOOKMARK_MUTATION_LOCK_KEY,
    BOOKMARK_MUTATION_LOCK_NAMESPACE,
)
from .application import BookmarkBackupContributor, BookmarkSnapshotContributor

type EngineFactory = Callable[[], Engine]
type BackupContributorFactory = Callable[[Connection], BookmarkBackupContributor]
type SnapshotContributorFactory = Callable[[Connection], BookmarkSnapshotContributor]


@dataclass(slots=True)
class PostgresBackupUnitOfWork:
    engine_factory: EngineFactory
    contributor_factory: BackupContributorFactory = PostgresBookmarkBackupContributor
    bookmarks: BookmarkBackupContributor = field(init=False)
    _connection: Connection | None = field(init=False, default=None)
    _transaction: Transaction | None = field(init=False, default=None)

    def __enter__(self) -> "PostgresBackupUnitOfWork":
        if self._connection is not None:
            raise RuntimeError("Backup unit of work is already active.")
        self._connection = connection = self.engine_factory().connect()
        try:
            self._transaction = connection.begin()
            acquired = connection.scalar(
                select(
                    func.pg_try_advisory_xact_lock(
                        BOOKMARK_MUTATION_LOCK_NAMESPACE, BOOKMARK_MUTATION_LOCK_KEY
                    )
                )
            )
            if acquired is not True:
                raise BookmarkMutationConflict
            self.bookmarks = self.contributor_factory(connection)
            return self
        except BaseException:
            try:
                self._close()
            except BaseException:
                # Cleanup cannot replace the original acquisition/binding error.
                pass
            raise

    def commit(self) -> None:
        if self._transaction is None or not self._transaction.is_active:
            raise RuntimeError("Backup unit of work is not active.")
        self._transaction.commit()

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        try:
            self._close()
        except BaseException:
            if exception is None:
                raise

    def _close(self) -> None:
        try:
            if self._transaction is not None and self._transaction.is_active:
                self._transaction.rollback()
        finally:
            try:
                if self._connection is not None:
                    self._connection.close()
            finally:
                self._connection = None
                self._transaction = None


@dataclass(frozen=True, slots=True)
class PostgresBackupUnitOfWorkFactory:
    engine_factory: EngineFactory
    contributor_factory: BackupContributorFactory = PostgresBookmarkBackupContributor

    def __call__(self) -> PostgresBackupUnitOfWork:
        return PostgresBackupUnitOfWork(self.engine_factory, self.contributor_factory)


@dataclass(slots=True)
class PostgresExportSnapshot:
    engine_factory: EngineFactory
    contributor_factory: SnapshotContributorFactory = (
        PostgresBookmarkSnapshotContributor
    )
    bookmarks: BookmarkSnapshotContributor = field(init=False)
    _connection: Connection | None = field(init=False, default=None)
    _transaction: Transaction | None = field(init=False, default=None)

    def __enter__(self) -> "PostgresExportSnapshot":
        if self._connection is not None:
            raise RuntimeError("Export snapshot is already active.")
        self._connection = connection = self.engine_factory().connect()
        try:
            # These per-checkout settings reset on pool return. Ordinary
            # Bookmark queries/writes keep READ COMMITTED and their own gate.
            connection.execution_options(
                isolation_level="REPEATABLE READ", postgresql_readonly=True
            )
            self._transaction = connection.begin()
            self.bookmarks = self.contributor_factory(connection)
            return self
        except BaseException:
            try:
                self._close()
            except BaseException:
                pass
            raise

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        try:
            self._close()
        except BaseException:
            if exception is None:
                raise

    def _close(self) -> None:
        try:
            if self._transaction is not None and self._transaction.is_active:
                self._transaction.rollback()
        finally:
            try:
                if self._connection is not None:
                    self._connection.close()
            finally:
                self._connection = None
                self._transaction = None


@dataclass(frozen=True, slots=True)
class PostgresExportSnapshotFactory:
    engine_factory: EngineFactory
    contributor_factory: SnapshotContributorFactory = (
        PostgresBookmarkSnapshotContributor
    )

    def __call__(self) -> PostgresExportSnapshot:
        return PostgresExportSnapshot(self.engine_factory, self.contributor_factory)
