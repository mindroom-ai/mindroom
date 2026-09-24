"""Thin capability-authenticated transport for response-owned CLI operations."""

from __future__ import annotations

__all__ = ["bind_agent_cli_registry", "get_call", "router", "submit_operation"]

import time
from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import APIRouter, FastAPI, HTTPException, Request
from pydantic import ValidationError
from starlette.responses import Response

from mindroom.agent_cli.json_io import MAX_ENVELOPE_BYTES, canonical_json, read_json
from mindroom.agent_cli.protocol import parse_operation
from mindroom.agent_cli.session import (
    CliAuthenticationError,
    CliBashWindowRequiredError,
    CliCallConflictError,
    CliOperationError,
)
from mindroom.api import config_lifecycle

if TYPE_CHECKING:
    from mindroom.agent_cli.session import CliOperationOwner, TurnToolRegistry

router = APIRouter(prefix="/api/agent-cli", tags=["agent-cli"])


def bind_agent_cli_registry(app: FastAPI, registry: TurnToolRegistry | None) -> None:
    """Expose the orchestrator's registry without taking ownership of its lifetime."""
    state = config_lifecycle.ensure_app_state(app)
    state.agent_cli_registry = registry


def _owner(request: Request) -> CliOperationOwner:
    registry = config_lifecycle.ensure_app_state(request.app).agent_cli_registry
    try:
        if registry is not None:
            return registry.resolve(request.headers.get("authorization"), now_ns=time.time_ns())
    except CliAuthenticationError:
        pass
    raise HTTPException(status_code=401, detail="Agent CLI authority is unavailable")


def _response(payload: dict[str, object]) -> Response:
    return Response(canonical_json(payload), media_type="application/json")


@router.post("/operations")
async def submit_operation(request: Request) -> Response:
    """Authenticate before streaming/parsing any untrusted discriminated body."""
    owner = _owner(request)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_ENVELOPE_BYTES:
            raise HTTPException(status_code=413, detail="Agent CLI request exceeds 64 KiB")
        body.extend(chunk)
    try:
        operation = parse_operation(read_json(bytes(body)))
        return _response(await owner.operation(operation))
    except CliAuthenticationError:
        raise HTTPException(status_code=401, detail="Agent CLI authority is unavailable") from None
    except (CliCallConflictError, CliBashWindowRequiredError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except CliOperationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except (ValidationError, ValueError):
        raise HTTPException(status_code=422, detail="Invalid Agent CLI operation") from None


@router.get("/calls/{call_id}")
async def get_call(request: Request, call_id: str) -> Response:
    """Hide absent and other-owner receipts behind the identical auth response."""
    owner = _owner(request)
    try:
        return _response(await owner.get_call(str(UUID(call_id))))
    except (CliAuthenticationError, ValueError):
        raise HTTPException(status_code=401, detail="Agent CLI authority is unavailable") from None
