import json
from typing import Annotated, Any, TypeVar

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError

PositiveStrictInt = Annotated[StrictInt, Field(ge=1)]
NonNegativeStrictInt = Annotated[StrictInt, Field(ge=0)]
ModelT = TypeVar("ModelT", bound=BaseModel)


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


def _request_validation_error(error: ValidationError) -> RequestValidationError:
    errors: list[dict[str, Any]] = []
    for item in error.errors():
        loc = item.get("loc", ())
        errors.append({**item, "loc": ("body", *loc)})
    return RequestValidationError(errors)


async def json_model(request: Request, model: type[ModelT]) -> ModelT:
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
