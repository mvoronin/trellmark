import ast
import json
from collections import Counter
from importlib.util import resolve_name
from pathlib import Path

import pytest
from fastapi import Request
from fastapi.routing import APIRoute, iter_route_contexts
from starlette.routing import Mount

from tests.helpers import run_async
from trellmark.app import create_app
from trellmark.backup import api as backup_api
from trellmark.backup.application import (
    BackupApplicationService,
    ImportInvalid,
    ImportSucceeded,
)
from trellmark.bookmarks import api, domain
from trellmark.bookmarks.application import BookmarksApplicationService
from trellmark.bookmarks.backup import (
    PostgresBookmarkBackupContributor,
    PostgresBookmarkSnapshotContributor,
)
from trellmark.identity import api as identity_api
from trellmark.identity import domain as identity_domain
from trellmark.identity.application import IdentityApplicationService

REPOSITORY_ROOT = Path(__file__).parents[2]
GENERATED_OPENAPI = REPOSITORY_ROOT / "web" / "src" / "generated" / "openapi.json"

EXPECTED_ROUTE_MANIFEST = (
    ("api", "/api/auth/login", frozenset({"POST"}), "login_api_auth_login_post", True),
    (
        "api",
        "/api/auth/session",
        frozenset({"GET"}),
        "session_status_api_auth_session_get",
        True,
    ),
    (
        "api",
        "/api/auth/logout",
        frozenset({"POST"}),
        "logout_api_auth_logout_post",
        True,
    ),
    ("api", "/api/health", frozenset({"GET"}), "health_api_health_get", True),
    ("api", "/internal/ready", frozenset({"GET"}), "readiness", False),
    ("api", "/api/export", frozenset({"GET"}), "export_data_api_export_get", True),
    (
        "api",
        "/api/import",
        frozenset({"POST"}),
        "import_data_api_import_post",
        True,
    ),
    ("api", "/api/groups", frozenset({"GET"}), "list_groups_api_groups_get", True),
    (
        "api",
        "/api/groups",
        frozenset({"POST"}),
        "create_group_api_groups_post",
        True,
    ),
    (
        "api",
        "/api/groups/order",
        frozenset({"PATCH"}),
        "reorder_groups_api_groups_order_patch",
        True,
    ),
    (
        "api",
        "/api/groups/{group_id}",
        frozenset({"PATCH"}),
        "edit_group_api_groups__group_id__patch",
        True,
    ),
    (
        "api",
        "/api/groups/{group_id}",
        frozenset({"DELETE"}),
        "delete_group_api_groups__group_id__delete",
        True,
    ),
    ("api", "/api/urls", frozenset({"GET"}), "list_urls_api_urls_get", True),
    ("api", "/api/urls", frozenset({"POST"}), "create_url_api_urls_post", True),
    (
        "api",
        "/api/urls/{url_id}/group",
        frozenset({"PATCH"}),
        "move_url_group_api_urls__url_id__group_patch",
        True,
    ),
    (
        "api",
        "/api/urls/{url_id}/important",
        frozenset({"PATCH"}),
        "set_important_api_urls__url_id__important_patch",
        True,
    ),
    (
        "api",
        "/api/urls/{url_id}/refresh-title",
        frozenset({"POST"}),
        "refresh_url_title_api_urls__url_id__refresh_title_post",
        True,
    ),
    (
        "api",
        "/api/urls/{url_id}/icon",
        frozenset({"GET"}),
        "get_url_icon_api_urls__url_id__icon_get",
        True,
    ),
    (
        "api",
        "/api/urls/{url_id}/refresh-metadata",
        frozenset({"POST"}),
        "refresh_url_metadata_api_urls__url_id__refresh_metadata_post",
        True,
    ),
    (
        "api",
        "/api/urls/{url_id}",
        frozenset({"PATCH"}),
        "edit_url_api_urls__url_id__patch",
        True,
    ),
    (
        "api",
        "/api/urls/{url_id}",
        frozenset({"DELETE"}),
        "delete_url_by_id_api_urls__url_id__delete",
        True,
    ),
    ("mount", "/static", frozenset(), "static", None),
    ("api", "/{spa_path:path}", frozenset({"GET", "HEAD"}), "_serve_spa", False),
)

