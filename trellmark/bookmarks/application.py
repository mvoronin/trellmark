import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Protocol, TypeVar, assert_never

from .domain import (
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
    RefreshURLMetadataOutcome,
    RefreshURLTitleOutcome,
    RemoveURL,
    RemoveURLOutcome,
    ReorderGroups,
    ReorderGroupsOutcome,
    SetImportant,
    SetImportantOutcome,
    SetImportantSucceeded,
    SiteIcon,
    SiteIconCacheRecord,
    UpdateGroup,
    UpdateGroupOutcome,
    URLConflict,
    URLCreated,
    URLMembershipNotFound,
    URLMetadataRefreshed,
    URLMoved,
    URLNotFound,
    URLRecord,
    URLRemoved,
    URLSourceRequired,
    URLTitleFetchFailed,
    URLTitleRefreshed,
    URLUpdated,
    URLVersionConflict,
)

ResultT = TypeVar("ResultT")
logger = logging.getLogger(__name__)


class TitleFetcher(Protocol):
    async def __call__(self, url: str) -> str | None: ...


class SiteIconFetcher(Protocol):
    async def __call__(self, url: str) -> SiteIcon | None: ...


class SiteIconGateway(Protocol):
    async def get(self, url: str) -> SiteIcon | None: ...
    async def refresh(self, url: str) -> bool: ...
    async def wait_for_idle(self) -> None: ...


class SiteIconCache(Protocol):
    async def read(self, origin: str) -> SiteIconCacheRecord | None: ...
    async def success(
        self, origin: str, icon: SiteIcon, fetched_at: datetime, retry_after: datetime
    ) -> SiteIconCacheRecord: ...
    async def failure(
        self, origin: str, retry_after: datetime
    ) -> SiteIconCacheRecord: ...


class SiteIconCacheQueries(Protocol):
    def read(self, origin: str) -> SiteIconCacheRecord | None: ...


class SiteIconCacheRepository(Protocol):
    def success(
        self, origin: str, icon: SiteIcon, fetched_at: datetime, retry_after: datetime
    ) -> SiteIconCacheRecord: ...
    def failure(self, origin: str, retry_after: datetime) -> SiteIconCacheRecord: ...


class DerivedStateUnitOfWork(Protocol):
    @property
    def cache(self) -> SiteIconCacheRepository: ...

    def __enter__(self) -> "DerivedStateUnitOfWork": ...
    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...
    def commit(self) -> None: ...


class DerivedStateUnitOfWorkFactory(Protocol):
    def __call__(self) -> DerivedStateUnitOfWork: ...


@dataclass(frozen=True, slots=True)
class SiteIconCacheApplicationService:
    work_runner: "WorkRunner"
    derived_uow_factory: DerivedStateUnitOfWorkFactory
    queries: SiteIconCacheQueries

    async def read(self, origin: str) -> SiteIconCacheRecord | None:
        return await self.work_runner.run(lambda: self.queries.read(origin))

    async def success(
        self, origin: str, icon: SiteIcon, fetched_at: datetime, retry_after: datetime
    ) -> SiteIconCacheRecord:
        def work() -> SiteIconCacheRecord:
            with self.derived_uow_factory() as uow:
                record = uow.cache.success(origin, icon, fetched_at, retry_after)
                uow.commit()
                return record

        return await self.work_runner.run(work)

    async def failure(self, origin: str, retry_after: datetime) -> SiteIconCacheRecord:
        def work() -> SiteIconCacheRecord:
            with self.derived_uow_factory() as uow:
                record = uow.cache.failure(origin, retry_after)
                uow.commit()
                return record

        return await self.work_runner.run(work)


class WorkRunner(Protocol):
    async def run(self, work: Callable[[], ResultT]) -> ResultT: ...


class BookmarkRepository(Protocol):
    def create_url(self, command: CreateURL) -> CreateURLOutcome: ...
    def url_by_id(self, url_id: int) -> URLRecord | None: ...
    def set_important(self, command: SetImportant) -> SetImportantOutcome: ...
    def edit_url(self, command: EditURL) -> EditURLOutcome: ...
    def move_url(self, command: MoveURL) -> MoveURLOutcome: ...
    def remove_url(self, command: RemoveURL) -> RemoveURLOutcome: ...


