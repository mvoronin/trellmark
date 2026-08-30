from collections.abc import Mapping

from fastapi.responses import JSONResponse


def error_response(
    message: str,
    status: int,
    *,
    code: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    content = {"error": message}
    if code is not None:
        content["code"] = code
    return JSONResponse(content, status_code=status, headers=headers)
