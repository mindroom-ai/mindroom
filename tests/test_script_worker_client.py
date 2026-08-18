"""Tests for primary-to-worker background script control requests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

from mindroom.script_runs.worker_client import (
    ScriptWorkerClient,
    ScriptWorkerError,
    WorkerScriptCancel,
    WorkerScriptLaunch,
    WorkerScriptStatus,
)
from mindroom.workers.models import WorkerHandle, worker_api_endpoint

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

_WORKER_TOKEN = "worker-token"  # noqa: S105


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
async def test_script_worker_client_launches_with_worker_auth_and_validates_receipt() -> None:
    """Launch should use the selected worker URL/token and return only a valid handle."""
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["url"] = str(request.url)
        observed["token"] = request.headers.get("x-mindroom-sandbox-token")
        observed["payload"] = request.read().decode()
        return httpx.Response(200, json={"ok": True, "supervisor_handle": "shell:1234abcd"})

    result = await _client(handler).launch(
        _handle(),
        run_id="run-1",
        source_path=".mindroom-script-runs/run-1/source.py",
        source_digest="a" * 64,
        token_path=".mindroom-script-runs/run-1/capability",  # noqa: S106
        gateway_url="http://primary.test/api/script-gateway",
        tail_lines=80,
    )

    assert result == WorkerScriptLaunch(supervisor_handle="shell:1234abcd")
    assert observed["url"] == "http://worker.test/api/sandbox-runner/scripts/run"
    assert observed["token"] == _WORKER_TOKEN
    assert '"worker_key":"v1:test:shared:scripts"' in str(observed["payload"])
    assert '"run_id":"run-1"' in str(observed["payload"])


@pytest.mark.asyncio
async def test_script_worker_client_returns_normalized_status_and_cancel_receipts() -> None:
    """Status and cancellation should preserve normalized worker process facts."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
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

    status = await client.status(handle, run_id="run-1", supervisor_handle="shell:1234abcd")
    cancelled = await client.cancel(handle, run_id="run-1", supervisor_handle="shell:1234abcd")

    assert status == WorkerScriptStatus(state="exited", output="done", exit_code=7)
    assert cancelled == WorkerScriptCancel(cancel_requested=False, already_finished=True, unknown_handle=False)


@pytest.mark.asyncio
async def test_script_worker_client_exposes_unknown_handle_as_status() -> None:
    """A lost supervisor handle is a lifecycle fact rather than a transport exception."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "state": "unknown", "output": ""})

    status = await _client(handler).status(
        _handle(),
        run_id="run-1",
        supervisor_handle="shell:1234abcd",
    )

    assert status == WorkerScriptStatus.unknown_handle()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected_kind"),
    [
        (httpx.Response(400, json={"detail": "invalid source"}), "tool"),
        (httpx.Response(503, json={"detail": "supervisor unavailable"}), "worker"),
        (httpx.Response(200, json={"ok": False, "error": "launch failed", "failure_kind": "worker"}), "worker"),
    ],
)
async def test_script_worker_client_classifies_request_and_worker_failures(
    response: httpx.Response,
    expected_kind: str,
) -> None:
    """Callers must be able to distinguish rejected input from worker failure."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            response.status_code,
            request=request,
            json=response.json(),
        )

    with pytest.raises(ScriptWorkerError) as exc_info:
        await _client(handler).launch(
            _handle(),
            run_id="run-1",
            source_path="source.py",
            source_digest="a" * 64,
            token_path="capability",  # noqa: S106
            gateway_url="http://primary.test/api/script-gateway",
        )

    assert exc_info.value.failure_kind == expected_kind


@pytest.mark.asyncio
async def test_script_worker_client_rejects_missing_worker_token_before_transport() -> None:
    """A remote worker operation without its existing handle token must fail closed."""

    async def handler(_request: httpx.Request) -> httpx.Response:
        pytest.fail("transport must not run without worker authentication")

    with pytest.raises(ScriptWorkerError, match="authentication token") as exc_info:
        await _client(handler).status(
            _handle(token=None),
            run_id="run-1",
            supervisor_handle="shell:1234abcd",
        )

    assert exc_info.value.failure_kind == "worker"


def test_worker_api_endpoint_adds_script_operations_without_changing_existing_urls() -> None:
    """New script paths must leave every existing worker operation stable."""
    handle = _handle()

    assert worker_api_endpoint(handle, "execute") == "http://worker.test/api/sandbox-runner/execute"
    assert worker_api_endpoint(handle, "leases") == "http://worker.test/api/sandbox-runner/leases"
    assert worker_api_endpoint(handle, "save-attachment") == "http://worker.test/api/sandbox-runner/save-attachment"
    assert worker_api_endpoint(handle, "script-run") == "http://worker.test/api/sandbox-runner/scripts/run"
    assert worker_api_endpoint(handle, "script-status") == "http://worker.test/api/sandbox-runner/scripts"
    assert worker_api_endpoint(handle, "script-cancel") == "http://worker.test/api/sandbox-runner/scripts"
