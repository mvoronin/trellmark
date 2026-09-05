import ipaddress
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto
from typing import Literal
from urllib.parse import urlparse, urlunparse


class BookmarkMutationConflict(RuntimeError):
    """Another bookmark mutation owns the shared PostgreSQL gate."""


@dataclass(frozen=True, slots=True)
class SetImportant:
    url_id: int
    important: bool


@dataclass(frozen=True, slots=True)
class SetImportantSucceeded:
    record: "URLRecord"


@dataclass(frozen=True, slots=True)
class URLNotFound:
    url_id: int


type SetImportantOutcome = SetImportantSucceeded | URLNotFound


@dataclass(frozen=True, slots=True)
class URLRecord:
    id: int
    url: str
    title: str | None
    created_at: str
    important: bool
    version: int


@dataclass(frozen=True, slots=True)
class CreateURL:
    url: str
    title: str | None = None


@dataclass(frozen=True, slots=True)
class URLCreated:
    record: URLRecord


@dataclass(frozen=True, slots=True)
class URLTitleRefreshed:
    record: URLRecord
    title_updated: bool


@dataclass(frozen=True, slots=True)
class URLMetadataRefreshed:
    record: URLRecord
    title_updated: bool
    icon_updated: bool


@dataclass(frozen=True, slots=True)
class SiteIcon:
    data: bytes
    media_type: str


@dataclass(frozen=True, slots=True)
class SiteIconCacheRecord:
    origin: str
    icon_bytes: bytes | None
    media_type: str | None
    fetched_at: datetime | None
    retry_after: datetime


@dataclass(frozen=True, slots=True)
class URLTitleFetchFailed:
    url_id: int


class UnchangedURLField(Enum):
    VALUE = auto()


@dataclass(frozen=True, slots=True)
class EditURL:
    url_id: int
    expected_version: int
    url: str | None = None
    title: str | None | UnchangedURLField = UnchangedURLField.VALUE


@dataclass(frozen=True, slots=True)
class MoveURL:
    url_id: int
    group_id: int
    source_group_id: int | None = None


@dataclass(frozen=True, slots=True)
class RemoveURL:
    url_id: int
    group_id: int


@dataclass(frozen=True, slots=True)
class URLUpdated:
    record: URLRecord


@dataclass(frozen=True, slots=True)
class URLConflict:
    url: str


@dataclass(frozen=True, slots=True)
class URLVersionConflict:
    url_id: int
    expected_version: int


@dataclass(frozen=True, slots=True)
class EmptyURLEdit:
    url_id: int


@dataclass(frozen=True, slots=True)
class URLMoved:
    record: URLRecord
    group_id: int
    source_group_id: int


@dataclass(frozen=True, slots=True)
class URLSourceRequired:
    url_id: int
    group_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class URLMembershipNotFound:
    url_id: int
    group_id: int


@dataclass(frozen=True, slots=True)
class URLRemoved:
    record: URLRecord
    group_id: int


type EditURLOutcome = (
    URLUpdated | URLNotFound | URLConflict | URLVersionConflict | EmptyURLEdit
)
type MoveURLOutcome = (
    URLMoved | URLNotFound | GroupNotFound | URLSourceRequired | URLMembershipNotFound
)
type RemoveURLOutcome = URLRemoved | URLNotFound | URLMembershipNotFound
type CreateURLOutcome = URLCreated | URLConflict
type RefreshURLTitleOutcome = (
    URLTitleRefreshed | URLTitleFetchFailed | URLNotFound | URLVersionConflict
)
type RefreshURLMetadataOutcome = URLMetadataRefreshed | URLNotFound | URLVersionConflict


def clean_title_text(value: str) -> str | None:
    title = " ".join(value.split())
    return title or None


@dataclass(frozen=True, slots=True)
class GroupRecord:
    """An ordered group subtree with direct URL memberships only."""

    id: int
    name: str
    parent_id: int | None
    position: int
    depth: int
    nsfw: bool
    domains: tuple[str, ...]
    urls: tuple[URLRecord, ...]
    children: tuple["GroupRecord", ...] = ()


UNCHANGED_PARENT: Literal["unchanged_parent"] = "unchanged_parent"
type ParentUpdate = int | None | Literal["unchanged_parent"]
type GroupURLAction = Literal["delete", "move_to_default"]
MAX_GROUP_DEPTH = 3


@dataclass(frozen=True, slots=True)
class CreateGroup:
    name: str
    nsfw: bool = False
    domains: tuple[str, ...] = ()
    parent_id: int | None = None


@dataclass(frozen=True, slots=True)
class UpdateGroup:
    group_id: int
    name: str | None = None
    nsfw: bool | None = None
    domains: tuple[str, ...] | None = None
    parent_id: ParentUpdate = UNCHANGED_PARENT


@dataclass(frozen=True, slots=True)
class DeleteGroup:
    group_id: int
    url_action: GroupURLAction


