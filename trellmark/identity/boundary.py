from typing import assert_never

from fastapi import Request
from sqlalchemy.exc import SQLAlchemyError
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .. import config
from ..platform.responses import error_response
from .api import (
    AUTHENTICATION_REQUIRED,
    CSRF_REJECTED,
    ORIGIN_REJECTED,
    clear_session_cookie,
    cross_site_fetch,
    trusted_origin,
)
from .application import IdentityApplicationService
from .domain import (
    SessionAuthenticated,
    SessionCommand,
    SessionMissing,
    csrf_matches,
)
from .routes import LOGIN_OPERATION, PUBLIC_OPERATIONS

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class AuthenticationBoundaryMiddleware:
    """Default-deny all API paths before FastAPI parses route inputs."""

    def __init__(self, app: ASGIApp, service: IdentityApplicationService) -> None:
        self.app = app
        self.service = service

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "GET").upper()
        if not path.startswith("/api/"):
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        if (method, path) in PUBLIC_OPERATIONS:
            if (method, path) == LOGIN_OPERATION and (
                not trusted_origin(request) or cross_site_fetch(request)
            ):
                await self._error(scope, receive, send, ORIGIN_REJECTED, 403)
                return
            await self.app(scope, receive, send)
            return

        cookie_value = request.cookies.get(config.session_cookie_name())
        try:
            outcome = await self.service.authenticate_session(
                SessionCommand(cookie_value)
            )
        except SQLAlchemyError:
            await self._error(scope, receive, send, "Service unavailable.", 503)
            return
        match outcome:
            case SessionMissing():
                response = error_response(AUTHENTICATION_REQUIRED, 401)
                if cookie_value is not None:
                    clear_session_cookie(response)
                await response(scope, receive, send)
                return
            case SessionAuthenticated(session):
                pass
            case _:
                assert_never(outcome)

        if method not in SAFE_METHODS:
            if not trusted_origin(request) or cross_site_fetch(request):
                await self._error(scope, receive, send, ORIGIN_REJECTED, 403)
                return
            if not csrf_matches(session, request.headers.get("x-csrf-token")):
                await self._error(scope, receive, send, CSRF_REJECTED, 403)
                return

        await self.app(scope, receive, send)

    @staticmethod
    async def _error(
        scope: Scope,
        receive: Receive,
        send: Send,
        message: str,
        status: int,
    ) -> None:
        response = error_response(message, status)
        await response(scope, receive, send)


class NoStoreResponseMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        cache_control = (
            "no-store" if path.startswith(("/api/", "/internal/")) else "no-cache"
        )

        async def cache_policy_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = cache_control
            await send(message)

        await self.app(scope, receive, cache_policy_send)
