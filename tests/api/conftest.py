import pytest
from anyio import CapacityLimiter

from trellmark.bookmarks.application import BookmarksApplicationService
from trellmark.bookmarks.persistence import (
    PostgresGroupQueries,
    PostgresLogicalBookmarkUnitOfWorkFactory,
    PostgresURLQueries,
)
from trellmark.platform.runtime import (
    BOOKMARKS_WORK_CAPACITY,
    AnyIOWorkRunner,
    get_engine,
)


@pytest.fixture
def bookmarks_service(database, title_fetcher, icon_service):
    return BookmarksApplicationService(
        AnyIOWorkRunner(CapacityLimiter(BOOKMARKS_WORK_CAPACITY)),
        PostgresLogicalBookmarkUnitOfWorkFactory(get_engine),
        PostgresGroupQueries(get_engine),
        PostgresURLQueries(get_engine),
        title_fetcher,
        icon_service,
    )
