"""Tests for primary-to-worker background script control requests."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx
import pytest

from mindroom.script_runs.wire import (
    SCRIPT_PROTOCOL_VERSION,
    ScriptWorkerError,
    WorkerScriptCancel,
    WorkerScriptStatus,
)
from mindroom.script_runs.worker_client import ScriptWorkerClient
from mindroom.workers.models import WorkerHandle, worker_api_endpoint

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

_WORKER_TOKEN = "worker-token"  # noqa: S105
_SUPERVISOR_HANDLE = f"shell:{'a' * 32}"


def _handle(*, token: str | None = _WORKER_TOKEN) -> WorkerHandle:
    return WorkerHandle(
        worker_id="worker-1",
        worker_key="v1:test:shared:scripts",
        endpoint="http://worker.test/api/sandbox-runner/execute",
        auth_token=token,
        status="ready",
        backend_name="test",
        last_used_at=1.0,
        created_at=1.0,
        debug_metadata={"api_root": "http://worker.test/api/sandbox-runner"},
    )


def _client(
    handler: Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]],
) -> ScriptWorkerClient:
    return ScriptWorkerClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_script_worker_client_sends_a_narrow_derived_launch_request() -> None:
    """The worker receives only immutable launch inputs, never primary path or handle choices."""
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["url"] = str(request.url)
        observed["token"] = request.headers.get("x-mindroom-sandbox-token")
        observed["payload"] = request.read().decode()
        return httpx.Response(200, json={"ok": True})

    result = await _client(handler).launch(
        _handle(),
        run_id=f"script-{'a' * 32}",
        source_digest="a" * 64,
        gateway_url="http://primary.test/api/script-gateway",
        max_runtime_seconds=3600,
        state_scope_worker_key="v1:test:user_agent:alice:private-agent",
        private_agent_names=("private-agent",),
        config_snapshot={"agents": {"private-agent": {"display_name": "Private"}}},
    )

    assert result is None
    assert observed["url"] == "http://worker.test/api/sandbox-runner/scripts/run"
    assert observed["token"] == _WORKER_TOKEN
    assert httpx.Response(200, content=str(observed["payload"])).json() == {
        "run_id": f"script-{'a' * 32}",
        "worker_key": "v1:test:shared:scripts",
        "state_scope_worker_key": "v1:test:user_agent:alice:private-agent",
        "source_digest": "a" * 64,
        "gateway_url": "http://primary.test/api/script-gateway",
        "max_runtime_seconds": 3600,
        "private_agent_names": ["private-agent"],
        "config_snapshot": {"agents": {"private-agent": {"display_name": "Private"}}},
        "protocol_version": SCRIPT_PROTOCOL_VERSION,
    }


@pytest.mark.asyncio
async def test_script_worker_client_returns_normalized_status_and_cancel_receipts() -> None:
    """Status and cancellation should preserve normalized worker process facts."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/status"):
            return httpx.Response(
                200,
                json={"ok": True, "state": "exited", "output": "done", "exit_code": 7},
            )
        return httpx.Response(
            200,
            json={
                "ok": True,
                "cancel_requested": False,
                "already_finished": True,
                "unknown_handle": False,
            },
        )

    client = _client(handler)
    handle = _handle()

    status = await client.status(handle, run_id="script-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    cancelled = await client.cancel(handle, run_id="script-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")

    assert status == WorkerScriptStatus(state="exited", output="done", exit_code=7)
    assert cancelled == WorkerScriptCancel(cancel_requested=False, already_finished=True, unknown_handle=False)
    # Access logs record request URLs, so the worker key, which names the requester, travels only in the body.
    assert all(request.url.query == b"" for request in requests)
    assert all(json.loads(request.content)["worker_key"] == handle.worker_key for request in requests)


@pytest.mark.asyncio
async def test_script_worker_client_exposes_unknown_handle_as_status() -> None:
    """A lost supervisor handle is a lifecycle fact rather than a transport exception."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "state": "unknown", "output": ""})

    status = await _client(handler).status(
        _handle(),
        run_id="script-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    )

    assert status == WorkerScriptStatus.unknown_handle()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_state", [[], {}])
async def test_script_worker_client_rejects_unhashable_status_state(invalid_state: object) -> None:
    """Malformed authenticated status payloads must produce stable worker errors."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "state": invalid_state, "output": ""})

    with pytest.raises(ScriptWorkerError, match="Invalid script worker message: state"):
        await _client(handler).status(
            _handle(),
            run_id="script-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "payload", "field"),
    [
        ("status", {"ok": True, "state": "exited", "exit_code": True}, "exit_code"),
        ("status", {"ok": True, "state": "exited", "exit_code": 1.0}, "exit_code"),
        ("status", {"ok": 1, "state": "running"}, "ok"),
        ("cancel", {"ok": True, "cancel_requested": "true"}, "cancel_requested"),
        ("cancel", {"ok": True, "cancel_requested": False, "already_finished": 1}, "already_finished"),
        ("cancel", {"ok": True, "cancel_requested": False, "unknown_handle": None}, "unknown_handle"),
    ],
)
async def test_script_worker_client_accepts_only_exact_receipt_types(
    operation: str,
    payload: dict[str, object],
    field: str,
) -> None:
    """Exit codes, cancellation flags, and success are taken only as exact JSON integers and booleans."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    request = getattr(_client(handler), operation)
    with pytest.raises(ScriptWorkerError, match=f"Invalid script worker message: {field}"):
        await request(_handle(), run_id="script-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")


@pytest.mark.asyncio
async def test_script_worker_client_ignores_fields_a_newer_worker_adds() -> None:
    """A receipt from a newer worker still parses when it carries fields this primary does not know."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": True, "state": "exited", "exit_code": 0, "failure_kind": None, "started_at": "later"},
        )

    status = await _client(handler).status(_handle(), run_id="script-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")

    assert status == WorkerScriptStatus(state="exited", exit_code=0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(400, json={"detail": "invalid source"}), "invalid source"),
        (httpx.Response(503, json={"detail": "supervisor unavailable"}), "supervisor unavailable"),
        (httpx.Response(200, json={"ok": False, "error": "launch failed"}), "launch failed"),
        (httpx.Response(200, json={"ok": False}), "Worker script operation failed."),
        (httpx.Response(200, text="not json"), "Worker returned a non-JSON script response."),
    ],
)
async def test_script_worker_client_reports_each_failure_as_one_error(response: httpx.Response, message: str) -> None:
    """A refused request, an unavailable worker, and a failed operation all surface as the worker's message."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(response.status_code, request=request, content=response.content)

    with pytest.raises(ScriptWorkerError) as exc_info:
        await _client(handler).launch(
            _handle(),
            run_id="script-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            source_digest="a" * 64,
            gateway_url="http://primary.test/api/script-gateway",
            max_runtime_seconds=3600,
        )

    assert str(exc_info.value) == message


@pytest.mark.asyncio
async def test_script_worker_client_rejects_an_invalid_request_before_transport() -> None:
    """A launch the worker would refuse fails in the primary without echoing the rejected value."""

    async def handler(_request: httpx.Request) -> httpx.Response:
        pytest.fail("an invalid request must not reach the worker")

    with pytest.raises(ScriptWorkerError) as exc_info:
        await _client(handler).launch(
            _handle(),
            run_id="script-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            source_digest="a" * 64,
            gateway_url="http://primary.test/api/script-gateway",
            max_runtime_seconds=3600,
            private_agent_names=tuple(f"private-sentinel-{index}" for index in range(129)),
        )

    assert str(exc_info.value).startswith("Invalid script worker message: private_agent_names:")
    assert "private-sentinel" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


@pytest.mark.asyncio
async def test_script_worker_client_never_relays_rejected_request_values() -> None:
    """A worker validation error names the rejected field but never echoes the launch body back to the model."""
    snapshot = {"agents": {"watcher": {"display_name": "snapshot-sentinel"}}}

    def handler(request: httpx.Request) -> httpx.Response:
        # FastAPI's default 422 for a field an older worker does not know repeats the field's value.
        return httpx.Response(
            422,
            request=request,
            json={
                "detail": [
                    {
                        "type": "extra_forbidden",
                        "loc": ["body", "config_snapshot"],
                        "msg": "Extra inputs are not permitted",
                        "input": snapshot,
                    },
                ],
            },
        )

    with pytest.raises(ScriptWorkerError) as exc_info:
        await _client(handler).launch(
            _handle(),
            run_id="script-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            source_digest="a" * 64,
            gateway_url="http://primary.test/api/script-gateway",
            max_runtime_seconds=3600,
            config_snapshot=snapshot,
        )

    assert "snapshot-sentinel" not in str(exc_info.value)
    assert "body.config_snapshot: Extra inputs are not permitted" in str(exc_info.value)


@pytest.mark.asyncio
async def test_script_worker_client_bounds_plain_text_worker_errors() -> None:
    """A non-JSON error body is shortened instead of relayed whole."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, request=request, text="upstream failure " * 1000)

    with pytest.raises(ScriptWorkerError) as exc_info:
        await _client(handler).status(_handle(), run_id="script-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")

    assert str(exc_info.value).startswith("upstream failure")
    assert len(str(exc_info.value)) < 1000


