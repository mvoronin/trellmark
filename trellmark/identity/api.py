import json

from anyio import CapacityLimiter, to_thread
from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from .. import config
from ..models import LoginRequest
from ..request_utils import media_type
from ..responses import error_response
from .policy import SESSION_ABSOLUTE_LIFETIME
from .repository import (
    LoginBlocked,
    LoginRejected,
    authenticate_session,
    create_login_session,
    csrf_matches,
    database_ready,
    revoke_session,
)

INVALID_CREDENTIALS = "Invalid login or password."
AUTHENTICATION_REQUIRED = "Authentication required."
ORIGIN_REJECTED = "Request origin not allowed."
CSRF_REJECTED = "CSRF validation failed."
_LOGIN_WORKER_LIMITER = CapacityLimiter(1)


async def login(request: Request) -> JSONResponse:
    if media_type(request) != "application/json":
        return error_response("Login requires a JSON request.", 415)
    try:
        payload = LoginRequest.model_validate(await request.json())
    except json.JSONDecodeError, UnicodeDecodeError, ValidationError:
        # Pydantic validation details include the rejected input. A password
        # must never be reflected just because it exceeded a size boundary.
        return error_response("Invalid login request.", 400)

    source = request.client.host if request.client is not None else "unknown"
    try:
        session, cookie_value = await to_thread.run_sync(
            create_login_session,
            source,
            payload.login,
            payload.password,
            limiter=_LOGIN_WORKER_LIMITER,
        )
    except LoginBlocked as blocked:
        return error_response(
            "Too many login attempts. Try again later.",
            429,
            headers={"Retry-After": str(blocked.retry_after)},
        )
    except LoginRejected:
        return error_response(INVALID_CREDENTIALS, 401)
    except SQLAlchemyError:
        return error_response("Service unavailable.", 503)

    response = JSONResponse(
        {
            "authenticated": True,
            "login": session.login,
            "csrf_token": session.csrf_token,
        }
    )
    response.set_cookie(
        key=config.session_cookie_name(),
        value=cookie_value,
        max_age=int(SESSION_ABSOLUTE_LIFETIME.total_seconds()),
        path="/",
        secure=config.secure_session_cookie(),
        httponly=True,
        samesite="strict",
    )
    return response


async def session_status(request: Request) -> JSONResponse:
    cookie_name = config.session_cookie_name()
    cookie_value = request.cookies.get(cookie_name)
    try:
        session = await run_in_threadpool(authenticate_session, cookie_value)
    except SQLAlchemyError:
        return error_response("Service unavailable.", 503)
    if session is None:
        response = JSONResponse({"authenticated": False})
        if cookie_value is not None:
            clear_session_cookie(response)
        return response
    return JSONResponse(
        {
            "authenticated": True,
            "login": session.login,
            "csrf_token": session.csrf_token,
        }
    )


async def logout(request: Request) -> JSONResponse:
    cookie_name = config.session_cookie_name()
    cookie_value = request.cookies.get(cookie_name)
    try:
        session = await run_in_threadpool(
            authenticate_session, cookie_value, touch=False
        )
    except SQLAlchemyError:
        return error_response("Service unavailable.", 503)

    if session is not None:
        if not trusted_origin(request) or cross_site_fetch(request):
            return error_response(ORIGIN_REJECTED, 403)
        if not csrf_matches(session, request.headers.get("x-csrf-token")):
            return error_response(CSRF_REJECTED, 403)
        try:
            await run_in_threadpool(revoke_session, session)
        except SQLAlchemyError:
            return error_response("Service unavailable.", 503)

    response = JSONResponse({"authenticated": False})
    if cookie_value is not None:
        clear_session_cookie(response)
    return response


async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


async def readiness() -> JSONResponse:
    if await run_in_threadpool(database_ready):
        return JSONResponse({"status": "ok"})
    return error_response("Service unavailable.", 503)


def trusted_origin(request: Request) -> bool:
    return request.headers.get("origin") == config.public_origin()


def cross_site_fetch(request: Request) -> bool:
    return request.headers.get("sec-fetch-site", "").lower() == "cross-site"


def clear_session_cookie(response: JSONResponse) -> None:
    response.delete_cookie(
        key=config.session_cookie_name(),
        path="/",
        secure=config.secure_session_cookie(),
        httponly=True,
        samesite="strict",
    )
