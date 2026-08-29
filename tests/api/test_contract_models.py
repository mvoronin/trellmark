from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

import trellmark
from tests.helpers import assert_validation_error, http_json, http_raw
from trellmark import config
from trellmark import models as contract_models
from trellmark.models import (
    CreateGroup,
    CreateURL,
    DeleteGroup,
    EditGroup,
    EditURL,
    ExportDocument,
    GroupsResponse,
    ImportDocument,
    ImportResponse,
    MoveURLGroup,
    ReorderGroups,
    SetImportant,
    URLsResponse,
)


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
    trellmark.dispose_engine()
    try:
        URLsResponse.model_validate({"urls": []})
        GroupsResponse.model_validate({"groups": []})
    finally:
        trellmark.dispose_engine()


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

    assert ImportDocument.model_validate(document).to_storage_document() == {
        "version": 1,
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
                        "created_at": datetime(
                            2026, 7, 3, 12, 0, 1, tzinfo=timezone.utc
                        ),
                        "important": False,
                    }
                ],
            },
        ],
    }


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

    assert ImportDocument.model_validate(document).to_storage_document() == {
        "version": 1,
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

    monkeypatch.setattr(contract_models, "_clean_import_document", reject_reclean)

    document.validate_against([])


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
    default_url = trellmark.add_url("https://one.example")
    reading = trellmark.add_group("Reading")
    reading_url = trellmark.add_url("https://two.example")
    trellmark.move_url_to_group(reading_url["id"], reading["id"])

    assert URLsResponse.model_validate(
        {"urls": trellmark.read_url_records()}
    ).model_dump() == {"urls": trellmark.read_url_records()}
    assert GroupsResponse.model_validate(
        {"groups": trellmark.read_group_records()}
    ).model_dump() == {"groups": trellmark.read_group_records()}

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
