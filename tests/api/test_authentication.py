import json
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from threading import Event, Lock, get_ident
from urllib import error, request
from urllib.parse import urlsplit

import pytest
from argon2 import PasswordHasher
from argon2.low_level import Type
from fastapi.routing import iter_route_contexts
from sqlalchemy import text

from tests.helpers import (
    RecordingTitleFetcher,
    db_connection,
    db_query,
    http_json,
    http_raw,
    run_async,
)
from tests.postgres import TEST_LOGIN, TEST_PASSWORD
from trellmark import config
from trellmark.app import create_app
from trellmark.identity import persistence as identity_persistence
from trellmark.identity.application import IdentityApplicationService
from trellmark.identity.domain import SESSION_ABSOLUTE_LIFETIME, LoginFailureRecorded
from trellmark.identity.persistence import (
    DUMMY_PASSWORD_HASH,
    PASSWORD_HASHER,
    LoginBlocked,
    LoginRejected,
    create_login_session,
    set_administrator_password,
)
from trellmark.identity.routes import PUBLIC_OPERATIONS


def _request_json(base_url, path, *, method="GET", payload=None, headers=None):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request_headers = dict(headers or {})
    if payload is not None:
        request_headers.setdefault("Content-Type", "application/json")
    req = request.Request(
        base_url + path,
        data=body,
        headers=request_headers,
        method=method,
    )
    try:
        with request.urlopen(req, timeout=5) as response:
            return (
                response.status,
                json.loads(response.read().decode("utf-8")),
                response.headers,
            )
    except error.HTTPError as response:
        return (
            response.code,
            json.loads(response.read().decode("utf-8")),
            response.headers,
        )


def _login(base_url, *, login=TEST_LOGIN, password=TEST_PASSWORD, origin=None):
    status, payload, headers = _request_json(
        base_url,
        "/api/auth/login",
        method="POST",
        payload={"login": login, "password": password},
        headers={"Origin": origin or base_url},
    )
    cookie = headers.get("Set-Cookie", "").split(";", 1)[0]
    return status, payload, headers, cookie


def _auth_headers(base_url):
    status, payload, _, cookie = _login(base_url)
    assert status == 200
    return {
        "Cookie": cookie,
        "Origin": base_url,
        "X-CSRF-Token": payload["csrf_token"],
    }


def test_anonymous_api_boundary_rejects_every_existing_route_before_validation(app):
    base_url, _ = app
    routes = [
        ("GET", "/api/export", None),
        ("POST", "/api/import", {"not": "an import"}),
        ("GET", "/api/groups", None),
        ("POST", "/api/groups", {"name": "would write"}),
        ("PATCH", "/api/groups/order", {"group_ids": []}),
        ("PATCH", "/api/groups/not-an-id", {"name": "x"}),
        ("DELETE", "/api/groups/not-an-id", {"url_action": "delete"}),
        ("GET", "/api/urls", None),
        ("POST", "/api/urls", {"url": "https://would-fetch.example"}),
        ("PATCH", "/api/urls/not-an-id", {"url": "x"}),
        ("DELETE", "/api/urls/not-an-id", None),
        ("PATCH", "/api/urls/not-an-id/group", {"group_id": 1}),
        ("PATCH", "/api/urls/not-an-id/important", {"important": True}),
        ("POST", "/api/urls/not-an-id/refresh-title", None),
        ("GET", "/api/urls/not-an-id/icon", None),
        ("POST", "/api/urls/not-an-id/refresh-metadata", None),
        ("GET", "/api/a-route-that-does-not-exist", None),
    ]

    for method, path, payload in routes:
        status, response, headers = _request_json(
            base_url, path, method=method, payload=payload
        )
        assert status == 401, (method, path, response)
        assert response == {"error": "Authentication required."}
        assert headers["Cache-Control"] == "no-store"

    assert db_query("SELECT COUNT(*) FROM urls") == [(0,)]


def test_default_deny_boundary_awaits_composed_identity_before_body_parsing(
    monkeypatch,
):
    from trellmark.identity.domain import SessionCommand, SessionMissing

    monkeypatch.setattr(config, "PUBLIC_ORIGIN", "http://localhost")
    application = create_app()
    service = application.state.identity_application_service
    application.state.identity_application_service = object()
    calls = []
    messages = []

    async def authenticate(instance, command):
        assert instance is service
        calls.append(command)
        return SessionMissing()

    monkeypatch.setattr(
        IdentityApplicationService, "authenticate_session", authenticate
    )

    async def receive():
        pytest.fail("Authentication must reject the request before reading its body.")

    async def send(message):
        messages.append(message)

    async def exercise():
        await application(
            {
                "type": "http",
                "method": "PATCH",
                "path": "/api/urls/not-an-integer",
                "headers": [(b"content-length", b"1000001")],
            },
            receive,
            send,
        )

    run_async(exercise)
    assert calls == [SessionCommand(None)]
    assert messages[0]["status"] == 401
    assert (b"cache-control", b"no-store") in messages[0]["headers"]
    assert json.loads(messages[1]["body"]) == {"error": "Authentication required."}


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize(
    ("source_headers", "message"),
    [
        ([], "Request origin not allowed."),
        ([(b"origin", b"https://other.example")], "Request origin not allowed."),
        (
            [(b"origin", b"http://localhost"), (b"sec-fetch-site", b"cross-site")],
            "Request origin not allowed.",
        ),
        ([(b"origin", b"http://localhost")], "CSRF validation failed."),
        (
            [(b"origin", b"http://localhost"), (b"x-csrf-token", b"wrong")],
            "CSRF validation failed.",
        ),
    ],
)
def test_default_deny_source_and_csrf_checks_precede_body_parsing(
    monkeypatch, method, source_headers, message
):
    from trellmark.identity.domain import (
        AuthSession,
        SessionAuthenticated,
        SessionCommand,
    )

    monkeypatch.setattr(config, "PUBLIC_ORIGIN", "http://localhost")
    application = create_app()
    service = application.state.identity_application_service
    calls = []
    messages = []

    async def authenticate(instance, command):
        assert instance is service
        calls.append(command)
        return SessionAuthenticated(AuthSession(7, 1, "admin", "synthetic-csrf"))

    monkeypatch.setattr(
        IdentityApplicationService, "authenticate_session", authenticate
    )

    async def receive():
        pytest.fail("Source and CSRF checks must reject before body parsing.")

    async def send(response):
        messages.append(response)

    async def exercise():
        await application(
            {
                "type": "http",
                "method": method,
                "path": "/api/urls/not-an-integer",
                "headers": [
                    (b"cookie", b"trellmark_session_dev=synthetic-cookie"),
                    (b"content-length", b"1000001"),
                    *source_headers,
                ],
            },
            receive,
            send,
        )

    run_async(exercise)
    assert calls == [SessionCommand("synthetic-cookie")]
    assert messages[0]["status"] == 403
    assert (b"cache-control", b"no-store") in messages[0]["headers"]
    assert json.loads(messages[1]["body"]) == {"error": message}