EXPECTED_OPERATION_MANIFEST = {
    ("/api/auth/login", "post"): ("login_api_auth_login_post", {"200"}, False),
    ("/api/auth/logout", "post"): ("logout_api_auth_logout_post", {"200"}, False),
    ("/api/auth/session", "get"): (
        "session_status_api_auth_session_get",
        {"200"},
        False,
    ),
    ("/api/export", "get"): ("export_data_api_export_get", {"200"}, True),
    ("/api/groups", "get"): ("list_groups_api_groups_get", {"200"}, True),
    ("/api/groups", "post"): ("create_group_api_groups_post", {"201"}, True),
    ("/api/groups/order", "patch"): (
        "reorder_groups_api_groups_order_patch",
        {"200", "422"},
        True,
    ),
    ("/api/groups/{group_id}", "delete"): (
        "delete_group_api_groups__group_id__delete",
        {"200", "422"},
        True,
    ),
    ("/api/groups/{group_id}", "patch"): (
        "edit_group_api_groups__group_id__patch",
        {"200", "422"},
        True,
    ),
    ("/api/health", "get"): ("health_api_health_get", {"200"}, False),
    ("/api/import", "post"): (
        "import_data_api_import_post",
        {"200", "409", "422", "500"},
        True,
    ),
    ("/api/urls", "get"): ("list_urls_api_urls_get", {"200"}, True),
    ("/api/urls", "post"): ("create_url_api_urls_post", {"201"}, True),
    ("/api/urls/{url_id}", "delete"): (
        "delete_url_by_id_api_urls__url_id__delete",
        {"200", "422"},
        True,
    ),
    ("/api/urls/{url_id}", "patch"): (
        "edit_url_api_urls__url_id__patch",
        {"200", "422"},
        True,
    ),
    ("/api/urls/{url_id}/group", "patch"): (
        "move_url_group_api_urls__url_id__group_patch",
        {"200", "422"},
        True,
    ),
    ("/api/urls/{url_id}/icon", "get"): (
        "get_url_icon_api_urls__url_id__icon_get",
        {"200", "404", "422"},
        True,
    ),
    ("/api/urls/{url_id}/important", "patch"): (
        "set_important_api_urls__url_id__important_patch",
        {"200", "422"},
        True,
    ),
    ("/api/urls/{url_id}/refresh-metadata", "post"): (
        "refresh_url_metadata_api_urls__url_id__refresh_metadata_post",
        {"200", "422"},
        True,
    ),
    ("/api/urls/{url_id}/refresh-title", "post"): (
        "refresh_url_title_api_urls__url_id__refresh_title_post",
        {"200", "422"},
        True,
    ),
}

EXPECTED_SCHEMA_NAMES = {
    "AuthenticatedSessionResponse",
    "CreateGroupResponse",
    "CreateURLResponse",
    "DeleteGroupResponse",
    "DeleteURLByIDResponse",
    "EditGroupResponse",
    "EditURLResponse",
    "ExportDocument",
    "ExportGroupRecord",
    "ExportURLRecord",
    "GroupRecord",
    "GroupsResponse",
    "HTTPValidationError",
    "HealthResponse",
    "ImportConflictResponse",
    "ImportDocument",
    "ImportFailedResponse",
    "ImportGroupRecord",
    "ImportResponse",
    "ImportURLRecord",
    "InvalidImportResponse",
    "MoveURLGroup",
    "MoveURLGroupResponse",
    "RefreshURLMetadataResponse",
    "RefreshURLTitleResponse",
    "ReorderGroups",
    "ReorderGroupsResponse",
    "SetImportant",
    "SetImportantResponse",
    "URLRecord",
    "URLsResponse",
    "UnauthenticatedSessionResponse",
    "ValidationError",
}


def _route_manifest_entry(route: object):
    if isinstance(route, APIRoute):
        identifier = route.unique_id if route.include_in_schema else route.name
        return (
            "api",
            route.path,
            frozenset(route.methods or ()),
            identifier,
            route.include_in_schema,
        )
    if isinstance(route, Mount):
        return ("mount", route.path, frozenset(), route.name, None)
    raise AssertionError(f"Unexpected application route type: {type(route).__name__}")


def test_application_route_manifest_is_complete_and_unique():
    observed = [
        _route_manifest_entry(context.route)
        for context in iter_route_contexts(create_app().routes)
    ]

    assert Counter(observed) == Counter(EXPECTED_ROUTE_MANIFEST)
    assert len(observed) == len(EXPECTED_ROUTE_MANIFEST)


def test_process_and_package_export_only_construction_and_cli_entry_points():
    import trellmark
    from trellmark.cli import main

    assert set(trellmark.__all__) == {"create_app", "main"}
    assert trellmark.create_app is create_app
    assert trellmark.main is main
    for name in ("add_url", "add_group", "run_migrations", "fetch_url_title"):
        assert not hasattr(trellmark, name)
    for path in ("trellmark/__init__.py", "server.py"):
        imports = [
            node
            for node in ast.walk(ast.parse((REPOSITORY_ROOT / path).read_text()))
            if isinstance(node, ast.ImportFrom)
        ]
        assert all(alias.name != "*" for node in imports for alias in node.names)
        assert all(node.module in {"app", "cli", "trellmark.cli"} for node in imports)
    assert any(
        node.module == "trellmark.cli"
        and [alias.name for alias in node.names] == ["main"]
        for node in imports
    )


