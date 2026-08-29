import json
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from threading import Event, Lock
from urllib import error, request
from urllib.parse import urlsplit

import pytest
from argon2 import PasswordHasher
from argon2.low_level import Type
from sqlalchemy import text

from tests.helpers import (
    RecordingTitleFetcher,
    db_connection,
    db_query,
    http_json,
    http_raw,
)
from tests.postgres import TEST_LOGIN, TEST_PASSWORD
from trellmark import config
from trellmark.app import create_app
from trellmark.identity import (
    LoginBlocked,
    LoginRejected,
    create_login_session,
    set_administrator_password,
)
from trellmark.identity import api as identity_api
from trellmark.identity import boundary as identity_boundary
from trellmark.identity import repository as identity_repository
from trellmark.identity.policy import (
    DUMMY_PASSWORD_HASH,
    PASSWORD_HASHER,
    SESSION_ABSOLUTE_LIFETIME,
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

    def unexpected_database_probe():
        pytest.fail("public health must not touch PostgreSQL")

    monkeypatch.setattr(identity_api, "database_ready", unexpected_database_probe)

    status, payload, headers = _request_json(base_url, "/api/health")
    assert (status, payload) == (200, {"status": "ok"})
    assert headers["Cache-Control"] == "no-store"


def test_blocking_login_identity_work_does_not_stall_event_loop(app, monkeypatch):
    base_url, _ = app
    entered = Event()
    release = Event()

    def stalled_login(_source, _login, _password):
        entered.set()
        assert release.wait(timeout=5), "test did not release stalled login"
        raise LoginRejected

    monkeypatch.setattr(identity_api, "create_login_session", stalled_login)

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

    def stalled_login(_source, _login, _password):
        nonlocal call_count
        with call_lock:
            call_count += 1
            current_call = call_count
        (first_entered if current_call == 1 else second_entered).set()
        assert release.wait(timeout=5), "test did not release stalled logins"
        raise LoginRejected

    monkeypatch.setattr(identity_api, "create_login_session", stalled_login)

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
    original_authenticate = identity_boundary.authenticate_session

    def stalled_authenticate(cookie_value, *, touch=True):
        entered.set()
        assert release.wait(timeout=5), "test did not release session lookup"
        return original_authenticate(cookie_value, touch=touch)

    monkeypatch.setattr(identity_boundary, "authenticate_session", stalled_authenticate)

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
        (route.path, frozenset(route.methods or ()))
        for route in create_app().routes
        if route.path.startswith("/internal/")
    }

    assert _request_json(base_url, "/internal/ready")[:2] == (
        200,
        {"status": "ok"},
    )
    monkeypatch.setattr(identity_api, "database_ready", lambda: False)
    status, payload, headers = _request_json(base_url, "/internal/ready")
    assert (status, payload) == (503, {"error": "Service unavailable."})
    assert headers["Cache-Control"] == "no-store"
    assert "/internal/ready" not in create_app().openapi()["paths"]
    assert internal_routes == {("/internal/ready", frozenset({"GET"}))}


def test_wrong_login_and_password_are_indistinguishable(app):
    base_url, _ = app

    unknown = _login(base_url, login="unknown", password="wrong")[:2]
    wrong = _login(base_url, password="wrong")[:2]

    assert unknown == wrong == (401, {"error": "Invalid login or password."})


def test_unknown_login_runs_bounded_dummy_argon_verification(app, monkeypatch):
    base_url, _ = app
    verified_hashes = []
    original_verify = identity_repository._verify_password

    def recording_verify(password_hash, password):
        verified_hashes.append(password_hash)
        return original_verify(password_hash, password)

    monkeypatch.setattr(identity_repository, "_verify_password", recording_verify)
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