def test_default_deny_uses_exact_shared_public_operations():
    from trellmark.identity import boundary, routes

    assert boundary.PUBLIC_OPERATIONS is routes.PUBLIC_OPERATIONS
    assert PUBLIC_OPERATIONS == {
        ("POST", "/api/auth/login"),
        ("GET", "/api/auth/session"),
        ("POST", "/api/auth/logout"),
        ("GET", "/api/health"),
    }
    assert routes.READINESS_OPERATION == ("GET", "/internal/ready")
    assert routes.READINESS_OPERATION not in PUBLIC_OPERATIONS


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/auth/login"),
        ("POST", "/api/auth/session"),
        ("GET", "/api/auth/logout"),
        ("POST", "/api/health"),
        ("GET", "/api/auth/session/"),
        ("OPTIONS", "/api/health"),
    ],
)
def test_default_deny_does_not_expand_public_paths_or_methods(app, method, path):
    base_url, _ = app
    status, payload, headers = _request_json(base_url, path, method=method)
    assert (status, payload) == (401, {"error": "Authentication required."})
    assert headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("helper_name", ["json", "raw"])
def test_authenticated_http_helpers_do_not_replay_a_401(app, helper_name):
    base_url, _ = app
    assert http_json(base_url, "/api/groups")[0] == 200
    with db_connection() as connection:
        connection.execute(text("UPDATE web_sessions SET revoked_at = now()"))

    if helper_name == "json":
        status, payload = http_json(
            base_url,
            "/api/groups",
            method="POST",
            payload={"name": "Must not be replayed"},
        )
    else:
        status, payload = http_raw(
            base_url,
            "/api/groups",
            method="POST",
            body=json.dumps({"name": "Must not be replayed"}).encode("utf-8"),
            content_type="application/json",
        )

    assert (status, payload) == (401, {"error": "Authentication required."})
    assert db_query("SELECT COUNT(*) FROM groups") == [(1,)]


def test_only_auth_session_logout_and_health_are_public(app):
    base_url, _ = app

    assert _request_json(base_url, "/api/auth/session")[:2] == (
        200,
        {"authenticated": False},
    )
    assert _request_json(base_url, "/api/auth/logout", method="POST")[:2] == (
        200,
        {"authenticated": False},
    )
    assert _request_json(base_url, "/api/health")[:2] == (
        200,
        {"status": "ok"},
    )
    for path in ["/openapi.json", "/docs", "/redoc"]:
        assert _request_json(base_url, path)[0] == 404


def test_public_health_is_process_only(app, monkeypatch):
    base_url, _ = app

    def unexpected_database_probe(_self):
        pytest.fail("public health must not touch PostgreSQL")

    monkeypatch.setattr(
        identity_persistence.PostgresIdentityQueries,
        "database_ready",
        unexpected_database_probe,
    )

    status, payload, headers = _request_json(base_url, "/api/health")
    assert (status, payload) == (200, {"status": "ok"})
    assert headers["Cache-Control"] == "no-store"