def test_production_sources_use_explicit_owners_after_all_legacy_modules_are_deleted():
    legacy_names = {
        "handlers",
        "models",
        "storage",
        "storage_types",
        "responses",
        "url_normalization",
        "page_titles",
        "title_text",
        "site_icons",
        "app_keys",
    }
    package_root = REPOSITORY_ROOT / "trellmark"
    for name in legacy_names:
        assert not (package_root / f"{name}.py").exists(), name
        assert not (package_root / name).exists(), name

    # Root submodule imports are explicit owners; root callable imports and
    # importing the package object would reintroduce the broad facade.
    root_modules = {
        path.stem for path in package_root.glob("*.py") if path.stem != "__init__"
    } | {
        path.name for path in package_root.iterdir() if (path / "__init__.py").exists()
    }
    forbidden = {f"trellmark.{name}" for name in legacy_names}
    sources = [REPOSITORY_ROOT / "server.py"]
    for directory in ("trellmark", "scripts", "migrations"):
        sources.extend((REPOSITORY_ROOT / directory).rglob("*.py"))
    for path in sources:
        package = ".".join(path.relative_to(REPOSITORY_ROOT).parent.parts)
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            targets = set()
            if isinstance(node, ast.Import):
                targets.update(alias.name for alias in node.names)
                assert "trellmark" not in targets, (path, node.lineno)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level:
                    module = resolve_name("." * node.level + module, package)
                targets.add(module)
                targets.update(f"{module}.{alias.name}" for alias in node.names)
                if module == "trellmark":
                    assert all(alias.name in root_modules for alias in node.names), (
                        path,
                        node.lineno,
                    )
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                targets.add(node.value)
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                if node.value.id == "trellmark":
                    assert node.attr in root_modules, (path, node.lineno)
            assert not any(
                target == old or target.startswith(f"{old}.")
                for target in targets
                for old in forbidden
            ), (path, node.lineno, targets)


def test_composition_installs_outward_exception_handlers_without_owning_mapping():
    from fastapi.exceptions import RequestValidationError
    from sqlalchemy.exc import SQLAlchemyError

    application = create_app()
    for kind in (RequestValidationError, SQLAlchemyError):
        assert (
            application.exception_handlers[kind].__module__ == "trellmark.platform.http"
        )
    source = ast.parse((REPOSITORY_ROOT / "trellmark/app.py").read_text())
    assert {
        node.name
        for node in source.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    } == {"create_app", "_serve_spa"}


def test_identity_login_session_health_contracts_and_routes_capture_one_service():
    from dataclasses import FrozenInstanceError
    from inspect import getclosurevars

    from trellmark.identity import api as identity_api
    from trellmark.identity.application import IdentityApplicationService

    application = create_app()
    service = getattr(application.state, "identity_application_service", None)
    assert isinstance(service, IdentityApplicationService)
    assert service.work_runner.limiter.total_tokens == 2
    with pytest.raises(FrozenInstanceError):
        service.queries = object()
    routes = [
        context.route
        for context in iter_route_contexts(application.routes)
        if isinstance(context.route, APIRoute)
        and context.route.path
        in {
            "/api/auth/login",
            "/api/auth/session",
            "/api/auth/logout",
            "/api/health",
            "/internal/ready",
        }
    ]
    assert len(routes) == 5
    for route in routes:
        assert route.endpoint.__module__ == identity_api.__name__
        if route.path != "/api/health":
            assert service in getclosurevars(route.endpoint).nonlocals.values()
    for name in (
        "LoginRequest",
        "AuthenticatedSessionResponse",
        "UnauthenticatedSessionResponse",
        "HealthResponse",
    ):
        assert getattr(identity_api, name).__module__ == identity_api.__name__


