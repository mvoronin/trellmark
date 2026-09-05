"""Framework-free portable-document workflows and coordinator ports."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Protocol, TypeVar

from ..bookmarks.domain import GroupRecord, URLRecord
from . import domain

ResultT = TypeVar("ResultT")


class WorkRunner(Protocol):
    async def run(self, work: Callable[[], ResultT]) -> ResultT: ...


class BookmarkBackupContributor(Protocol):
    def locked_groups(self) -> tuple[GroupRecord, ...]: ...
    def list_groups(self) -> tuple[GroupRecord, ...]: ...
    def restore_group(
        self,
        name: str,
        nsfw: bool,
        domains: tuple[str, ...],
        existing: GroupRecord | None,
    ) -> GroupRecord: ...
    def detach_group(self, group_id: int) -> None: ...
    def attach_group(self, group_id: int, parent_id: int) -> None: ...
    def reorder_siblings(
        self, parent_id: int | None, group_ids: tuple[int, ...]
    ) -> None: ...
    def insert_url(self, url: str, title: str | None) -> URLRecord | None: ...
    def restore_membership(
        self, url_id: int, group_id: int, *, replace: bool
    ) -> None: ...
    def restore_url_metadata(
        self, url_id: int, created_at: datetime, important: bool
    ) -> None: ...


class BackupUnitOfWork(Protocol):
    @property
    def bookmarks(self) -> BookmarkBackupContributor: ...
    def __enter__(self) -> "BackupUnitOfWork": ...
    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...
    def commit(self) -> None: ...


class BackupUnitOfWorkFactory(Protocol):
    def __call__(self) -> BackupUnitOfWork: ...


class BookmarkSnapshotContributor(Protocol):
    def list_groups(self) -> tuple[GroupRecord, ...]: ...


class ExportSnapshot(Protocol):
    @property
    def bookmarks(self) -> BookmarkSnapshotContributor: ...
    def __enter__(self) -> "ExportSnapshot": ...
    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


class ExportSnapshotFactory(Protocol):
    def __call__(self) -> ExportSnapshot: ...


@dataclass(frozen=True, slots=True)
class ImportSucceeded:
    imported: int
    skipped: int
    groups: tuple[GroupRecord, ...]


@dataclass(frozen=True, slots=True)
class ImportInvalid:
    """The resulting hierarchy is invalid; no mutation was attempted."""


type ImportOutcome = ImportSucceeded | ImportInvalid


@dataclass(frozen=True, slots=True)
class BackupApplicationService:
    work_runner: WorkRunner
    uow_factory: BackupUnitOfWorkFactory
    snapshot_factory: ExportSnapshotFactory

    async def import_document(self, document: domain.PortableDocument) -> ImportOutcome:
        return await self.work_runner.run(
            lambda: import_document_in_uow(self.uow_factory, document)
        )

    async def export_document(self, exported_at: str) -> domain.PortableDocument:
        return await self.work_runner.run(
            lambda: export_document_in_snapshot(self.snapshot_factory, exported_at)
        )


def export_document_in_snapshot(
    factory: ExportSnapshotFactory, exported_at: str
) -> domain.PortableDocument:
    with factory() as snapshot:
        groups = _flatten(snapshot.bookmarks.list_groups())
        names_by_id = {group.id: group.name for group in groups}
        return domain.PortableDocument(
            version=1,
            exported_at=exported_at,
            groups=tuple(
                domain.PortableGroup(
                    name=group.name,
                    parent=None
                    if group.parent_id is None
                    else names_by_id[group.parent_id],
                    position=group.position,
                    nsfw=group.nsfw,
                    domains=group.domains,
                    urls=tuple(
                        domain.PortableURL(
                            url.url, url.title, url.created_at, url.important
                        )
                        for url in group.urls
                    ),
                )
                for group in groups
            ),
        )


def import_document_in_uow(
    factory: BackupUnitOfWorkFactory, document: domain.PortableDocument
) -> ImportOutcome:
    """Use an already-normalized document within one complete coordinator scope."""
    with factory() as uow:
        existing = uow.bookmarks.locked_groups()
        try:
            domain.validate_resulting_import_hierarchy(document.groups, existing)
        except domain.InvalidImportDocument:
            return ImportInvalid()
        result = _restore_bookmarks(uow.bookmarks, document, existing)
        uow.commit()
        return result


def _flatten(groups: tuple[GroupRecord, ...]) -> tuple[GroupRecord, ...]:
    return tuple(
        record for group in groups for record in (group, *_flatten(group.children))
    )


def _restore_bookmarks(
    contributor: BookmarkBackupContributor,
    document: domain.PortableDocument,
    existing: tuple[GroupRecord, ...],
) -> ImportSucceeded:
    groups_by_name = {group.name.lower(): group for group in _flatten(existing)}
    for group in document.groups:
        key = group.name.lower()
        stored = groups_by_name.get(key)
        if (
            stored is None
            or stored.nsfw != group.nsfw
            or stored.domains != group.domains
        ):
            groups_by_name[key] = contributor.restore_group(
                group.name, group.nsfw, group.domains, stored
            )

    desired_parents = {
        group.name.lower(): (
            None if group.parent is None else groups_by_name[group.parent.lower()].id
        )
        for group in document.groups
    }
    # Detach deepest imported groups before rebuilding edges, so valid final
    # ancestry does not fail against a temporary old descendant relationship.
    for group in sorted(groups_by_name.values(), key=lambda item: -item.depth):
        if group.name.lower() in desired_parents and group.parent_id is not None:
            contributor.detach_group(group.id)
    for group in document.groups:
        key = group.name.lower()
        parent_id = desired_parents[key]
        if parent_id is not None:
            contributor.attach_group(groups_by_name[key].id, parent_id)

    current_by_parent: dict[int | None, list[int]] = {}
    for group in _flatten(contributor.list_groups()):
        current_by_parent.setdefault(group.parent_id, []).append(group.id)
    imported_by_parent: dict[int | None, list[tuple[int, int]]] = {}
    for group in document.groups:
        key = group.name.lower()
        imported_by_parent.setdefault(desired_parents[key], []).append(
            (group.position, groups_by_name[key].id)
        )
    for parent_id, positioned in imported_by_parent.items():
        imported_ids = tuple(group_id for _, group_id in sorted(positioned))
        current = current_by_parent.get(parent_id)
        if current is None:
            raise RuntimeError("Imported sibling set no longer exists.")
        contributor.reorder_siblings(
            parent_id,
            imported_ids
            + tuple(group_id for group_id in current if group_id not in imported_ids),
        )

    imported = skipped = 0
    imported_urls: dict[str, int] = {}
    for group in document.groups:
        group_id = groups_by_name[group.name.lower()].id
        for record in group.urls:
            existing_id = imported_urls.get(record.url)
            if existing_id is not None:
                contributor.restore_membership(existing_id, group_id, replace=False)
                continue
            inserted = contributor.insert_url(record.url, record.title)
            if inserted is None:
                skipped += 1
                continue
            contributor.restore_membership(inserted.id, group_id, replace=True)
            imported_urls[record.url] = inserted.id
            contributor.restore_url_metadata(
                inserted.id,
                domain.import_timestamp(record.created_at),
                record.important,
            )
            imported += 1
    return ImportSucceeded(imported, skipped, contributor.list_groups())
