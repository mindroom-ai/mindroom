"""Sandbox runner API for executing tool calls inside isolated containers."""

from __future__ import annotations

import asyncio
import inspect
import io
import os
import secrets
import subprocess
import sys
import threading
import time
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException
from loguru import logger
from pydantic import BaseModel, Field, ValidationError

import mindroom.sandbox_proxy as _sandbox_proxy
from mindroom.sandbox_proxy import sandbox_proxy_token_matches, to_json_compatible
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

# Sentinel written to stderr to delimit the JSON response from tool output.
_RESPONSE_MARKER = "__SANDBOX_RESPONSE__"


@dataclass
class CredentialLease:
    """In-memory lease for short-lived credential overrides."""

    lease_id: str
    tool_name: str
    function_name: str
    credential_overrides: dict[str, Any]
    expires_at: float
    uses_remaining: int


# NOTE: In-process dict — leases are not shared across multiple uvicorn workers.
# The sandbox runner must be deployed with a single worker for lease correctness.
LEASES_BY_ID: dict[str, CredentialLease] = {}
LEASES_LOCK = threading.Lock()


class SandboxRunnerExecuteRequest(BaseModel):
    """Tool call payload forwarded from a primary runtime to the sandbox runtime.

    Also used internally for in-process and subprocess execution when
    ``credential_overrides`` are resolved from a lease.
    """

    tool_name: str
    function_name: str
    args: list[Any] = Field(default_factory=list)
    kwargs: dict[str, Any] = Field(default_factory=dict)
    lease_id: str | None = None
    credential_overrides: dict[str, Any] = Field(default_factory=dict)


class SandboxRunnerLeaseRequest(BaseModel):
    """Request for creating a short-lived credential lease."""

    tool_name: str
    function_name: str
    credential_overrides: dict[str, Any] = Field(default_factory=dict)
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


async def _validate_runner_token(x_mindroom_sandbox_token: Annotated[str | None, Header()] = None) -> None:
    if _sandbox_proxy._PROXY_TOKEN is None:
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


async def _execute_request_inprocess(request: SandboxRunnerExecuteRequest) -> SandboxRunnerExecuteResponse:
    toolkit, entrypoint = _resolve_entrypoint(
        tool_name=request.tool_name,
        function_name=request.function_name,
        credential_overrides=request.credential_overrides or None,
    )

    try:
        if toolkit.requires_connect:
            connect = getattr(toolkit, "connect", None)
            close = getattr(toolkit, "close", None)
            if connect is not None:
                await _maybe_await(connect())
            try:
                result = await _maybe_await(entrypoint(*request.args, **request.kwargs))
            finally:
                if close is not None:
                    await _maybe_await(close())
        else:
            result = await _maybe_await(entrypoint(*request.args, **request.kwargs))
    except Exception as exc:
        logger.opt(exception=True).warning(
            f"Sandbox tool execution failed: {request.tool_name}.{request.function_name}",
        )
        return SandboxRunnerExecuteResponse(
            ok=False,
            error=f"Sandbox tool execution failed: {type(exc).__name__}: {exc}",
        )

    return SandboxRunnerExecuteResponse(ok=True, result=to_json_compatible(result))


def _subprocess_worker_command() -> list[str]:
    return [sys.executable, "-m", "mindroom.api.sandbox_runner", SUBPROCESS_WORKER_ARG]


def _execute_request_subprocess_sync(request: SandboxRunnerExecuteRequest) -> SandboxRunnerExecuteResponse:
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

    # The worker writes the JSON response to stderr after a marker line so that
    # tool stdout (e.g. print() inside python tools) does not corrupt the protocol.
    stderr = completed.stderr or ""
    marker_pos = stderr.rfind(_RESPONSE_MARKER)
    if marker_pos != -1:
        response_json = stderr[marker_pos + len(_RESPONSE_MARKER) :].strip()
        if response_json:
            try:
                return SandboxRunnerExecuteResponse.model_validate_json(response_json)
            except ValidationError:
                pass

    if completed.returncode != 0:
        error = (
            stderr.strip() or completed.stdout.strip() or f"Sandbox subprocess exited with code {completed.returncode}."
        )
        return SandboxRunnerExecuteResponse(ok=False, error=error)

    return SandboxRunnerExecuteResponse(ok=False, error="Sandbox subprocess returned an invalid response.")


async def _execute_request_subprocess(request: SandboxRunnerExecuteRequest) -> SandboxRunnerExecuteResponse:
    return await asyncio.to_thread(_execute_request_subprocess_sync, request)


def _run_subprocess_worker() -> int:
    payload = sys.stdin.read()
    if not payload.strip():
        print(
            _RESPONSE_MARKER
            + SandboxRunnerExecuteResponse(
                ok=False,
                error="Sandbox subprocess received empty payload.",
            ).model_dump_json(),
            file=sys.stderr,
        )
        return 1

    try:
        request = SandboxRunnerExecuteRequest.model_validate_json(payload)
    except ValidationError as exc:
        print(
            _RESPONSE_MARKER
            + SandboxRunnerExecuteResponse(
                ok=False,
                error=f"Sandbox subprocess payload validation failed: {exc}",
            ).model_dump_json(),
            file=sys.stderr,
        )
        return 1

    # Redirect stdout/stderr during tool execution so tool output doesn't
    # interfere with the protocol marker we write to stderr afterwards.
    captured_out = io.StringIO()
    captured_err = io.StringIO()
    with redirect_stdout(captured_out), redirect_stderr(captured_err):
        response = asyncio.run(_execute_request_inprocess(request))

    # Flush captured tool output to real stdout/stderr (informational only).
    tool_stdout = captured_out.getvalue()
    if tool_stdout:
        sys.stdout.write(tool_stdout)
    tool_stderr = captured_err.getvalue()
    if tool_stderr:
        sys.stdout.write(tool_stderr)

    # Write the response JSON to stderr after the marker.
    print(_RESPONSE_MARKER + response.model_dump_json(), file=sys.stderr)
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

    request.credential_overrides = credential_overrides
    if _runner_uses_subprocess():
        return await _execute_request_subprocess(request)
    return await _execute_request_inprocess(request)


if __name__ == "__main__":
    if SUBPROCESS_WORKER_ARG in sys.argv:
        raise SystemExit(_run_subprocess_worker())
