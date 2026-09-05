import json
from typing import Annotated, Any, Literal, assert_never
from urllib.parse import parse_qs

from fastapi import APIRouter, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import (
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from ..platform.contracts import (
    ContractModel,
    NonNegativeStrictInt,
    PositiveStrictInt,
    RequestModel,
    json_model,
)
from ..platform.responses import error_response
from ..request_utils import media_type
from . import domain
from .application import BookmarksApplicationService
from .domain import clean_title_text, normalize_domains

PositiveID = Annotated[int, Path(ge=1)]
# Three levels, root inclusive, matching the database check constraint.
GroupDepth = Annotated[StrictInt, Field(ge=1, le=3)]


class URLRecord(ContractModel):
    id: PositiveStrictInt
    url: StrictStr
    title: StrictStr | None
    created_at: StrictStr
    important: StrictBool
    version: PositiveStrictInt


class CreateURL(RequestModel):
    url: StrictStr


class EditURL(RequestModel):
    version: PositiveStrictInt
    url: StrictStr = Field(default_factory=str)
    title: StrictStr | None = None

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return clean_title_text(value)

    @model_validator(mode="after")
    def require_editable_field(self) -> "EditURL":
        if not self.model_fields_set.intersection({"url", "title"}):
            raise ValueError("Enter a valid value.")
        return self


class MoveURLGroup(RequestModel):
    group_id: PositiveStrictInt
    source_group_id: PositiveStrictInt | None = None


class SetImportant(RequestModel):
    important: StrictBool


class GroupRecord(ContractModel):
    """A group and the groups nested under it.

    `depth` is derived from storage and never accepted as input, and the
    internal `ltree` path is not part of the contract: a client positions a
    group with `parent_id` and sibling `position`.
    """

    id: PositiveStrictInt
    name: StrictStr
    parent_id: PositiveStrictInt | None
    position: NonNegativeStrictInt
    depth: GroupDepth
    nsfw: StrictBool
    domains: list[StrictStr]
    urls: list[URLRecord]
    children: list["GroupRecord"]


class URLsResponse(ContractModel):
    urls: list[URLRecord]


class CreateURLResponse(ContractModel):
    url: URLRecord
    urls: list[URLRecord]
    groups: list[GroupRecord]


class RefreshURLTitleResponse(ContractModel):
    url: URLRecord
    groups: list[GroupRecord]
    title_updated: StrictBool


class RefreshURLMetadataResponse(RefreshURLTitleResponse):
    icon_updated: StrictBool


class EditURLResponse(ContractModel):
    url: URLRecord
    groups: list[GroupRecord]


class MoveURLGroupResponse(ContractModel):
    url: URLRecord
    group_id: PositiveStrictInt
    source_group_id: PositiveStrictInt
    groups: list[GroupRecord]


class SetImportantResponse(ContractModel):
    url: URLRecord
    groups: list[GroupRecord]


class DeleteURLByIDResponse(ContractModel):
    url: URLRecord
    urls: list[URLRecord]
    groups: list[GroupRecord]


class CreateGroup(RequestModel):
    name: StrictStr
    parent_id: PositiveStrictInt | None = None
    nsfw: StrictBool = False
    domains: list[StrictStr] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("Enter a group name.")
        return name

    @field_validator("domains")
    @classmethod
    def clean_domains(cls, values: list[str]) -> list[str]:
        return normalize_domains(values)


class EditGroup(RequestModel):
    """A group edit, where an omitted `parent_id` is not the same as `null`.

    `null` moves the group to the root; leaving the field out leaves the parent
    alone. Only `model_fields_set` can tell those apart, so the handler reads
    it rather than the value.
    """

    name: StrictStr | None = None
    nsfw: StrictBool | None = None
    domains: list[StrictStr] | None = None
    parent_id: PositiveStrictInt | None = None

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("Enter a group name.")
        name = value.strip()
        if not name:
            raise ValueError("Enter a group name.")
        return name

    @field_validator("nsfw")
    @classmethod
    def reject_null_nsfw(cls, value: bool | None) -> bool:
        if value is None:
            raise ValueError("Enter a valid value.")
        return value

    @field_validator("domains")
    @classmethod
    def clean_domains(cls, values: list[str] | None) -> list[str]:
        if values is None:
            raise ValueError("Enter valid domains.")
        return normalize_domains(values)

    @model_validator(mode="after")
    def require_editable_field(self) -> "EditGroup":
        if not self.model_fields_set.intersection(
            {"name", "nsfw", "domains", "parent_id"}
        ):
            raise ValueError("Enter a valid value.")
        return self


class DeleteGroup(RequestModel):
    url_action: Literal["delete", "move_to_default"]


class ReorderGroups(RequestModel):
    """One sibling set, in its new order.

    `parent_id` is required — `null` addresses the roots — because reordering
    is sibling-scoped and a bare list of IDs would not say which set it means.
    """

    parent_id: PositiveStrictInt | None
    group_ids: list[PositiveStrictInt]


class GroupsResponse(ContractModel):
    groups: list[GroupRecord]


class CreateGroupResponse(ContractModel):
    group: GroupRecord
    groups: list[GroupRecord]


class EditGroupResponse(ContractModel):
    group: GroupRecord
    groups: list[GroupRecord]


class DeleteGroupResponse(ContractModel):
    group_id: PositiveStrictInt
    url_action: Literal["delete", "move_to_default"]
    moved: NonNegativeStrictInt
    deleted: NonNegativeStrictInt
    groups: list[GroupRecord]


class ReorderGroupsResponse(ContractModel):
    groups: list[GroupRecord]


CREATE_GROUP_REQUEST_BODY = {
    "required": True,
    "content": {
        "application/json": {
            "schema": CreateGroup.model_json_schema(),
        },
    },
}


EDIT_GROUP_REQUEST_BODY = {
    "required": True,
    "content": {
        "application/json": {
            "schema": EditGroup.model_json_schema(),
        },
    },
}


DELETE_GROUP_REQUEST_BODY = {
    "required": True,
    "content": {
        "application/json": {
            "schema": DeleteGroup.model_json_schema(),
        },
    },
}


EDIT_URL_REQUEST_BODY = {
    "required": True,
    "content": {
        "application/json": {
            "schema": EditURL.model_json_schema(),
        },
    },
}


CREATE_URL_REQUEST_BODY = {
    "required": True,
    "content": {
        "application/json": {
            "schema": CreateURL.model_json_schema(),
        },
    },
}


SITE_ICON_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "Saved site's icon.",
        "content": {
            "image/png": {"schema": {"type": "string", "format": "binary"}},
            "image/vnd.microsoft.icon": {
                "schema": {"type": "string", "format": "binary"},
            },
        },
    },
    404: {
        "description": "The URL or its icon is unavailable.",
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "properties": {"error": {"type": "string"}},
                    "required": ["error"],
                    "additionalProperties": False,
                },
            },
        },
    },
}


