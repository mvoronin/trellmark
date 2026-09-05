import asyncio
import json
import threading
from contextlib import contextmanager
from urllib import error, parse, request

from sqlalchemy import inspect, text

from tests.bookmarks import helpers as bookmark_helpers
from tests.postgres import TEST_LOGIN, TEST_PASSWORD
from trellmark.platform import runtime

# The Alembic revision this build expects. Defined once: a test that restores
# `alembic_version` after tampering with it has to write the real head back, and
# a second, stale copy of this string silently leaves the shared session
# database one revision behind.
CURRENT_HEAD = "0001_trellmark_baseline"

_AUTH_BY_ORIGIN = {}


def clear_authentication(base_url):
    _AUTH_BY_ORIGIN.pop(base_url, None)


def _authentication_headers(base_url):
    existing = _AUTH_BY_ORIGIN.get(base_url)
    if existing is not None:
        return existing

    body = json.dumps({"login": TEST_LOGIN, "password": TEST_PASSWORD}).encode("utf-8")
    login_request = request.Request(
        base_url + "/api/auth/login",
        data=body,
        headers={"Content-Type": "application/json", "Origin": base_url},
        method="POST",
    )
    with request.urlopen(login_request, timeout=5) as response:
        payload = json.loads(response.read().decode("utf-8"))
        cookie = response.headers["Set-Cookie"].split(";", 1)[0]
    headers = {"Cookie": cookie, "X-CSRF-Token": payload["csrf_token"]}
    _AUTH_BY_ORIGIN[base_url] = headers
    return headers


def http_json(
    base_url,
    path,
    method="GET",
    payload=None,
    content_type="application/json",
    authenticate=True,
    headers=None,
):
    body = None
    request_headers = dict(headers or {})

    if authenticate:
        request_headers.update(_authentication_headers(base_url))
        if method not in {"GET", "HEAD", "OPTIONS"}:
            request_headers["Origin"] = base_url

    if payload is not None:
        if content_type == "application/json":
            body = json.dumps(payload).encode("utf-8")
        elif content_type == "application/x-www-form-urlencoded":
            body = parse.urlencode(payload).encode("utf-8")
        else:
            body = str(payload).encode("utf-8")
        request_headers["Content-Type"] = content_type

    req = request.Request(
        base_url + path, data=body, headers=request_headers, method=method
    )

    try:
        with request.urlopen(req, timeout=5) as response:
            response_body = response.read().decode("utf-8")
            return response.status, json.loads(response_body)
    except error.HTTPError as response:
        response_body = response.read().decode("utf-8")
        return response.code, json.loads(response_body)


def http_raw(
    base_url,
    path,
    method="GET",
    body=b"",
    content_type=None,
    authenticate=True,
    headers=None,
):
    request_headers = dict(headers or {})
    if authenticate:
        request_headers.update(_authentication_headers(base_url))
        if method not in {"GET", "HEAD", "OPTIONS"}:
            request_headers["Origin"] = base_url
    if content_type:
        request_headers["Content-Type"] = content_type

    req = request.Request(
        base_url + path, data=body, headers=request_headers, method=method
    )

    try:
        with request.urlopen(req, timeout=5) as response:
            response_body = response.read().decode("utf-8")
            return response.status, json.loads(response_body)
    except error.HTTPError as response:
        response_body = response.read().decode("utf-8")
        return response.code, json.loads(response_body)


def assert_validation_error(status, payload):
    assert status == 422
    assert "detail" in payload


class RecordingTitleFetcher:
    def __init__(self, title=None, error=None):
        self.title = title
        self.error = error
        self.calls = []

    async def __call__(self, url):
        self.calls.append(url)
        if self.error:
            raise self.error
        return self.title


def run_async(coro_factory):
    result = {}

    def target():
        try:
            result["value"] = asyncio.run(coro_factory())
        except BaseException as error:
            result["error"] = error

    thread = threading.Thread(target=target)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive(), "async test helper did not finish"
    if "error" in result:
        raise result["error"]
    return result["value"]


def urls_in(payload):
    """The API returns records ({url, created_at}); pull out just the URLs."""
    return [record["url"] for record in payload["urls"]]


