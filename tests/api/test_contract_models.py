import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from textwrap import dedent

import pytest
from pydantic import ValidationError

from tests.bookmarks import helpers as bookmark_helpers
from tests.helpers import assert_validation_error, http_json, http_raw
from trellmark import config
from trellmark.backup import domain as backup_domain
from trellmark.backup.api import ExportDocument, ImportDocument, ImportResponse
from trellmark.bookmarks.api import (
    CreateGroup,
    CreateURL,
    DeleteGroup,
    EditGroup,
    EditURL,
    GroupsResponse,
    MoveURLGroup,
    ReorderGroups,
    SetImportant,
    URLsResponse,
)
from trellmark.platform import runtime


@pytest.mark.parametrize("path", ["/api/urls", "/api/groups"])
def test_post_rejects_malformed_json_with_validation_error(app, path):
    base_url, _ = app

    status, payload = http_raw(
        base_url,
        path,
        method="POST",
        body=b'{"url":',
        content_type="application/json",
    )

    assert_validation_error(status, payload)


@pytest.mark.parametrize(
    ("path", "method", "payload"),
    [
        ("/api/urls/not-an-id", "DELETE", None),
        ("/api/urls/not-an-id/group", "PATCH", {"group_id": 1}),
        ("/api/urls/0/important", "PATCH", {"important": True}),
    ],
)
def test_invalid_url_id_path_returns_validation_error(app, path, method, payload):
    base_url, _ = app

    status, response = http_json(base_url, path, method=method, payload=payload)

    assert_validation_error(status, response)


def test_contract_models_validate_without_touching_database(monkeypatch):
    """Response models must not reach the database to validate.

    Pointed at a DSN with nothing listening, so any connection attempt would
    raise instead of passing silently.
    """
    monkeypatch.setattr(
        config,
        "DATABASE_URL",
        "postgresql+psycopg://trellmark:secret@127.0.0.1:1/trellmark",
    )
    runtime.dispose_engine()
    try:
        URLsResponse.model_validate({"urls": []})
        GroupsResponse.model_validate({"groups": []})
    finally:
        runtime.dispose_engine()


def test_request_contract_models_validate_current_json_payloads():
    assert CreateURL.model_validate({"url": " example.com "}).url == " example.com "
    create_group = CreateGroup.model_validate({"name": "  Reading  ", "nsfw": True})
    assert create_group.name == "Reading"
    assert create_group.nsfw is True
    assert CreateGroup.model_validate({"name": "Work"}).nsfw is False
    edit_group = EditGroup.model_validate({"name": "  Later  ", "nsfw": False})
    assert edit_group.name == "Later"
    assert edit_group.nsfw is False
    edit_url = EditURL.model_validate(
        {
            "url": " example.com ",
            "title": "  Example   title ",
            "version": 1,
        }
    )
    assert edit_url.url == " example.com "
    assert edit_url.title == "Example title"
    assert EditURL.model_validate({"title": None, "version": 1}).title is None
    assert (
        DeleteGroup.model_validate({"url_action": "move_to_default"}).url_action
        == "move_to_default"
    )
    assert MoveURLGroup.model_validate({"group_id": 1}).group_id == 1
    assert SetImportant.model_validate({"important": True}).important is True
    roots = ReorderGroups.model_validate({"parent_id": None, "group_ids": [2, 1]})
    assert roots.parent_id is None
    assert roots.group_ids == [2, 1]
    assert (
        ReorderGroups.model_validate({"parent_id": 3, "group_ids": [2, 1]}).parent_id
        == 3
    )
    # An omitted parent_id is not the same as an explicit null: only one of
    # these asks to move the group to the root.
    assert (
        "parent_id" not in EditGroup.model_validate({"name": "Later"}).model_fields_set
    )
    to_root = EditGroup.model_validate({"parent_id": None})
    assert "parent_id" in to_root.model_fields_set
    assert to_root.parent_id is None
    assert EditGroup.model_validate({"parent_id": 2}).parent_id == 2
    assert CreateGroup.model_validate({"name": "Work", "parent_id": 2}).parent_id == 2
    assert CreateGroup.model_validate({"name": "Work"}).parent_id is None

    document = {
        "version": 1,
        "exported_at": "2026-07-03T12:00:00Z",
        "groups": [
            {
                "name": "  Later  ",
                "position": 1,
                "urls": [],
            },
            {
                "name": "default",
                "position": 0,
                "urls": [
                    {
                        "url": "HTTPS://Example.com/",
                        "created_at": "2026-07-03T15:00:01+03:00",
                    }
                ],
            },
        ],
    }

    normalized = ImportDocument.model_validate(document)
    assert normalized.model_dump() == {
        "version": 1,
        "exported_at": "2026-07-03T12:00:00Z",
        "groups": [
            {
                "name": "Later",
                "parent": None,
                "position": 1,
                "nsfw": False,
                "domains": [],
                "urls": [],
            },
            {
                "name": "default",
                "parent": None,
                "position": 0,
                "nsfw": False,
                "domains": [],
                "urls": [
                    {
                        "url": "https://example.com",
                        "title": None,
                        "created_at": "2026-07-03T12:00:01Z",
                        "important": False,
                    }
                ],
            },
        ],
    }
    assert backup_domain.import_timestamp(
        normalized.to_domain().groups[1].urls[0].created_at
    ) == datetime(2026, 7, 3, 12, 0, 1, tzinfo=timezone.utc)


