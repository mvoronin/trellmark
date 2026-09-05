from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from anyio import CapacityLimiter
from fastapi import Depends, FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse
from fastapi.security import APIKeyCookie
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError

from . import config
from .backup.api import (
    build_backup_router,
    import_database_exception_handler,
    import_validation_exception_handler,
)
from .backup.application import BackupApplicationService, BackupUnitOfWorkFactory
from .backup.persistence import (
    PostgresBackupUnitOfWorkFactory,
    PostgresExportSnapshotFactory,
)
from .bookmarks.api import build_bookmarks_router
from .bookmarks.application import (
    BookmarksApplicationService,
    SiteIconCacheApplicationService,
    SiteIconGateway,
    TitleFetcher,
)
from .bookmarks.backup import (
    PostgresBookmarkBackupContributor,
    PostgresBookmarkSnapshotContributor,
)
from .bookmarks.integrations import SiteIconService, fetch_url_title
from .bookmarks.persistence import (
    PostgresDerivedStateUnitOfWorkFactory,
    PostgresGroupQueries,
    PostgresLogicalBookmarkUnitOfWorkFactory,
    PostgresSiteIconCacheQueries,
    PostgresURLQueries,
)
from .identity.api import build_identity_router
from .identity.application import IdentityApplicationService
from .identity.boundary import (
    AuthenticationBoundaryMiddleware,
    NoStoreResponseMiddleware,
)
from .identity.persistence import (
    PostgresIdentityQueries,
    PostgresIdentityUnitOfWorkFactory,
)
from .platform import runtime
from .platform.http import (
    RequestBodyLimitMiddleware,
    database_exception_handler,
    exception_handler_for_paths,
    validation_exception_handler,
)
from .platform.runtime import (
    BACKUP_WORK_CAPACITY,
    BOOKMARKS_DERIVED_WORK_CAPACITY,
    BOOKMARKS_WORK_CAPACITY,
    IDENTITY_WORK_CAPACITY,
    AnyIOWorkRunner,
)

MAX_REQUEST_BODY_BYTES = 1_000_000
RESERVED_PATH_PREFIXES = {
    "api",
    "docs",
    "internal",
    "openapi.json",
    "redoc",
    "static",
}


async def _serve_spa(spa_path: str = "") -> FileResponse:
    """Serve the app shell without turning unknown private routes into HTML."""
    first_segment = spa_path.partition("/")[0]
    if first_segment in RESERVED_PATH_PREFIXES:
        raise HTTPException(status_code=404)
    return FileResponse(config.INDEX_FILE)


_COOKIE_SECURITY = APIKeyCookie(
    name=config.PRODUCTION_SESSION_COOKIE,
    scheme_name="CookieAuth",
    description="Opaque server-side Trellmark web session.",
    auto_error=False,
)
PROTECTED_DEPENDENCIES = [Depends(_COOKIE_SECURITY)]


def create_app(
    title_fetcher: TitleFetcher | None = None,
    icon_service: SiteIconGateway | None = None,
    *,
    backup_uow_factory: BackupUnitOfWorkFactory | None = None,
) -> FastAPI:
    icon_gateway = (
        icon_service
        if icon_service is not None
        else SiteIconService(
            cache=SiteIconCacheApplicationService(
                AnyIOWorkRunner(CapacityLimiter(BOOKMARKS_DERIVED_WORK_CAPACITY)),
                PostgresDerivedStateUnitOfWorkFactory(runtime.get_engine),
                PostgresSiteIconCacheQueries(runtime.get_engine),
            )
        )
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        try:
            yield
        finally:
            # Drain the same gateway captured by the service and routes. Cache
            # lookups may schedule refreshes, so both must finish before disposal.
            try:
                await icon_gateway.wait_for_idle()
            finally:
                runtime.dispose_engine()

    # Cap request bodies globally so chunked/unknown-length import requests
    # cannot grow without bound.
    app = FastAPI(
        title="Trellmark API",
        lifespan=lifespan,
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )
    app.add_exception_handler(
        RequestValidationError,
        exception_handler_for_paths(
            validation_exception_handler,
            {"/api/import": import_validation_exception_handler},
        ),
    )
    app.add_exception_handler(
        SQLAlchemyError,
        exception_handler_for_paths(
            database_exception_handler,
            {"/api/import": import_database_exception_handler},
        ),
    )
    identity_service = IdentityApplicationService(
        work_runner=AnyIOWorkRunner(CapacityLimiter(IDENTITY_WORK_CAPACITY)),
        uow_factory=PostgresIdentityUnitOfWorkFactory(runtime.get_engine),
        queries=PostgresIdentityQueries(runtime.get_engine),
    )
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_body_size=MAX_REQUEST_BODY_BYTES,
    )
    # Added after the body limiter so authentication wraps it: an
    # unauthenticated private request is rejected before a large or malformed
    # route-specific body is read.
    app.add_middleware(AuthenticationBoundaryMiddleware, service=identity_service)
    # Added last so no-store is outermost and also covers responses generated
    # by either middleware above.
    app.add_middleware(NoStoreResponseMiddleware)
    bookmarks_service = BookmarksApplicationService(
        work_runner=AnyIOWorkRunner(CapacityLimiter(BOOKMARKS_WORK_CAPACITY)),
        logical_uow_factory=PostgresLogicalBookmarkUnitOfWorkFactory(
            runtime.get_engine
        ),
        group_queries=PostgresGroupQueries(runtime.get_engine),
        url_queries=PostgresURLQueries(runtime.get_engine),
        title_fetcher=title_fetcher or fetch_url_title,
        icon_gateway=icon_gateway,
    )
    app.state.bookmarks_application_service = bookmarks_service
    backup_service = BackupApplicationService(
        work_runner=AnyIOWorkRunner(CapacityLimiter(BACKUP_WORK_CAPACITY)),
        uow_factory=backup_uow_factory
        if backup_uow_factory is not None
        else PostgresBackupUnitOfWorkFactory(
            runtime.get_engine, PostgresBookmarkBackupContributor
        ),
        snapshot_factory=PostgresExportSnapshotFactory(
            runtime.get_engine, PostgresBookmarkSnapshotContributor
        ),
    )

    app.state.backup_application_service = backup_service

    app.state.identity_application_service = identity_service
    app.include_router(build_identity_router(identity_service))

    app.include_router(
        build_backup_router(backup_service), dependencies=PROTECTED_DEPENDENCIES
    )
    app.include_router(
        build_bookmarks_router(bookmarks_service), dependencies=PROTECTED_DEPENDENCIES
    )
    # Trellmark is a single deployable service. The shared edge Caddy lives in
    # the infrastructure repository and reverse-proxies the whole origin here;
    # this app owns its static files and SPA fallback.
    app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")
    app.add_api_route(
        "/{spa_path:path}",
        _serve_spa,
        methods=["GET", "HEAD"],
        response_class=FileResponse,
        include_in_schema=False,
    )
    return app