@pytest.mark.parametrize("operation", ["login", "session", "logout", "health", "ready"])
def test_identity_routes_await_the_exact_captured_service(monkeypatch, operation):
    from trellmark import config

    monkeypatch.setattr(config, "PUBLIC_ORIGIN", "http://localhost")
    application = create_app()
    service = application.state.identity_application_service
    application.state.identity_application_service = object()
    session = identity_domain.AuthSession(7, 1, "admin", "synthetic-csrf")
    calls = []

    def observe(name, result):
        async def observed(instance, *arguments):
            assert instance is service
            calls.append((name, arguments))
            return result

        monkeypatch.setattr(IdentityApplicationService, name, observed)

    observe("login", identity_domain.LoginCreated(session, "synthetic-cookie"))
    observe("authenticate_session", identity_domain.SessionAuthenticated(session))
    observe("revoke_session", identity_domain.SessionRevoked())
    observe("database_ready", True)
    path = {
        "health": "/api/health",
        "ready": "/internal/ready",
    }.get(operation, f"/api/auth/{operation}")
    endpoint = next(
        context.route.endpoint
        for context in iter_route_contexts(application.routes)
        if isinstance(context.route, APIRoute) and context.route.path == path
    )

    async def receive():
        return {
            "type": "http.request",
            "body": b'{"login":"  AdMiN  ","password":" synthetic-password "}',
        }

    async def exercise():
        for _ in range(2):
            request = Request(
                {
                    "type": "http",
                    "app": application,
                    "client": ("203.0.113.8", 1234),
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"origin", b"http://localhost"),
                        (b"x-csrf-token", b"synthetic-csrf"),
                        (b"cookie", b"trellmark_session_dev=synthetic-cookie"),
                    ],
                },
                receive,
            )
            arguments = {} if operation in {"health", "ready"} else {"request": request}
            response = await endpoint(**arguments)
            assert response.status_code == 200
            assert json.loads(response.body) == (
                {"status": "ok"}
                if operation in {"health", "ready"}
                else {"authenticated": False}
                if operation == "logout"
                else {
                    "authenticated": True,
                    "login": "admin",
                    "csrf_token": "synthetic-csrf",
                }
            )

    run_async(exercise)
    expected = {
        "login": [
            (
                "login",
                (
                    identity_domain.LoginCommand(
                        "203.0.113.8", "admin", " synthetic-password "
                    ),
                ),
            )
        ],
        "session": [
            (
                "authenticate_session",
                (identity_domain.SessionCommand("synthetic-cookie"),),
            )
        ],
        "logout": [
            (
                "authenticate_session",
                (identity_domain.SessionCommand("synthetic-cookie", touch=False),),
            ),
            ("revoke_session", (identity_domain.RevokeSessionCommand(7),)),
        ],
        "health": [],
        "ready": [("database_ready", ())],
    }
    assert calls == expected[operation] * 2


@pytest.mark.parametrize("cookie_present", [False, True])
@pytest.mark.parametrize("secure", [False, True])
@pytest.mark.parametrize(
    "kind", ["created", "rejected", "throttled", "authenticated", "missing", "revoked"]
)
def test_identity_mapper_preserves_every_outcome_and_cookie_attribute(
    monkeypatch, kind, secure, cookie_present
):
    from http.cookies import SimpleCookie

    from trellmark import config

    monkeypatch.setattr(
        config,
        "PUBLIC_ORIGIN",
        "https://EXAMPLE.com:443/" if secure else "http://localhost",
    )
    session = identity_domain.AuthSession(7, 1, "admin", "synthetic-csrf")
    outcome = {
        "created": identity_domain.LoginCreated(session, "synthetic-cookie"),
        "rejected": identity_domain.LoginFailureRecorded(),
        "throttled": identity_domain.LoginThrottled(73),
        "authenticated": identity_domain.SessionAuthenticated(session),
        "missing": identity_domain.SessionMissing(),
        "revoked": identity_domain.SessionRevoked(),
    }[kind]
    response = identity_api.map_identity_outcome(outcome, cookie_present=cookie_present)
    assert response.status_code == {"rejected": 401, "throttled": 429}.get(kind, 200)
    assert response.headers.get("Retry-After") == (
        "73" if kind == "throttled" else None
    )
    authenticated = {
        "authenticated": True,
        "login": "admin",
        "csrf_token": "synthetic-csrf",
    }
    expected = {
        "created": authenticated,
        "authenticated": authenticated,
        "rejected": {"error": "Invalid login or password."},
        "throttled": {"error": "Too many login attempts. Try again later."},
        "missing": {"authenticated": False},
        "revoked": {"authenticated": False},
    }
    assert json.loads(response.body) == expected[kind]
    if kind != "created" and not (cookie_present and kind in {"missing", "revoked"}):
        assert "set-cookie" not in response.headers
        return
    cookies = SimpleCookie(response.headers["set-cookie"])
    cookie_name = "__Host-trellmark_session" if secure else "trellmark_session_dev"
    assert set(cookies) == {cookie_name}
    cookie = cookies[cookie_name]
    assert cookie.value == ("synthetic-cookie" if kind == "created" else "")
    assert cookie["max-age"] == ("86400" if kind == "created" else "0")
    assert cookie["path"] == "/"
    assert cookie["httponly"] is True
    assert cookie["samesite"] == "strict"
    assert bool(cookie["secure"]) is secure
    assert cookie["domain"] == ""
    assert bool(cookie["expires"]) is (kind != "created")


