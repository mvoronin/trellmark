from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Annotated, Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from . import storage_types
from .title_text import clean_title_text
from .url_normalization import normalize_domains, normalize_url

PositiveStrictInt = Annotated[StrictInt, Field(ge=1)]
NonNegativeStrictInt = Annotated[StrictInt, Field(ge=0)]
# Three levels, root inclusive, matching the database check constraint.
GroupDepth = Annotated[StrictInt, Field(ge=1, le=3)]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


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


class URLRecord(ContractModel):
    id: PositiveStrictInt
    url: StrictStr
    title: StrictStr | None
    created_at: StrictStr
    important: StrictBool
    version: PositiveStrictInt


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


class MoveURLGroup(RequestModel):
    group_id: PositiveStrictInt
    source_group_id: PositiveStrictInt | None = None


class SetImportant(RequestModel):
    important: StrictBool


class ReorderGroups(RequestModel):
    """One sibling set, in its new order.

    `parent_id` is required — `null` addresses the roots — because reordering
    is sibling-scoped and a bare list of IDs would not say which set it means.
    """

    parent_id: PositiveStrictInt | None
    group_ids: list[PositiveStrictInt]


class ImportURLRecord(ContractModel):
    url: StrictStr
    created_at: StrictStr
    title: StrictStr | None = None
    important: StrictBool = False


class ImportGroupRecord(ContractModel):
    name: StrictStr
    parent: StrictStr | None = None
    position: NonNegativeStrictInt
    nsfw: StrictBool = False
    domains: list[StrictStr] = Field(default_factory=list)
    urls: list[ImportURLRecord]


class ImportDocument(ContractModel):
    version: Literal[1]
    exported_at: StrictStr
    groups: list[ImportGroupRecord]

    @model_validator(mode="before")
    @classmethod
    def clean_document(cls, value: object) -> storage_types.ImportPayloadDocument:
        return _clean_import_document(value)

    def validate_against(
        self,
        existing_groups: Sequence[storage_types.GroupRecord],
    ) -> None:
        """Validate parent names against storage without recleaning the payload."""
        _validate_resulting_import_hierarchy(self.groups, existing_groups)

    def to_storage_document(self) -> storage_types.ImportDocument:
        return {
            "version": self.version,
            "groups": [
                {
                    "name": group.name,
                    "parent": group.parent,
                    "position": group.position,
                    "nsfw": group.nsfw,
                    "domains": group.domains,
                    "urls": [
                        {
                            "url": record.url,
                            "title": record.title,
                            "created_at": import_timestamp(record.created_at),
                            "important": record.important,
                        }
                        for record in group.urls
                    ],
                }
                for group in self.groups
            ],
        }


class ExportURLRecord(ContractModel):
    url: StrictStr
    title: StrictStr | None
    created_at: StrictStr
    important: StrictBool


class ExportGroupRecord(ContractModel):
    name: StrictStr
    parent: StrictStr | None
    position: NonNegativeStrictInt
    nsfw: StrictBool
    domains: list[StrictStr]
    urls: list[ExportURLRecord]


class ExportDocument(ContractModel):
    version: Literal[1]
    exported_at: StrictStr
    groups: list[ExportGroupRecord]


class URLsResponse(ContractModel):
    urls: list[URLRecord]


class GroupsResponse(ContractModel):
    groups: list[GroupRecord]


class CreateURLResponse(ContractModel):
    url: URLRecord
    urls: list[URLRecord]
    groups: list[GroupRecord]


class EditURLResponse(ContractModel):
    url: URLRecord
    groups: list[GroupRecord]


class RefreshURLTitleResponse(ContractModel):
    url: URLRecord
    groups: list[GroupRecord]
    title_updated: StrictBool


class RefreshURLMetadataResponse(RefreshURLTitleResponse):
    icon_updated: StrictBool


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


