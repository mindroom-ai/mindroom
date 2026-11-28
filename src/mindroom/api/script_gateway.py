"""Capability-authenticated HTTP gateway for background-script tool calls."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Annotated, Protocol, cast

from fastapi import APIRouter, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, field_validator
from pydantic_core import PydanticCustomError

from mindroom.api.agent_cli import bind_agent_cli_registry
from mindroom.api.agent_cli import router as agent_cli_router
from mindroom.api.listener import SCRIPT_GATEWAY_PORT_ENV, listener_port, serve_listener
from mindroom.api.workflow_gateway import bind_workflow_engine
from mindroom.api.workflow_gateway import router as workflow_gateway_router
from mindroom.bounded_bytes import ByteLimitExceededError, collect_bounded_bytes
from mindroom.json_utils import has_only_finite_numbers
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
    from collections.abc import AsyncIterator

    from mindroom.agent_cli.session import TurnToolRegistry
    from mindroom.constants import RuntimePaths
    from mindroom.workflow_runs.engine import WorkflowEngine

__all__ = [
    "ScriptCallReceiptResponse",
    "ScriptCheckInputResponse",
    "ScriptCheckResultRequest",
    "ScriptToolCallRequestModel",
    "bind_script_tool_broker",
    "get_script_call",
    "get_script_check_input",
    "router",
    "serve_script_gateway_listener",
    "submit_script_call",
    "submit_script_check_result",
]

_MAX_REQUEST_BYTES = 64 * 1024
_MAX_CHECK_RESULT_BYTES = 128 * 1024


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

    async def get_authenticated_check_input(
        self,
        run_id: str,
        authorization: str | None,
    ) -> dict[str, object]:
        """Authenticate a script_check tick and return its source and initial state."""
        ...

    async def submit_authenticated_check_result(
        self,
        run_id: str,
        authorization: str | None,
        *,
        result: object,
        state: dict[str, object],
        state_changed: bool,
        error: str | None,
    ) -> None:
        """Authenticate a script_check tick and record its final check result."""
        ...


class ScriptToolCallRequestModel(BaseModel):
    """Strict untrusted wire payload for one background tool call."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, max_length=128)
    call_id: str = Field(min_length=1, max_length=128)
    toolkit_name: str = Field(min_length=1, max_length=128)
    function_name: str = Field(min_length=1, max_length=128)
    arguments: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("arguments")
    @classmethod
    def _reject_non_finite_numbers(cls, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        # Pydantic's JSON parser accepts NaN, Infinity, and overflowing numbers such as 1e400,
        # which the broker's strict-JSON call digest cannot encode.
        if not has_only_finite_numbers(arguments):
            # A custom error, unlike a ValueError, carries no exception object that the 422 body cannot render.
            raise PydanticCustomError("finite_number", "Arguments must contain only finite numbers")  # noqa: EM101
        return arguments

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


class ScriptCheckInputResponse(BaseModel):
    """Snapshotted source and persisted state delivered to one script_check tick."""

    source: str
    source_digest: str
    state: dict[str, JsonValue] = Field(default_factory=dict)


class ScriptCheckResultRequest(BaseModel):
    """Strict untrusted wire payload posted by the script_check bootstrap upon completion."""

    model_config = ConfigDict(extra="forbid")

    result: JsonValue = None
    state: dict[str, JsonValue] = Field(default_factory=dict)
    state_changed: bool = False
    error: str | None = Field(default=None, max_length=8192)

    @field_validator("result", "state")
    @classmethod
    def _reject_non_finite_numbers(cls, value: JsonValue) -> JsonValue:
        if not has_only_finite_numbers(value):
            raise PydanticCustomError("finite_number", "Payload must contain only finite numbers")  # noqa: EM101
        return value


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


async def _bounded_json_body(request: Request, *, max_bytes: int, too_large_detail: str) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_bytes = int(content_length)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length header.") from exc
        if declared_bytes > max_bytes:
            raise HTTPException(status_code=413, detail=too_large_detail)

    try:
        return await collect_bounded_bytes(request.stream(), max_bytes=max_bytes)
    except ByteLimitExceededError as exc:
        raise HTTPException(status_code=413, detail=too_large_detail) from exc


async def _bounded_payload(request: Request) -> ScriptToolCallRequestModel:
    body = await _bounded_json_body(
        request,
        max_bytes=_MAX_REQUEST_BYTES,
        too_large_detail="Script call request is too large.",
    )
    try:
        return ScriptToolCallRequestModel.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors(include_url=False, include_input=False)) from exc