def test_group_routes_and_contracts_are_feature_owned():
    routes = [
        context.route
        for context in iter_route_contexts(create_app().routes)
        if isinstance(context.route, APIRoute)
        and context.route.path.startswith("/api/groups")
    ]
    assert len(routes) == 5
    assert all(
        route.endpoint.__module__ == "trellmark.bookmarks.api" for route in routes
    )
    for name in (
        "CreateGroup",
        "EditGroup",
        "DeleteGroup",
        "ReorderGroups",
        "GroupsResponse",
    ):
        assert getattr(api, name).__module__ == api.__name__


def test_import_export_routes_and_contracts_are_backup_owned():
    import trellmark
    from trellmark.backup import api as backup_api

    routes = [
        context.route
        for context in iter_route_contexts(create_app().routes)
        if isinstance(context.route, APIRoute)
        and context.route.path in {"/api/import", "/api/export"}
    ]
    assert len(routes) == 2
    assert all(route.endpoint.__module__ == backup_api.__name__ for route in routes)
    for name in ("import_data", "export_data"):
        assert not hasattr(trellmark, name)
    for name in ("import_saved_data", "export_saved_data", "legacy_import_document"):
        assert not hasattr(trellmark, name)
    for name in (
        "ImportDocument",
        "ImportGroupRecord",
        "ImportURLRecord",
        "ExportDocument",
        "ExportGroupRecord",
        "ExportURLRecord",
        "ImportResponse",
        "InvalidImportResponse",
        "ImportConflictResponse",
        "ImportFailedResponse",
    ):
        assert getattr(backup_api, name).__module__ == backup_api.__name__


@pytest.mark.parametrize("operation", ["import", "export"])
def test_import_export_routes_capture_one_composed_backup_service(
    monkeypatch, operation
):
    from datetime import datetime, timezone

    application = create_app()
    service = application.state.backup_application_service
    assert service.work_runner.limiter.total_tokens == 1
    assert service.uow_factory.contributor_factory is PostgresBookmarkBackupContributor
    assert (
        service.snapshot_factory.contributor_factory
        is PostgresBookmarkSnapshotContributor
    )
    application.state.backup_application_service = object()
    document = backup_api.ImportDocument.model_validate(
        {
            "version": 1,
            "exported_at": "2026-09-05T12:34:56Z",
            "groups": [{"name": "Reading", "position": 0, "urls": []}],
        }
    )
    group = domain.GroupRecord(2, "Reading", None, 0, 1, False, (), ())
    calls = []

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz is timezone.utc
            return cls(2026, 9, 5, 12, 34, 56, 123456, tzinfo=tz)

    async def observe(instance, argument):
        assert instance is service
        calls.append(argument)
        if operation == "import":
            return ImportSucceeded(0, 2, (group,))
        return document.to_domain()

    monkeypatch.setattr(backup_api, "datetime", Clock)
    monkeypatch.setattr(BackupApplicationService, f"{operation}_document", observe)
    endpoint = next(
        context.route.endpoint
        for context in iter_route_contexts(application.routes)
        if isinstance(context.route, APIRoute)
        and context.route.path == f"/api/{operation}"
    )
    arguments = {"document": document} if operation == "import" else {}
    for _ in range(2):
        response = run_async(lambda: endpoint(**arguments))
        assert response.status_code == 200
        if operation == "import":
            assert json.loads(response.body) == {
                "imported": 0,
                "skipped": 2,
                "groups": [api.group_to_wire(group).model_dump()],
            }
        else:
            assert json.loads(response.body) == document.model_dump()
            assert response.headers["content-disposition"] == (
                'attachment; filename="trellmark-export-20260905T123456Z.json"'
            )
    assert (
        calls
        == [document.to_domain() if operation == "import" else document.exported_at] * 2
    )


@pytest.mark.parametrize(
    ("outcome", "status", "payload"),
    [
        (ImportSucceeded(1, 2, ()), 200, {"imported": 1, "skipped": 2, "groups": []}),
        (
            ImportInvalid(),
            422,
            {"error": "Invalid import file.", "code": "invalid_import"},
        ),
        (
            domain.BookmarkMutationConflict(),
            409,
            {
                "error": "Another bookmark change is in progress. "
                "No import changes were saved. Try again.",
                "code": "import_conflict",
            },
        ),
    ],
)
def test_import_mapper_preserves_every_application_outcome(outcome, status, payload):
    response = backup_api.map_import_outcome(outcome)

    assert response.status_code == status
    assert json.loads(response.body) == payload


def test_ordinary_url_routes_and_contracts_are_feature_owned():
    from trellmark.bookmarks import api

    operations = {
        ("/api/urls", "GET"),
        ("/api/urls/{url_id}", "PATCH"),
        ("/api/urls/{url_id}", "DELETE"),
        ("/api/urls/{url_id}/group", "PATCH"),
        ("/api/urls/{url_id}/important", "PATCH"),
    }
    routes = [
        context.route
        for context in iter_route_contexts(create_app().routes)
        if isinstance(context.route, APIRoute)
        and any(
            (context.route.path, method) in operations
            for method in context.route.methods
        )
    ]
    assert len(routes) == len(operations)
    assert all(route.endpoint.__module__ == api.__name__ for route in routes)
    for name in (
        "EditURL",
        "MoveURLGroup",
        "SetImportant",
        "URLsResponse",
        "EditURLResponse",
        "MoveURLGroupResponse",
        "SetImportantResponse",
        "DeleteURLByIDResponse",
    ):
        assert getattr(api, name).__module__ == api.__name__


