"""Protected Bookmark restore operations on a coordinator-supplied connection.

This adapter never acquires or finalizes a transaction. Only Backup persistence
binds it, so all cross-feature work stays in the coordinator's root scope.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import assert_never

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from . import domain
from .persistence import (
    PostgresBookmarkRepository,
    PostgresGroupRepository,
    groups_table,
    url_record,
    urls_table,
)


@dataclass(frozen=True, slots=True)
class PostgresBookmarkSnapshotContributor:
    connection: Connection

    def list_groups(self) -> tuple[domain.GroupRecord, ...]:
        return PostgresGroupRepository(self.connection).list_groups()


@dataclass(frozen=True, slots=True)
class PostgresBookmarkBackupContributor:
    connection: Connection

    def locked_groups(self) -> tuple[domain.GroupRecord, ...]:
        # The logical gate is already held; lock current group rows in stable
        # ID order before the shared multi-query forest used by validation.
        self.connection.execute(
            select(groups_table.c.id).order_by(groups_table.c.id).with_for_update()
        ).all()
        return self.list_groups()

    def list_groups(self) -> tuple[domain.GroupRecord, ...]:
        return PostgresGroupRepository(self.connection).list_groups()

    def restore_group(
        self,
        name: str,
        nsfw: bool,
        domains: tuple[str, ...],
        existing: domain.GroupRecord | None,
    ) -> domain.GroupRecord:
        repository = PostgresGroupRepository(self.connection)
        if existing is None:
            outcome = repository.create_group(domain.CreateGroup(name, nsfw, domains))
            match outcome:
                case domain.GroupCreated(record=record):
                    return record
                case (
                    domain.GroupNameConflict()
                    | domain.ParentNotFound()
                    | domain.ParentIsSelfOrDescendant()
                    | domain.GroupDepthExceeded()
                ):
                    raise RuntimeError("Imported group could not be created.")
            assert_never(outcome)
        if existing.nsfw != nsfw:
            repository.set_group_nsfw(existing.id, nsfw)
        if existing.domains != domains:
            repository.set_group_domains(existing.id, domains)
        return existing

    def detach_group(self, group_id: int) -> None:
        self._reparent(group_id, None)

    def attach_group(self, group_id: int, parent_id: int) -> None:
        self._reparent(group_id, parent_id)

    def _reparent(self, group_id: int, parent_id: int | None) -> None:
        outcome = PostgresGroupRepository(self.connection).update_group(
            domain.UpdateGroup(group_id, parent_id=parent_id)
        )
        match outcome:
            case domain.GroupUpdated():
                return
            case (
                domain.GroupNotFound()
                | domain.DefaultGroupProtected()
                | domain.GroupNameConflict()
                | domain.ParentNotFound()
                | domain.ParentIsSelfOrDescendant()
                | domain.GroupDepthExceeded()
            ):
                raise RuntimeError("Imported group could not be reparented.")
        assert_never(outcome)

    def reorder_siblings(
        self, parent_id: int | None, group_ids: tuple[int, ...]
    ) -> None:
        outcome = PostgresGroupRepository(self.connection).reorder_groups(
            domain.ReorderGroups(parent_id, group_ids)
        )
        match outcome:
            case domain.GroupsReordered():
                return
            case domain.InvalidGroupOrder():
                raise RuntimeError("Imported group order could not be restored.")
        assert_never(outcome)

    def insert_url(self, url: str, title: str | None) -> domain.URLRecord | None:
        row = (
            self.connection.execute(
                pg_insert(urls_table)
                .values(url=url, title=title)
                .on_conflict_do_nothing(index_elements=[urls_table.c.url])
                .returning(*urls_table.c)
            )
            .mappings()
            .first()
        )
        return None if row is None else url_record(row)

    def restore_membership(self, url_id: int, group_id: int, *, replace: bool) -> None:
        repository = PostgresBookmarkRepository(self.connection)
        if replace:
            repository.replace_memberships(url_id, group_id)
        else:
            repository.add_membership(url_id, group_id)

    def restore_url_metadata(
        self, url_id: int, created_at: datetime, important: bool
    ) -> None:
        self.connection.execute(
            update(urls_table)
            .where(urls_table.c.id == url_id)
            .values(created_at=created_at)
        )
        outcome = PostgresBookmarkRepository(self.connection).set_important(
            domain.SetImportant(url_id, important)
        )
        match outcome:
            case domain.SetImportantSucceeded():
                return
            case domain.URLNotFound():
                raise RuntimeError("Imported URL no longer exists.")
        assert_never(outcome)