async def _submitted_url(request: Request) -> str:
    request_media_type = media_type(request)
    if request_media_type == "application/json":
        return (await json_model(request, CreateURL)).url

    if request_media_type == "application/x-www-form-urlencoded":
        body = (await request.body()).decode("utf-8", errors="replace")
        return parse_qs(body, keep_blank_values=True).get("url", [""])[0]

    raise ValueError("Unsupported request format.")


def url_to_wire(record: domain.URLRecord) -> URLRecord:
    return URLRecord(
        id=record.id,
        url=record.url,
        title=record.title,
        created_at=record.created_at,
        important=record.important,
        version=record.version,
    )


def group_to_wire(record: domain.GroupRecord) -> GroupRecord:
    return GroupRecord(
        id=record.id,
        name=record.name,
        parent_id=record.parent_id,
        position=record.position,
        depth=record.depth,
        nsfw=record.nsfw,
        domains=list(record.domains),
        urls=[url_to_wire(url) for url in record.urls],
        children=[group_to_wire(child) for child in record.children],
    )


def group_outcome_response(
    outcome: domain.GroupOutcome | domain.BookmarkMutationConflict,
    groups: tuple[domain.GroupRecord, ...] = (),
) -> JSONResponse:
    match outcome:
        case domain.GroupCreated(record=record):
            payload = CreateGroupResponse(
                group=group_to_wire(record),
                groups=[group_to_wire(group) for group in groups],
            )
            return JSONResponse(payload.model_dump(), status_code=201)
        case domain.GroupUpdated(record=record):
            updated = domain.find_group(groups, record.id)
            if updated is None:
                # A concurrent delete may follow our successful commit before
                # this response read. Keep the committed operation successful.
                updated = record
            return JSONResponse(
                EditGroupResponse(
                    group=group_to_wire(updated),
                    groups=[group_to_wire(group) for group in groups],
                ).model_dump()
            )
        case domain.GroupDeleted(
            group_id=group_id, url_action=url_action, moved=moved, deleted=deleted
        ):
            return JSONResponse(
                DeleteGroupResponse(
                    group_id=group_id,
                    url_action=url_action,
                    moved=moved,
                    deleted=deleted,
                    groups=[group_to_wire(group) for group in groups],
                ).model_dump()
            )
        case domain.GroupsReordered():
            return JSONResponse(
                ReorderGroupsResponse(
                    groups=[group_to_wire(group) for group in groups]
                ).model_dump()
            )
        case domain.GroupNotFound() | domain.ParentNotFound():
            return error_response("This group does not exist.", 404)
        case domain.GroupNameConflict():
            return error_response("This group already exists.", 409)
        case domain.ParentIsSelfOrDescendant():
            return error_response("A group cannot be moved into itself.", 400)
        case domain.GroupDepthExceeded():
            return error_response("Groups can be nested three levels deep.", 400)
        case domain.DefaultGroupProtected(operation=operation):
            match operation:
                case "edit":
                    return error_response("The default group cannot be edited.", 400)
                case "delete":
                    return error_response("The default group cannot be deleted.", 400)
            assert_never(operation)
        case domain.GroupHasChildren():
            return error_response(
                "Move or delete this group's child groups first.", 409
            )
        case domain.InvalidGroupOrder():
            return error_response("Invalid group order.", 400)
        case domain.BookmarkMutationConflict():
            return error_response(
                "Another bookmark change is in progress. Try again.", 409
            )
    assert_never(outcome)