def test_blocking_login_identity_work_does_not_stall_event_loop(app, monkeypatch):
    base_url, _ = app
    entered = Event()
    release = Event()

    def stalled_login(_self, _command):
        entered.set()
        assert release.wait(timeout=5), "test did not release stalled login"
        return LoginFailureRecorded()

    monkeypatch.setattr(
        identity_persistence.PostgresIdentityRepository, "login", stalled_login
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        login_future = executor.submit(_login, base_url, password="wrong")
        assert entered.wait(timeout=2), "login work did not start"
        try:
            health_future = executor.submit(_request_json, base_url, "/api/health")
            assert health_future.result(timeout=1)[:2] == (
                200,
                {"status": "ok"},
            )
        finally:
            release.set()

        assert login_future.result(timeout=2)[:2] == (
            401,
            {"error": "Invalid login or password."},
        )


def test_login_has_dedicated_single_worker_capacity(app, monkeypatch):
    base_url, _ = app
    status, _, _, cookie = _login(base_url)
    assert status == 200
    first_entered = Event()
    second_entered = Event()
    release = Event()
    call_lock = Lock()
    call_count = 0

    def stalled_login(_self, _command):
        nonlocal call_count
        with call_lock:
            call_count += 1
            current_call = call_count
        (first_entered if current_call == 1 else second_entered).set()
        assert release.wait(timeout=5), "test did not release stalled logins"
        return LoginFailureRecorded()

    monkeypatch.setattr(
        identity_persistence.PostgresIdentityRepository, "login", stalled_login
    )

    with ThreadPoolExecutor(max_workers=3) as executor:
        first_login = executor.submit(_login, base_url, password="wrong")
        assert first_entered.wait(timeout=2), "first login work did not start"
        second_login = executor.submit(_login, base_url, password="wrong")
        assert not second_entered.wait(timeout=0.2)

        urls_future = executor.submit(
            _request_json,
            base_url,
            "/api/urls",
            headers={"Cookie": cookie},
        )
        try:
            assert urls_future.result(timeout=1)[:2] == (200, {"urls": []})
            # Queued logins must not consume both Identity worker slots.
            session_future = executor.submit(
                _request_json,
                base_url,
                "/api/auth/session",
                headers={"Cookie": cookie},
            )
            session_status, session_payload, _ = session_future.result(timeout=1)
            assert session_status == 200
            assert session_payload["authenticated"] is True
        finally:
            release.set()

        assert first_login.result(timeout=2)[0] == 401
        assert second_login.result(timeout=2)[0] == 401
        assert second_entered.is_set()


def test_blocking_session_lookup_does_not_stall_event_loop(app, monkeypatch):
    base_url, _ = app
    status, _, _, cookie = _login(base_url)
    assert status == 200
    entered = Event()
    release = Event()
    original_authenticate = (
        identity_persistence.PostgresIdentityRepository.authenticate_session
    )

    def stalled_authenticate(self, command):
        entered.set()
        assert release.wait(timeout=5), "test did not release session lookup"
        return original_authenticate(self, command)

    monkeypatch.setattr(
        identity_persistence.PostgresIdentityRepository,
        "authenticate_session",
        stalled_authenticate,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        urls_future = executor.submit(
            _request_json,
            base_url,
            "/api/urls",
            headers={"Cookie": cookie},
        )
        assert entered.wait(timeout=2), "session lookup did not start"
        try:
            health_future = executor.submit(_request_json, base_url, "/api/health")
            assert health_future.result(timeout=1)[:2] == (200, {"status": "ok"})
        finally:
            release.set()

        assert urls_future.result(timeout=2)[:2] == (200, {"urls": []})


def test_internal_readiness_is_generic_and_absent_from_openapi(app, monkeypatch):
    base_url, _ = app
    internal_routes = {
        (context.route.path, frozenset(context.route.methods or ()))
        for context in iter_route_contexts(create_app().routes)
        if context.route.path.startswith("/internal/")
    }

    assert _request_json(base_url, "/internal/ready")[:2] == (
        200,
        {"status": "ok"},
    )
    monkeypatch.setattr(
        identity_persistence.PostgresIdentityQueries,
        "database_ready",
        lambda _self: False,
    )
    status, payload, headers = _request_json(base_url, "/internal/ready")
    assert (status, payload) == (503, {"error": "Service unavailable."})
    assert headers["Cache-Control"] == "no-store"
    assert "/internal/ready" not in create_app().openapi()["paths"]
    assert internal_routes == {("/internal/ready", frozenset({"GET"}))}


@pytest.mark.parametrize("failure_kind", ["database", "operational", "pool", "alchemy"])
@pytest.mark.parametrize(
    "stage", ["login", "session", "logout_lookup", "revoke", "ready", "boundary"]
)
def test_identity_http_database_failures_are_redacted_and_rollback(
    app, monkeypatch, caplog, stage, failure_kind
):
    from sqlalchemy.exc import OperationalError, SQLAlchemyError
    from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

    from tests.api.test_database_error_boundaries import (
        PRIVATE_MARKERS,
        _database_failure,
    )

    base_url, _ = app
    headers = _auth_headers(base_url)
    if stage == "boundary":
        with db_connection() as connection:
            connection.execute(
                text(
                    "UPDATE web_sessions SET created_at = created_at - interval '1 hour', "
                    "last_used_at = last_used_at - interval '6 minutes'"
                )
            )
    session_snapshot = (
        "SELECT id, revoked_at, last_used_at, idle_expires_at "
        "FROM web_sessions ORDER BY id"
    )
    initial_sessions = db_query(session_snapshot)
    initial_throttles = db_query(
        "SELECT * FROM auth_login_throttle ORDER BY scope, bucket_key"
    )

    class UnserializableFailure(SQLAlchemyError):
        def __str__(self):
            pytest.fail("Identity failures must never serialize raw database errors.")

    failure = {
        "database": _database_failure(),
        "operational": _database_failure(OperationalError),
        "pool": SQLAlchemyTimeoutError(" | ".join(PRIVATE_MARKERS)),
        "alchemy": UnserializableFailure(" | ".join(PRIVATE_MARKERS)),
    }[failure_kind]
    owner, name = {
        "login": (identity_persistence.PostgresIdentityRepository, "login"),
        "session": (
            identity_persistence.PostgresIdentityRepository,
            "authenticate_session",
        ),
        "logout_lookup": (
            identity_persistence.PostgresIdentityQueries,
            "lookup_session",
        ),
        "revoke": (identity_persistence.PostgresIdentityRepository, "revoke_session"),
        "ready": (identity_persistence.PostgresIdentityQueries, "database_ready"),
        "boundary": (
            identity_persistence.PostgresIdentityRepository,
            "authenticate_session",
        ),
    }[stage]
    original = getattr(owner, name)

    def fail_after_operation(self, *arguments):
        original(self, *arguments)
        raise failure

    with monkeypatch.context() as patch:
        patch.setattr(owner, name, fail_after_operation)
        if stage == "login":
            status, payload, response_headers, _ = _login(base_url)
        else:
            path = {
                "session": "/api/auth/session",
                "logout_lookup": "/api/auth/logout",
                "revoke": "/api/auth/logout",
                "ready": "/internal/ready",
                "boundary": "/api/urls/not-an-integer",
            }[stage]
            status, payload, response_headers = _request_json(
                base_url,
                path,
                method="POST" if stage in {"logout_lookup", "revoke"} else "GET",
                headers=headers,
            )
    assert (status, payload) == (503, {"error": "Service unavailable."})
    assert response_headers["Cache-Control"] == "no-store"
    assert "Set-Cookie" not in response_headers
    assert all(marker not in json.dumps(payload) for marker in PRIVATE_MARKERS)
    assert all(marker not in str(response_headers) for marker in PRIVATE_MARKERS)
    assert all(marker not in caplog.text for marker in PRIVATE_MARKERS)
    assert db_query(session_snapshot) == initial_sessions
    assert (
        db_query("SELECT * FROM auth_login_throttle ORDER BY scope, bucket_key")
        == initial_throttles
    )
    assert (
        _request_json(base_url, "/api/auth/session", headers=headers)[1][
            "authenticated"
        ]
        is True
    )


def test_wrong_login_and_password_are_indistinguishable(app):
    base_url, _ = app

    unknown = _login(base_url, login="unknown", password="wrong")[:2]
    wrong = _login(base_url, password="wrong")[:2]

    assert unknown == wrong == (401, {"error": "Invalid login or password."})


def test_unknown_login_runs_bounded_dummy_argon_verification(app, monkeypatch):
    base_url, _ = app
    verified_hashes = []
    original_verify = identity_persistence._verify_password

    def recording_verify(password_hash, password):
        verified_hashes.append(password_hash)
        return original_verify(password_hash, password)

    monkeypatch.setattr(identity_persistence, "_verify_password", recording_verify)
    unknown_durations = []
    known_durations = []
    for _ in range(3):
        started = time.perf_counter()
        assert _login(base_url, login="unknown", password="wrong")[0] == 401
        unknown_durations.append(time.perf_counter() - started)

        started = time.perf_counter()
        assert _login(base_url, password="wrong")[0] == 401
        known_durations.append(time.perf_counter() - started)

    assert verified_hashes.count(DUMMY_PASSWORD_HASH) == 3
    assert len(verified_hashes) == 6
    unknown_median = statistics.median(unknown_durations)
    known_median = statistics.median(known_durations)
    assert max(unknown_median, known_median) < 5
    assert abs(unknown_median - known_median) < 0.25


def test_login_trims_and_case_folds_only_the_login(app):
    base_url, _ = app

    assert _login(base_url, login="  AdMiN  ")[0] == 200
    assert _login(base_url, password=f" {TEST_PASSWORD}")[0] == 401


def test_login_limits_fields_without_reflecting_the_password(app):
    base_url, _ = app
    secret = "s" * 1025

    status, payload, _ = _request_json(
        base_url,
        "/api/auth/login",
        method="POST",
        payload={"login": "a" * 129, "password": secret},
        headers={"Origin": base_url},
    )

    assert status == 400
    assert payload == {"error": "Invalid login request."}
    assert secret not in json.dumps(payload)


def test_login_requires_json_exact_origin_and_same_origin_fetch_metadata(app):
    base_url, _ = app

    assert _request_json(
        base_url,
        "/api/auth/login",
        method="POST",
        payload={"login": TEST_LOGIN, "password": TEST_PASSWORD},
    )[:2] == (403, {"error": "Request origin not allowed."})
    assert (
        _request_json(
            base_url,
            "/api/auth/login",
            method="POST",
            payload={"login": TEST_LOGIN, "password": TEST_PASSWORD},
            headers={"Origin": "https://evil.example"},
        )[0]
        == 403
    )
    assert (
        _request_json(
            base_url,
            "/api/auth/login",
            method="POST",
            payload={"login": TEST_LOGIN, "password": TEST_PASSWORD},
            headers={"Origin": base_url, "Sec-Fetch-Site": "cross-site"},
        )[0]
        == 403
    )
    status, payload, _ = _request_json(
        base_url,
        "/api/auth/login",
        method="POST",
        headers={"Origin": base_url, "Content-Type": "text/plain"},
    )
    assert (status, payload) == (415, {"error": "Login requires a JSON request."})


def test_login_creates_hashed_session_and_session_status_recovers_csrf(app):
    base_url, _ = app
    status, payload, headers, cookie = _login(base_url)

    assert status == 200
    assert payload["authenticated"] is True
    assert payload["login"] == "admin"
    cookie_value = cookie.split("=", 1)[1]
    assert cookie_value
    set_cookie = headers["Set-Cookie"]
    assert "trellmark_session_dev=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=strict" in set_cookie
    assert "Path=/" in set_cookie
    assert f"Max-Age={int(SESSION_ABSOLUTE_LIFETIME.total_seconds())}" in set_cookie
    assert "Secure" not in set_cookie

    [(secret_hash, csrf_hash)] = db_query(
        "SELECT secret_hash, csrf_secret_hash FROM web_sessions"
    )
    assert isinstance(secret_hash, bytes) and len(secret_hash) == 32
    assert isinstance(csrf_hash, bytes) and len(csrf_hash) == 32
    assert cookie_value.encode() not in {secret_hash, csrf_hash}
    assert payload["csrf_token"].encode() not in {secret_hash, csrf_hash}

    session_status = _request_json(
        base_url,
        "/api/auth/session",
        headers={"Cookie": cookie},
    )
    assert session_status[:2] == (200, payload)


def test_https_origin_uses_host_only_secure_cookie(app, monkeypatch):
    base_url, _ = app
    monkeypatch.setattr(config, "PUBLIC_ORIGIN", "https://urls.example.com")

    status, _, headers, _ = _login(base_url, origin="https://urls.example.com")

    assert status == 200
    set_cookie = headers["Set-Cookie"]
    assert set_cookie.startswith("__Host-trellmark_session=")
    assert "Secure" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=strict" in set_cookie
    assert "Path=/" in set_cookie
    assert "Domain=" not in set_cookie


def test_safe_read_needs_no_csrf_but_every_mutation_does(app):
    base_url, _ = app
    headers = _auth_headers(base_url)
    cookie_only = {"Cookie": headers["Cookie"]}

    assert _request_json(base_url, "/api/groups", headers=cookie_only)[0] == 200
    assert _request_json(
        base_url,
        "/api/urls",
        method="POST",
        payload={"url": "example.com"},
        headers=cookie_only,
    )[:2] == (403, {"error": "Request origin not allowed."})
    assert _request_json(
        base_url,
        "/api/urls",
        method="POST",
        payload={"url": "example.com"},
        headers={"Cookie": headers["Cookie"], "Origin": base_url},
    )[:2] == (403, {"error": "CSRF validation failed."})
    assert (
        _request_json(
            base_url,
            "/api/urls",
            method="POST",
            payload={"url": "example.com"},
            headers={**headers, "Origin": "https://evil.example"},
        )[0]
        == 403
    )
    assert (
        _request_json(
            base_url,
            "/api/urls",
            method="POST",
            payload={"url": "example.com"},
            headers={**headers, "Sec-Fetch-Site": "cross-site"},
        )[0]
        == 403
    )
    assert (
        _request_json(
            base_url,
            "/api/urls",
            method="POST",
            payload={"url": "example.com"},
            headers=headers,
        )[0]
        == 201
    )


def test_csrf_token_is_bound_to_its_own_session(app):
    base_url, _ = app
    _, first, _, first_cookie = _login(base_url)
    _, _, _, second_cookie = _login(base_url)

    status, payload, _ = _request_json(
        base_url,
        "/api/groups",
        method="POST",
        payload={"name": "Nope"},
        headers={
            "Cookie": second_cookie,
            "Origin": base_url,
            "X-CSRF-Token": first["csrf_token"],
        },
    )

    assert (status, payload) == (403, {"error": "CSRF validation failed."})
    assert db_query("SELECT COUNT(*) FROM groups") == [(1,)]


@pytest.mark.parametrize(
    "title_fetcher",
    [RecordingTitleFetcher("Must Not Fetch")],
    indirect=True,
)
def test_non_ascii_csrf_header_is_a_uniform_rejection(app, title_fetcher):
    base_url, _ = app
    headers = _auth_headers(base_url)
    parsed = urlsplit(base_url)
    connection = HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    try:
        connection.request(
            "POST",
            "/api/urls",
            body=json.dumps({"url": "https://must-not-save.example"}),
            headers={
                "Cookie": headers["Cookie"],
                "Origin": base_url,
                "Content-Type": "application/json",
                # Recreate UTF-8 bytes accepted as obs-text by h11 and decoded
                # as Latin-1 by Starlette. compare_digest(str, str) raises for
                # either resulting non-ASCII character unless guarded first.
                "X-CSRF-Token": b"\xc3\xa9abc".decode("latin-1"),
            },
        )
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
    finally:
        connection.close()

    assert (response.status, payload) == (403, {"error": "CSRF validation failed."})
    assert response.headers["Cache-Control"] == "no-store"
    assert db_query("SELECT COUNT(*) FROM urls") == [(0,)]
    assert title_fetcher.calls == []
    assert (
        _request_json(
            base_url,
            "/api/groups",
            headers={"Cookie": headers["Cookie"]},
        )[0]
        == 200
    )


def test_logout_revokes_only_current_session_and_clears_cookie(app):
    base_url, _ = app
    _, first, _, first_cookie = _login(base_url)
    _, _, _, second_cookie = _login(base_url)

    status, payload, headers = _request_json(
        base_url,
        "/api/auth/logout",
        method="POST",
        headers={
            "Cookie": first_cookie,
            "Origin": base_url,
            "X-CSRF-Token": first["csrf_token"],
        },
    )

    assert (status, payload) == (200, {"authenticated": False})
    assert "Max-Age=0" in headers["Set-Cookie"]
    assert (
        _request_json(base_url, "/api/groups", headers={"Cookie": first_cookie})[0]
        == 401
    )
    assert (
        _request_json(base_url, "/api/groups", headers={"Cookie": second_cookie})[0]
        == 200
    )
    assert db_query(
        "SELECT COUNT(*) FROM web_sessions WHERE revoked_at IS NOT NULL"
    ) == [(1,)]


def test_operator_password_rotation_revokes_sessions_and_clears_throttles(app):
    base_url, _ = app
    _, _, _, first_cookie = _login(base_url)
    _, _, _, second_cookie = _login(base_url)
    assert _login(base_url, password="wrong")[0] == 401

    new_password = "rotated-test-password-32-bytes"
    assert set_administrator_password(new_password) == 2
    assert db_query("SELECT COUNT(*) FROM auth_login_throttle") == [(0,)]
    assert db_query(
        "SELECT COUNT(*) FROM web_sessions WHERE revoked_at IS NOT NULL"
    ) == [(2,)]

    for cookie in (first_cookie, second_cookie):
        assert (
            _request_json(base_url, "/api/groups", headers={"Cookie": cookie})[0] == 401
        )
    assert _login(base_url, password=TEST_PASSWORD)[0] == 401
    assert _login(base_url, password=new_password)[0] == 200


def test_operator_password_rotation_rejects_weak_or_oversized_values(app):
    _base_url, _ = app

    with pytest.raises(ValueError, match="at least 20 UTF-8 bytes"):
        set_administrator_password("too-short")
    with pytest.raises(ValueError, match="at most 1024 UTF-8 bytes"):
        set_administrator_password("x" * 1_025)


def test_invalid_logout_is_idempotent_without_origin_or_csrf(app):
    base_url, _ = app
    status, payload, headers = _request_json(
        base_url,
        "/api/auth/logout",
        method="POST",
        headers={"Cookie": "trellmark_session_dev=not-a-session"},
    )

    assert (status, payload) == (200, {"authenticated": False})
    assert "Max-Age=0" in headers["Set-Cookie"]


def test_cross_site_logout_without_a_presented_cookie_cannot_clear_session(app):
    base_url, _ = app
    _, _, _, cookie = _login(base_url)

    status, payload, headers = _request_json(
        base_url,
        "/api/auth/logout",
        method="POST",
        headers={
            "Origin": "https://evil.example",
            "Sec-Fetch-Site": "cross-site",
        },
    )

    assert (status, payload) == (200, {"authenticated": False})
    assert "Set-Cookie" not in headers
    assert _request_json(base_url, "/api/groups", headers={"Cookie": cookie})[0] == 200


@pytest.mark.parametrize("column", ["idle_expires_at", "absolute_expires_at"])
def test_expired_sessions_are_rejected_and_cleared(app, column):
    base_url, _ = app
    _, _, _, cookie = _login(base_url)
    with db_connection() as connection:
        connection.execute(
            text(
                "UPDATE web_sessions SET "
                "created_at = created_at - interval '1 day', "
                "last_used_at = last_used_at - interval '1 day', "
                f"{column} = clock_timestamp() - interval '1 second'"
            )
        )

    status, payload, headers = _request_json(
        base_url, "/api/groups", headers={"Cookie": cookie}
    )

    assert (status, payload) == (401, {"error": "Authentication required."})
    assert "Max-Age=0" in headers["Set-Cookie"]


def test_login_throttle_blocks_after_ten_source_failures(app):
    base_url, _ = app

    for _ in range(10):
        assert _login(base_url, password="wrong")[0] == 401
    status, payload, headers, _ = _login(base_url, password=TEST_PASSWORD)

    assert (status, payload) == (
        429,
        {"error": "Too many login attempts. Try again later."},
    )
    assert 1 <= int(headers["Retry-After"]) <= 15 * 60

    with db_connection() as connection:
        connection.execute(
            text(
                "UPDATE auth_login_throttle SET blocked_until = now() - interval '1 second', "
                "window_started_at = now() - interval '20 minutes'"
            )
        )
    assert _login(base_url)[0] == 200


def test_source_throttle_is_atomic_under_concurrent_failures(app):
    def attempt(_index):
        try:
            create_login_session("203.0.113.8", TEST_LOGIN, "wrong")
        except LoginRejected:
            return "rejected"
        except LoginBlocked:
            return "blocked"
        return "unexpected"

    with ThreadPoolExecutor(max_workers=12) as executor:
        outcomes = list(executor.map(attempt, range(12)))

    assert outcomes.count("rejected") == 10
    assert outcomes.count("blocked") == 2
    assert db_query(
        "SELECT failure_count FROM auth_login_throttle "
        "WHERE scope = 'source' AND bucket_key = '203.0.113.8'"
    ) == [(10,)]


def test_global_throttle_is_persistent_across_sources(app):
    for index in range(100):
        with pytest.raises(LoginRejected):
            create_login_session(f"198.51.100.{index}", TEST_LOGIN, "wrong")

    with pytest.raises(LoginBlocked) as blocked:
        create_login_session("203.0.113.1", TEST_LOGIN, TEST_PASSWORD)
    assert 1 <= blocked.value.retry_after <= 5 * 60


def test_forged_forwarding_headers_do_not_change_proxy_observed_source(app):
    base_url, _ = app
    observed_source = "198.51.100.250"
    for index in range(10):
        status, _, _ = _request_json(
            base_url,
            "/api/auth/login",
            method="POST",
            payload={"login": TEST_LOGIN, "password": "wrong"},
            headers={
                "Origin": base_url,
                "X-Forwarded-For": f"203.0.113.{index}, {observed_source}",
                "Forwarded": f"for=198.51.100.{index};proto=https",
            },
        )
        assert status == 401

    status, _, _ = _request_json(
        base_url,
        "/api/auth/login",
        method="POST",
        payload={"login": TEST_LOGIN, "password": TEST_PASSWORD},
        headers={
            "Origin": base_url,
            "X-Forwarded-For": f"203.0.113.200, {observed_source}",
        },
    )
    assert status == 429
    assert db_query(
        "SELECT bucket_key, failure_count FROM auth_login_throttle "
        "WHERE scope = 'source'"
    ) == [(observed_source, 10)]


def test_successful_login_rehashes_an_outdated_supported_verifier(app):
    base_url, _ = app
    older = PasswordHasher(
        time_cost=3,
        memory_cost=19_456,
        parallelism=1,
        hash_len=32,
        salt_len=16,
        type=Type.ID,
    ).hash(TEST_PASSWORD)
    with db_connection() as connection:
        connection.execute(
            text("UPDATE password_credentials SET password_hash = :password_hash"),
            {"password_hash": older},
        )

    assert _login(base_url)[0] == 200

    [(updated,)] = db_query("SELECT password_hash FROM password_credentials")
    assert updated != older
    assert PASSWORD_HASHER.check_needs_rehash(updated) is False


def test_malformed_or_inactive_credential_uses_uniform_failure(app):
    base_url, _ = app
    with db_connection() as connection:
        connection.execute(
            text("UPDATE password_credentials SET password_hash = 'not-a-phc-string'")
        )

    assert _login(base_url)[:2] == (
        401,
        {"error": "Invalid login or password."},
    )


def test_session_last_use_write_is_coalesced(app):
    base_url, _ = app
    _, _, _, cookie = _login(base_url)
    [(first_last_used, first_idle)] = db_query(
        "SELECT last_used_at, idle_expires_at FROM web_sessions"
    )

    assert _request_json(base_url, "/api/groups", headers={"Cookie": cookie})[0] == 200
    assert db_query("SELECT last_used_at, idle_expires_at FROM web_sessions") == [
        (first_last_used, first_idle)
    ]

    with db_connection() as connection:
        connection.execute(
            text(
                "UPDATE web_sessions SET created_at = created_at - interval '1 hour', "
                "last_used_at = last_used_at - interval '6 minutes'"
            )
        )
    assert _request_json(base_url, "/api/groups", headers={"Cookie": cookie})[0] == 200
    [(updated_last_used, updated_idle)] = db_query(
        "SELECT last_used_at, idle_expires_at FROM web_sessions"
    )
    assert updated_last_used > first_last_used
    assert updated_idle > first_idle


def test_openapi_marks_only_existing_application_routes_as_protected():
    schema = create_app().openapi()

    scheme = schema["components"]["securitySchemes"]["CookieAuth"]
    assert scheme == {
        "type": "apiKey",
        "description": "Opaque server-side Trellmark web session.",
        "in": "cookie",
        "name": config.PRODUCTION_SESSION_COOKIE,
    }
    public = {(path, method.lower()) for method, path in PUBLIC_OPERATIONS}
    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            if (path, method) in public:
                assert "security" not in operation
            else:
                assert operation["security"] == [{"CookieAuth": []}]


@pytest.mark.parametrize(
    ("origin", "normalized"),
    [
        ("https://EXAMPLE.com:443/", "https://example.com"),
        ("https://example.com:8443", "https://example.com:8443"),
        ("http://localhost:8080", "http://localhost:8080"),
        ("http://127.0.0.1:8080", "http://127.0.0.1:8080"),
        ("http://[::1]:8080", "http://[::1]:8080"),
    ],
)
def test_public_origin_normalization(monkeypatch, origin, normalized):
    monkeypatch.setattr(config, "PUBLIC_ORIGIN", origin)
    assert config.public_origin() == normalized


def test_public_origin_parsing_is_cached_by_configured_value(monkeypatch):
    configured_origin = "https://cache-test.example"
    original_urlsplit = config.urlsplit
    calls = []

    def recording_urlsplit(value):
        calls.append(value)
        return original_urlsplit(value)

    monkeypatch.setattr(config, "PUBLIC_ORIGIN", configured_origin)
    monkeypatch.setattr(config, "urlsplit", recording_urlsplit)

    assert config.public_origin() == configured_origin
    assert config.secure_session_cookie() is True
    assert config.session_cookie_name() == config.PRODUCTION_SESSION_COOKIE
    assert calls == [configured_origin]


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "http://urls.example.com",
        "http://192.168.1.10",
        "https://example.com/path",
        "https://user@example.com",
        "https://*.example.com",
    ],
)
def test_public_origin_fails_closed(monkeypatch, origin):
    monkeypatch.setattr(config, "PUBLIC_ORIGIN", origin)
    with pytest.raises(RuntimeError, match="TRELLMARK_PUBLIC_ORIGIN"):
        config.public_origin()