def test_create_and_title_routes_and_contracts_are_feature_owned():
    paths = {"/api/urls", "/api/urls/{url_id}/refresh-title"}
    routes = [
        context.route
        for context in iter_route_contexts(create_app().routes)
        if isinstance(context.route, APIRoute)
        and context.route.path in paths
        and "POST" in context.route.methods
    ]
    assert len(routes) == 2
    assert all(route.endpoint.__module__ == api.__name__ for route in routes)
    for name in ("CreateURL", "CreateURLResponse", "RefreshURLTitleResponse"):
        assert getattr(api, name).__module__ == api.__name__


def test_icon_and_metadata_routes_and_contracts_are_feature_owned():
    paths = {"/api/urls/{url_id}/icon", "/api/urls/{url_id}/refresh-metadata"}
    routes = [
        context.route
        for context in iter_route_contexts(create_app().routes)
        if isinstance(context.route, APIRoute) and context.route.path in paths
    ]
    assert len(routes) == 2
    assert all(route.endpoint.__module__ == api.__name__ for route in routes)
    assert api.RefreshURLMetadataResponse.__module__ == api.__name__


def test_icon_metadata_legacy_modules_and_forwarders_are_absent():
    import trellmark

    for filename in (
        "site_icons.py",
        "app_keys.py",
        "handlers.py",
        "models.py",
        "responses.py",
        "storage.py",
        "storage_types.py",
    ):
        assert not (REPOSITORY_ROOT / "trellmark" / filename).exists()
    for name in ("get_url_icon", "refresh_url_metadata", "_site_icon_service"):
        assert not hasattr(trellmark, name)


@pytest.mark.parametrize("operation", ["icon", "refresh-metadata"])
@pytest.mark.parametrize("title_updated", [False, True])
@pytest.mark.parametrize("icon_updated", [False, True])
def test_icon_and_metadata_routes_capture_composed_service(
    monkeypatch, operation, title_updated, icon_updated
):
    from tests.api.test_site_icon_api import PNG, RecordingIconService

    gateway = RecordingIconService(icon=domain.SiteIcon(PNG, "image/png"))
    application = create_app(icon_service=gateway)
    service = application.state.bookmarks_application_service
    assert service.icon_gateway is gateway
    application.state.bookmarks_application_service = object()
    application.state.site_icon_service = object()
    record = domain.URLRecord(
        7, "https://example.com", "Example", "2026-08-30T00:00:00Z", False, 2
    )
    calls = []

    def observe(name, result):
        async def observed(instance, *arguments):
            assert instance is service
            calls.append((name, arguments))
            return result

        monkeypatch.setattr(BookmarksApplicationService, name, observed)

    observe("url_by_id", record)
    observe(
        "refresh_url_metadata",
        domain.URLMetadataRefreshed(record, title_updated, icon_updated),
    )
    observe("list_groups", ())
    endpoint = next(
        context.route.endpoint
        for context in iter_route_contexts(application.routes)
        if isinstance(context.route, APIRoute)
        and context.route.path == f"/api/urls/{{url_id}}/{operation}"
    )
    response = run_async(lambda: endpoint(url_id=7))
    assert response.status_code == 200
    if operation == "icon":
        assert calls == [("url_by_id", (7,))]
        assert gateway.get_calls == [record.url]
        assert response.body == PNG
        assert response.headers["content-type"] == "image/png"
        assert response.headers["x-content-type-options"] == "nosniff"
    else:
        assert calls == [("refresh_url_metadata", (7,)), ("list_groups", ())]
        assert json.loads(response.body) == {
            "url": api.url_to_wire(record).model_dump(),
            "groups": [],
            "title_updated": title_updated,
            "icon_updated": icon_updated,
        }
        assert gateway.get_calls == gateway.refresh_calls == []


