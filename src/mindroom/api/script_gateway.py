"""Capability-authenticated HTTP gateway for background-script tool calls."""

from __future__ import annotations

from typing import Annotated, Protocol, cast

from fastapi import APIRouter, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from mindroom.script_runs.broker import (
    ScriptBrokerAuthenticationError,
    ScriptCallPreparationPendingError,
    ScriptToolCallRequest,
)
from mindroom.script_runs.models import ScriptCallRecord, ScriptCallState, ScriptToolGrant
from mindroom.script_runs.store import (
    ScriptCallConflictError,
    ScriptCallNotFoundError,
    ScriptCallRateLimitError,
    ScriptCapabilityError,
)

_MAX_REQUEST_BYTES = 64 * 1024


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

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > _MAX_REQUEST_BYTES:
            raise HTTPException(status_code=413, detail="Script call request is too large.")
    try:
        return ScriptToolCallRequestModel.model_validate_json(bytes(body))
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
    except (ScriptBrokerAuthenticationError, ScriptCapabilityError) as exc:
        raise _unavailable() from exc
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
    except (ScriptBrokerAuthenticationError, ScriptCallNotFoundError) as exc:
        raise _unavailable() from exc
    except ScriptCallPreparationPendingError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return ScriptCallReceiptResponse.from_domain(receipt)


_PUBLIC_ENTRYPOINTS = (bind_script_tool_broker, submit_script_call, get_script_call)
