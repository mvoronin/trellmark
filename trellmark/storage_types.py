from datetime import datetime
from typing import Literal, TypedDict


class URLRecord(TypedDict):
    id: int
    url: str
    title: str | None
    created_at: str
    important: bool
    version: int


class SiteIconCacheRecord(TypedDict):
    """Private derived cache state; never part of URLRecord or exports."""

    origin: str
    icon_bytes: bytes | None
    media_type: str | None
    fetched_at: datetime | None
    retry_after: datetime


class UpdateURLFields(TypedDict, total=False):
    url: str
    title: str | None


class GroupRecord(TypedDict):
    """One group and, recursively, the groups nested under it.

    `position` is the group's place among its own siblings, `depth` its
    one-based level (1..3) derived from the stored `ltree` path, and `urls` its
    **direct** memberships only — a parent never aggregates a descendant's URLs.
    The path itself is storage-only and never reaches this record.
    """

    id: int
    name: str
    parent_id: int | None
    position: int
    depth: int
    nsfw: bool
    domains: list[str]
    urls: list[URLRecord]
    children: list["GroupRecord"]


class CleanedImportURLRecord(TypedDict):
    """An import record after validation, still carrying the wire timestamp.

    The import request contract is a string; the conversion to an instant
    happens at the storage boundary, in ImportDocument.to_storage_document.
    """

    url: str
    title: str | None
    created_at: str
    important: bool


class ImportURLRecord(TypedDict):
    url: str
    title: str | None
    created_at: datetime
    important: bool


class CleanedImportGroupRecord(TypedDict):
    name: str
    parent: str | None
    position: int
    nsfw: bool
    domains: list[str]
    urls: list[CleanedImportURLRecord]


class ImportGroupRecord(TypedDict):
    name: str
    parent: str | None
    position: int
    nsfw: bool
    domains: list[str]
    urls: list[ImportURLRecord]


class ImportDocument(TypedDict):
    version: Literal[1]
    groups: list[ImportGroupRecord]


class ImportPayloadDocument(TypedDict):
    """The validated request body, before timestamps become instants."""

    version: Literal[1]
    exported_at: str
    groups: list[CleanedImportGroupRecord]


class ExportURLRecord(TypedDict):
    url: str
    title: str | None
    created_at: str
    important: bool


class ExportGroupRecord(TypedDict):
    name: str
    parent: str | None
    position: int
    nsfw: bool
    domains: list[str]
    urls: list[ExportURLRecord]


class ExportDocument(TypedDict):
    version: Literal[1]
    exported_at: str
    groups: list[ExportGroupRecord]


class ImportResult(TypedDict):
    imported: int
    skipped: int
    groups: list[GroupRecord]


class DeleteGroupResult(TypedDict):
    group_id: int
    url_action: Literal["delete", "move_to_default"]
    moved: int
    deleted: int


class MoveURLGroupResult(TypedDict):
    url: URLRecord
    source_group_id: int
