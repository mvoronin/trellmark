from collections.abc import Mapping

from fastapi.responses import JSONResponse


def error_response(
    message: str,
    status: int,
    *,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status, headers=headers)