type URLOutcome = (
    domain.CreateURLOutcome
    | domain.RefreshURLMetadataOutcome
    | domain.RefreshURLTitleOutcome
    | domain.EditURLOutcome
    | domain.MoveURLOutcome
    | domain.SetImportantOutcome
    | domain.RemoveURLOutcome
)


def map_url_outcome(
    outcome: URLOutcome | domain.BookmarkMutationConflict,
    groups: tuple[domain.GroupRecord, ...] = (),
    urls: tuple[domain.URLRecord, ...] = (),
    *,
    removal: bool = False,
) -> JSONResponse:
    match outcome:
        case domain.URLMetadataRefreshed(
            record=record, title_updated=title_updated, icon_updated=icon_updated
        ):
            return JSONResponse(
                RefreshURLMetadataResponse(
                    url=url_to_wire(record),
                    groups=[group_to_wire(group) for group in groups],
                    title_updated=title_updated,
                    icon_updated=icon_updated,
                ).model_dump()
            )
        case domain.URLCreated(record=record):
            return JSONResponse(
                CreateURLResponse(
                    url=url_to_wire(record),
                    urls=[url_to_wire(url) for url in urls],
                    groups=[group_to_wire(group) for group in groups],
                ).model_dump(),
                status_code=201,
            )
        case domain.URLTitleRefreshed(record=record, title_updated=updated):
            return JSONResponse(
                RefreshURLTitleResponse(
                    url=url_to_wire(record),
                    groups=[group_to_wire(group) for group in groups],
                    title_updated=updated,
                ).model_dump()
            )
        case domain.URLTitleFetchFailed():
            return error_response("Could not fetch a page title.", 502)
        case domain.URLUpdated(record=record):
            return JSONResponse(
                EditURLResponse(
                    url=url_to_wire(record),
                    groups=[group_to_wire(group) for group in groups],
                ).model_dump()
            )
        case domain.URLMoved(
            record=record, group_id=group_id, source_group_id=source_group_id
        ):
            return JSONResponse(
                MoveURLGroupResponse(
                    url=url_to_wire(record),
                    group_id=group_id,
                    source_group_id=source_group_id,
                    groups=[group_to_wire(group) for group in groups],
                ).model_dump()
            )
        case domain.SetImportantSucceeded(record=record):
            return JSONResponse(
                SetImportantResponse(
                    url=url_to_wire(record),
                    groups=[group_to_wire(group) for group in groups],
                ).model_dump()
            )
        case domain.URLRemoved(record=record):
            return JSONResponse(
                DeleteURLByIDResponse(
                    url=url_to_wire(record),
                    urls=[url_to_wire(url) for url in urls],
                    groups=[group_to_wire(group) for group in groups],
                ).model_dump()
            )
        case domain.URLNotFound():
            return error_response("This URL is not saved.", 404)
        case domain.URLConflict():
            return error_response("This URL is already saved.", 409)
        case domain.URLVersionConflict():
            return error_response("This URL was changed. Reload and try again.", 409)
        case domain.EmptyURLEdit():
            return error_response("Enter a valid value.", 400)
        case domain.GroupNotFound():
            return error_response("This group does not exist.", 404)
        case domain.URLSourceRequired():
            return error_response("Choose the URL's source group.", 400)
        case domain.URLMembershipNotFound():
            # Removal has always used the same response for a missing URL
            # and a missing direct membership; moves identify the source.
            return error_response(
                "This URL is not saved."
                if removal
                else "This URL is not saved in the source group.",
                404,
            )
        case domain.BookmarkMutationConflict():
            return error_response(
                "Another bookmark change is in progress. Try again.", 409
            )
    assert_never(outcome)