class URLQueries(Protocol):
    def list_urls(self) -> tuple[URLRecord, ...]: ...
    def url_by_id(self, url_id: int) -> URLRecord | None: ...
    def url_by_url(self, url: str) -> URLRecord | None: ...
    def url_group_ids(self, url_id: int) -> tuple[int, ...]: ...


class GroupRepository(Protocol):
    def create_group(self, command: CreateGroup) -> CreateGroupOutcome: ...
    def update_group(self, command: UpdateGroup) -> UpdateGroupOutcome: ...
    def delete_group(self, command: DeleteGroup) -> DeleteGroupOutcome: ...
    def reorder_groups(self, command: ReorderGroups) -> ReorderGroupsOutcome: ...


class GroupQueries(Protocol):
    def list_groups(self) -> tuple[GroupRecord, ...]: ...
    def group_by_name(self, name: str) -> GroupRecord | None: ...


class LogicalBookmarkUnitOfWork(Protocol):
    @property
    def groups(self) -> GroupRepository: ...

    @property
    def bookmarks(self) -> BookmarkRepository: ...

    def __enter__(self) -> "LogicalBookmarkUnitOfWork": ...

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def commit(self) -> None: ...


class LogicalBookmarkUnitOfWorkFactory(Protocol):
    def __call__(self) -> LogicalBookmarkUnitOfWork: ...


