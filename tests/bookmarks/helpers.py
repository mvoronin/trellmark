"""Seed and read real PostgreSQL records independently of HTTP serialization."""

from dataclasses import asdict

from anyio import CapacityLimiter
from sqlalchemy import update

from trellmark.bookmarks import application, domain
from trellmark.bookmarks.application import (
    SiteIconCacheApplicationService,
    create_url_in_uow,
)
from trellmark.bookmarks.domain import CreateURL, URLCreated
from trellmark.bookmarks.integrations import SiteIconService
from trellmark.bookmarks.persistence import (
    PostgresDerivedStateUnitOfWorkFactory,
    PostgresGroupQueries,
    PostgresLogicalBookmarkUnitOfWorkFactory,
    PostgresSiteIconCacheQueries,
    PostgresURLQueries,
    urls_table,
)
from trellmark.platform.runtime import (
    BOOKMARKS_DERIVED_WORK_CAPACITY,
    AnyIOWorkRunner,
    get_engine,
)


async def no_icon_fetcher(_url):
    return None


def icon_cache_service(*, runner=None, factory=None):
    return SiteIconCacheApplicationService(
        runner or AnyIOWorkRunner(CapacityLimiter(BOOKMARKS_DERIVED_WORK_CAPACITY)),
        factory or PostgresDerivedStateUnitOfWorkFactory(get_engine),
        PostgresSiteIconCacheQueries(get_engine),
    )


def make_icon_service(**kwargs):
    kwargs.setdefault("fetcher", no_icon_fetcher)
    return SiteIconService(cache=icon_cache_service(), **kwargs)


def seed_url(url, title=None):
    """Seed real Bookmark rows without running the test's title-fetch port."""
    outcome = create_url_in_uow(
        PostgresLogicalBookmarkUnitOfWorkFactory(get_engine), CreateURL(url, title)
    )
    assert isinstance(outcome, URLCreated)
    return asdict(outcome.record)


def url_payloads():
    return [asdict(record) for record in PostgresURLQueries(get_engine).list_urls()]


def url_payload(url_id):
    record = PostgresURLQueries(get_engine).url_by_id(url_id)
    return None if record is None else asdict(record)


def saved_urls():
    return [record.url for record in PostgresURLQueries(get_engine).list_urls()]


def url_group_ids(url_id):
    return list(PostgresURLQueries(get_engine).url_group_ids(url_id))


def group_payload(record):
    payload = asdict(record)
    payload["domains"] = list(record.domains)
    payload["urls"] = [asdict(url) for url in record.urls]
    payload["children"] = [group_payload(child) for child in record.children]
    return payload


def group_payloads():
    return [
        group_payload(group) for group in PostgresGroupQueries(get_engine).list_groups()
    ]


def group_by_name(name):
    record = PostgresGroupQueries(get_engine).group_by_name(name)
    return None if record is None else group_payload(record)


def create_group_outcome(name, nsfw=False, domains=(), parent_id=None):
    return application.create_group_in_uow(
        PostgresLogicalBookmarkUnitOfWorkFactory(get_engine),
        domain.CreateGroup(name, nsfw, tuple(domains), parent_id),
    )


def seed_group(name, nsfw=False, domains=(), parent_id=None):
    outcome = create_group_outcome(name, nsfw, domains, parent_id)
    assert isinstance(outcome, domain.GroupCreated)
    return group_payload(outcome.record)


def seed_membership(url_id, group_id, source_group_id=None):
    outcome = application.move_url_in_uow(
        PostgresLogicalBookmarkUnitOfWorkFactory(get_engine),
        domain.MoveURL(url_id, group_id, source_group_id),
    )
    assert isinstance(outcome, domain.URLMoved)


def seed_important(url_id, important):
    outcome = application.set_important_in_uow(
        PostgresLogicalBookmarkUnitOfWorkFactory(get_engine),
        domain.SetImportant(url_id, important),
    )
    assert isinstance(outcome, domain.SetImportantSucceeded)


def seed_title(url_id, title):
    record = application.update_title_in_uow(
        PostgresLogicalBookmarkUnitOfWorkFactory(get_engine), url_id, title
    )
    assert record is not None


def seed_group_order(parent_id, group_ids):
    outcome = application.reorder_groups_in_uow(
        PostgresLogicalBookmarkUnitOfWorkFactory(get_engine),
        domain.ReorderGroups(parent_id, tuple(group_ids)),
    )
    assert isinstance(outcome, domain.GroupsReordered)
    return True


def seed_created_at(url_id, created_at):
    # Timestamp setup is test-only; use the same logical root gate as writers.
    with PostgresLogicalBookmarkUnitOfWorkFactory(get_engine)() as uow:
        uow.bookmarks.connection.execute(
            update(urls_table)
            .where(urls_table.c.id == url_id)
            .values(created_at=created_at)
        )
        uow.commit()


def remove_membership(url_id, group_id):
    outcome = application.remove_url_in_uow(
        PostgresLogicalBookmarkUnitOfWorkFactory(get_engine),
        domain.RemoveURL(url_id, group_id),
    )
    assert isinstance(outcome, domain.URLRemoved)
    return asdict(outcome.record)
