from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, cast

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.security import APIKeyCookie
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import config
from .app_keys import SITE_ICON_SERVICE_STATE, IconService, TitleFetcher
from .handlers import (
    create_group,
    create_url,
    delete_group,
    delete_url_by_id,
    edit_group,
    edit_url,
    export_data,
    get_url_icon,
    import_data,
    list_groups,
    list_urls,
    move_url_group,
    refresh_url_metadata,
    refresh_url_title,
    reorder_groups,
    set_important,
)
from .identity.api import health, login, logout, readiness, session_status
from .identity.boundary import (
    AuthenticationBoundaryMiddleware,
    NoStoreResponseMiddleware,
)
from .identity.routes import (
    HEALTH_OPERATION,
    LOGIN_OPERATION,
    LOGOUT_OPERATION,
    SESSION_OPERATION,
)
from .models import (
    AuthenticatedSessionResponse,
    CreateGroup,
    CreateGroupResponse,
    CreateURL,
    CreateURLResponse,
    DeleteGroup,
    DeleteGroupResponse,
    DeleteURLByIDResponse,
    EditGroup,
    EditGroupResponse,
    EditURL,
    EditURLResponse,
    ExportDocument,
    GroupsResponse,
    HealthResponse,
    ImportConflictResponse,
    ImportFailedResponse,
    ImportResponse,
    InvalidImportResponse,
    LoginRequest,
    MoveURLGroupResponse,
    RefreshURLMetadataResponse,
    RefreshURLTitleResponse,
    ReorderGroupsResponse,
    SetImportantResponse,
    UnauthenticatedSessionResponse,
    URLsResponse,
)
from .page_titles import fetch_url_title
from .responses import error_response
from .site_icons import SiteIconService
from .storage import dispose_engine

MAX_REQUEST_BODY_BYTES = 1_000_000
RESERVED_PATH_PREFIXES = {
    "api",
    "docs",
    "internal",
    "openapi.json",
    "redoc",
    "static",
}
CREATE_URL_REQUEST_BODY = {
    "required": True,
    "content": {
        "application/json": {
            "schema": CreateURL.model_json_schema(),
        },
    },
}
CREATE_GROUP_REQUEST_BODY = {
    "required": True,
    "content": {
        "application/json": {
            "schema": CreateGroup.model_json_schema(),
        },
    },
}
EDIT_GROUP_REQUEST_BODY = {
    "required": True,
    "content": {
        "application/json": {
            "schema": EditGroup.model_json_schema(),
        },
    },
}
EDIT_URL_REQUEST_BODY = {
    "required": True,
    "content": {
        "application/json": {
            "schema": EditURL.model_json_schema(),
        },
    },
}
DELETE_GROUP_REQUEST_BODY = {
    "required": True,
    "content": {
        "application/json": {
            "schema": DeleteGroup.model_json_schema(),
        },
    },
}
LOGIN_REQUEST_BODY = {
    "required": True,
    "content": {
        "application/json": {
            "schema": LoginRequest.model_json_schema(),
        },
    },
}
SITE_ICON_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "Saved site's icon.",
        "content": {
            "image/png": {"schema": {"type": "string", "format": "binary"}},
            "image/vnd.microsoft.icon": {
                "schema": {"type": "string", "format": "binary"},
            },
        },
    },
    404: {
        "description": "The URL or its icon is unavailable.",
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "properties": {"error": {"type": "string"}},
                    "required": ["error"],
                    "additionalProperties": False,
                },
            },
        },
    },
}
IMPORT_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "model": ImportResponse,
        "description": "Bookmark import completed.",
        "content": {
            "application/json": {
                "example": {"imported": 1, "skipped": 0, "groups": []},
            }
        },
    },
    409: {
        "model": ImportConflictResponse,
        "description": "Another bookmark mutation currently owns the import gate.",
        "content": {
            "application/json": {
                "example": {
                    "error": (
                        "Another bookmark change is in progress. "
                        "No import changes were saved. Try again."
                    ),
                    "code": "import_conflict",
                },
            }
        },
    },
    422: {
        "model": InvalidImportResponse,
        "description": "The import document is invalid.",
        "content": {
            "application/json": {
                "example": {
                    "error": "Invalid import file.",
                    "code": "invalid_import",
                },
            }
        },
    },
    500: {
        "model": ImportFailedResponse,
        "description": "The import was rolled back after an unexpected failure.",
        "content": {
            "application/json": {
                "example": {
                    "error": "Import failed. No import changes were saved. Try again.",
                    "code": "import_failed",
                },
            }
        },
    },
}