async def _url_response(
    service: BookmarksApplicationService,
    outcome: URLOutcome,
    *,
    removal: bool = False,
) -> JSONResponse:
    # Preserve response queries after the mutation commits, with the flat URL
    # query before the forest query for creation and removal.
    urls = (
        await service.list_urls()
        if isinstance(outcome, (domain.URLCreated, domain.URLRemoved))
        else ()
    )
    groups = (
        await service.list_groups()
        if isinstance(
            outcome,
            (
                domain.URLCreated,
                domain.URLMetadataRefreshed,
                domain.URLTitleRefreshed,
                domain.URLUpdated,
                domain.URLMoved,
                domain.SetImportantSucceeded,
                domain.URLRemoved,
            ),
        )
        else ()
    )
    return map_url_outcome(outcome, groups, urls, removal=removal)


async def _group_response(
    service: BookmarksApplicationService, outcome: domain.GroupOutcome
) -> JSONResponse:
    groups = (
        await service.list_groups()
        if isinstance(
            outcome,
            (
                domain.GroupCreated,
                domain.GroupUpdated,
                domain.GroupDeleted,
                domain.GroupsReordered,
            ),
        )
        else ()
    )
    return group_outcome_response(outcome, groups)


def build_bookmarks_router(service: BookmarksApplicationService) -> APIRouter:
    router = APIRouter()

    async def get_url_icon(url_id: PositiveID) -> Response:
        record = await service.url_by_id(url_id)
        if record is None:
            return map_url_outcome(domain.URLNotFound(url_id))
        icon = await service.icon_gateway.get(record.url)
        match icon:
            case domain.SiteIcon(data=data, media_type=icon_media_type):
                return Response(
                    content=data,
                    media_type=icon_media_type,
                    headers={"X-Content-Type-Options": "nosniff"},
                )
            case None:
                return error_response("No site icon available.", 404)
        assert_never(icon)

    async def refresh_url_metadata(url_id: PositiveID) -> JSONResponse:
        try:
            outcome = await service.refresh_url_metadata(url_id)
        except domain.BookmarkMutationConflict as error:
            return map_url_outcome(error)
        return await _url_response(service, outcome)

    async def create_url(request: Request) -> JSONResponse:
        try:
            url = domain.normalize_url(await _submitted_url(request))
        except ValueError as error:
            return error_response(str(error), 400)
        try:
            outcome = await service.create_url(domain.CreateURL(url))
        except domain.BookmarkMutationConflict as error:
            return map_url_outcome(error)
        return await _url_response(service, outcome)

    async def refresh_url_title(url_id: PositiveID) -> JSONResponse:
        try:
            outcome = await service.refresh_url_title(url_id)
        except domain.BookmarkMutationConflict as error:
            return map_url_outcome(error)
        return await _url_response(service, outcome)

    async def list_urls() -> JSONResponse:
        records = await service.list_urls()
        return JSONResponse(
            URLsResponse(urls=[url_to_wire(record) for record in records]).model_dump()
        )

    async def edit_url(request: Request, url_id: PositiveID) -> JSONResponse:
        try:
            payload = EditURL.model_validate(await request.json())
        except json.JSONDecodeError:
            return error_response("Enter a valid value.", 400)
        except ValidationError as error:
            if any(item.get("loc") == ("url",) for item in error.errors()):
                return error_response("Enter a URL.", 400)
            return error_response("Enter a valid value.", 400)

        url = None
        if "url" in payload.model_fields_set:
            try:
                url = domain.normalize_url(payload.url)
            except ValueError as error:
                return error_response(str(error), 400)
        title = (
            payload.title
            if "title" in payload.model_fields_set
            else domain.UnchangedURLField.VALUE
        )
        try:
            outcome = await service.edit_url(
                domain.EditURL(url_id, payload.version, url=url, title=title)
            )
        except domain.BookmarkMutationConflict as error:
            return map_url_outcome(error)
        return await _url_response(service, outcome)

    async def move_url_group(url_id: PositiveID, payload: MoveURLGroup) -> JSONResponse:
        try:
            outcome = await service.move_url(
                domain.MoveURL(url_id, payload.group_id, payload.source_group_id)
            )
        except domain.BookmarkMutationConflict as error:
            return map_url_outcome(error)
        return await _url_response(service, outcome)

    async def set_important(url_id: PositiveID, payload: SetImportant) -> JSONResponse:
        try:
            outcome = await service.set_important(
                domain.SetImportant(url_id, payload.important)
            )
        except domain.BookmarkMutationConflict as error:
            return map_url_outcome(error)
        return await _url_response(service, outcome)

    async def delete_url_by_id(
        url_id: PositiveID, group_id: Annotated[int, Query(ge=1)]
    ) -> JSONResponse:
        try:
            outcome = await service.remove_url(domain.RemoveURL(url_id, group_id))
        except domain.BookmarkMutationConflict as error:
            return map_url_outcome(error)
        return await _url_response(service, outcome, removal=True)

    async def list_groups() -> JSONResponse:
        groups = await service.list_groups()
        return JSONResponse(
            GroupsResponse(
                groups=[group_to_wire(group) for group in groups]
            ).model_dump()
        )

    async def create_group(request: Request) -> JSONResponse:
        try:
            payload = await json_model(request, CreateGroup)
        except RequestValidationError as error:
            if any(item.get("loc") == ("body", "nsfw") for item in error.errors()):
                return error_response("Enter a valid value.", 400)
            if any(
                item.get("loc", ())[:2] == ("body", "domains")
                for item in error.errors()
            ):
                return error_response("Enter valid domains.", 400)
            raise
        try:
            outcome = await service.create_group(
                domain.CreateGroup(
                    payload.name,
                    payload.nsfw,
                    tuple(payload.domains),
                    payload.parent_id,
                )
            )
        except domain.BookmarkMutationConflict as error:
            return group_outcome_response(error)
        return await _group_response(service, outcome)

    async def edit_group(request: Request, group_id: PositiveID) -> JSONResponse:
        try:
            payload = EditGroup.model_validate(await request.json())
        except json.JSONDecodeError:
            return error_response("Enter a valid value.", 400)
        except ValidationError as error:
            if any(item.get("loc") == ("name",) for item in error.errors()):
                return error_response("Enter a group name.", 400)
            if any(item.get("loc", ())[:1] == ("domains",) for item in error.errors()):
                return error_response("Enter valid domains.", 400)
            return error_response("Enter a valid value.", 400)
        parent_update = (
            payload.parent_id
            if "parent_id" in payload.model_fields_set
            else domain.UNCHANGED_PARENT
        )
        try:
            outcome = await service.update_group(
                domain.UpdateGroup(
                    group_id,
                    payload.name,
                    payload.nsfw,
                    None if payload.domains is None else tuple(payload.domains),
                    parent_update,
                )
            )
        except domain.BookmarkMutationConflict as error:
            return group_outcome_response(error)
        return await _group_response(service, outcome)

    async def delete_group(request: Request, group_id: PositiveID) -> JSONResponse:
        try:
            payload = DeleteGroup.model_validate(await request.json())
        except json.JSONDecodeError, ValidationError:
            return error_response("Choose how to handle this group's URLs.", 400)
        try:
            outcome = await service.delete_group(
                domain.DeleteGroup(group_id, payload.url_action)
            )
        except domain.BookmarkMutationConflict as error:
            return group_outcome_response(error)
        return await _group_response(service, outcome)

    async def reorder_groups(payload: ReorderGroups) -> JSONResponse:
        try:
            outcome = await service.reorder_groups(
                domain.ReorderGroups(payload.parent_id, tuple(payload.group_ids))
            )
        except domain.BookmarkMutationConflict as error:
            return group_outcome_response(error)
        return await _group_response(service, outcome)

    router.add_api_route(
        "/api/groups",
        list_groups,
        methods=["GET"],
        response_model=GroupsResponse,
    )
    router.add_api_route(
        "/api/groups",
        create_group,
        methods=["POST"],
        response_model=CreateGroupResponse,
        status_code=201,
        openapi_extra={"requestBody": CREATE_GROUP_REQUEST_BODY},
    )
    router.add_api_route(
        "/api/groups/order",
        reorder_groups,
        methods=["PATCH"],
        response_model=ReorderGroupsResponse,
    )
    router.add_api_route(
        "/api/groups/{group_id}",
        edit_group,
        methods=["PATCH"],
        response_model=EditGroupResponse,
        openapi_extra={"requestBody": EDIT_GROUP_REQUEST_BODY},
    )
    router.add_api_route(
        "/api/groups/{group_id}",
        delete_group,
        methods=["DELETE"],
        response_model=DeleteGroupResponse,
        openapi_extra={"requestBody": DELETE_GROUP_REQUEST_BODY},
    )
    router.add_api_route(
        "/api/urls",
        list_urls,
        methods=["GET"],
        response_model=URLsResponse,
    )
    router.add_api_route(
        "/api/urls/{url_id}/group",
        move_url_group,
        methods=["PATCH"],
        response_model=MoveURLGroupResponse,
    )
    router.add_api_route(
        "/api/urls",
        create_url,
        methods=["POST"],
        response_model=CreateURLResponse,
        status_code=201,
        openapi_extra={"requestBody": CREATE_URL_REQUEST_BODY},
    )
    router.add_api_route(
        "/api/urls/{url_id}/refresh-title",
        refresh_url_title,
        methods=["POST"],
        response_model=RefreshURLTitleResponse,
    )
    router.add_api_route(
        "/api/urls/{url_id}/important",
        set_important,
        methods=["PATCH"],
        response_model=SetImportantResponse,
    )
    router.add_api_route(
        "/api/urls/{url_id}/icon",
        get_url_icon,
        methods=["GET"],
        response_class=Response,
        response_model=None,
        responses=SITE_ICON_RESPONSES,
    )
    router.add_api_route(
        "/api/urls/{url_id}/refresh-metadata",
        refresh_url_metadata,
        methods=["POST"],
        response_model=RefreshURLMetadataResponse,
    )
    router.add_api_route(
        "/api/urls/{url_id}",
        edit_url,
        methods=["PATCH"],
        response_model=EditURLResponse,
        openapi_extra={"requestBody": EDIT_URL_REQUEST_BODY},
    )
    router.add_api_route(
        "/api/urls/{url_id}",
        delete_url_by_id,
        methods=["DELETE"],
        response_model=DeleteURLByIDResponse,
    )
    return router
