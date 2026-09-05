import ast
import json
from pathlib import Path
from urllib import error, request

import pytest
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from tests.bookmarks import helpers as bookmark_helpers
from tests.helpers import _authentication_headers, http_json
from trellmark.bookmarks.persistence import (
    PostgresBookmarkRepository,
    PostgresGroupQueries,
)
from trellmark.identity.application import IdentityApplicationService

SERVICE_UNAVAILABLE = {"error": "Service unavailable."}
INTERNAL_SERVER_ERROR = {"error": "Internal server error."}
IMPORT_FAILED = {
    "error": "Import failed. No import changes were saved. Try again.",
    "code": "import_failed",
}
PRIVATE_MARKERS = (
    "SELECT password_hash FROM administrator",
    "private-parameter-value",
    "postgresql://admin:secret@private-host.invalid/trellmark",
    "private-session-token",
    "private-host.invalid",
    "private saved content",
)


def _database_failure(
    error_type: type[DBAPIError] = DBAPIError,
) -> DBAPIError:
    return error_type(
        PRIVATE_MARKERS[0],
        {
            "parameter": PRIVATE_MARKERS[1],
            "credential": PRIVATE_MARKERS[2],
            "token": PRIVATE_MARKERS[3],
        },
        RuntimeError(" | ".join(PRIVATE_MARKERS[4:])),
        hide_parameters=True,
    )


def _api_json_response(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict | None = None,
) -> tuple[int, dict, str, str]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = _authentication_headers(base_url)
    if payload is not None:
        headers = {**headers, "Content-Type": "application/json"}
    if method not in {"GET", "HEAD", "OPTIONS"}:
        headers = {**headers, "Origin": base_url}
    response_request = request.Request(
        base_url + path,
        data=body,
        headers=headers,
        method=method,
    )
    try:
        response = request.urlopen(response_request, timeout=5)
    except error.HTTPError as caught:
        response = caught
    with response:
        raw_body = response.read().decode("utf-8")
        return (
            response.status,
            json.loads(raw_body),
            response.headers["Cache-Control"],
            raw_body,
        )


@pytest.mark.parametrize(
    "failure",
    (
        _database_failure(OperationalError),
        SQLAlchemyTimeoutError(" | ".join(PRIVATE_MARKERS)),
    ),
    ids=("operational_error", "pool_timeout"),
)
def test_content_unavailable_failures_are_redacted_no_store_503(
    app,
    caplog,
    monkeypatch,
    failure,
):
    base_url, _ = app
    assert http_json(base_url, "/api/groups")[0] == 200

    def fail_read(_self):
        raise failure

    monkeypatch.setattr(PostgresGroupQueries, "list_groups", fail_read)

    status, payload, cache_control, raw_body = _api_json_response(
        base_url,
        "/api/groups",
    )

    assert (status, payload) == (503, SERVICE_UNAVAILABLE)
    assert cache_control == "no-store"
    assert all(marker not in raw_body for marker in PRIVATE_MARKERS)
    assert all(marker not in caplog.text for marker in PRIVATE_MARKERS)


def test_other_content_database_failure_is_redacted_no_store_500(
    app,
    caplog,
    monkeypatch,
):
    base_url, _ = app
    assert http_json(base_url, "/api/groups")[0] == 200

    def fail_read(_self):
        raise _database_failure()

    monkeypatch.setattr(PostgresGroupQueries, "list_groups", fail_read)

    status, payload, cache_control, raw_body = _api_json_response(
        base_url,
        "/api/groups",
    )

    assert (status, payload) == (500, INTERNAL_SERVER_ERROR)
    assert cache_control == "no-store"
    assert all(marker not in raw_body for marker in PRIVATE_MARKERS)
    assert all(marker not in caplog.text for marker in PRIVATE_MARKERS)


def test_content_database_failure_rolls_back_before_safe_serialization(
    app,
    caplog,
    monkeypatch,
):
    base_url, _ = app
    record = bookmark_helpers.seed_url("https://rollback-content.invalid/item")
    assert record is not None
    original_set_important = PostgresBookmarkRepository.set_important

    def fail_after_update(self, command):
        original_set_important(self, command)
        raise _database_failure()

    monkeypatch.setattr(
        PostgresBookmarkRepository,
        "set_important",
        fail_after_update,
    )

    status, payload, cache_control, raw_body = _api_json_response(
        base_url,
        f"/api/urls/{record['id']}/important",
        method="PATCH",
        payload={"important": True},
    )

    stored = bookmark_helpers.url_payload(record["id"])
    assert stored is not None
    assert stored["important"] is False
    assert (status, payload) == (500, INTERNAL_SERVER_ERROR)
    assert cache_control == "no-store"
    assert all(marker not in raw_body for marker in PRIVATE_MARKERS)
    assert all(marker not in caplog.text for marker in PRIVATE_MARKERS)


def test_identity_endpoint_database_failure_is_redacted_no_store_503(
    app,
    caplog,
    monkeypatch,
):
    base_url, _ = app
    _authentication_headers(base_url)

    async def fail_session_lookup(_self, _command):
        raise _database_failure()

    monkeypatch.setattr(
        IdentityApplicationService, "authenticate_session", fail_session_lookup
    )

    status, payload, cache_control, raw_body = _api_json_response(
        base_url,
        "/api/auth/session",
    )

    assert (status, payload) == (503, SERVICE_UNAVAILABLE)
    assert cache_control == "no-store"
    assert all(marker not in raw_body for marker in PRIVATE_MARKERS)
    assert all(marker not in caplog.text for marker in PRIVATE_MARKERS)


def test_identity_boundary_database_failure_never_reaches_content_handler(
    app,
    caplog,
    monkeypatch,
):
    base_url, _ = app
    _authentication_headers(base_url)

    async def fail_session_lookup(_self, _command):
        raise _database_failure()

    def fail_if_content_route_is_reached(_self):
        pytest.fail("pre-routing authentication failure reached the content handler")

    monkeypatch.setattr(
        IdentityApplicationService,
        "authenticate_session",
        fail_session_lookup,
    )
    monkeypatch.setattr(
        PostgresGroupQueries,
        "list_groups",
        fail_if_content_route_is_reached,
    )

    status, payload, cache_control, raw_body = _api_json_response(
        base_url,
        "/api/groups",
    )

    assert (status, payload) == (503, SERVICE_UNAVAILABLE)
    assert cache_control == "no-store"
    assert all(marker not in raw_body for marker in PRIVATE_MARKERS)
    assert all(marker not in caplog.text for marker in PRIVATE_MARKERS)


def test_every_sqlalchemy_engine_constructor_hides_error_parameters():
    root = Path(__file__).parents[2]
    engine_sources = (
        root / "trellmark" / "platform" / "runtime.py",
        root / "migrations" / "env.py",
    )

    for source_path in engine_sources:
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "create_engine"
        ]
        assert calls, f"No create_engine call found in {source_path}"
        for call in calls:
            hide_parameters = next(
                (
                    keyword.value
                    for keyword in call.keywords
                    if keyword.arg == "hide_parameters"
                ),
                None,
            )
            assert isinstance(hide_parameters, ast.Constant)
            assert hide_parameters.value is True
