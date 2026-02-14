"""Sandbox runner API for executing tool calls inside isolated containers."""

from __future__ import annotations

import asyncio
import inspect
import os
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException
from loguru import logger
from pydantic import BaseModel, Field, ValidationError

from mindroom.sandbox_proxy import sandbox_proxy_token, sandbox_proxy_token_matches, to_json_compatible
from mindroom.tools_metadata import ensure_tool_registry_loaded, get_tool_by_name

if TYPE_CHECKING:
    from collections.abc import Callable

    from agno.tools.toolkit import Toolkit

MAX_LEASE_TTL_SECONDS = 3600
DEFAULT_LEASE_TTL_SECONDS = 60
DEFAULT_SUBPROCESS_TIMEOUT_SECONDS = 120.0
SUBPROCESS_WORKER_ARG = "--sandbox-subprocess-worker"
RUNNER_EXECUTION_MODE_ENV = "MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE"
RUNNER_SUBPROCESS_TIMEOUT_ENV = "MINDROOM_SANDBOX_RUNNER_SUBPROCESS_TIMEOUT_SECONDS"


@dataclass
class CredentialLease:
    """In-memory lease for short-lived credential overrides."""

    lease_id: str
    tool_name: str
    function_name: str
    credential_overrides: dict[str, object]
    expires_at: float
    uses_remaining: int


LEASES_BY_ID: dict[str, CredentialLease] = {}
LEASES_LOCK = threading.Lock()


class SandboxRunnerExecuteRequest(BaseModel):
    """Tool call payload forwarded from a primary runtime to the sandbox runtime."""

    tool_name: str
    function_name: str
    args: list[Any] = Field(default_factory=list)
    kwargs: dict[str, Any] = Field(default_factory=dict)
    lease_id: str | None = None


class SandboxRunnerLeaseRequest(BaseModel):
    """Request for creating a short-lived credential lease."""

    tool_name: str
    function_name: str
    credential_overrides: dict[str, object] = Field(default_factory=dict)
    ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS
    max_uses: int = 1


class SandboxRunnerLeaseResponse(BaseModel):
    """Response describing a created credential lease."""

    lease_id: str
    expires_at: float
    max_uses: int


class SandboxRunnerExecuteResponse(BaseModel):
    """Sandbox tool execution response."""

    ok: bool
    result: Any | None = None
    error: str | None = None


class SandboxRunnerExecutionRequest(BaseModel):
    """Internal execution request used for in-process and subprocess runners."""

    tool_name: str
    function_name: str
    args: list[Any] = Field(default_factory=list)
    kwargs: dict[str, Any] = Field(default_factory=dict)
    credential_overrides: dict[str, object] = Field(default_factory=dict)


async def _validate_runner_token(x_mindroom_sandbox_token: Annotated[str | None, Header()] = None) -> None:
    if sandbox_proxy_token() is None:
        raise HTTPException(status_code=503, detail="Sandbox runner token is not configured.")
    if not sandbox_proxy_token_matches(x_mindroom_sandbox_token):
        raise HTTPException(status_code=401, detail="Unauthorized sandbox runner request")


router = APIRouter(
    prefix="/api/sandbox-runner",
    tags=["sandbox-runner"],
    dependencies=[Depends(_validate_runner_token)],
)


async def _maybe_await(value: object) -> object:
    if inspect.isawaitable(value):
        return await value
    return value