@dataclass(frozen=True, slots=True)
class BookmarksApplicationService:
    work_runner: WorkRunner
    logical_uow_factory: LogicalBookmarkUnitOfWorkFactory

    group_queries: GroupQueries
    url_queries: URLQueries
    title_fetcher: TitleFetcher
    icon_gateway: SiteIconGateway

    async def refresh_url_metadata(self, url_id: int) -> RefreshURLMetadataOutcome:
        record = await self.url_by_id(url_id)
        if record is None:
            return URLNotFound(url_id)

        async def fetch_title() -> str | None:
            try:
                return await self.title_fetcher(record.url)
            except Exception:
                logger.warning("Unexpected page title fetch failure.", exc_info=True)
                return None

        # The gateway owns a pure fetch followed by its separate derived UoW.
        # Never contain this composite call: cache errors must reach the caller.
        title, icon_updated = await asyncio.gather(
            fetch_title(), self.icon_gateway.refresh(record.url)
        )
        if title is None:
            current = await self.url_by_id(url_id)
            if current is None:
                return URLNotFound(url_id)
            return URLMetadataRefreshed(current, False, icon_updated)

        outcome = await self.edit_url(EditURL(record.id, record.version, title=title))
        match outcome:
            case URLUpdated(record=current):
                return URLMetadataRefreshed(current, True, icon_updated)
            case URLNotFound() | URLVersionConflict():
                return outcome
            case URLConflict() | EmptyURLEdit():
                raise RuntimeError("URL metadata update failed without a reason.")
        assert_never(outcome)

    async def create_url(self, command: CreateURL) -> CreateURLOutcome:
        outcome = await self.work_runner.run(
            lambda: create_url_in_uow(self.logical_uow_factory, command)
        )
        match outcome:
            case URLConflict():
                return outcome
            case URLCreated(record=record):
                # Only the pure external port is best effort. The committed
                # insert and later database failures remain outside this catch.
                try:
                    title = await self.title_fetcher(record.url)
                except Exception:
                    logger.warning(
                        "Unexpected page title fetch failure.", exc_info=True
                    )
                    title = None
                if title is None:
                    return outcome
                updated = await self.work_runner.run(
                    lambda: update_title_in_uow(
                        self.logical_uow_factory, record.id, title
                    )
                )
                return URLCreated(updated or record)
        assert_never(outcome)

    async def refresh_url_title(self, url_id: int) -> RefreshURLTitleOutcome:
        record = await self.url_by_id(url_id)
        if record is None:
            return URLNotFound(url_id)
        try:
            title = await self.title_fetcher(record.url)
        except Exception:
            logger.warning("Unexpected page title fetch failure.", exc_info=True)
            return URLTitleFetchFailed(url_id)
        if title is None:
            return URLTitleRefreshed(record, False)
        outcome = await self.edit_url(EditURL(record.id, record.version, title=title))
        match outcome:
            case URLUpdated(record=updated):
                return URLTitleRefreshed(updated, True)
            case URLNotFound() | URLVersionConflict():
                return outcome
            case URLConflict() | EmptyURLEdit():
                raise RuntimeError("URL title update failed without a reason.")
        assert_never(outcome)

    async def list_urls(self) -> tuple[URLRecord, ...]:
        return await self.work_runner.run(self.url_queries.list_urls)

    async def url_by_id(self, url_id: int) -> URLRecord | None:
        return await self.work_runner.run(lambda: self.url_queries.url_by_id(url_id))

    async def url_by_url(self, url: str) -> URLRecord | None:
        return await self.work_runner.run(lambda: self.url_queries.url_by_url(url))

    async def url_group_ids(self, url_id: int) -> tuple[int, ...]:
        return await self.work_runner.run(
            lambda: self.url_queries.url_group_ids(url_id)
        )

    async def edit_url(self, command: EditURL) -> EditURLOutcome:
        return await self.work_runner.run(
            lambda: edit_url_in_uow(self.logical_uow_factory, command)
        )

    async def move_url(self, command: MoveURL) -> MoveURLOutcome:
        return await self.work_runner.run(
            lambda: move_url_in_uow(self.logical_uow_factory, command)
        )

    async def remove_url(self, command: RemoveURL) -> RemoveURLOutcome:
        return await self.work_runner.run(
            lambda: remove_url_in_uow(self.logical_uow_factory, command)
        )

    async def list_groups(self) -> tuple[GroupRecord, ...]:
        return await self.work_runner.run(self.group_queries.list_groups)

    async def group_by_name(self, name: str) -> GroupRecord | None:
        return await self.work_runner.run(
            lambda: self.group_queries.group_by_name(name)
        )

    async def set_important(self, command: SetImportant) -> SetImportantOutcome:
        return await self.work_runner.run(
            lambda: set_important_in_uow(self.logical_uow_factory, command)
        )

    async def create_group(self, command: CreateGroup) -> CreateGroupOutcome:
        return await self.work_runner.run(
            lambda: create_group_in_uow(self.logical_uow_factory, command)
        )

    async def update_group(self, command: UpdateGroup) -> UpdateGroupOutcome:
        return await self.work_runner.run(
            lambda: update_group_in_uow(self.logical_uow_factory, command)
        )

    async def delete_group(self, command: DeleteGroup) -> DeleteGroupOutcome:
        return await self.work_runner.run(
            lambda: delete_group_in_uow(self.logical_uow_factory, command)
        )

    async def reorder_groups(self, command: ReorderGroups) -> ReorderGroupsOutcome:
        return await self.work_runner.run(
            lambda: reorder_groups_in_uow(self.logical_uow_factory, command)
        )


def create_url_in_uow(
    logical_uow_factory: LogicalBookmarkUnitOfWorkFactory, command: CreateURL
) -> CreateURLOutcome:
    with logical_uow_factory() as unit_of_work:
        outcome = unit_of_work.bookmarks.create_url(command)
        match outcome:
            case URLCreated():
                unit_of_work.commit()
                return outcome
            case URLConflict():
                return outcome
        assert_never(outcome)


def update_title_in_uow(
    logical_uow_factory: LogicalBookmarkUnitOfWorkFactory, url_id: int, title: str
) -> URLRecord | None:
    with logical_uow_factory() as unit_of_work:
        record = unit_of_work.bookmarks.url_by_id(url_id)
        if record is None:
            return None
        # Create has no caller-supplied expected version. Re-read after the gate
        # and preserve its existing last-title-write behavior on the current row.
        outcome = unit_of_work.bookmarks.edit_url(
            EditURL(record.id, record.version, title=title)
        )
        match outcome:
            case URLUpdated(record=updated):
                unit_of_work.commit()
                return updated
            case URLNotFound():
                return None
            case URLVersionConflict():
                # Logical writers cannot race while this UoW owns the gate.
                raise RuntimeError("URL changed inside the title update boundary.")
            case URLConflict() | EmptyURLEdit():
                raise RuntimeError("URL title update failed without a reason.")
        assert_never(outcome)