class MoveURLGroupResponse(ContractModel):
    url: URLRecord
    group_id: PositiveStrictInt
    source_group_id: PositiveStrictInt
    groups: list[GroupRecord]


class SetImportantResponse(ContractModel):
    url: URLRecord
    groups: list[GroupRecord]


class ReorderGroupsResponse(ContractModel):
    groups: list[GroupRecord]


class ImportResponse(ContractModel):
    imported: NonNegativeStrictInt
    skipped: NonNegativeStrictInt
    groups: list[GroupRecord]


class DeleteURLByIDResponse(ContractModel):
    url: URLRecord
    urls: list[URLRecord]
    groups: list[GroupRecord]


def _clean_import_document(payload: object) -> storage_types.ImportPayloadDocument:
    if not isinstance(payload, dict):
        raise ValueError("Invalid import file.")
    document = cast(dict[str, object], payload)

    version_value = document.get("version")
    if version_value != 1:
        raise ValueError("Invalid import file.")
    version = cast(Literal[1], version_value)

    exported_at = document.get("exported_at")
    if not isinstance(exported_at, str):
        raise ValueError("Invalid import file.")
    _parse_import_timestamp(exported_at)

    groups_value = document.get("groups")
    if not isinstance(groups_value, list) or not groups_value:
        raise ValueError("Invalid import file.")
    groups = cast(list[object], groups_value)

    positions_by_parent: dict[str | None, set[int]] = {}
    names: set[str] = set()
    cleaned_groups: list[storage_types.CleanedImportGroupRecord] = []
    for group in groups:
        cleaned_group = _clean_import_group(
            group,
            version,
            names,
            positions_by_parent,
        )
        cleaned_groups.append(cleaned_group)

    _validate_cleaned_import_hierarchy(cleaned_groups)
    return {
        "version": version,
        "exported_at": exported_at,
        "groups": cleaned_groups,
    }


def _clean_import_group(
    group: object,
    version: Literal[1],
    names: set[str],
    positions_by_parent: dict[str | None, set[int]],
) -> storage_types.CleanedImportGroupRecord:
    if not isinstance(group, dict):
        raise ValueError("Invalid import file.")
    group_data = cast(dict[str, object], group)

    name = group_data.get("name")
    if not isinstance(name, str):
        raise ValueError("Invalid import file.")
    name = name.strip()
    if not name:
        raise ValueError("Invalid import file.")

    # Match the functional unique index and lookup predicate, both lower(name),
    # rather than the more aggressive Unicode semantics of casefold().
    name_key = name.lower()
    if name_key in names:
        raise ValueError("Invalid import file.")
    names.add(name_key)

    parent: str | None = None
    parent_value = group_data.get("parent")
    if parent_value is not None:
        if not isinstance(parent_value, str):
            raise ValueError("Invalid import file.")
        parent = parent_value.strip()
        if not parent:
            raise ValueError("Invalid import file.")

    position = group_data.get("position")
    sibling_key = None if parent is None else parent.lower()
    sibling_positions = positions_by_parent.setdefault(sibling_key, set())
    if type(position) is not int or position < 0 or position in sibling_positions:
        raise ValueError("Invalid import file.")
    sibling_positions.add(position)

    nsfw = group_data.get("nsfw", False)
    if type(nsfw) is not bool:
        raise ValueError("Invalid import file.")

    domains_value = group_data.get("domains", [])
    if not isinstance(domains_value, list):
        raise ValueError("Invalid import file.")
    raw_domains = cast(list[object], domains_value)
    if not all(isinstance(domain, str) for domain in raw_domains):
        raise ValueError("Invalid import file.")
    try:
        domains = normalize_domains(cast(list[str], raw_domains))
    except ValueError as error:
        raise ValueError("Invalid import file.") from error

    urls_value = group_data.get("urls")
    if not isinstance(urls_value, list):
        raise ValueError("Invalid import file.")
    urls = cast(list[object], urls_value)

    return {
        "name": name,
        "parent": parent,
        "position": position,
        "nsfw": bool(nsfw),
        "domains": domains,
        "urls": [_clean_import_url(record, version) for record in urls],
    }


