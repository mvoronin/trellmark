import asyncio
import json
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Annotated, Any, TypeVar, cast
from urllib.parse import parse_qs

from fastapi import Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ValidationError

from .app_keys import (
    SITE_ICON_SERVICE_STATE,
    TITLE_FETCHER_STATE,
    IconService,
    TitleFetcher,
)
from .models import (
    CreateGroup,
    CreateGroupResponse,
    CreateURL,
    CreateURLResponse,
    DeleteGroup,
    DeleteGroupResponse,
    DeleteURLByIDResponse,
    EditGroup,
    EditGroupResponse,
    EditURL,
    EditURLResponse,
    ExportDocument,
    GroupsResponse,
    ImportConflictResponse,
    ImportDocument,
    ImportFailedResponse,
    ImportResponse,
    InvalidImportResponse,
    MoveURLGroup,
    MoveURLGroupResponse,
    RefreshURLMetadataResponse,
    RefreshURLTitleResponse,
    ReorderGroups,
    ReorderGroupsResponse,
    SetImportant,
    SetImportantResponse,
    URLsResponse,
)
from .request_utils import media_type
from .responses import error_response as _error_response
from .storage import (
    DEFAULT_GROUP,
    DEPTH_EXCEEDED,
    GROUP_HAS_CHILDREN,
    GROUP_NAME_CONFLICT,
    GROUP_NOT_FOUND,
    MOVE_GROUP_NOT_FOUND,
    MOVE_SOURCE_NOT_FOUND,
    MOVE_SOURCE_REQUIRED,
    MOVE_URL_NOT_FOUND,
    PARENT_IS_SELF_OR_DESCENDANT,
    PARENT_NOT_FOUND,
    UNCHANGED_PARENT,
    URL_CONFLICT,
    URL_NOT_FOUND,
    URL_VERSION_CONFLICT,
    BookmarkMutationConflict,
    ParentUpdate,
    add_url,
    export_saved_data,
    find_group_record,
    import_saved_data,
    move_url_to_group,
    read_group_records,
    read_url_record_by_id,
    read_url_records,
    remove_url_by_id,
    set_url_important,
    update_group_order,
    update_url_record,
    update_url_title,
)
from .storage import (
    create_group as create_group_record,
)
from .storage import (
    delete_group as delete_group_record,
)
from .storage import (
    update_group as update_group_record,
)
from .storage_types import GroupRecord, UpdateURLFields
from .url_normalization import normalize_url

PositiveID = Annotated[int, Path(ge=1)]
ModelT = TypeVar("ModelT", bound=BaseModel)
type JSONValue = (
    str | int | float | bool | None | list[JSONValue] | dict[str, JSONValue]
)
type JSONObject = dict[str, JSONValue]


def _validated_response(model: type[ModelT], payload: object) -> JSONObject:
    return cast(JSONObject, model.model_validate(payload).model_dump())


def _request_validation_error(error: ValidationError) -> RequestValidationError:
    errors: list[dict[str, Any]] = []
    for item in error.errors():
        loc = item.get("loc", ())
        errors.append({**item, "loc": ("body", *loc)})
    return RequestValidationError(errors)


async def _json_model(request: Request, model: type[ModelT]) -> ModelT:
    try:
        payload: object = await request.json()
    except json.JSONDecodeError as error:
        raise RequestValidationError(
            [
                {
                    "type": "json_invalid",
                    "loc": ("body", error.pos),
                    "msg": "JSON decode error",
                    "input": {},
                    "ctx": {"error": error.msg},
                }
            ]
        ) from error

    try:
        return model.model_validate(payload)
    except ValidationError as error:
        raise _request_validation_error(error) from error


async def _submitted_url(request: Request) -> str:
    request_media_type = media_type(request)
    if request_media_type == "application/json":
        return (await _json_model(request, CreateURL)).url

    if request_media_type == "application/x-www-form-urlencoded":
        body = (await request.body()).decode("utf-8", errors="replace")
        return parse_qs(body, keep_blank_values=True).get("url", [""])[0]

    raise ValueError("Unsupported request format.")


