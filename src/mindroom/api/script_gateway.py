"""Capability-authenticated HTTP gateway for background-script tool calls."""

from __future__ import annotations

import asyncio
import socket
from contextlib import asynccontextmanager, contextmanager
from typing import TYPE_CHECKING, Annotated, Protocol, cast

import uvicorn
from fastapi import APIRouter, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from mindroom.bounded_bytes import ByteLimitExceededError, collect_bounded_bytes
from mindroom.logging_config import get_logger
from mindroom.script_runs.broker import (
    ScriptBrokerAuthenticationError,
    ScriptCallPreparationPendingError,
    ScriptRuntimeUnavailableError,
    ScriptToolCallRequest,
)
from mindroom.script_runs.models import ScriptCallRecord, ScriptCallState, ScriptToolGrant
from mindroom.script_runs.store import (
    ScriptCallConflictError,
    ScriptCallNotFoundError,
    ScriptCallRateLimitError,
    ScriptCapabilityError,
    ScriptRunNotFoundError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from mindroom.constants import RuntimePaths

__all__ = [
    "ScriptCallReceiptResponse",
    "ScriptToolCallRequestModel",
    "bind_script_tool_broker",
    "get_script_call",
    "router",
    "serve_script_gateway_listener",
    "submit_script_call",
]

logger = get_logger(__name__)

_MAX_REQUEST_BYTES = 64 * 1024
_LISTENER_PORT_ENV = "MINDROOM_SCRIPT_GATEWAY_PORT"


class _ScriptGatewayBroker(Protocol):
    """Broker surface consumed by the primary HTTP gateway."""

    async def accept_authenticated(
        self,
        request: ScriptToolCallRequest,
        authorization: str | None,
    ) -> ScriptCallRecord:
        """Authenticate and durably claim one stable call."""
        ...

    async def get_authenticated(
        self,
        run_id: str,
        call_id: str,
        authorization: str | None,
    ) -> ScriptCallRecord:
        """Authenticate and retrieve one stable receipt."""
        ...


class ScriptToolCallRequestModel(BaseModel):
    """Strict untrusted wire payload for one background tool call."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, max_length=128)
    call_id: str = Field(min_length=1, max_length=128)
    toolkit_name: str = Field(min_length=1, max_length=128)
    function_name: str = Field(min_length=1, max_length=128)
    arguments: dict[str, JsonValue] = Field(default_factory=dict)

    def to_domain(self) -> ScriptToolCallRequest:
        """Build the token-free domain request authenticated only from the header."""
        return ScriptToolCallRequest(
            run_id=self.run_id,
            call_id=self.call_id,
            grant=ScriptToolGrant(self.toolkit_name, self.function_name),
            arguments=cast("dict[str, object]", self.arguments),
        )


class ScriptCallReceiptResponse(BaseModel):
    """Bounded JSON receipt returned to the stdlib SDK."""

    run_id: str
    call_id: str
    toolkit_name: str
    function_name: str
    arguments_digest: str
    state: ScriptCallState
    created_at: str
    result: JsonValue = None
    error: JsonValue = None

    @classmethod
    def from_domain(cls, receipt: ScriptCallRecord) -> ScriptCallReceiptResponse:
        """Translate one canonical broker receipt without adding identity fields."""
        return cls(
            run_id=receipt.run_id,
            call_id=receipt.call_id,
            toolkit_name=receipt.grant.toolkit_name,
            function_name=receipt.grant.function_name,
            arguments_digest=receipt.arguments_digest,
            state=receipt.state,
            created_at=receipt.created_at,
            result=cast("JsonValue", receipt.result),
            error=cast("JsonValue", receipt.error),
        )


router = APIRouter(prefix="/api/script-gateway", tags=["script-gateway"])


def bind_script_tool_broker(
    app: FastAPI,
    broker: _ScriptGatewayBroker | None,
) -> None:
    """Bind the lifecycle-owned broker to one primary API app."""
    app.state.script_tool_broker = broker


def _app_script_tool_broker(app: FastAPI) -> _ScriptGatewayBroker:
    """Return the app-bound broker or fail closed while runtime wiring is unavailable."""
    try:
        broker = app.state.script_tool_broker
    except AttributeError:
        raise HTTPException(status_code=503, detail="Background script gateway is unavailable.") from None
    if broker is None:
        raise HTTPException(status_code=503, detail="Background script gateway is unavailable.")
    return cast("_ScriptGatewayBroker", broker)


async def _bounded_payload(request: Request) -> ScriptToolCallRequestModel:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_bytes = int(content_length)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length header.") from exc
        if declared_bytes > _MAX_REQUEST_BYTES:
            raise HTTPException(status_code=413, detail="Script call request is too large.")

    try:
        body = await collect_bounded_bytes(request.stream(), max_bytes=_MAX_REQUEST_BYTES)
    except ByteLimitExceededError as exc:
        raise HTTPException(status_code=413, detail="Script call request is too large.") from exc
    try:
        return ScriptToolCallRequestModel.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors(include_url=False)) from exc


def _unavailable() -> HTTPException:
    return HTTPException(status_code=404, detail="Background script call is unavailable.")


@router.post("/calls", response_model=ScriptCallReceiptResponse)
async def submit_script_call(
    request: Request,
    response: Response,
    authorization: Annotated[str | None, Header()] = None,
) -> ScriptCallReceiptResponse:
    """Authenticate and accept one stable logical call."""
    payload = await _bounded_payload(request)
    broker = _app_script_tool_broker(request.app)
    try:
        receipt = await broker.accept_authenticated(payload.to_domain(), authorization)
    except (ScriptBrokerAuthenticationError, ScriptCapabilityError, ScriptRunNotFoundError) as exc:
        raise _unavailable() from exc
    except ScriptRuntimeUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ScriptCallConflictError as exc:
        raise HTTPException(status_code=409, detail="Stable call ID conflicts with its accepted request.") from exc
    except ScriptCallRateLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    if receipt.state is ScriptCallState.PENDING:
        response.status_code = 202
    return ScriptCallReceiptResponse.from_domain(receipt)


@router.get("/runs/{run_id}/calls/{call_id}", response_model=ScriptCallReceiptResponse)
async def get_script_call(
    request: Request,
    run_id: str,
    call_id: str,
    authorization: Annotated[str | None, Header()] = None,
) -> ScriptCallReceiptResponse:
    """Authenticate and return the current stable receipt for one logical call."""
    broker = _app_script_tool_broker(request.app)
    try:
        receipt = await broker.get_authenticated(run_id, call_id, authorization)
    except (ScriptBrokerAuthenticationError, ScriptCallNotFoundError, ScriptRunNotFoundError) as exc:
        raise _unavailable() from exc
    except ScriptRuntimeUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ScriptCallPreparationPendingError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return ScriptCallReceiptResponse.from_domain(receipt)


class _GatewayListenerServer(uvicorn.Server):
    """Uvicorn server that leaves process signals to the primary API server it runs beside."""

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        """Install no handlers; the primary server owns shutdown signals."""
        yield


def _listener_port(runtime_paths: RuntimePaths) -> int | None:
    raw_port = (runtime_paths.env_value(_LISTENER_PORT_ENV) or "").strip()
    if not raw_port:
        return None
    try:
        port = int(raw_port)
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        msg = f"{_LISTENER_PORT_ENV} must be a TCP port from 1 to 65535."
        raise ValueError(msg)
    return port


@asynccontextmanager
async def serve_script_gateway_listener(
    runtime_paths: RuntimePaths,
    *,
    host: str,
    broker: _ScriptGatewayBroker | None,
    log_level: str,
) -> AsyncIterator[None]:
    """Serve only the script gateway routes on `MINDROOM_SCRIPT_GATEWAY_PORT` while the primary API runs.

    The listener's app contains nothing but this router, so a network path that
    reaches only this port cannot reach any other primary API route.
    """
    port = _listener_port(runtime_paths)
    if port is None:
        yield
        return
    gateway_app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    gateway_app.include_router(router)
    bind_script_tool_broker(gateway_app, broker)
    server = _GatewayListenerServer(
        uvicorn.Config(gateway_app, lifespan="off", log_level=log_level.lower(), ws="none"),
    )
    listener = socket.create_server((host, port), family=socket.AF_INET6 if ":" in host else socket.AF_INET)
    serve_task = asyncio.create_task(server.serve(sockets=[listener]), name="script_gateway_listener")
    logger.info("script_gateway_listener_started", host=host, port=port)
    try:
        yield
    except BaseException:
        serve_task.cancel()
        await asyncio.wait({serve_task})
        raise
    server.should_exit = True
    await serve_task