def _identity_service():
    from anyio import CapacityLimiter

    from trellmark.identity.application import IdentityApplicationService
    from trellmark.identity.persistence import (
        PostgresIdentityQueries,
        PostgresIdentityUnitOfWorkFactory,
    )
    from trellmark.platform.runtime import (
        IDENTITY_WORK_CAPACITY,
        AnyIOWorkRunner,
        get_engine,
    )

    return IdentityApplicationService(
        AnyIOWorkRunner(CapacityLimiter(IDENTITY_WORK_CAPACITY)),
        PostgresIdentityUnitOfWorkFactory(get_engine),
        PostgresIdentityQueries(get_engine),
    )


def test_identity_login_service_preserves_throttle_effects_and_session_values(database):
    import anyio

    from trellmark.identity.domain import (
        LoginCommand,
        LoginCreated,
        LoginFailureRecorded,
        LoginThrottled,
        SessionAuthenticated,
        SessionCommand,
        csrf_matches,
    )

    async def scenario():
        service = _identity_service()
        for _ in range(10):
            outcome = await service.login(LoginCommand("203.0.113.8", "admin", "wrong"))
            assert isinstance(outcome, LoginFailureRecorded)
        blocked = await service.login(
            LoginCommand("203.0.113.8", "admin", TEST_PASSWORD)
        )
        assert isinstance(blocked, LoginThrottled)
        assert 1 <= blocked.retry_after <= 900
        created = await service.login(
            LoginCommand("203.0.113.9", "  AdMiN  ", TEST_PASSWORD)
        )
        assert isinstance(created, LoginCreated)
        authenticated = await service.authenticate_session(
            SessionCommand(created.cookie_value, touch=False)
        )
        assert isinstance(authenticated, SessionAuthenticated)
        assert authenticated.session == created.session
        assert csrf_matches(created.session, created.session.csrf_token)
        assert not csrf_matches(created.session, "wrong")
        assert not csrf_matches(created.session, "é")
        assert not hasattr(created.session, "__dict__")
        assert not hasattr(service, "__dict__")
        assert created.cookie_value not in repr(created)
        assert created.session.csrf_token not in repr(created.session)

    anyio.run(scenario)
    assert db_query(
        "SELECT failure_count FROM auth_login_throttle "
        "WHERE scope = 'source' AND bucket_key = '203.0.113.8'"
    ) == [(10,)]