def test_import_contract_carries_name_based_parents():
    document = {
        "version": 1,
        "exported_at": "2026-08-08T12:00:00Z",
        "groups": [
            {
                "name": "Engineering",
                "parent": None,
                "position": 0,
                "urls": [],
            },
            {
                "name": "Backend",
                "parent": " engineering ",
                "position": 0,
                "nsfw": True,
                "domains": ["EXAMPLE.com"],
                "urls": [],
            },
        ],
    }

    assert ImportDocument.model_validate(document).model_dump() == {
        "version": 1,
        "exported_at": "2026-08-08T12:00:00Z",
        "groups": [
            {
                "name": "Engineering",
                "parent": None,
                "position": 0,
                "nsfw": False,
                "domains": [],
                "urls": [],
            },
            {
                "name": "Backend",
                "parent": "engineering",
                "position": 0,
                "nsfw": True,
                "domains": ["example.com"],
                "urls": [],
            },
        ],
    }


def test_existing_hierarchy_validation_does_not_reclean_document(monkeypatch):
    document = ImportDocument.model_validate(
        {
            "version": 1,
            "exported_at": "2026-08-08T12:00:00Z",
            "groups": [
                {
                    "name": "Root",
                    "parent": None,
                    "position": 0,
                    "urls": [],
                }
            ],
        }
    )

    def reject_reclean(_payload):
        raise AssertionError("document was cleaned twice")

    monkeypatch.setattr(backup_domain, "normalize_import_document", reject_reclean)

    backup_domain.validate_resulting_import_hierarchy(document.to_domain().groups, [])


def test_edit_url_schema_makes_url_optional_but_not_nullable():
    schema = EditURL.model_json_schema()

    assert schema["required"] == ["version"]
    assert schema["properties"]["url"] == {"title": "Url", "type": "string"}
    assert schema["properties"]["title"]["anyOf"] == [
        {"type": "string"},
        {"type": "null"},
    ]


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (CreateURL, {"url": 42}),
        (CreateGroup, {"name": "   "}),
        (CreateGroup, {"name": "Reading", "nsfw": 1}),
        (EditGroup, {}),
        (EditGroup, {"name": "   "}),
        (EditGroup, {"nsfw": 1}),
        (EditURL, {}),
        (EditURL, {"url": "example.com"}),
        (EditURL, {"url": None, "version": 1}),
        (EditURL, {"title": 42, "version": 1}),
        (EditURL, {"title": "Example", "version": 0}),
        (DeleteGroup, {}),
        (DeleteGroup, {"url_action": "archive"}),
        (MoveURLGroup, {"group_id": True}),
        (SetImportant, {"important": 1}),
        (ReorderGroups, {"parent_id": None, "group_ids": [1, True]}),
        (ReorderGroups, {"group_ids": [1, 2]}),
        (ReorderGroups, {"parent_id": 0, "group_ids": [1, 2]}),
        (CreateGroup, {"name": "Reading", "parent_id": 0}),
        (CreateGroup, {"name": "Reading", "parent_id": "2"}),
        (EditGroup, {"parent_id": 0}),
        (
            ImportDocument,
            {
                "version": 2,
                "exported_at": "2026-07-03T12:00:00Z",
                "groups": [
                    {
                        "name": "default",
                        "position": 0,
                        "urls": [
                            {
                                "url": "https://example.com",
                                "created_at": "2026-07-03T12:00:00Z",
                                "important": 1,
                            }
                        ],
                    }
                ],
            },
        ),
    ],
)
def test_request_contract_models_reject_invalid_payloads(model, payload):
    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_response_contract_models_validate_storage_and_route_shapes(app):
    base_url, _ = app
    default_url = bookmark_helpers.seed_url("https://one.example")
    reading = bookmark_helpers.seed_group("Reading")
    reading_url = bookmark_helpers.seed_url("https://two.example")
    bookmark_helpers.seed_membership(reading_url["id"], reading["id"])

    assert URLsResponse.model_validate(
        {"urls": bookmark_helpers.url_payloads()}
    ).model_dump() == {"urls": bookmark_helpers.url_payloads()}
    assert GroupsResponse.model_validate(
        {"groups": bookmark_helpers.group_payloads()}
    ).model_dump() == {"groups": bookmark_helpers.group_payloads()}

    status, payload = http_json(base_url, "/api/urls")
    assert status == 200
    assert URLsResponse.model_validate(payload).model_dump() == payload
    assert payload["urls"][0] == default_url

    status, payload = http_json(base_url, "/api/groups")
    assert status == 200
    assert GroupsResponse.model_validate(payload).model_dump() == payload

    status, payload = http_json(base_url, "/api/export")
    assert status == 200
    assert ExportDocument.model_validate(payload).model_dump() == payload

    import_document = {
        "version": 1,
        "exported_at": "2026-07-03T12:00:00Z",
        "groups": [
            {
                "name": "Imported",
                "position": 0,
                "urls": [
                    {
                        "url": "https://imported.example",
                        "created_at": "2026-07-03T12:00:01Z",
                        "important": True,
                    }
                ],
            }
        ],
    }

    status, payload = http_json(
        base_url, "/api/import", method="POST", payload=import_document
    )
    assert status == 200
    assert ImportResponse.model_validate(payload).model_dump() == payload