def _resolve_entrypoint(
    *,
    tool_name: str,
    function_name: str,
    credential_overrides: dict[str, object] | None = None,
) -> tuple[Toolkit, Callable[..., object]]:
    ensure_tool_registry_loaded()
    try:
        toolkit = get_tool_by_name(
            tool_name,
            disable_sandbox_proxy=True,
            credential_overrides=credential_overrides,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    function = toolkit.functions.get(function_name) or toolkit.async_functions.get(function_name)
    if function is None or function.entrypoint is None:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' does not expose '{function_name}'.")
    return toolkit, function.entrypoint


def _bounded_ttl_seconds(raw_ttl_seconds: int) -> int:
    return max(1, min(MAX_LEASE_TTL_SECONDS, raw_ttl_seconds))


def _bounded_max_uses(raw_max_uses: int) -> int:
    return max(1, min(10, raw_max_uses))


def _cleanup_expired_leases(now: float) -> None:
    expired_ids = [lease_id for lease_id, lease in LEASES_BY_ID.items() if lease.expires_at <= now]
    for lease_id in expired_ids:
        LEASES_BY_ID.pop(lease_id, None)


def _create_credential_lease(request: SandboxRunnerLeaseRequest) -> CredentialLease:
    ttl_seconds = _bounded_ttl_seconds(request.ttl_seconds)
    max_uses = _bounded_max_uses(request.max_uses)
    now = time.time()
    expires_at = now + ttl_seconds
    lease = CredentialLease(
        lease_id=secrets.token_urlsafe(24),
        tool_name=request.tool_name,
        function_name=request.function_name,
        credential_overrides=dict(request.credential_overrides),
        expires_at=expires_at,
        uses_remaining=max_uses,
    )
    with LEASES_LOCK:
        _cleanup_expired_leases(now)
        LEASES_BY_ID[lease.lease_id] = lease
    return lease


def _consume_credential_lease(lease_id: str, *, tool_name: str, function_name: str) -> dict[str, object]:
    now = time.time()
    with LEASES_LOCK:
        _cleanup_expired_leases(now)
        lease = LEASES_BY_ID.get(lease_id)
        if lease is None:
            raise HTTPException(status_code=400, detail="Credential lease is invalid or expired.")
        if lease.tool_name != tool_name or lease.function_name != function_name:
            raise HTTPException(status_code=400, detail="Credential lease does not match tool/function.")

        lease.uses_remaining -= 1
        if lease.uses_remaining <= 0:
            LEASES_BY_ID.pop(lease_id, None)
        else:
            LEASES_BY_ID[lease_id] = lease

    return dict(lease.credential_overrides)


def _runner_execution_mode() -> str:
    return os.getenv(RUNNER_EXECUTION_MODE_ENV, "inprocess").strip().lower()


def _runner_uses_subprocess() -> bool:
    return _runner_execution_mode() == "subprocess"


def _runner_subprocess_timeout_seconds() -> float:
    raw_timeout = os.getenv(RUNNER_SUBPROCESS_TIMEOUT_ENV, str(DEFAULT_SUBPROCESS_TIMEOUT_SECONDS))
    try:
        timeout = float(raw_timeout)
    except ValueError:
        timeout = DEFAULT_SUBPROCESS_TIMEOUT_SECONDS
    return max(1.0, timeout)


async def _execute_request_inprocess(request: SandboxRunnerExecutionRequest) -> SandboxRunnerExecuteResponse:
    toolkit, entrypoint = _resolve_entrypoint(
        tool_name=request.tool_name,
        function_name=request.function_name,
        credential_overrides=request.credential_overrides,
    )

    try:
        if toolkit.requires_connect:
            connect_callable: Any = toolkit.connect
            connect_result = connect_callable()
            await _maybe_await(connect_result)
            try:
                result = await _maybe_await(entrypoint(*request.args, **request.kwargs))
            finally:
                close_callable: Any = toolkit.close
                close_result = close_callable()
                await _maybe_await(close_result)
        else:
            result = await _maybe_await(entrypoint(*request.args, **request.kwargs))
    except Exception:
        logger.opt(exception=True).warning(
            f"Sandbox tool execution failed: {request.tool_name}.{request.function_name}",
        )
        return SandboxRunnerExecuteResponse(ok=False, error="Sandbox tool execution failed.")

    return SandboxRunnerExecuteResponse(ok=True, result=to_json_compatible(result))


def _subprocess_worker_command() -> list[str]:
    return [sys.executable, "-m", "mindroom.api.sandbox_runner", SUBPROCESS_WORKER_ARG]


def _execute_request_subprocess_sync(request: SandboxRunnerExecutionRequest) -> SandboxRunnerExecuteResponse:
    try:
        completed = subprocess.run(
            _subprocess_worker_command(),
            input=request.model_dump_json(),
            capture_output=True,
            text=True,
            timeout=_runner_subprocess_timeout_seconds(),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return SandboxRunnerExecuteResponse(ok=False, error="Sandbox subprocess timed out.")
    except OSError as exc:
        return SandboxRunnerExecuteResponse(ok=False, error=f"Failed to start sandbox subprocess: {exc}")

    stdout = completed.stdout.strip()
    if stdout:
        try:
            payload = SandboxRunnerExecuteResponse.model_validate_json(stdout)
        except ValidationError:
            payload = None
        if payload is not None:
            return payload

    if completed.returncode != 0:
        error = completed.stderr.strip() or stdout or f"Sandbox subprocess exited with code {completed.returncode}."
        return SandboxRunnerExecuteResponse(ok=False, error=error)

    return SandboxRunnerExecuteResponse(ok=False, error="Sandbox subprocess returned an invalid response.")


async def _execute_request_subprocess(request: SandboxRunnerExecutionRequest) -> SandboxRunnerExecuteResponse:
    return await asyncio.to_thread(_execute_request_subprocess_sync, request)


def _run_subprocess_worker() -> int:
    payload = sys.stdin.read()
    if not payload.strip():
        print(
            SandboxRunnerExecuteResponse(
                ok=False,
                error="Sandbox subprocess received empty payload.",
            ).model_dump_json(),
        )
        return 1

    try:
        request = SandboxRunnerExecutionRequest.model_validate_json(payload)
    except ValidationError as exc:
        print(
            SandboxRunnerExecuteResponse(
                ok=False,
                error=f"Sandbox subprocess payload validation failed: {exc}",
            ).model_dump_json(),
        )
        return 1

    response = asyncio.run(_execute_request_inprocess(request))
    print(response.model_dump_json())
    return 0


@router.post("/leases", response_model=SandboxRunnerLeaseResponse)
async def create_credential_lease(
    request: SandboxRunnerLeaseRequest,
) -> SandboxRunnerLeaseResponse:
    """Create a short-lived, one-or-few-use credential lease."""
    lease = _create_credential_lease(request)
    return SandboxRunnerLeaseResponse(
        lease_id=lease.lease_id,
        expires_at=lease.expires_at,
        max_uses=lease.uses_remaining,
    )


@router.post("/execute", response_model=SandboxRunnerExecuteResponse)
async def execute_tool_call(
    request: SandboxRunnerExecuteRequest,
) -> SandboxRunnerExecuteResponse:
    """Execute a tool function locally and return the serialized result."""
    credential_overrides: dict[str, object] = {}
    if request.lease_id is not None:
        credential_overrides = _consume_credential_lease(
            request.lease_id,
            tool_name=request.tool_name,
            function_name=request.function_name,
        )

    execution_request = SandboxRunnerExecutionRequest(
        tool_name=request.tool_name,
        function_name=request.function_name,
        args=request.args,
        kwargs=request.kwargs,
        credential_overrides=credential_overrides,
    )
    if _runner_uses_subprocess():
        return await _execute_request_subprocess(execution_request)
    return await _execute_request_inprocess(execution_request)


if __name__ == "__main__":
    if SUBPROCESS_WORKER_ARG in sys.argv:
        raise SystemExit(_run_subprocess_worker())