async def _request_validation_exception_handler(
    request: Request,
    error: Exception,
) -> JSONResponse:
    if not isinstance(error, RequestValidationError):
        raise error
    if request.url.path == "/api/import":
        payload = InvalidImportResponse.model_validate(
            {"error": "Invalid import file.", "code": "invalid_import"}
        )
        return JSONResponse(payload.model_dump(), status_code=422)
    return await request_validation_exception_handler(request, error)


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


class RequestBodyLimitMiddleware:
    def __init__(self, app: ASGIApp, max_body_size: int) -> None:
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        content_length = _content_length(scope)
        if content_length is not None and content_length > self.max_body_size:
            await self._too_large_response(scope, receive, send)
            return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] != "http.request":
                await self.app(scope, receive, send)
                return

            body.extend(cast(bytes, message.get("body", b"")))
            if len(body) > self.max_body_size:
                await self._too_large_response(scope, receive, send)
                return

            if not message.get("more_body", False):
                break

        sent = False

        async def replay_receive() -> Message:
            nonlocal sent
            if sent:
                return {"type": "http.request", "body": b"", "more_body": False}
            sent = True
            return {
                "type": "http.request",
                "body": bytes(body),
                "more_body": False,
            }

        await self.app(scope, replay_receive, send)

    async def _too_large_response(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        response = error_response("Request body too large.", 413)
        await response(scope, receive, send)


def _content_length(scope: Scope) -> int | None:
    for name, value in scope["headers"]:
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None]:
    # The engine holds a pool of real network connections now, so shutdown has
    # to hand them back rather than leave the server to time them out.
    try:
        yield
    finally:
        icon_service = cast(
            IconService,
            getattr(app.state, SITE_ICON_SERVICE_STATE),
        )
        try:
            await icon_service.wait_for_idle()
        finally:
            dispose_engine()


