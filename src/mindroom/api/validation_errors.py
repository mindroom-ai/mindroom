"""Request validation errors answered without the rejected request values.

FastAPI's default 422 body repeats each rejected value under ``input``, which echoes request bodies back
and cannot be rendered when the value is a non-finite number such as ``NaN``, so the client got a 500.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    from fastapi import Request
    from fastapi.exceptions import RequestValidationError


async def validation_error_response(_request: Request, exc: RequestValidationError) -> JSONResponse:
    """Return FastAPI's 422 body with each error's ``input`` left out."""
    errors = [{key: value for key, value in error.items() if key != "input"} for error in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})