async def list_urls() -> JSONObject:
    return _validated_response(URLsResponse, {"urls": read_url_records()})


async def list_groups() -> JSONObject:
    return _validated_response(GroupsResponse, {"groups": read_group_records()})


async def export_data() -> JSONResponse:
    exported_at = datetime.now(timezone.utc).replace(microsecond=0)
    document = export_saved_data(exported_at.isoformat().replace("+00:00", "Z"))
    filename = f"trellmark-export-{exported_at:%Y%m%dT%H%M%SZ}.json"
    return JSONResponse(
        _validated_response(ExportDocument, document),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def import_data(document: ImportDocument) -> JSONObject | JSONResponse:
    def validate_import(existing_groups: Sequence[GroupRecord]) -> None:
        try:
            document.validate_against(existing_groups)
        except ValueError as error:
            raise InvalidImportDocumentError from error

    try:
        result = import_saved_data(
            document.to_storage_document(),
            validate_against=validate_import,
        )
    except InvalidImportDocumentError:
        return _import_error_response(
            InvalidImportResponse,
            422,
            message=INVALID_IMPORT_MESSAGE,
            code="invalid_import",
        )
    except BookmarkMutationConflict:
        return _import_error_response(
            ImportConflictResponse,
            409,
            message=IMPORT_CONFLICT_MESSAGE,
            code="import_conflict",
        )
    except Exception:
        return _import_error_response(
            ImportFailedResponse,
            500,
            message=IMPORT_FAILED_MESSAGE,
            code="import_failed",
        )
    return _validated_response(ImportResponse, result)


class InvalidImportDocumentError(ValueError):
    """Import hierarchy validation failed before the transaction's first DML."""


INVALID_IMPORT_MESSAGE = "Invalid import file."
BOOKMARK_MUTATION_CONFLICT_MESSAGE = (
    "Another bookmark change is in progress. Try again."
)
IMPORT_CONFLICT_MESSAGE = (
    "Another bookmark change is in progress. No import changes were saved. Try again."
)
IMPORT_FAILED_MESSAGE = "Import failed. No import changes were saved. Try again."
GROUP_MISSING = "This group does not exist."
GROUP_TOO_DEEP = "Groups can be nested three levels deep."
GROUP_INSIDE_ITSELF = "A group cannot be moved into itself."


def _bookmark_mutation_conflict_response() -> JSONResponse:
    return _error_response(BOOKMARK_MUTATION_CONFLICT_MESSAGE, 409)


def _import_error_response(
    model: type[ModelT],
    status: int,
    *,
    message: str,
    code: str,
) -> JSONResponse:
    model.model_validate({"error": message, "code": code})
    return _error_response(message, status, code=code)


def _hierarchy_error_response(error: str | None) -> JSONResponse | None:
    """The shared mapping for the three ways a destination can be wrong."""
    if error == PARENT_NOT_FOUND:
        return _error_response(GROUP_MISSING, 404)
    if error == PARENT_IS_SELF_OR_DESCENDANT:
        return _error_response(GROUP_INSIDE_ITSELF, 400)
    if error == DEPTH_EXCEEDED:
        return _error_response(GROUP_TOO_DEEP, 400)
    return None


async def create_group(request: Request) -> JSONObject | JSONResponse:
    try:
        payload = await _json_model(request, CreateGroup)
    except RequestValidationError as error:
        if any(item.get("loc") == ("body", "nsfw") for item in error.errors()):
            return _error_response("Enter a valid value.", 400)
        if any(
            item.get("loc", ())[:2] == ("body", "domains") for item in error.errors()
        ):
            return _error_response("Enter valid domains.", 400)
        raise

    try:
        group, error = create_group_record(
            payload.name,
            nsfw=payload.nsfw,
            domains=payload.domains,
            parent_id=payload.parent_id,
        )
    except BookmarkMutationConflict:
        return _bookmark_mutation_conflict_response()
    if error == GROUP_NAME_CONFLICT:
        return _error_response("This group already exists.", 409)
    hierarchy_response = _hierarchy_error_response(error)
    if hierarchy_response is not None:
        return hierarchy_response
    if group is None:
        raise RuntimeError("Group creation failed without a reason.")

    response_payload = {"group": group, "groups": read_group_records()}
    return _validated_response(CreateGroupResponse, response_payload)


async def edit_group(
    request: Request,
    group_id: PositiveID,
) -> JSONObject | JSONResponse:
    try:
        payload = EditGroup.model_validate(await request.json())
    except json.JSONDecodeError:
        return _error_response("Enter a valid value.", 400)
    except ValidationError as error:
        if any(item.get("loc") == ("name",) for item in error.errors()):
            return _error_response("Enter a group name.", 400)
        if any(item.get("loc", ())[:1] == ("domains",) for item in error.errors()):
            return _error_response("Enter valid domains.", 400)
        return _error_response("Enter a valid value.", 400)

    # An omitted parent_id leaves the parent alone; an explicit null moves the
    # group to the root. Only the set of supplied fields tells them apart.
    parent_update: ParentUpdate = (
        payload.parent_id
        if "parent_id" in payload.model_fields_set
        else UNCHANGED_PARENT
    )
    try:
        record, error = update_group_record(
            group_id,
            name=payload.name,
            nsfw=payload.nsfw,
            domains=payload.domains,
            parent_id=parent_update,
        )
    except BookmarkMutationConflict:
        return _bookmark_mutation_conflict_response()
    if error == GROUP_NOT_FOUND:
        return _error_response(GROUP_MISSING, 404)
    if error == DEFAULT_GROUP:
        return _error_response("The default group cannot be edited.", 400)
    if error == GROUP_NAME_CONFLICT:
        return _error_response("This group already exists.", 409)
    hierarchy_response = _hierarchy_error_response(error)
    if hierarchy_response is not None:
        return hierarchy_response
    if record is None:
        raise RuntimeError("Group update failed without a reason.")

    groups = read_group_records()
    updated = find_group_record(groups, group_id)
    if updated is None:
        raise RuntimeError("Updated group could not be read.")
    return _validated_response(
        EditGroupResponse,
        {"group": updated, "groups": groups},
    )


async def delete_group(
    request: Request,
    group_id: PositiveID,
) -> JSONObject | JSONResponse:
    try:
        payload = DeleteGroup.model_validate(await request.json())
    except json.JSONDecodeError, ValidationError:
        return _error_response("Choose how to handle this group's URLs.", 400)

    try:
        result, error = delete_group_record(group_id, payload.url_action)
    except BookmarkMutationConflict:
        return _bookmark_mutation_conflict_response()
    if error == GROUP_NOT_FOUND:
        return _error_response(GROUP_MISSING, 404)
    if error == DEFAULT_GROUP:
        return _error_response("The default group cannot be deleted.", 400)
    if error == GROUP_HAS_CHILDREN:
        return _error_response("Move or delete this group's child groups first.", 409)
    if result is None:
        raise RuntimeError("Group deletion failed without a reason.")

    response = {**result, "groups": read_group_records()}
    return _validated_response(DeleteGroupResponse, response)


async def create_url(request: Request) -> JSONObject | JSONResponse:
    try:
        url = normalize_url(await _submitted_url(request))
    except ValueError as error:
        return _error_response(str(error), 400)

    try:
        record = add_url(url)
    except BookmarkMutationConflict:
        return _bookmark_mutation_conflict_response()
    if not record:
        return _error_response("This URL is already saved.", 409)

    try:
        title = await _fetch_page_title(request, url)
    except Exception:
        title = None
    if title is not None:
        try:
            record = update_url_title(record["id"], title) or record
        except BookmarkMutationConflict:
            return _bookmark_mutation_conflict_response()

    payload = {
        "url": record,
        "urls": read_url_records(),
        "groups": read_group_records(),
    }
    return _validated_response(CreateURLResponse, payload)


async def edit_url(
    request: Request,
    url_id: PositiveID,
) -> JSONObject | JSONResponse:
    try:
        payload = EditURL.model_validate(await request.json())
    except json.JSONDecodeError:
        return _error_response("Enter a valid value.", 400)
    except ValidationError as error:
        if any(item.get("loc") == ("url",) for item in error.errors()):
            return _error_response("Enter a URL.", 400)
        return _error_response("Enter a valid value.", 400)

    fields: UpdateURLFields = {}
    if "url" in payload.model_fields_set:
        try:
            fields["url"] = normalize_url(payload.url)
        except ValueError as error:
            return _error_response(str(error), 400)
    if "title" in payload.model_fields_set:
        fields["title"] = payload.title

    try:
        record, error = update_url_record(
            url_id,
            expected_version=payload.version,
            fields=fields,
        )
    except BookmarkMutationConflict:
        return _bookmark_mutation_conflict_response()
    if error == URL_NOT_FOUND:
        return _error_response("This URL is not saved.", 404)
    if error == URL_CONFLICT:
        return _error_response("This URL is already saved.", 409)
    if error == URL_VERSION_CONFLICT:
        return _error_response("This URL was changed. Reload and try again.", 409)
    if record is None:
        raise RuntimeError("URL update failed without a reason.")

    return _validated_response(
        EditURLResponse,
        {"url": record, "groups": read_group_records()},
    )


async def refresh_url_title(
    request: Request,
    url_id: PositiveID,
) -> JSONObject | JSONResponse:
    record = read_url_record_by_id(url_id)
    if record is None:
        return _error_response("This URL is not saved.", 404)

    try:
        title = await _fetch_page_title(request, record["url"])
    except Exception:
        return _error_response("Could not fetch a page title.", 502)
    if title is None:
        return _validated_response(
            RefreshURLTitleResponse,
            {
                "url": record,
                "groups": read_group_records(),
                "title_updated": False,
            },
        )

    try:
        updated, error = update_url_record(
            url_id,
            expected_version=record["version"],
            fields={"title": title},
        )
    except BookmarkMutationConflict:
        return _bookmark_mutation_conflict_response()
    if error == URL_NOT_FOUND:
        return _error_response("This URL is not saved.", 404)
    if error == URL_VERSION_CONFLICT:
        return _error_response("This URL was changed. Reload and try again.", 409)
    if error is not None or updated is None:
        raise RuntimeError("URL title update failed without a reason.")

    return _validated_response(
        RefreshURLTitleResponse,
        {
            "url": updated,
            "groups": read_group_records(),
            "title_updated": True,
        },
    )


async def get_url_icon(
    request: Request,
    url_id: PositiveID,
) -> Response:
    record = read_url_record_by_id(url_id)
    if record is None:
        return _error_response("This URL is not saved.", 404)

    icon = await _site_icon_service(request).get(record["url"])
    if icon is None:
        return _error_response("No site icon available.", 404)
    return Response(
        content=icon.data,
        media_type=icon.media_type,
        headers={"X-Content-Type-Options": "nosniff"},
    )


async def refresh_url_metadata(
    request: Request,
    url_id: PositiveID,
) -> JSONObject | JSONResponse:
    record = read_url_record_by_id(url_id)
    if record is None:
        return _error_response("This URL is not saved.", 404)

    title, icon_updated = await asyncio.gather(
        _fetch_page_title_or_none(request, record["url"]),
        _site_icon_service(request).refresh(record["url"]),
    )
    title_updated = title is not None
    if title is not None:
        try:
            current, error = update_url_record(
                url_id,
                expected_version=record["version"],
                fields={"title": title},
            )
        except BookmarkMutationConflict:
            return _bookmark_mutation_conflict_response()
        if error == URL_NOT_FOUND:
            return _error_response("This URL is not saved.", 404)
        if error == URL_VERSION_CONFLICT:
            return _error_response("This URL was changed. Reload and try again.", 409)
        if error is not None or current is None:
            raise RuntimeError("URL metadata update failed without a reason.")
    else:
        current = read_url_record_by_id(url_id)
        if current is None:
            return _error_response("This URL is not saved.", 404)

    return _validated_response(
        RefreshURLMetadataResponse,
        {
            "url": current,
            "groups": read_group_records(),
            "title_updated": title_updated,
            "icon_updated": icon_updated,
        },
    )


async def _fetch_page_title(request: Request, url: str) -> str | None:
    title_fetcher = cast(
        TitleFetcher,
        getattr(request.app.state, TITLE_FETCHER_STATE),
    )
    return await title_fetcher(url)


async def _fetch_page_title_or_none(request: Request, url: str) -> str | None:
    try:
        return await _fetch_page_title(request, url)
    except Exception:
        return None


def _site_icon_service(request: Request) -> IconService:
    return cast(
        IconService,
        getattr(request.app.state, SITE_ICON_SERVICE_STATE),
    )


async def move_url_group(
    url_id: PositiveID,
    payload: MoveURLGroup,
) -> JSONObject | JSONResponse:
    try:
        result, error = move_url_to_group(
            url_id,
            payload.group_id,
            payload.source_group_id,
        )
    except BookmarkMutationConflict:
        return _bookmark_mutation_conflict_response()
    if error == MOVE_URL_NOT_FOUND:
        return _error_response("This URL is not saved.", 404)
    if error == MOVE_GROUP_NOT_FOUND:
        return _error_response(GROUP_MISSING, 404)
    if error == MOVE_SOURCE_NOT_FOUND:
        return _error_response("This URL is not saved in the source group.", 404)
    if error == MOVE_SOURCE_REQUIRED:
        return _error_response("Choose the URL's source group.", 400)
    if result is None:
        raise RuntimeError("URL move failed without a reason.")

    response = {
        "url": result["url"],
        "group_id": payload.group_id,
        "source_group_id": result["source_group_id"],
        "groups": read_group_records(),
    }
    return _validated_response(MoveURLGroupResponse, response)


async def set_important(
    url_id: PositiveID,
    payload: SetImportant,
) -> JSONObject | JSONResponse:
    try:
        record = set_url_important(url_id, payload.important)
    except BookmarkMutationConflict:
        return _bookmark_mutation_conflict_response()
    if not record:
        return _error_response("This URL is not saved.", 404)

    response_payload = {"url": record, "groups": read_group_records()}
    return _validated_response(SetImportantResponse, response_payload)


async def reorder_groups(payload: ReorderGroups) -> JSONObject | JSONResponse:
    try:
        reordered = update_group_order(payload.parent_id, payload.group_ids)
    except BookmarkMutationConflict:
        return _bookmark_mutation_conflict_response()
    if not reordered:
        return _error_response("Invalid group order.", 400)

    return _validated_response(ReorderGroupsResponse, {"groups": read_group_records()})


async def delete_url_by_id(
    url_id: PositiveID,
    group_id: Annotated[int, Query(ge=1)],
) -> JSONObject | JSONResponse:
    try:
        record = remove_url_by_id(url_id, group_id)
    except BookmarkMutationConflict:
        return _bookmark_mutation_conflict_response()
    if not record:
        return _error_response("This URL is not saved.", 404)

    payload = {
        "url": record,
        "urls": read_url_records(),
        "groups": read_group_records(),
    }
    return _validated_response(DeleteURLByIDResponse, payload)