async def _bounded_check_result(request: Request) -> ScriptCheckResultRequest:
    body = await _bounded_json_body(
        request,
        max_bytes=_MAX_CHECK_RESULT_BYTES,
        too_large_detail="Script check result payload is too large.",
    )
    try:
        return ScriptCheckResultRequest.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors(include_url=False, include_input=False)) from exc


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


@router.get("/runs/{run_id}/check-input", response_model=ScriptCheckInputResponse)
async def get_script_check_input(
    request: Request,
    run_id: str,
    authorization: Annotated[str | None, Header()] = None,
) -> ScriptCheckInputResponse:
    """Authenticate a script_check tick and return its snapshotted source and persisted state."""
    broker = _app_script_tool_broker(request.app)
    try:
        payload = await broker.get_authenticated_check_input(run_id, authorization)
    except (ScriptBrokerAuthenticationError, ScriptCapabilityError, ScriptRunNotFoundError) as exc:
        raise _unavailable() from exc
    except ScriptRuntimeUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return ScriptCheckInputResponse.model_validate(payload)


@router.post("/runs/{run_id}/check-result")
async def submit_script_check_result(
    request: Request,
    run_id: str,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, bool]:
    """Authenticate a script_check tick and record its final check result."""
    payload = await _bounded_check_result(request)
    broker = _app_script_tool_broker(request.app)
    try:
        await broker.submit_authenticated_check_result(
            run_id,
            authorization,
            result=payload.result,
            state=cast("dict[str, object]", payload.state),
            state_changed=payload.state_changed,
            error=payload.error,
        )
    except (ScriptBrokerAuthenticationError, ScriptCapabilityError, ScriptRunNotFoundError) as exc:
        raise _unavailable() from exc
    except ScriptCallConflictError as exc:
        raise HTTPException(status_code=409, detail="Conflicting script check result submission.") from exc
    except ScriptRuntimeUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"ok": True}


@asynccontextmanager
async def serve_script_gateway_listener(
    runtime_paths: RuntimePaths,
    *,
    host: str,
    broker: _ScriptGatewayBroker | None,
    log_level: str,
    agent_cli_registry: TurnToolRegistry | None,
    workflow_engine: WorkflowEngine | None,
) -> AsyncIterator[None]:
    """Serve only worker-facing capability routes on `MINDROOM_SCRIPT_GATEWAY_PORT` while the primary API runs.

    The listener's app contains nothing but the script gateway, workflow gateway and Agent CLI
    routers, all authenticated by short-lived capabilities, so a network path
    that reaches only this port cannot reach any other primary API route.
    """
    port = listener_port(runtime_paths, SCRIPT_GATEWAY_PORT_ENV)
    if port is None:
        yield
        return
    gateway_app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    gateway_app.include_router(router)
    gateway_app.include_router(workflow_gateway_router)
    gateway_app.include_router(agent_cli_router)
    bind_script_tool_broker(gateway_app, broker)
    bind_workflow_engine(gateway_app, workflow_engine)
    bind_agent_cli_registry(gateway_app, agent_cli_registry)
    async with serve_listener(
        gateway_app,
        name="script_gateway_listener",
        host=host,
        port=port,
        log_level=log_level,
        ws="none",
        access_log=True,
    ):
        yield