@dataclass(frozen=True, slots=True)
class ReorderGroups:
    parent_id: int | None
    group_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class GroupCreated:
    record: GroupRecord


@dataclass(frozen=True, slots=True)
class GroupUpdated:
    record: GroupRecord


@dataclass(frozen=True, slots=True)
class GroupDeleted:
    group_id: int
    url_action: GroupURLAction
    moved: int
    deleted: int


@dataclass(frozen=True, slots=True)
class GroupsReordered:
    parent_id: int | None
    group_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class GroupNotFound:
    group_id: int


@dataclass(frozen=True, slots=True)
class GroupNameConflict:
    name: str


@dataclass(frozen=True, slots=True)
class ParentNotFound:
    parent_id: int | None


@dataclass(frozen=True, slots=True)
class ParentIsSelfOrDescendant:
    parent_id: int | None


@dataclass(frozen=True, slots=True)
class GroupDepthExceeded:
    maximum_depth: int = MAX_GROUP_DEPTH


@dataclass(frozen=True, slots=True)
class DefaultGroupProtected:
    group_id: int
    operation: Literal["edit", "delete"]


@dataclass(frozen=True, slots=True)
class GroupHasChildren:
    group_id: int


@dataclass(frozen=True, slots=True)
class InvalidGroupOrder:
    parent_id: int | None
    group_ids: tuple[int, ...]


type GroupHierarchyFailure = (
    ParentNotFound | ParentIsSelfOrDescendant | GroupDepthExceeded
)
type CreateGroupOutcome = GroupCreated | GroupNameConflict | GroupHierarchyFailure
type UpdateGroupOutcome = (
    GroupUpdated
    | GroupNotFound
    | DefaultGroupProtected
    | GroupNameConflict
    | GroupHierarchyFailure
)
type DeleteGroupOutcome = (
    GroupDeleted | GroupNotFound | DefaultGroupProtected | GroupHasChildren
)
type ReorderGroupsOutcome = GroupsReordered | InvalidGroupOrder
type GroupOutcome = (
    CreateGroupOutcome | UpdateGroupOutcome | DeleteGroupOutcome | ReorderGroupsOutcome
)


def flatten_groups(groups: Sequence[GroupRecord]) -> tuple[GroupRecord, ...]:
    return tuple(
        record
        for group in groups
        for record in (group, *flatten_groups(group.children))
    )


def find_group(groups: Sequence[GroupRecord], group_id: int) -> GroupRecord | None:
    return next(
        (group for group in flatten_groups(groups) if group.id == group_id), None
    )


def valid_group_order(group_ids: Sequence[int], existing_ids: Sequence[int]) -> bool:
    return len(group_ids) == len(existing_ids) and set(group_ids) == set(existing_ids)


DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def normalize_url(value: str) -> str:
    url = value.strip()
    if not url:
        raise ValueError("Enter a URL.")

    if "\n" in url or "\r" in url:
        raise ValueError("URL cannot contain line breaks.")

    if any(char.isspace() for char in url):
        raise ValueError("URL cannot contain spaces.")

    if "://" not in url:
        url = f"https://{url}"

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Enter a valid http or https URL.")

    # Canonicalize so equivalent URLs dedupe: scheme and host are
    # case-insensitive, and a bare "/" path is equivalent to no path. A
    # trailing slash deeper in the path (e.g. /docs/ vs /docs) is left alone,
    # since those can be distinct resources.
    path = "" if parsed.path == "/" else parsed.path
    return urlunparse(
        parsed._replace(
            scheme=parsed.scheme.lower(),
            netloc=parsed.netloc.lower(),
            path=path,
        )
    )


def normalize_domain(value: str) -> str:
    """Return a canonical hostname suitable for exact URL matching."""
    domain = value.strip().rstrip(".").lower()
    if not domain or any(char.isspace() for char in domain):
        raise ValueError("Enter valid domains.")

    try:
        return str(ipaddress.ip_address(domain))
    except ValueError:
        pass

    if any(char in domain for char in "/:@?#"):
        raise ValueError("Enter valid domains.")

    try:
        ascii_domain = domain.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise ValueError("Enter valid domains.") from error

    if len(ascii_domain) > 253 or any(
        not DOMAIN_LABEL.fullmatch(label) for label in ascii_domain.split(".")
    ):
        raise ValueError("Enter valid domains.")
    return ascii_domain


def normalize_domains(values: Sequence[str]) -> list[str]:
    """Normalize, deduplicate, and sort domain rules."""
    return sorted({normalize_domain(value) for value in values})


def domain_for_url(url: str) -> str | None:
    """Return a matchable URL hostname, or None for a non-domain hostname.

    URL validation deliberately accepts a wider set of HTTP host strings than
    group domain rules. Those URLs remain saveable and simply fall back to the
    default group.
    """
    hostname = urlparse(url).hostname
    if hostname is None:
        raise ValueError("Enter a valid http or https URL.")
    try:
        return normalize_domain(hostname)
    except ValueError:
        return None