@pytest.mark.parametrize("operation", ["create_url", "refresh_url_title"])
@pytest.mark.parametrize("title_updated", [False, True])
def test_create_and_title_routes_await_the_exact_composed_service(
    monkeypatch, operation, title_updated
):
    async def title_fetcher(_url):
        raise AssertionError("The transport must await the service, not fetch titles.")

    application = create_app(title_fetcher=title_fetcher)
    service = application.state.bookmarks_application_service
    assert service.title_fetcher is title_fetcher
    # Route closures retain the constructed service, independent of mutable state.
    application.state.bookmarks_application_service = object()
    record = domain.URLRecord(
        7, "https://example.com", "Example", "2026-08-30T00:00:00Z", False, 2
    )
    group = domain.GroupRecord(
        2, "Reading", None, 1, 1, False, ("example.com",), (record,)
    )
    expected_record = {
        "id": 7,
        "url": "https://example.com",
        "title": "Example",
        "created_at": "2026-08-30T00:00:00Z",
        "important": False,
        "version": 2,
    }
    expected_groups = [
        {
            "id": 2,
            "name": "Reading",
            "parent_id": None,
            "position": 1,
            "depth": 1,
            "nsfw": False,
            "domains": ["example.com"],
            "urls": [expected_record],
            "children": [],
        }
    ]
    calls = []

    def observe(name, result):
        async def observed(instance, *arguments):
            assert instance is service
            calls.append((name, arguments))
            return result

        monkeypatch.setattr(BookmarksApplicationService, name, observed)

    observe("create_url", domain.URLCreated(record))
    observe("refresh_url_title", domain.URLTitleRefreshed(record, title_updated))
    observe("list_urls", (record,))
    observe("list_groups", (group,))

    async def receive():
        return {"type": "http.request", "body": b'{"url":"Example.COM/"}'}

    path = (
        "/api/urls" if operation == "create_url" else "/api/urls/{url_id}/refresh-title"
    )
    request = Request(
        {
            "type": "http",
            "app": application,
            "headers": [(b"content-type", b"application/json")],
        },
        receive,
    )
    arguments = {"request": request} if operation == "create_url" else {"url_id": 7}
    endpoint = next(
        context.route.endpoint
        for context in iter_route_contexts(application.routes)
        if isinstance(context.route, APIRoute)
        and context.route.path == path
        and "POST" in context.route.methods
    )
    response = run_async(lambda: endpoint(**arguments))
    expected_payload = {"url": expected_record, "groups": expected_groups}
    if operation == "create_url":
        assert response.status_code == 201
        expected_payload["urls"] = [expected_record]
        assert calls == [
            ("create_url", (domain.CreateURL("https://example.com"),)),
            ("list_urls", ()),
            ("list_groups", ()),
        ]
    else:
        assert response.status_code == 200
        expected_payload["title_updated"] = title_updated
        assert calls == [("refresh_url_title", (7,)), ("list_groups", ())]
    assert json.loads(response.body) == expected_payload


@pytest.mark.parametrize(
    ("outcome", "removal", "status", "message"),
    [
        (domain.URLNotFound(7), False, 404, "This URL is not saved."),
        (
            domain.URLConflict("https://example.com"),
            False,
            409,
            "This URL is already saved.",
        ),
        (
            domain.URLVersionConflict(7, 2),
            False,
            409,
            "This URL was changed. Reload and try again.",
        ),
        (domain.EmptyURLEdit(7), False, 400, "Enter a valid value."),
        (domain.URLTitleFetchFailed(7), False, 502, "Could not fetch a page title."),
        (domain.GroupNotFound(2), False, 404, "This group does not exist."),
        (
            domain.URLSourceRequired(7, (1, 2)),
            False,
            400,
            "Choose the URL's source group.",
        ),
        (
            domain.URLMembershipNotFound(7, 2),
            False,
            404,
            "This URL is not saved in the source group.",
        ),
        (domain.URLMembershipNotFound(7, 2), True, 404, "This URL is not saved."),
        (
            domain.BookmarkMutationConflict(),
            False,
            409,
            "Another bookmark change is in progress. Try again.",
        ),
    ],
)
def test_url_outcome_mapper_preserves_every_rejection(
    outcome, removal, status, message
):
    response = api.map_url_outcome(outcome, removal=removal)

    assert response.status_code == status
    assert json.loads(response.body) == {"error": message}