def _validate_cleaned_import_hierarchy(
    groups: Sequence[storage_types.CleanedImportGroupRecord],
) -> None:
    """Reject hierarchy errors that can be proven from the document alone."""
    parents = {
        group["name"].lower(): (
            None if group["parent"] is None else group["parent"].lower()
        )
        for group in groups
    }
    if parents.get("default") is not None:
        raise ValueError("Invalid import file.")
    _validate_parent_graph(parents, require_known_parents=False)


def _validate_resulting_import_hierarchy(
    groups: Sequence[ImportGroupRecord],
    existing_groups: Sequence[storage_types.GroupRecord],
) -> None:
    """Validate the complete forest after overlaying an import document.

    Existing groups absent from the document keep their current parents. A
    document group replaces that one edge, so cycles and depth are checked
    across imported and stored ancestry together before storage mutates either.
    """
    parents: dict[str, str | None] = {}

    def add_existing(
        siblings: Sequence[storage_types.GroupRecord],
        parent: str | None,
    ) -> None:
        for group in siblings:
            key = group["name"].lower()
            parents[key] = parent
            add_existing(group["children"], key)

    add_existing(existing_groups, None)

    # Register new document groups before resolving references, because a
    # child may appear earlier than its parent in the portable flat list.
    for group in groups:
        parents.setdefault(group.name.lower(), None)

    for group in groups:
        key = group.name.lower()
        parent = None if group.parent is None else group.parent.lower()
        if parent is not None and parent not in parents:
            raise ValueError("Invalid import file.")
        if key == "default" and parent is not None:
            raise ValueError("Invalid import file.")
        parents[key] = parent

    _validate_parent_graph(parents, require_known_parents=True)


def _validate_parent_graph(
    parents: dict[str, str | None],
    *,
    require_known_parents: bool,
) -> None:
    for origin in parents:
        path: set[str] = set()
        current: str | None = origin
        depth = 0
        while current is not None:
            if current in path:
                raise ValueError("Invalid import file.")
            path.add(current)
            depth += 1
            if depth > 3:
                raise ValueError("Invalid import file.")
            if current not in parents:
                if require_known_parents:
                    raise ValueError("Invalid import file.")
                break
            current = parents[current]


def _clean_import_url(
    record: object,
    version: Literal[1],
) -> storage_types.CleanedImportURLRecord:
    if not isinstance(record, dict):
        raise ValueError("Invalid import file.")
    record_data = cast(dict[str, object], record)

    url = record_data.get("url")
    if not isinstance(url, str):
        raise ValueError("Invalid import file.")

    created_at = _canonical_import_timestamp(record_data.get("created_at"))
    important = record_data.get("important", False)
    if type(important) is not bool:
        raise ValueError("Invalid import file.")
    title = _clean_import_title(record_data.get("title"))

    return {
        "url": normalize_url(url),
        "title": title,
        "created_at": created_at,
        "important": bool(important),
    }


def _clean_import_title(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Invalid import file.")
    return clean_title_text(value)


def _canonical_import_timestamp(value: object) -> str:
    """Validate an imported timestamp and normalize it to the API's UTC form.

    The wire contract for an import document is a string, so this stays a
    string; `import_timestamp` turns it into the instant storage wants.
    """
    return import_timestamp(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def import_timestamp(value: object) -> datetime:
    """Return an imported timestamp as an aware UTC instant.

    Export documents carry ISO 8601 strings. A naive value is interpreted as
    UTC so hand-authored imports cannot accidentally depend on server locale.
    """
    parsed = _parse_import_timestamp(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_import_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Invalid import file.")
    if value.endswith("Z"):
        value = f"{value[:-1]}+00:00"
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("Invalid import file.") from error
