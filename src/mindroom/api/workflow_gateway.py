"""Capability-authenticated routes a workflow attempt's script calls: its run, its nodes, its log and its report."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import asdict
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from mindroom.json_utils import has_only_finite_numbers
from mindroom.script_runs.broker import (
    ScriptBrokerAuthenticationError,
    ScriptRuntimeUnavailableError,
    WorkflowAttemptRevokedError,
)
from mindroom.script_runs.models import ScriptRunRecord
from mindroom.workflow_runs.engine import SupersededAttemptError, WorkflowEngine
from mindroom.workflow_runs.models import NodeRequest
from mindroom.workflow_runs.store import WorkflowRunStoreError

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = [
    "append_log",
    "bind_workflow_engine",
    "finish",
    "node_receipt",
    "node_result",
    "router",
    "run_info",
    "submit_node",
]

_MAX_REQUEST_BYTES = 1024 * 1024
_MAX_REPORT_BYTES = 8 * 1024 * 1024

router = APIRouter(prefix="/api/script-gateway/workflow/{script_run_id}", tags=["script-gateway"])


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _NodeBody(_WireModel):
    key: str = Field(pattern=r"^[^@]*@\d+$", max_length=4096)
    kind: str = Field(min_length=1, max_length=32)
    args: dict[str, JsonValue]
    meta: dict[str, JsonValue] = Field(default_factory=dict)


class _LogBody(_WireModel):
    text: str


class _FinishBody(_WireModel):
    result: JsonValue = None
    error: str | None = None


def bind_workflow_engine(app: FastAPI, engine: WorkflowEngine | None) -> None:
    """Lend one app the lifecycle-owned workflow engine, or withdraw it."""
    app.state.workflow_engine = engine


async def _authenticated(
    request: Request,
    script_run_id: str,
    authorization: Annotated[str | None, Header()] = None,
) -> tuple[WorkflowEngine, ScriptRunRecord]:
    """Return the bound engine and the attempt the bearer capability names; an unknown one reads as no run at all."""
    try:
        engine: WorkflowEngine | None = request.app.state.workflow_engine
    except AttributeError:
        engine = None
    if engine is None:
        raise HTTPException(status_code=503, detail="Workflow gateway is unavailable.")
    try:
        return engine, await asyncio.to_thread(engine.broker.authenticate, script_run_id, authorization)
    except WorkflowAttemptRevokedError as exc:
        # The process proved it is an attempt of ours, and a revoked one is told to exit as a replaced one is.
        raise HTTPException(status_code=409, detail="superseded") from exc
    except ScriptBrokerAuthenticationError as exc:
        raise HTTPException(status_code=404, detail="Workflow run not found.") from exc
    except ScriptRuntimeUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


_Attempt = Annotated[tuple[WorkflowEngine, ScriptRunRecord], Depends(_authenticated)]


@contextmanager
def _refusals() -> Iterator[None]:
    """Answer the engine's refusals with the statuses the SDK acts on: 409 makes a superseded script exit."""
    try:
        yield
    except SupersededAttemptError as exc:
        raise HTTPException(status_code=409, detail="superseded") from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except WorkflowRunStoreError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _read[Body: _WireModel](request: Request, body_type: type[Body], limit: int) -> Body | Response:
    """Parse the request's JSON body, or return the 413 that names its size, measured on the whole body."""
    size, chunks = 0, []
    async for chunk in request.stream():
        size += len(chunk)
        if size <= limit:
            chunks.append(chunk)
    if size > limit:
        return JSONResponse({"bytes": size, "limit": limit}, status_code=413)
    try:
        body = body_type.model_validate_json(b"".join(chunks))
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors(include_url=False, include_input=False)) from exc
    if not has_only_finite_numbers(body.model_dump()):
        raise HTTPException(status_code=422, detail="Numbers must be finite.")
    return body


@router.get("/run")
async def run_info(authenticated: _Attempt) -> dict[str, object]:
    """Return the run's ID, attempt number and arguments."""
    engine, attempt = authenticated
    with _refusals():
        return await engine.run_info(attempt)


@router.post("/nodes")
async def submit_node(request: Request, authenticated: _Attempt) -> Response:
    """Save and dispatch one call of the script, and return its receipt."""
    engine, attempt = authenticated
    body = await _read(request, _NodeBody, _MAX_REQUEST_BYTES)
    if isinstance(body, Response):
        return body
    with _refusals():
        receipt = await engine.submit_node(attempt, NodeRequest(body.key, body.kind, dict(body.args), dict(body.meta)))
    return JSONResponse(asdict(receipt))


@router.get("/nodes/{node_id}")
async def node_receipt(node_id: str, authenticated: _Attempt, wait: float = 0) -> dict[str, object]:
    """Return a node's receipt, held up to `wait` seconds until the node ends."""
    engine, attempt = authenticated
    with _refusals():
        return asdict(await engine.node_receipt(attempt, node_id, wait=min(max(wait, 0.0), 30.0)))


@router.get("/results/{node_id}")
async def node_result(node_id: str, authenticated: _Attempt) -> Response:
    """Return a node's whole result as JSON, from its row or from the file a large one was moved to."""
    engine, attempt = authenticated
    with _refusals():
        node = await engine.node(attempt, node_id)
    if node.result_file is not None:
        return FileResponse(engine.store.directory / node.result_file, media_type="application/json")
    return JSONResponse(node.result)


@router.post("/log")
async def append_log(request: Request, authenticated: _Attempt) -> Response:
    """Add one progress line to the run."""
    engine, attempt = authenticated
    body = await _read(request, _LogBody, _MAX_REQUEST_BYTES)
    if isinstance(body, Response):
        return body
    with _refusals():
        await engine.append_log(attempt, body.text)
    return JSONResponse({})


@router.post("/finish")
async def finish(request: Request, authenticated: _Attempt) -> Response:
    """Store what the script's `main` returned or raised."""
    engine, attempt = authenticated
    body = await _read(request, _FinishBody, _MAX_REPORT_BYTES)
    if isinstance(body, Response):
        return body
    with _refusals():
        await engine.finish(attempt, result=body.result, error=body.error)
    return JSONResponse({})