def create_app(
    title_fetcher: TitleFetcher | None = None,
    icon_service: IconService | None = None,
) -> FastAPI:
    # Cap request bodies globally so chunked/unknown-length import requests
    # cannot grow without bound.
    app = FastAPI(
        title="Trellmark API",
        lifespan=_lifespan,
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )
    app.add_exception_handler(
        RequestValidationError,
        _request_validation_exception_handler,
    )
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_body_size=MAX_REQUEST_BODY_BYTES,
    )
    # Added after the body limiter so authentication wraps it: an
    # unauthenticated private request is rejected before a large or malformed
    # route-specific body is read.
    app.add_middleware(AuthenticationBoundaryMiddleware)
    # Added last so no-store is outermost and also covers responses generated
    # by either middleware above.
    app.add_middleware(NoStoreResponseMiddleware)
    app.state.title_fetcher = title_fetcher or fetch_url_title
    app.state.site_icon_service = icon_service or SiteIconService()

    app.add_api_route(
        LOGIN_OPERATION[1],
        login,
        methods=[LOGIN_OPERATION[0]],
        response_model=AuthenticatedSessionResponse,
        openapi_extra={"requestBody": LOGIN_REQUEST_BODY},
    )
    app.add_api_route(
        SESSION_OPERATION[1],
        session_status,
        methods=[SESSION_OPERATION[0]],
        response_model=AuthenticatedSessionResponse | UnauthenticatedSessionResponse,
    )
    app.add_api_route(
        LOGOUT_OPERATION[1],
        logout,
        methods=[LOGOUT_OPERATION[0]],
        response_model=UnauthenticatedSessionResponse,
    )
    app.add_api_route(
        HEALTH_OPERATION[1],
        health,
        methods=[HEALTH_OPERATION[0]],
        response_model=HealthResponse,
    )
    app.add_api_route(
        "/internal/ready",
        readiness,
        methods=["GET"],
        response_model=HealthResponse,
        include_in_schema=False,
    )

    app.add_api_route(
        "/api/export",
        export_data,
        methods=["GET"],
        response_model=ExportDocument,
        dependencies=PROTECTED_DEPENDENCIES,
    )
    app.add_api_route(
        "/api/import",
        import_data,
        methods=["POST"],
        response_model=ImportResponse,
        responses=IMPORT_RESPONSES,
        dependencies=PROTECTED_DEPENDENCIES,
    )
    app.add_api_route(
        "/api/groups",
        list_groups,
        methods=["GET"],
        response_model=GroupsResponse,
        dependencies=PROTECTED_DEPENDENCIES,
    )
    app.add_api_route(
        "/api/groups",
        create_group,
        methods=["POST"],
        response_model=CreateGroupResponse,
        status_code=201,
        dependencies=PROTECTED_DEPENDENCIES,
        openapi_extra={"requestBody": CREATE_GROUP_REQUEST_BODY},
    )
    app.add_api_route(
        "/api/groups/order",
        reorder_groups,
        methods=["PATCH"],
        response_model=ReorderGroupsResponse,
        dependencies=PROTECTED_DEPENDENCIES,
    )
    app.add_api_route(
        "/api/groups/{group_id}",
        edit_group,
        methods=["PATCH"],
        response_model=EditGroupResponse,
        dependencies=PROTECTED_DEPENDENCIES,
        openapi_extra={"requestBody": EDIT_GROUP_REQUEST_BODY},
    )
    app.add_api_route(
        "/api/groups/{group_id}",
        delete_group,
        methods=["DELETE"],
        response_model=DeleteGroupResponse,
        dependencies=PROTECTED_DEPENDENCIES,
        openapi_extra={"requestBody": DELETE_GROUP_REQUEST_BODY},
    )
    app.add_api_route(
        "/api/urls",
        list_urls,
        methods=["GET"],
        response_model=URLsResponse,
        dependencies=PROTECTED_DEPENDENCIES,
    )
    app.add_api_route(
        "/api/urls",
        create_url,
        methods=["POST"],
        response_model=CreateURLResponse,
        status_code=201,
        dependencies=PROTECTED_DEPENDENCIES,
        openapi_extra={"requestBody": CREATE_URL_REQUEST_BODY},
    )
    app.add_api_route(
        "/api/urls/{url_id}/group",
        move_url_group,
        methods=["PATCH"],
        response_model=MoveURLGroupResponse,
        dependencies=PROTECTED_DEPENDENCIES,
    )
    app.add_api_route(
        "/api/urls/{url_id}/important",
        set_important,
        methods=["PATCH"],
        response_model=SetImportantResponse,
        dependencies=PROTECTED_DEPENDENCIES,
    )
    app.add_api_route(
        "/api/urls/{url_id}/refresh-title",
        refresh_url_title,
        methods=["POST"],
        response_model=RefreshURLTitleResponse,
        dependencies=PROTECTED_DEPENDENCIES,
    )
    app.add_api_route(
        "/api/urls/{url_id}/icon",
        get_url_icon,
        methods=["GET"],
        response_class=Response,
        response_model=None,
        responses=SITE_ICON_RESPONSES,
        dependencies=PROTECTED_DEPENDENCIES,
    )
    app.add_api_route(
        "/api/urls/{url_id}/refresh-metadata",
        refresh_url_metadata,
        methods=["POST"],
        response_model=RefreshURLMetadataResponse,
        dependencies=PROTECTED_DEPENDENCIES,
    )
    app.add_api_route(
        "/api/urls/{url_id}",
        edit_url,
        methods=["PATCH"],
        response_model=EditURLResponse,
        dependencies=PROTECTED_DEPENDENCIES,
        openapi_extra={"requestBody": EDIT_URL_REQUEST_BODY},
    )
    app.add_api_route(
        "/api/urls/{url_id}",
        delete_url_by_id,
        methods=["DELETE"],
        response_model=DeleteURLByIDResponse,
        dependencies=PROTECTED_DEPENDENCIES,
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
