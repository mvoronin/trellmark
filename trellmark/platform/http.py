"""Product-neutral HTTP limits and injectable outward exception boundaries."""

from collections.abc import Awaitable, Callable, Mapping
from typing import cast

from fastapi import Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .responses import error_response

type ExceptionHandler = Callable[[Request, Exception], Awaitable[JSONResponse]]


def exception_handler_for_paths(
    default: ExceptionHandler, overrides: Mapping[str, ExceptionHandler]
) -> ExceptionHandler:
    # Capture the wiring once; later caller mutations must not change policy.
    handlers = dict(overrides)

    async def handle(request: Request, error: Exception) -> JSONResponse:
        return await handlers.get(request.url.path, default)(request, error)

    return handle


async def validation_exception_handler(
    request: Request, error: Exception
) -> JSONResponse:
    if not isinstance(error, RequestValidationError):
        raise error
    return await request_validation_exception_handler(request, error)


async def database_exception_handler(
    request: Request, error: Exception
) -> JSONResponse:
    if not isinstance(error, SQLAlchemyError):
        raise error
    if isinstance(error, (OperationalError, SQLAlchemyTimeoutError)):
        return error_response("Service unavailable.", 503)
    return error_response("Internal server error.", 500)


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