@pytest.mark.asyncio
async def test_script_worker_client_rejects_missing_worker_token_before_transport() -> None:
    """A remote worker operation without its existing handle token must fail closed."""

    async def handler(_request: httpx.Request) -> httpx.Response:
        pytest.fail("transport must not run without worker authentication")

    with pytest.raises(ScriptWorkerError, match="authentication token"):
        await _client(handler).status(
            _handle(token=None),
            run_id="script-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )


def test_worker_api_endpoint_adds_script_operations_without_changing_existing_urls() -> None:
    """New script paths must leave every existing worker operation stable."""
    handle = _handle()

    assert worker_api_endpoint(handle, "execute") == "http://worker.test/api/sandbox-runner/execute"
    assert worker_api_endpoint(handle, "leases") == "http://worker.test/api/sandbox-runner/leases"
    assert worker_api_endpoint(handle, "save-attachment") == "http://worker.test/api/sandbox-runner/save-attachment"
    assert worker_api_endpoint(handle, "script-run") == "http://worker.test/api/sandbox-runner/scripts/run"
    assert worker_api_endpoint(handle, "script-status") == "http://worker.test/api/sandbox-runner/scripts"
    assert worker_api_endpoint(handle, "execute-cancel") == "http://worker.test/api/sandbox-runner/execute/cancel"
    assert worker_api_endpoint(handle, "script-cancel") == "http://worker.test/api/sandbox-runner/scripts"
