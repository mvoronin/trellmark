from dataclasses import FrozenInstanceError

import pytest
from anyio import CapacityLimiter, run

from tests.bookmarks.helpers import make_icon_service
from tests.helpers import RecordingTitleFetcher
from trellmark.bookmarks import domain
from trellmark.bookmarks.application import BookmarksApplicationService
from trellmark.bookmarks.persistence import PostgresLogicalBookmarkUnitOfWorkFactory
from trellmark.platform.runtime import AnyIOWorkRunner, get_engine


def test_group_service_returns_immutable_recursive_records_and_typed_rejections(
    database,
):
    assert hasattr(domain, "CreateGroup"), "Group commands must belong to the core"
    from trellmark.bookmarks.persistence import PostgresGroupQueries, PostgresURLQueries

    async def exercise():
        service = BookmarksApplicationService(
            AnyIOWorkRunner(CapacityLimiter(4)),
            PostgresLogicalBookmarkUnitOfWorkFactory(get_engine),
            PostgresGroupQueries(get_engine),
            PostgresURLQueries(get_engine),
            RecordingTitleFetcher(),
            make_icon_service(),
        )
        created = await service.create_group(
            domain.CreateGroup("Reading", domains=(" Example.COM. ",))
        )
        assert isinstance(created, domain.GroupCreated)
        parent = created.record
        assert parent.domains == ("example.com",)
        with pytest.raises(FrozenInstanceError):
            parent.name = "Changed"
        child = await service.create_group(
            domain.CreateGroup("Child", parent_id=parent.id, nsfw=True)
        )
        assert isinstance(child, domain.GroupCreated)
        groups = await service.list_groups()
        assert isinstance(groups, tuple)
        assert groups[1].children == (child.record,)
        before = groups
        rejected = await service.update_group(
            domain.UpdateGroup(parent.id, name="Changed", parent_id=child.record.id)
        )
        assert isinstance(rejected, domain.ParentIsSelfOrDescendant)
        assert await service.list_groups() == before
        duplicate = await service.create_group(domain.CreateGroup("reading"))
        assert isinstance(duplicate, domain.GroupNameConflict)
        blocked = await service.delete_group(domain.DeleteGroup(parent.id, "delete"))
        assert isinstance(blocked, domain.GroupHasChildren)
        deleted = await service.delete_group(
            domain.DeleteGroup(child.record.id, "move_to_default")
        )
        assert isinstance(deleted, domain.GroupDeleted)
        reordered = await service.reorder_groups(
            domain.ReorderGroups(None, (parent.id, groups[0].id))
        )
        assert isinstance(reordered, domain.GroupsReordered)
        assert [group.id for group in await service.list_groups()] == [
            parent.id,
            groups[0].id,
        ]

    run(exercise)