def url_ids_in(payload):
    return [record["id"] for record in payload["urls"]]


def all_groups_in(payload):
    """Every group in the response tree, each parent before its own children."""

    def walk(groups):
        for group in groups:
            yield group
            yield from walk(group["children"])

    return list(walk(payload["groups"]))


def group_in(payload, group_name):
    """One group from anywhere in the tree, by name."""
    return next(
        group for group in all_groups_in(payload) if group["name"] == group_name
    )


def grouped_urls_in(payload, group_name):
    return [record["url"] for record in group_in(payload, group_name)["urls"]]


def grouped_url_ids_in(payload, group_name):
    return [record["id"] for record in group_in(payload, group_name)["urls"]]


def group_names_in(payload):
    """Root group names, in sibling order."""
    return [group["name"] for group in payload["groups"]]


def child_names_in(payload, group_name):
    """One group's direct child names, in sibling order."""
    return [child["name"] for child in group_in(payload, group_name)["children"]]


def group_positions_in(payload):
    return [group["position"] for group in payload["groups"]]


def stored_groups():
    """The stored tree, shaped like an API payload so the helpers above fit."""
    return {"groups": bookmark_helpers.group_payloads()}


def capture_storage_statements(monkeypatch):
    """Record every statement storage issues, and hand back the engine.

    The engine has to be disposed by the caller once it is done, because the
    listener stays attached to it.
    """
    from sqlalchemy import event

    statements = []
    engine = runtime.get_engine()

    def record_statement(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record_statement)
    return statements, engine


def capture_connection_identities(monkeypatch):
    """Record DBAPI connection identities from the shared runtime pool."""
    from sqlalchemy import event

    engine = runtime.get_engine()
    identities = []

    def recording_connection(dbapi_connection, _record, _proxy):
        identities.append(id(dbapi_connection))

    event.listen(engine, "checkout", recording_connection)
    return identities, engine


def exported_without_timestamp(payload):
    return {key: value for key, value in payload.items() if key != "exported_at"}


def logical_bookmark_snapshot():
    """Return every durable bookmark value in deterministic row order."""
    return {
        "groups": db_query(
            """
            SELECT id, name, position, created_at, nsfw, parent_id, path::text
            FROM groups
            ORDER BY id
            """
        ),
        "urls": db_query(
            """
            SELECT id, url, title, created_at, important, version
            FROM urls
            ORDER BY id
            """
        ),
        "group_domains": db_query(
            """
            SELECT group_id, domain
            FROM group_domains
            ORDER BY group_id, domain
            """
        ),
        "url_groups": db_query(
            """
            SELECT url_id, group_id
            FROM url_groups
            ORDER BY url_id, group_id
            """
        ),
    }


def public_bookmark_snapshot(base_url):
    """Return the authenticated bookmark views with volatile export time removed."""
    groups_status, groups = http_json(base_url, "/api/groups")
    urls_status, urls = http_json(base_url, "/api/urls")
    export_status, exported = http_json(base_url, "/api/export")
    assert (groups_status, urls_status, export_status) == (200, 200, 200)
    return {
        "groups": groups,
        "urls": urls,
        "export": exported_without_timestamp(exported),
    }


@contextmanager
def db_connection():
    """Open a committing connection to the test database.

    Tests that need to assert on or set up raw rows go through the app's own
    engine, so they always target whatever database the `database` fixture
    pointed the app at.
    """
    with runtime.get_engine().connect() as connection:
        with connection.begin():
            yield connection


def db_query(sql, **params):
    """Run one read-only statement and return its rows as tuples."""
    with db_connection() as connection:
        return [tuple(row) for row in connection.execute(text(sql), params)]


def table_columns(table):
    """Return a table's column names, via SQLAlchemy inspection."""
    inspector = inspect(runtime.get_engine())
    return [column["name"] for column in inspector.get_columns(table)]


def table_indexes(table):
    """Return {index name: [column names]} for a table."""
    inspector = inspect(runtime.get_engine())
    return {
        index["name"]: list(index["column_names"])
        for index in inspector.get_indexes(table)
    }