def edit_url_in_uow(
    logical_uow_factory: LogicalBookmarkUnitOfWorkFactory, command: EditURL
) -> EditURLOutcome:
    with logical_uow_factory() as unit_of_work:
        outcome = unit_of_work.bookmarks.edit_url(command)
        match outcome:
            case URLUpdated():
                unit_of_work.commit()
                return outcome
            case URLNotFound() | URLConflict() | URLVersionConflict() | EmptyURLEdit():
                return outcome
        assert_never(outcome)


def move_url_in_uow(
    logical_uow_factory: LogicalBookmarkUnitOfWorkFactory, command: MoveURL
) -> MoveURLOutcome:
    with logical_uow_factory() as unit_of_work:
        outcome = unit_of_work.bookmarks.move_url(command)
        match outcome:
            case URLMoved():
                unit_of_work.commit()
                return outcome
            case (
                URLNotFound()
                | GroupNotFound()
                | URLSourceRequired()
                | URLMembershipNotFound()
            ):
                return outcome
        assert_never(outcome)


def remove_url_in_uow(
    logical_uow_factory: LogicalBookmarkUnitOfWorkFactory, command: RemoveURL
) -> RemoveURLOutcome:
    with logical_uow_factory() as unit_of_work:
        outcome = unit_of_work.bookmarks.remove_url(command)
        match outcome:
            case URLRemoved():
                unit_of_work.commit()
                return outcome
            case URLNotFound() | URLMembershipNotFound():
                return outcome
        assert_never(outcome)


def set_important_in_uow(
    logical_uow_factory: LogicalBookmarkUnitOfWorkFactory, command: SetImportant
) -> SetImportantOutcome:
    with logical_uow_factory() as unit_of_work:
        outcome = unit_of_work.bookmarks.set_important(command)
        match outcome:
            case SetImportantSucceeded():
                unit_of_work.commit()
                return outcome
            case URLNotFound():
                return outcome
        assert_never(outcome)


def reorder_groups_in_uow(
    logical_uow_factory: LogicalBookmarkUnitOfWorkFactory, command: ReorderGroups
) -> ReorderGroupsOutcome:
    with logical_uow_factory() as unit_of_work:
        outcome = unit_of_work.groups.reorder_groups(command)
        match outcome:
            case GroupsReordered():
                unit_of_work.commit()
                return outcome
            case InvalidGroupOrder():
                return outcome
        assert_never(outcome)


def create_group_in_uow(
    logical_uow_factory: LogicalBookmarkUnitOfWorkFactory, command: CreateGroup
) -> CreateGroupOutcome:
    with logical_uow_factory() as unit_of_work:
        outcome = unit_of_work.groups.create_group(command)
        match outcome:
            case GroupCreated():
                unit_of_work.commit()
                return outcome
            case (
                GroupNameConflict()
                | ParentNotFound()
                | ParentIsSelfOrDescendant()
                | GroupDepthExceeded()
            ):
                return outcome
        assert_never(outcome)


def update_group_in_uow(
    logical_uow_factory: LogicalBookmarkUnitOfWorkFactory, command: UpdateGroup
) -> UpdateGroupOutcome:
    with logical_uow_factory() as unit_of_work:
        outcome = unit_of_work.groups.update_group(command)
        match outcome:
            case GroupUpdated():
                unit_of_work.commit()
                return outcome
            case (
                GroupNotFound()
                | DefaultGroupProtected()
                | GroupNameConflict()
                | ParentNotFound()
                | ParentIsSelfOrDescendant()
                | GroupDepthExceeded()
            ):
                return outcome
        assert_never(outcome)


def delete_group_in_uow(
    logical_uow_factory: LogicalBookmarkUnitOfWorkFactory, command: DeleteGroup
) -> DeleteGroupOutcome:
    with logical_uow_factory() as unit_of_work:
        outcome = unit_of_work.groups.delete_group(command)
        match outcome:
            case GroupDeleted():
                unit_of_work.commit()
                return outcome
            case GroupNotFound() | DefaultGroupProtected() | GroupHasChildren():
                return outcome
        assert_never(outcome)
