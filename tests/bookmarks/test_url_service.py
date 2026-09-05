from dataclasses import FrozenInstanceError

import pytest
from anyio import CapacityLimiter, run

from tests.bookmarks import helpers as bookmark_helpers
from tests.bookmarks.helpers import make_icon_service
from tests.helpers import RecordingTitleFetcher
from trellmark.bookmarks import domain
from trellmark.bookmarks.application import BookmarksApplicationService
from trellmark.bookmarks.persistence import (
    PostgresGroupQueries,
    PostgresLogicalBookmarkUnitOfWorkFactory,
)
from trellmark.platform.runtime import AnyIOWorkRunner, get_engine


def test_url_service_preserves_records_versions_and_direct_memberships(database):
    assert hasattr(domain, "EditURL"), "Ordinary URL commands must belong to Bookmarks"
    from trellmark.bookmarks.persistence import PostgresURLQueries

    first = bookmark_helpers.seed_url("https://one.example", "One")
    second = bookmark_helpers.seed_url("https://two.example", "Two")
    target = bookmark_helpers.seed_group("Target")

    async def exercise():
        service = BookmarksApplicationService(
            AnyIOWorkRunner(CapacityLimiter(4)),
            PostgresLogicalBookmarkUnitOfWorkFactory(get_engine),
            PostgresGroupQueries(get_engine),
            PostgresURLQueries(get_engine),
            RecordingTitleFetcher(),
            make_icon_service(),
        )
        records = await service.list_urls()
        assert isinstance(records, tuple)
        assert [record.id for record in records] == [first["id"], second["id"]]
        record = records[0]
        assert record.created_at.endswith("Z")
        assert type(record.version) is int
        assert await service.url_by_id(record.id) == record
        assert await service.url_by_url(record.url) == record
        assert await service.url_group_ids(record.id) == (1,)
        with pytest.raises(FrozenInstanceError):
            record.title = "Changed"

        updated = await service.edit_url(
            domain.EditURL(record.id, record.version, url="NEW.example/", title=None)
        )
        assert isinstance(updated, domain.URLUpdated)
        assert updated.record.url == "https://new.example"
        assert updated.record.title is None
        assert updated.record.version == record.version + 1
        assert await service.url_group_ids(record.id) == (1,)
        assert isinstance(
            await service.edit_url(
                domain.EditURL(record.id, record.version, title="Stale")
            ),
            domain.URLVersionConflict,
        )
        assert isinstance(
            await service.edit_url(domain.EditURL(record.id, updated.record.version)),
            domain.EmptyURLEdit,
        )
        assert isinstance(
            await service.edit_url(domain.EditURL(999, 1, title="Missing")),
            domain.URLNotFound,
        )
        assert isinstance(
            await service.edit_url(
                domain.EditURL(record.id, updated.record.version, url="TWO.example/")
            ),
            domain.URLConflict,
        )
        assert await service.url_by_id(record.id) == updated.record
        flagged = await service.set_important(domain.SetImportant(record.id, True))
        assert isinstance(flagged.record, domain.URLRecord)
        assert flagged.record.important is True
        assert flagged.record.version == updated.record.version
        moved = await service.move_url(domain.MoveURL(record.id, target["id"]))
        assert isinstance(moved, domain.URLMoved)
        assert moved.source_group_id == 1
        assert moved.record == flagged.record
        assert await service.url_group_ids(record.id) == (target["id"],)
        removed = await service.remove_url(domain.RemoveURL(record.id, target["id"]))
        assert isinstance(removed, domain.URLRemoved)
        assert removed.record == flagged.record
        assert await service.url_by_id(record.id) is None
        assert await service.url_group_ids(record.id) == ()
        assert [record.id for record in await service.list_urls()] == [second["id"]]

    run(exercise)