def test_identity_session_uow_rolls_back_without_explicit_commit(database):
    from trellmark.identity.domain import LoginCommand, LoginCreated
    from trellmark.identity.persistence import PostgresIdentityUnitOfWorkFactory
    from trellmark.platform.runtime import get_engine

    factory = PostgresIdentityUnitOfWorkFactory(get_engine)
    with factory() as unit_of_work:
        result = unit_of_work.identity.login(
            LoginCommand("203.0.113.10", TEST_LOGIN, TEST_PASSWORD)
        )
        assert isinstance(result, LoginCreated)

    assert db_query("SELECT COUNT(*) FROM web_sessions") == [(0,)]
    assert db_query("SELECT COUNT(*) FROM auth_login_throttle") == [(0,)]


@pytest.mark.parametrize("failure_stage", ["begin", "work", "commit"])
@pytest.mark.parametrize("cleanup_stage", ["rollback", "close"])
def test_identity_session_uow_preserves_primary_exception_during_cleanup(
    failure_stage, cleanup_stage
):
    """Lifecycle doubles test exception priority, not PostgreSQL semantics."""
    from trellmark.identity.persistence import PostgresIdentityUnitOfWorkFactory

    primary = RuntimeError("synthetic private operation marker")
    secondary = RuntimeError("synthetic private cleanup marker")
    events = []

    class Transaction:
        is_active = True

        def commit(self):
            events.append("commit")
            if failure_stage == "commit":
                raise primary
            self.is_active = False

        def rollback(self):
            events.append("rollback")
            if cleanup_stage == "rollback":
                raise secondary
            self.is_active = False

    class Connection:
        def begin(self):
            events.append("begin")
            if failure_stage == "begin":
                raise primary
            return Transaction()

        def close(self):
            events.append("close")
            if cleanup_stage == "close":
                raise secondary

    class Engine:
        def connect(self):
            events.append("connect")
            return Connection()

    factory = PostgresIdentityUnitOfWorkFactory(Engine)
    with pytest.raises(RuntimeError) as caught:
        with factory() as unit_of_work:
            if failure_stage == "work":
                raise primary
            unit_of_work.commit()

    assert caught.value is primary
    assert events[0:2] == ["connect", "begin"]
    assert events[-1] == "close"


