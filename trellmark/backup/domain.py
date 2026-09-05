"""Portable version-1 bookmark documents and framework-free import policy."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, cast

from ..bookmarks.domain import (
    GroupRecord,
    clean_title_text,
    normalize_domains,
    normalize_url,
)


class InvalidImportDocument(ValueError):
    """The supplied document or resulting bookmark hierarchy is invalid."""


@dataclass(frozen=True, slots=True)
class PortableURL:
    url: str
    title: str | None
    created_at: str
    important: bool


@dataclass(frozen=True, slots=True)
class PortableGroup:
    name: str
    parent: str | None
    position: int
    nsfw: bool
    domains: tuple[str, ...]
    urls: tuple[PortableURL, ...]


@dataclass(frozen=True, slots=True)
class PortableDocument:
    version: Literal[1]
    exported_at: str
    groups: tuple[PortableGroup, ...]


def normalize_import_document(payload: object) -> PortableDocument:
    if not isinstance(payload, dict):
        raise InvalidImportDocument("Invalid import file.")
    document = cast(dict[str, object], payload)

    version_value = document.get("version")
    if version_value != 1:
        raise InvalidImportDocument("Invalid import file.")
    version = cast(Literal[1], version_value)

    exported_at = document.get("exported_at")
    if not isinstance(exported_at, str):
        raise InvalidImportDocument("Invalid import file.")
    _parse_import_timestamp(exported_at)

    groups_value = document.get("groups")
    if not isinstance(groups_value, list) or not groups_value:
        raise InvalidImportDocument("Invalid import file.")
    groups = cast(list[object], groups_value)

    positions_by_parent: dict[str | None, set[int]] = {}
    names: set[str] = set()
    cleaned_groups: list[PortableGroup] = []
    for group in groups:
        cleaned_group = _clean_import_group(
            group,
            version,
            names,
            positions_by_parent,
        )
        cleaned_groups.append(cleaned_group)

    _validate_cleaned_import_hierarchy(cleaned_groups)
    return PortableDocument(version, exported_at, tuple(cleaned_groups))


def _clean_import_group(
    group: object,
    version: Literal[1],
    names: set[str],
    positions_by_parent: dict[str | None, set[int]],
) -> PortableGroup:
    if not isinstance(group, dict):
        raise InvalidImportDocument("Invalid import file.")
    group_data = cast(dict[str, object], group)

    name = group_data.get("name")
    if not isinstance(name, str):
        raise InvalidImportDocument("Invalid import file.")
    name = name.strip()
    if not name:
        raise InvalidImportDocument("Invalid import file.")

    # Match the functional unique index and lookup predicate, both lower(name),
    # rather than the more aggressive Unicode semantics of casefold().
    name_key = name.lower()
    if name_key in names:
        raise InvalidImportDocument("Invalid import file.")
    names.add(name_key)

    parent: str | None = None
    parent_value = group_data.get("parent")
    if parent_value is not None:
        if not isinstance(parent_value, str):
            raise InvalidImportDocument("Invalid import file.")
        parent = parent_value.strip()
        if not parent:
            raise InvalidImportDocument("Invalid import file.")

    position = group_data.get("position")
    sibling_key = None if parent is None else parent.lower()
    sibling_positions = positions_by_parent.setdefault(sibling_key, set())
    if type(position) is not int or position < 0 or position in sibling_positions:
        raise InvalidImportDocument("Invalid import file.")
    sibling_positions.add(position)

    nsfw = group_data.get("nsfw", False)
    if type(nsfw) is not bool:
        raise InvalidImportDocument("Invalid import file.")

    domains_value = group_data.get("domains", [])
    if not isinstance(domains_value, list):
        raise InvalidImportDocument("Invalid import file.")
    raw_domains = cast(list[object], domains_value)
    if not all(isinstance(domain, str) for domain in raw_domains):
        raise InvalidImportDocument("Invalid import file.")
    try:
        domains = normalize_domains(cast(list[str], raw_domains))
    except ValueError as error:
        raise InvalidImportDocument("Invalid import file.") from error

    urls_value = group_data.get("urls")
    if not isinstance(urls_value, list):
        raise InvalidImportDocument("Invalid import file.")
    urls = cast(list[object], urls_value)

    return PortableGroup(
        name=name,
        parent=parent,
        position=position,
        nsfw=bool(nsfw),
        domains=tuple(domains),
        urls=tuple(_clean_import_url(record, version) for record in urls),
    )


def _validate_cleaned_import_hierarchy(
    groups: Sequence[PortableGroup],
) -> None:
    """Reject hierarchy errors that can be proven from the document alone."""
    parents = {
        group.name.lower(): (None if group.parent is None else group.parent.lower())
        for group in groups
    }
    if parents.get("default") is not None:
        raise InvalidImportDocument("Invalid import file.")
    _validate_parent_graph(parents, require_known_parents=False)


def validate_resulting_import_hierarchy(
    groups: Sequence[PortableGroup],
    existing_groups: Sequence[GroupRecord],
) -> None:
    """Validate the complete forest after overlaying an import document.

    Existing groups absent from the document keep their current parents. A
    document group replaces that one edge, so cycles and depth are checked
    across imported and stored ancestry together before storage mutates either.
    """
    parents: dict[str, str | None] = {}

    def add_existing(
        siblings: Sequence[GroupRecord],
        parent: str | None,
    ) -> None:
        for group in siblings:
            key = group.name.lower()
            parents[key] = parent
            add_existing(group.children, key)

    add_existing(existing_groups, None)

    # Register new document groups before resolving references, because a
    # child may appear earlier than its parent in the portable flat list.
    for group in groups:
        parents.setdefault(group.name.lower(), None)

    for group in groups:
        key = group.name.lower()
        parent = None if group.parent is None else group.parent.lower()
        if parent is not None and parent not in parents:
            raise InvalidImportDocument("Invalid import file.")
        if key == "default" and parent is not None:
            raise InvalidImportDocument("Invalid import file.")
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
                raise InvalidImportDocument("Invalid import file.")
            path.add(current)
            depth += 1
            if depth > 3:
                raise InvalidImportDocument("Invalid import file.")
            if current not in parents:
                if require_known_parents:
                    raise InvalidImportDocument("Invalid import file.")
                break
            current = parents[current]


def _clean_import_url(
    record: object,
    version: Literal[1],
) -> PortableURL:
    if not isinstance(record, dict):
        raise InvalidImportDocument("Invalid import file.")
    record_data = cast(dict[str, object], record)

    url = record_data.get("url")
    if not isinstance(url, str):
        raise InvalidImportDocument("Invalid import file.")

    created_at = _canonical_import_timestamp(record_data.get("created_at"))
    important = record_data.get("important", False)
    if type(important) is not bool:
        raise InvalidImportDocument("Invalid import file.")
    title = _clean_import_title(record_data.get("title"))

    try:
        normalized_url = normalize_url(url)
    except ValueError as error:
        raise InvalidImportDocument(str(error)) from error
    return PortableURL(normalized_url, title, created_at, bool(important))


def _clean_import_title(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidImportDocument("Invalid import file.")
    return clean_title_text(value)


def _canonical_import_timestamp(value: object) -> str:
    """Validate an imported timestamp and normalize it to the API's UTC form.

    Portable documents retain strings; persistence adapters can use
    `import_timestamp` to obtain the corresponding absolute instant.
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
        raise InvalidImportDocument("Invalid import file.")
    if value.endswith("Z"):
        value = f"{value[:-1]}+00:00"
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise InvalidImportDocument("Invalid import file.") from error
