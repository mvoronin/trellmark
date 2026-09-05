import json
from typing import Literal, assert_never

from anyio import CapacityLimiter
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import Field, StrictStr, ValidationError, field_validator
from sqlalchemy.exc import SQLAlchemyError

from .. import config
from ..platform.contracts import ContractModel
from ..platform.responses import error_response
from ..request_utils import media_type
from .application import IdentityApplicationService
from .domain import (
    SESSION_ABSOLUTE_LIFETIME,
    AuthSession,
    LoginCommand,
    LoginCreated,
    LoginFailureRecorded,
    LoginOutcome,
    LoginThrottled,
    RevokeSessionCommand,
    SessionAuthenticated,
    SessionCommand,
    SessionMissing,
    SessionOutcome,
    SessionRevoked,
    csrf_matches,
)
from .routes import (
    HEALTH_OPERATION,
    LOGIN_OPERATION,
    LOGOUT_OPERATION,
    READINESS_OPERATION,
    SESSION_OPERATION,
)

INVALID_CREDENTIALS = "Invalid login or password."
AUTHENTICATION_REQUIRED = "Authentication required."
ORIGIN_REJECTED = "Request origin not allowed."
CSRF_REJECTED = "CSRF validation failed."


class LoginRequest(ContractModel):
    login: StrictStr = Field(max_length=128)
    password: StrictStr

    @field_validator("password")
    @classmethod
    def limit_password_bytes(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 1024:
            raise ValueError("Password is too long.")
        return value


class AuthenticatedSessionResponse(ContractModel):
    authenticated: Literal[True]
    login: StrictStr
    csrf_token: StrictStr


class UnauthenticatedSessionResponse(ContractModel):
    authenticated: Literal[False]


class HealthResponse(ContractModel):
    status: Literal["ok"]


LOGIN_REQUEST_BODY = {
    "required": True,
    "content": {
        "application/json": {
            "schema": LoginRequest.model_json_schema(),
        },
    },
}


def map_identity_outcome(
    outcome: LoginOutcome | SessionOutcome | SessionRevoked,
    *,
    cookie_present: bool = False,
) -> JSONResponse:
    match outcome:
        case LoginCreated(session, cookie_value):
            response = _authenticated_response(session)
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
        case LoginFailureRecorded():
            return error_response(INVALID_CREDENTIALS, 401)
        case LoginThrottled(retry_after):
            return error_response(
                "Too many login attempts. Try again later.",
                429,
                headers={"Retry-After": str(retry_after)},
            )
        case SessionAuthenticated(session):
            return _authenticated_response(session)
        case SessionMissing() | SessionRevoked():
            response = JSONResponse(
                UnauthenticatedSessionResponse(authenticated=False).model_dump()
            )
            if cookie_present:
                clear_session_cookie(response)
            return response
    assert_never(outcome)


def _authenticated_response(session: AuthSession) -> JSONResponse:
    return JSONResponse(
        AuthenticatedSessionResponse(
            authenticated=True,
            login=session.login,
            csrf_token=session.csrf_token,
        ).model_dump()
    )


def build_identity_router(service: IdentityApplicationService) -> APIRouter:
    router = APIRouter()
    # Preserve single-login admission without occupying a worker while waiting.
    # The service's capacity-2 runner owns the complete database/Argon2 scope.
    login_admission = CapacityLimiter(1)

    async def login(request: Request) -> JSONResponse:
        if media_type(request) != "application/json":
            return error_response("Login requires a JSON request.", 415)
        try:
            payload = LoginRequest.model_validate(await request.json())
        except json.JSONDecodeError, UnicodeDecodeError, ValidationError:
            # Validation details contain rejected input, including passwords.
            return error_response("Invalid login request.", 400)

        source = request.client.host if request.client is not None else "unknown"
        try:
            async with login_admission:
                outcome = await service.login(
                    LoginCommand(source, payload.login, payload.password)
                )
        except SQLAlchemyError:
            return error_response("Service unavailable.", 503)
        return map_identity_outcome(outcome)

    async def session_status(request: Request) -> JSONResponse:
        cookie_value = request.cookies.get(config.session_cookie_name())
        try:
            outcome = await service.authenticate_session(SessionCommand(cookie_value))
        except SQLAlchemyError:
            return error_response("Service unavailable.", 503)
        return map_identity_outcome(outcome, cookie_present=cookie_value is not None)

    async def logout(request: Request) -> JSONResponse:
        cookie_value = request.cookies.get(config.session_cookie_name())
        try:
            outcome = await service.authenticate_session(
                SessionCommand(cookie_value, touch=False)
            )
        except SQLAlchemyError:
            return error_response("Service unavailable.", 503)

        match outcome:
            case SessionAuthenticated(session):
                if not trusted_origin(request) or cross_site_fetch(request):
                    return error_response(ORIGIN_REJECTED, 403)
                if not csrf_matches(session, request.headers.get("x-csrf-token")):
                    return error_response(CSRF_REJECTED, 403)
                try:
                    revoked = await service.revoke_session(
                        RevokeSessionCommand(session.id)
                    )
                except SQLAlchemyError:
                    return error_response("Service unavailable.", 503)
                return map_identity_outcome(
                    revoked, cookie_present=cookie_value is not None
                )
            case SessionMissing():
                return map_identity_outcome(
                    outcome, cookie_present=cookie_value is not None
                )
        assert_never(outcome)

    async def health() -> JSONResponse:
        return JSONResponse(HealthResponse(status="ok").model_dump())

    async def readiness() -> JSONResponse:
        try:
            ready = await service.database_ready()
        except SQLAlchemyError:
            return error_response("Service unavailable.", 503)
        if ready:
            return JSONResponse(HealthResponse(status="ok").model_dump())
        return error_response("Service unavailable.", 503)

    router.add_api_route(
        LOGIN_OPERATION[1],
        login,
        methods=[LOGIN_OPERATION[0]],
        response_model=AuthenticatedSessionResponse,
        openapi_extra={"requestBody": LOGIN_REQUEST_BODY},
    )
    router.add_api_route(
        SESSION_OPERATION[1],
        session_status,
        methods=[SESSION_OPERATION[0]],
        response_model=AuthenticatedSessionResponse | UnauthenticatedSessionResponse,
    )
    router.add_api_route(
        LOGOUT_OPERATION[1],
        logout,
        methods=[LOGOUT_OPERATION[0]],
        response_model=UnauthenticatedSessionResponse,
    )
    router.add_api_route(
        HEALTH_OPERATION[1],
        health,
        methods=[HEALTH_OPERATION[0]],
        response_model=HealthResponse,
    )
    router.add_api_route(
        READINESS_OPERATION[1],
        readiness,
        methods=[READINESS_OPERATION[0]],
        response_model=HealthResponse,
        include_in_schema=False,
    )
    return router


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