def test_backup_normalization_preserves_immutable_document_order_and_timestamps():
    from dataclasses import FrozenInstanceError

    from trellmark.backup.domain import normalize_import_document

    payload = {
        "version": 1,
        "exported_at": "2026-08-08T15:00:00+03:00",
        "groups": [
            {
                "name": " Child ",
                "parent": " root ",
                "position": 4,
                "nsfw": True,
                "domains": ["EXAMPLE.com.", "example.com", "BÜCHER.example"],
                "urls": [
                    {
                        "url": " HTTPS://Example.com/ ",
                        "title": " First\n metadata ",
                        "created_at": "2026-08-08T15:00:01.987+03:00",
                        "important": True,
                    },
                    {
                        "url": "example.com",
                        "title": "  ",
                        "created_at": "2026-08-08T12:00:02",
                    },
                ],
            },
            {"name": "Root", "position": 8, "urls": []},
        ],
    }

    document = normalize_import_document(payload)

    assert document.version == 1
    assert document.exported_at == "2026-08-08T15:00:00+03:00"
    assert isinstance(document.groups, tuple)
    child, root = document.groups
    assert (child.name, child.parent, child.position, child.nsfw) == (
        "Child",
        "root",
        4,
        True,
    )
    assert child.domains == ("example.com", "xn--bcher-kva.example")
    assert isinstance(child.urls, tuple)
    assert [url.url for url in child.urls] == ["https://example.com"] * 2
    assert [url.title for url in child.urls] == ["First metadata", None]
    assert [url.created_at for url in child.urls] == [
        "2026-08-08T12:00:01Z",
        "2026-08-08T12:00:02Z",
    ]
    assert [url.important for url in child.urls] == [True, False]
    assert root.urls == ()
    assert root.domains == ()
    for value, field, replacement in (
        (document, "version", 2),
        (child, "name", "Changed"),
        (child.urls[0], "title", "Changed"),
    ):
        assert not hasattr(value, "__dict__")
        with pytest.raises(FrozenInstanceError):
            setattr(value, field, replacement)
    assert payload["groups"][0]["name"] == " Child "


@pytest.mark.parametrize(
    ("imported_name", "parent", "valid"),
    [
        ("New", "Child", True),
        ("New", "Deep", False),
        ("Root", "Child", False),
        ("New", "Missing", False),
    ],
)
def test_backup_resulting_hierarchy_uses_shared_bookmark_record(
    imported_name, parent, valid
):
    from trellmark.backup.domain import (
        InvalidImportDocument,
        normalize_import_document,
        validate_resulting_import_hierarchy,
    )
    from trellmark.bookmarks.domain import GroupRecord

    deep = GroupRecord(3, "Deep", 2, 0, 3, False, (), ())
    child = GroupRecord(2, "Child", 1, 0, 2, False, (), (), (deep,))
    root = GroupRecord(1, "Root", None, 0, 1, False, (), (), (child,))
    document = normalize_import_document(
        {
            "version": 1,
            "exported_at": "2026-08-08T12:00:00Z",
            "groups": [
                {"name": imported_name, "parent": parent, "position": 0, "urls": []}
            ],
        }
    )

    if valid:
        validate_resulting_import_hierarchy(document.groups, (root,))
    else:
        with pytest.raises(InvalidImportDocument, match=r"^Invalid import file\.$"):
            validate_resulting_import_hierarchy(document.groups, (root,))