def test_identity_capacity_bounds_database_work_and_keeps_event_loop_responsive(
    database,
):
    import anyio
    from sqlalchemy import event

    from trellmark.platform.runtime import get_engine

    engine = get_engine()
    two_entered = Event()
    release = Event()
    lock = Lock()
    started = []

    def stall_query(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.strip() != "SELECT 1":
            return
        with lock:
            started.append(get_ident())
            if len(started) == 2:
                two_entered.set()
        assert release.wait(timeout=5), "readiness worker was not released"

    async def scenario():
        service = _identity_service()
        results = []
        loop_thread = get_ident()

        async def probe():
            results.append(await service.database_ready())

        try:
            async with anyio.create_task_group() as tasks:
                for _ in range(3):
                    tasks.start_soon(probe)
                assert await anyio.to_thread.run_sync(two_entered.wait, 2)
                with anyio.fail_after(2):
                    while service.work_runner.limiter.statistics().tasks_waiting != 1:
                        await anyio.sleep(0)
                assert len(started) == 2
                assert service.work_runner.limiter.total_tokens == 2
                assert service.work_runner.limiter.borrowed_tokens == 2
                assert all(thread != loop_thread for thread in started)
                # Reaching this line while both SQL calls wait proves loop progress.
                release.set()
        finally:
            release.set()
        assert results == [True, True, True]
        assert len(started) == 3

    event.listen(engine, "before_cursor_execute", stall_query)
    try:
        anyio.run(scenario)
    finally:
        release.set()
        event.remove(engine, "before_cursor_execute", stall_query)


def test_identity_capacity_covers_every_complete_database_operation(
    database, monkeypatch
):
    import anyio
    from sqlalchemy import event

    from trellmark.identity.domain import (
        IdentityCleaned,
        LoginCommand,
        LoginCreated,
        LoginFailureRecorded,
        PasswordRotated,
        RevokeSessionCommand,
        RotatePasswordCommand,
        SeededIdentityValid,
        SessionAuthenticated,
        SessionCommand,
        SessionRevoked,
    )
    from trellmark.platform.runtime import get_engine

    engine = get_engine()
    events = []
    limiters = []
    workload_limiters = []
    original_run_sync = anyio.to_thread.run_sync

    async def recording_run_sync(function, *args, **kwargs):
        assert workload_limiters[0].borrowed_tokens == 1
        limiters.append(kwargs["limiter"])
        return await original_run_sync(function, *args, **kwargs)

    monkeypatch.setattr(anyio.to_thread, "run_sync", recording_run_sync)

    def listener(name):
        def record(*_args):
            assert workload_limiters[0].borrowed_tokens == 1
            events.append((name, get_ident()))

        return record

    listeners = {
        name: listener(name)
        for name in (
            "checkout",
            "begin",
            "before_cursor_execute",
            "commit",
            "rollback",
            "checkin",
        )
    }

    async def scenario():
        service = _identity_service()
        workload_limiters.append(service.work_runner.limiter)
        loop_thread = get_ident()
        operations = [
            (service.database_ready(), bool),
            (service.seeded_identity(), SeededIdentityValid),
            (
                service.login(LoginCommand("203.0.113.11", TEST_LOGIN, "wrong")),
                LoginFailureRecorded,
            ),
            (
                service.login(LoginCommand("203.0.113.11", TEST_LOGIN, TEST_PASSWORD)),
                LoginCreated,
            ),
        ]

        async def check(operation, expected):
            events.clear()
            result = await operation
            assert isinstance(result, expected)
            names = [name for name, _thread in events]
            assert names.count("checkout") == names.count("checkin") == 1
            assert names[0] == "checkout" and names[-1] == "checkin"
            assert names.count("begin") == 1
            assert len({thread for _name, thread in events}) == 1
            assert events[0][1] != loop_thread
            return result

        for operation, expected in operations:
            result = await check(operation, expected)
        assert isinstance(result, LoginCreated)
        for touch in (False, True):
            await check(
                service.authenticate_session(
                    SessionCommand(result.cookie_value, touch=touch)
                ),
                SessionAuthenticated,
            )
            assert ("commit" in [name for name, _ in events]) is touch
        rotated = await check(
            service.rotate_password(RotatePasswordCommand(TEST_PASSWORD)),
            PasswordRotated,
        )
        assert rotated.revoked_sessions == 1
        await check(
            service.revoke_session(RevokeSessionCommand(result.session.id)),
            SessionRevoked,
        )
        await check(service.cleanup(), IdentityCleaned)
        assert len(limiters) == 9
        assert all(limiter.total_tokens == float("inf") for limiter in limiters)
        assert service.work_runner.limiter.borrowed_tokens == 0
        assert service.work_runner.limiter.total_tokens == 2

    for name, callback in listeners.items():
        event.listen(engine, name, callback)
    try:
        anyio.run(scenario)
    finally:
        for name, callback in listeners.items():
            event.remove(engine, name, callback)
    assert db_query("SELECT COUNT(*) FROM web_sessions") == [(0,)]
    assert db_query("SELECT COUNT(*) FROM auth_login_throttle") == [(0,)]


@pytest.mark.parametrize("failure_stage", ["repository", "commit"])
def test_identity_login_capacity_preserves_unexpected_exception_and_rolls_back(
    database, monkeypatch, caplog, failure_stage
):
    import anyio

    from trellmark.identity.domain import LoginCommand

    failure = RuntimeError("synthetic-credential-and-session-marker")
    if failure_stage == "repository":
        original = identity_persistence.PostgresIdentityRepository.login

        def fail_after_write(self, command):
            original(self, command)
            raise failure

        monkeypatch.setattr(
            identity_persistence.PostgresIdentityRepository, "login", fail_after_write
        )
    else:

        def fail_commit(_self):
            raise failure

        monkeypatch.setattr(
            identity_persistence.PostgresIdentityUnitOfWork, "commit", fail_commit
        )

    async def scenario():
        with pytest.raises(RuntimeError) as caught:
            await _identity_service().login(
                LoginCommand("203.0.113.12", TEST_LOGIN, TEST_PASSWORD)
            )
        assert caught.value is failure

    anyio.run(scenario)
    assert db_query("SELECT COUNT(*) FROM web_sessions") == [(0,)]
    assert db_query("SELECT COUNT(*) FROM auth_login_throttle") == [(0,)]
    assert str(failure) not in caplog.text


@pytest.mark.parametrize(
    "operation", ["database_ready", "seeded_identity", "lookup_session"]
)
def test_identity_session_queries_preserve_unexpected_exception_identity(
    database, monkeypatch, operation
):
    import anyio

    from trellmark.identity.domain import LoginCommand, SessionCommand

    failure = RuntimeError("synthetic-private-query-marker")

    def fail_query(_self, *_args):
        raise failure

    async def scenario():
        service = _identity_service()
        created = await service.login(
            LoginCommand("203.0.113.13", TEST_LOGIN, TEST_PASSWORD)
        )
        monkeypatch.setattr(
            identity_persistence.PostgresIdentityQueries, operation, fail_query
        )
        with pytest.raises(RuntimeError) as caught:
            if operation == "lookup_session":
                await service.authenticate_session(
                    SessionCommand(created.cookie_value, touch=False)
                )
            else:
                await getattr(service, operation)()
        assert caught.value is failure

    anyio.run(scenario)


def test_identity_session_missing_outcome_rolls_back_prior_work(database, monkeypatch):
    import anyio

    from trellmark.identity.domain import LoginCommand, SessionCommand, SessionMissing

    def fail_after_write(self, _command):
        self.connection.execute(text("UPDATE users SET status = 'inactive'"))
        return SessionMissing()

    async def scenario():
        service = _identity_service()
        created = await service.login(
            LoginCommand("203.0.113.14", TEST_LOGIN, TEST_PASSWORD)
        )
        monkeypatch.setattr(
            identity_persistence.PostgresIdentityRepository,
            "authenticate_session",
            fail_after_write,
        )
        outcome = await service.authenticate_session(
            SessionCommand(created.cookie_value)
        )
        assert isinstance(outcome, SessionMissing)

    anyio.run(scenario)
    assert db_query("SELECT status FROM users") == [("active",)]


@pytest.mark.parametrize("cookie", [None, "not-a-session", "é", "x" * 129])
def test_identity_session_invalid_cookie_needs_no_database(cookie, monkeypatch):
    import anyio

    from trellmark.identity.domain import SessionCommand, SessionMissing

    def unavailable():
        pytest.fail("invalid or absent cookies must not acquire a connection")

    monkeypatch.setattr(identity_persistence, "get_engine", unavailable)
    assert identity_persistence.authenticate_session(cookie) is None
    assert identity_persistence.authenticate_session(cookie, touch=False) is None

    async def scenario():
        from dataclasses import replace

        service = replace(_identity_service(), uow_factory=unavailable, queries=None)
        for touch in (True, False):
            assert isinstance(
                await service.authenticate_session(SessionCommand(cookie, touch=touch)),
                SessionMissing,
            )

    anyio.run(scenario)