@pytest.mark.parametrize(
    "operation", ["list_urls", "edit_url", "move_url", "set_important", "remove_url"]
)
def test_ordinary_url_routes_await_the_exact_composed_service(monkeypatch, operation):
    application = create_app()
    service = application.state.bookmarks_application_service
    record = domain.URLRecord(
        7, "https://example.com", "Example", "2026-08-30T00:00:00Z", True, 2
    )
    group = domain.GroupRecord(
        2, "Reading", None, 1, 1, False, ("example.com",), (record,)
    )
    expected_record = {
        "id": 7,
        "url": "https://example.com",
        "title": "Example",
        "created_at": "2026-08-30T00:00:00Z",
        "important": True,
        "version": 2,
    }
    expected_groups = [
        {
            "id": 2,
            "name": "Reading",
            "parent_id": None,
            "position": 1,
            "depth": 1,
            "nsfw": False,
            "domains": ["example.com"],
            "urls": [expected_record],
            "children": [],
        }
    ]
    calls = []

    def observe(name, result):
        async def observed(instance, *arguments):
            assert instance is service
            calls.append((name, arguments))
            return result

        monkeypatch.setattr(BookmarksApplicationService, name, observed)

    observe("list_urls", (record,))
    observe("list_groups", (group,))
    observe("edit_url", domain.URLUpdated(record))
    observe("move_url", domain.URLMoved(record, 2, 1))
    observe("set_important", domain.SetImportantSucceeded(record))
    observe("remove_url", domain.URLRemoved(record, 2))

    async def receive():
        return {"type": "http.request", "body": b'{"version":2,"url":"Example.COM/"}'}

    path, method, arguments, command = {
        "list_urls": ("/api/urls", "GET", {}, None),
        "edit_url": (
            "/api/urls/{url_id}",
            "PATCH",
            {"url_id": 7, "request": Request({"type": "http"}, receive)},
            domain.EditURL(7, 2, url="https://example.com"),
        ),
        "move_url": (
            "/api/urls/{url_id}/group",
            "PATCH",
            {"url_id": 7, "payload": api.MoveURLGroup(group_id=2, source_group_id=1)},
            domain.MoveURL(7, 2, 1),
        ),
        "set_important": (
            "/api/urls/{url_id}/important",
            "PATCH",
            {"url_id": 7, "payload": api.SetImportant(important=True)},
            domain.SetImportant(7, True),
        ),
        "remove_url": (
            "/api/urls/{url_id}",
            "DELETE",
            {"url_id": 7, "group_id": 2},
            domain.RemoveURL(7, 2),
        ),
    }[operation]
    endpoint = next(
        context.route.endpoint
        for context in iter_route_contexts(application.routes)
        if isinstance(context.route, APIRoute)
        and context.route.path == path
        and method in context.route.methods
    )
    response = run_async(lambda: endpoint(**arguments))
    assert response.status_code == 200
    if operation == "list_urls":
        assert calls == [("list_urls", ())]
        assert json.loads(response.body) == {"urls": [expected_record]}
    else:
        expected_calls = [(operation, (command,))]
        expected_payload = {"url": expected_record, "groups": expected_groups}
        if operation == "remove_url":
            expected_calls.append(("list_urls", ()))
            expected_payload["urls"] = [expected_record]
        if operation == "move_url":
            expected_payload.update(group_id=2, source_group_id=1)
        expected_calls.append(("list_groups", ()))
        assert calls == expected_calls
        assert json.loads(response.body) == expected_payload


def test_openapi_operation_and_schema_manifest_is_exact():
    schema = create_app().openapi()
    operations = {
        (path, method): (
            operation["operationId"],
            set(operation["responses"]),
            operation.get("security") == [{"CookieAuth": []}],
        )
        for path, path_item in schema["paths"].items()
        for method, operation in path_item.items()
    }

    assert operations == EXPECTED_OPERATION_MANIFEST
    assert set(schema["components"]["schemas"]) == EXPECTED_SCHEMA_NAMES
    assert schema["components"]["securitySchemes"] == {
        "CookieAuth": {
            "type": "apiKey",
            "description": "Opaque server-side Trellmark web session.",
            "in": "cookie",
            "name": "__Host-trellmark_session",
        }
    }
    assert "/internal/ready" not in schema["paths"]


def test_openapi_response_models_examples_and_media_types_match_baseline():
    schema = create_app().openapi()
    committed_schema = json.loads(GENERATED_OPENAPI.read_text(encoding="utf-8"))

    assert schema == committed_schema

    import_responses = schema["paths"]["/api/import"]["post"]["responses"]
    assert import_responses["409"]["content"]["application/json"] == {
        "example": {
            "error": (
                "Another bookmark change is in progress. "
                "No import changes were saved. Try again."
            ),
            "code": "import_conflict",
        },
        "schema": {"$ref": "#/components/schemas/ImportConflictResponse"},
    }
    assert import_responses["422"]["content"]["application/json"] == {
        "example": {"error": "Invalid import file.", "code": "invalid_import"},
        "schema": {"$ref": "#/components/schemas/InvalidImportResponse"},
    }
    assert import_responses["500"]["content"]["application/json"] == {
        "example": {
            "error": "Import failed. No import changes were saved. Try again.",
            "code": "import_failed",
        },
        "schema": {"$ref": "#/components/schemas/ImportFailedResponse"},
    }

    icon_responses = schema["paths"]["/api/urls/{url_id}/icon"]["get"]["responses"]
    assert set(icon_responses["200"]["content"]) == {
        "image/png",
        "image/vnd.microsoft.icon",
    }
    assert set(icon_responses["404"]["content"]) == {"application/json"}