def test_backup_core_imports_and_normalizes_without_framework_or_row_types():
    # Isolate application construction imports while importing the real core
    # package and its dependencies without any transport or persistence adapter.
    script = dedent("""
        import importlib
        import importlib.util
        import sys
        import types
        from importlib.abc import MetaPathFinder
        from pathlib import Path

        package = types.ModuleType("trellmark")
        package.__path__ = [str(Path.cwd() / "trellmark")]
        sys.modules["trellmark"] = package

        class CoreOnlyImports(MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                forbidden = (
                    "fastapi", "starlette", "pydantic", "sqlalchemy", "psycopg",
                    "trellmark.backup.api", "trellmark.backup.persistence",
                    "trellmark.bookmarks.api", "trellmark.bookmarks.persistence",
                )
                if any(fullname == name or fullname.startswith(name + ".")
                       for name in forbidden):
                    raise ImportError("Forbidden core dependency: " + fullname)

        sys.meta_path.insert(0, CoreOnlyImports())
        from trellmark.backup import normalize_import_document

        application = "trellmark.backup.application"
        if importlib.util.find_spec(application) is not None:
            importlib.import_module(application)
        document = normalize_import_document({
            "version": 1, "exported_at": "2026-08-08T12:00:00Z",
            "groups": [{"name": "Empty", "position": 0, "urls": []}],
        })
        assert document.groups[0].urls == ()
    """)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("target", "field", "value", "message"),
    [
        ("document", "version", "1", "Invalid import file."),
        ("document", "exported_at", "yesterday", "Invalid import file."),
        ("document", "groups", [], "Invalid import file."),
        ("group", "name", "  ", "Invalid import file."),
        ("group", "parent", " ", "Invalid import file."),
        ("group", "parent", "Root", "Invalid import file."),
        ("group", "position", True, "Invalid import file."),
        ("group", "position", -1, "Invalid import file."),
        ("group", "nsfw", 1, "Invalid import file."),
        ("group", "domains", [42], "Invalid import file."),
        ("group", "domains", ["https://example.com"], "Invalid import file."),
        ("group", "urls", None, "Invalid import file."),
        ("url", "url", "", "Enter a URL."),
        ("url", "url", "https://exam ple.com", "URL cannot contain spaces."),
        ("url", "url", "https://example.com/a\nb", "URL cannot contain line breaks."),
        ("url", "url", "ftp://example.com", "Enter a valid http or https URL."),
        ("url", "created_at", None, "Invalid import file."),
        ("url", "created_at", "invalid", "Invalid import file."),
        ("url", "title", 42, "Invalid import file."),
        ("url", "important", 1, "Invalid import file."),
    ],
)
def test_backup_domain_and_transport_preserve_validation_messages(
    target, field, value, message
):
    url = {"url": "example.com", "created_at": "2026-08-08T12:00:00Z"}
    group = {"name": "Root", "position": 0, "urls": [url]}
    document = {
        "version": 1,
        "exported_at": "2026-08-08T12:00:00Z",
        "groups": [group],
    }
    {"document": document, "group": group, "url": url}[target][field] = value

    with pytest.raises(backup_domain.InvalidImportDocument) as domain_error:
        backup_domain.normalize_import_document(document)
    assert str(domain_error.value) == message
    with pytest.raises(ValidationError) as transport_error:
        ImportDocument.model_validate(document)
    assert transport_error.value.errors()[0]["msg"] == f"Value error, {message}"


@pytest.mark.parametrize("version", [1, True, 1.0])
def test_backup_retains_version_one_acceptance_and_normalized_wire_values(version):
    document = {
        "version": version,
        "exported_at": "2026-08-08",
        "ignored": "legacy extras remain ignored",
        "groups": [
            {"name": "Straße", "position": 9, "urls": [], "ignored": True},
            {"name": "STRASSE", "position": 2, "urls": []},
        ],
    }

    transport = ImportDocument.model_validate(document)
    normalized = backup_domain.normalize_import_document(document)

    assert transport.version == 1
    assert transport.to_domain() == normalized
    assert transport.exported_at == "2026-08-08"
    assert [(group.name, group.position) for group in transport.groups] == [
        ("Straße", 9),
        ("STRASSE", 2),
    ]
    assert "ignored" not in transport.model_dump()
    assert "ignored" not in transport.groups[0].model_dump()
    assert ImportDocument.__module__ == "trellmark.backup.api"
    assert ExportDocument.__module__ == "trellmark.backup.api"
