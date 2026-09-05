"""Backup HTTP contracts, outcome mapping, and focused route construction."""

from datetime import datetime, timezone
from typing import Any, Literal, assert_never

from fastapi import APIRouter, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import Field, StrictBool, StrictStr, model_validator
from sqlalchemy.exc import SQLAlchemyError

from ..bookmarks import api as bookmark_contracts
from ..bookmarks.domain import BookmarkMutationConflict
from ..platform.contracts import ContractModel, NonNegativeStrictInt
from ..platform.responses import error_response
from . import domain
from .application import (
    BackupApplicationService,
    ImportInvalid,
    ImportOutcome,
    ImportSucceeded,
)


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
    def clean_document(cls, value: object) -> dict[str, object]:
        return import_document_payload(domain.normalize_import_document(value))

    def to_domain(self) -> domain.PortableDocument:
        """Map already-normalized transport values without rerunning policy."""
        return domain.PortableDocument(
            version=self.version,
            exported_at=self.exported_at,
            groups=tuple(
                domain.PortableGroup(
                    name=group.name,
                    parent=group.parent,
                    position=group.position,
                    nsfw=group.nsfw,
                    domains=tuple(group.domains),
                    urls=tuple(
                        domain.PortableURL(
                            url=record.url,
                            title=record.title,
                            created_at=record.created_at,
                            important=record.important,
                        )
                        for record in group.urls
                    ),
                )
                for group in self.groups
            ),
        )


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


class ImportResponse(ContractModel):
    imported: NonNegativeStrictInt
    skipped: NonNegativeStrictInt
    groups: list[bookmark_contracts.GroupRecord]


class InvalidImportResponse(ContractModel):
    error: Literal["Invalid import file."]
    code: Literal["invalid_import"]


class ImportConflictResponse(ContractModel):
    error: Literal[
        "Another bookmark change is in progress. "
        "No import changes were saved. Try again."
    ]
    code: Literal["import_conflict"]


class ImportFailedResponse(ContractModel):
    error: Literal["Import failed. No import changes were saved. Try again."]
    code: Literal["import_failed"]


INVALID_IMPORT_MESSAGE = "Invalid import file."
IMPORT_CONFLICT_MESSAGE = (
    "Another bookmark change is in progress. No import changes were saved. Try again."
)
IMPORT_FAILED_MESSAGE = "Import failed. No import changes were saved. Try again."


async def import_validation_exception_handler(
    request: Request, error: Exception
) -> JSONResponse:
    if not isinstance(error, RequestValidationError):
        raise error
    payload = InvalidImportResponse(error=INVALID_IMPORT_MESSAGE, code="invalid_import")
    return JSONResponse(payload.model_dump(), status_code=422)


async def import_database_exception_handler(
    request: Request, error: Exception
) -> JSONResponse:
    if not isinstance(error, SQLAlchemyError):
        raise error
    return error_response(IMPORT_FAILED_MESSAGE, 500, code="import_failed")


IMPORT_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "model": ImportResponse,
        "description": "Bookmark import completed.",
        "content": {
            "application/json": {
                "example": {"imported": 1, "skipped": 0, "groups": []},
            }
        },
    },
    409: {
        "model": ImportConflictResponse,
        "description": "Another bookmark mutation currently owns the import gate.",
        "content": {
            "application/json": {
                "example": {
                    "error": IMPORT_CONFLICT_MESSAGE,
                    "code": "import_conflict",
                },
            }
        },
    },
    422: {
        "model": InvalidImportResponse,
        "description": "The import document is invalid.",
        "content": {
            "application/json": {
                "example": {
                    "error": INVALID_IMPORT_MESSAGE,
                    "code": "invalid_import",
                },
            }
        },
    },
    500: {
        "model": ImportFailedResponse,
        "description": "The import was rolled back after an unexpected failure.",
        "content": {
            "application/json": {
                "example": {
                    "error": IMPORT_FAILED_MESSAGE,
                    "code": "import_failed",
                },
            }
        },
    },
}


def map_import_outcome(
    outcome: ImportOutcome | BookmarkMutationConflict,
) -> JSONResponse:
    match outcome:
        case BookmarkMutationConflict():
            payload = ImportConflictResponse(
                error=IMPORT_CONFLICT_MESSAGE, code="import_conflict"
            )
            return error_response(payload.error, 409, code=payload.code)
        case ImportInvalid():
            payload = InvalidImportResponse(
                error=INVALID_IMPORT_MESSAGE, code="invalid_import"
            )
            return error_response(payload.error, 422, code=payload.code)
        case ImportSucceeded(imported=imported, skipped=skipped, groups=groups):
            return JSONResponse(
                ImportResponse(
                    imported=imported,
                    skipped=skipped,
                    groups=[
                        bookmark_contracts.group_to_wire(group) for group in groups
                    ],
                ).model_dump()
            )
    assert_never(outcome)


def build_backup_router(service: BackupApplicationService) -> APIRouter:
    router = APIRouter()

    async def export_data() -> JSONResponse:
        exported_at = datetime.now(timezone.utc).replace(microsecond=0)
        document = await service.export_document(
            exported_at.isoformat().replace("+00:00", "Z")
        )
        filename = f"trellmark-export-{exported_at:%Y%m%dT%H%M%SZ}.json"
        return JSONResponse(
            ExportDocument.model_validate(
                import_document_payload(document)
            ).model_dump(),
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    async def import_data(document: ImportDocument) -> JSONResponse:
        try:
            outcome = await service.import_document(document.to_domain())
        except BookmarkMutationConflict as error:
            return map_import_outcome(error)
        return map_import_outcome(outcome)

    router.add_api_route(
        "/api/export", export_data, methods=["GET"], response_model=ExportDocument
    )
    router.add_api_route(
        "/api/import",
        import_data,
        methods=["POST"],
        response_model=ImportResponse,
        responses=IMPORT_RESPONSES,
    )
    return router


def import_document_payload(document: domain.PortableDocument) -> dict[str, object]:
    """Map portable records to JSON fields; field order remains the v1 contract."""
    return {
        "version": document.version,
        "exported_at": document.exported_at,
        "groups": [
            {
                "name": group.name,
                "parent": group.parent,
                "position": group.position,
                "nsfw": group.nsfw,
                "domains": list(group.domains),
                "urls": [
                    {
                        "url": record.url,
                        "title": record.title,
                        "created_at": record.created_at,
                        "important": record.important,
                    }
                    for record in group.urls
                ],
            }
            for group in document.groups
        ],
    }
