"""Tests for generic sandbox proxy tool wrapping."""

from __future__ import annotations

import ast
import asyncio
import base64
import contextlib
import contextvars
import functools
import gc
import hashlib
import inspect
import json
import os
import re
import signal
import stat
import sys
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Self
from unittest.mock import patch

import httpx
import pytest
from agno.tools import Toolkit
from agno.tools.function import Function
from fastapi.testclient import TestClient
from PIL import Image as PILImage

import mindroom.api.sandbox_exec as sandbox_exec_module
import mindroom.api.sandbox_runner as sandbox_runner_module
import mindroom.tool_system.sandbox_proxy as sandbox_proxy_module
import mindroom.tools  # noqa: F401
import mindroom.tools.shell as shell_tool_module
from mindroom.agent_cli.shell_contract import AgentCliShellEnv, bound_agent_cli_shell_env
from mindroom.api.sandbox_runner_app import app as sandbox_runner_app
from mindroom.browser.call_limit import browser_request_budget_seconds
from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config, load_config
from mindroom.constants import (
    RETAINED_MEDIA_MAX_BYTES,
    RuntimePaths,
    build_execution_tool_env,
    isolated_runtime_paths,
    resolve_runtime_paths,
    shell_execution_runtime_env_values,
    shell_extra_env_values,
    subprocess_path_with_prepends,
)
from mindroom.credentials.manager import CredentialsManager, get_runtime_credentials_manager, save_scoped_credentials
from mindroom.hooks import HookRegistry
from mindroom.llm.media import Image
from mindroom.message_target import MessageTarget
from mindroom.runtime_env_policy import VENDOR_TELEMETRY_ENV_VALUES
from mindroom.tool_system.agent_tools import resolve_agent_tools
from mindroom.tool_system.agno_toolkits import (
    adapt_toolkit,
    configure_toolkit_functions,
    toolkit_function,
)
from mindroom.tool_system.authoring import Toolkit as OwnedToolkit
from mindroom.tool_system.authoring import tool, toolkit_functions
from mindroom.tool_system.declarations import (
    SupportsPrimaryCallPlacement,
    ToolCategory,
    ToolExecutionTarget,
    ToolFileAccess,
)
from mindroom.tool_system.dedicated_threads import JOB_CALL_EXECUTOR
from mindroom.tool_system.invocation import ToolCallContext, execute_tool_call
from mindroom.tool_system.media_transport import encode_media_result
from mindroom.tool_system.metadata import (
    ConfigField,
    ToolInitOverrideError,
    ToolMetadata,
    ToolValidationInfo,
    get_tool_by_name,
)
from mindroom.tool_system.output_files import OUTPUT_PATH_ARGUMENT, ToolOutputFilePolicy
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tool_system.runtime_context import (
    tool_runtime_context,
    worker_progress_pump_scope,
)
from mindroom.tool_system.tool_environment import ToolEnvironment
from mindroom.tool_system.tool_function import ToolValue
from mindroom.tool_system.tool_hooks import (
    SyncToolCompletionTracker,
    owned_sync_tool_completion,
    run_sync_tool_entrypoint,
    track_sync_tool_completion,
)
from mindroom.tool_system.worker_proxy_client import (
    WorkerProxyClientConfig,
    _WorkerHttpError,
    execute_worker_proxy_request,
)
from mindroom.tool_system.worker_routing import (
    ResolvedWorkerTarget,
    ToolExecutionIdentity,
    agent_workspace_root_path,
    resolve_worker_key,
    resolve_worker_target,
    serialize_tool_execution_identity,
)
from mindroom.workers import runtime as workers_runtime_module
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.backend_kinds import WorkerBackendKind
from mindroom.workers.backends import static_runner as static_runner_module
from mindroom.workers.backends.local import local_worker_state_paths_for_root
from mindroom.workers.backends.static_runner import StaticSandboxRunnerBackend
from mindroom.workers.compatibility import worker_health_payload
from mindroom.workers.models import ConfiguredWorkerBackend, WorkerHandle, WorkerReadyProgress, WorkerSpec
from mindroom.workers.protocol import (
    WORKER_PROTOCOL_VERSION,
    ExecuteRequest,
    LeaseRequest,
    SaveAttachmentRequest,
    SaveAttachmentResponse,
    WorkerFailure,
    WorkerRoute,
)
from tests.agent_tool_helpers import blocking_entrypoint, detached_policy
from tests.api.sandbox_runner_support import execute_call
from tests.authorization_helpers import (
    make_test_tool_runtime_context,
)
from tests.conftest import (
    FakeCredentialsManager,
    call_proxy_sync_for_test,
    make_conversation_reader_mock,
    make_relation_lookup,
    register_test_tool,
    replace_test_tool_factory,
    requires_linux,
)
from tests.process_helpers import assert_linux_pid_not_running
from tests.test_agent_tool_calls import _catalog, _events
from tests.test_attachments_tool import _tool_context

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterator, Mapping

    from mindroom.tool_system.agno_toolkits import AnyToolkit
    from mindroom.workers.backend import WorkerBackend
    from mindroom.workers.models import WorkerLaunchConfig

_TEST_AUTH_TOKEN = "test-token"  # noqa: S105
# Set for each test by ``_env_runtime_storage_in_tmp_path``.
_ENV_RUNTIME_STORAGE_ROOT: Path | None = None


def _offered_entrypoint(tool: AnyToolkit, name: str) -> Callable[..., Any]:
    """Return the entrypoint an agent's run calls for ``name``: sent to the worker when routed, else validated here."""
    function = toolkit_function(tool, name)
    assert function is not None
    return function.entrypoint


@pytest.fixture(autouse=True)
def _env_runtime_storage_in_tmp_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Store what ``_runtime_paths_from_env`` runtimes write under the test's own directory, never the checkout."""
    monkeypatch.setattr(f"{__name__}._ENV_RUNTIME_STORAGE_ROOT", tmp_path / "mindroom_data")


@pytest.fixture(autouse=True)
def _isolate_primary_worker_manager_runtime() -> Iterator[None]:
    """Reopen the process-global worker runtime around final-shutdown tests."""
    workers_runtime_module.reset_primary_worker_manager()
    try:
        yield
    finally:
        workers_runtime_module.reset_primary_worker_manager()


@pytest.mark.asyncio
async def test_worker_proxy_executor_isolated_from_default_pool_and_preserves_context() -> None:
    """Worker proxy calls must not queue behind unrelated default-pool work."""
    loop = asyncio.get_running_loop()
    default_executor = ThreadPoolExecutor(max_workers=1)
    replacement_executor = ThreadPoolExecutor()
    loop.set_default_executor(default_executor)
    default_started = threading.Event()
    release_default = threading.Event()
    request_scope = ContextVar[str]("test_worker_proxy_request_scope")

    def block_default_executor() -> None:
        default_started.set()
        release_default.wait(timeout=5)

    default_blocker = loop.run_in_executor(None, block_default_executor)
    assert default_started.wait(timeout=1)
    token = request_scope.set("request-scope")

    try:
        result = await asyncio.wait_for(
            sandbox_proxy_module._run_in_worker_proxy_executor(request_scope.get),
            timeout=1,
        )

        assert result == "request-scope"
        assert not default_blocker.done()
    finally:
        request_scope.reset(token)
        release_default.set()
        await default_blocker
        loop.set_default_executor(replacement_executor)
        default_executor.shutdown(wait=True)
        replacement_executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_long_worker_proxy_calls_do_not_delay_another_call() -> None:
    """Shell calls waiting on long commands must not make other worker proxy calls queue."""
    release = threading.Event()
    # As many as the largest default-sized thread pool holds.
    long_calls = [
        asyncio.ensure_future(sandbox_proxy_module._run_in_worker_proxy_executor(functools.partial(release.wait, 5)))
        for _ in range(32)
    ]
    await asyncio.sleep(0)

    try:
        result = await asyncio.wait_for(sandbox_proxy_module._run_in_worker_proxy_executor(lambda: "done"), timeout=1)

        assert result == "done"
    finally:
        release.set()
        await asyncio.gather(*long_calls)


def test_approved_egress_tool_stays_primary_even_when_sandbox_mode_all(tmp_path: Path) -> None:
    """Policy API grants must be requested from the primary runtime, not a worker."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        process_env={
            "MINDROOM_WORKER_BACKEND": "kubernetes",
            "MINDROOM_SANDBOX_EXECUTION_MODE": "all",
            "MINDROOM_SANDBOX_PROXY_TOKEN": "test-token",
        },
    )

    assert not _runs_in_worker(runtime_paths, "approved_egress", None)


def test_github_oauth_tool_stays_primary_when_worker_routing_is_requested(tmp_path: Path) -> None:
    """Requester OAuth credentials must remain in the primary runtime credential boundary."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        process_env={},
    )

    assert not _runs_in_worker(runtime_paths, "github", ["github"])


def test_attachment_save_protocol_decode_round_trip() -> None:
    """A save-attachment payload decodes back to the bytes it carries."""
    payload_bytes = b"attachment-bytes"
    assert (
        sandbox_proxy_module.decode_attachment_save_bytes(
            bytes_b64=base64.b64encode(payload_bytes).decode("ascii"),
            sha256=hashlib.sha256(payload_bytes).hexdigest(),
            size_bytes=len(payload_bytes),
        )
        == payload_bytes
    )


@pytest.mark.parametrize(
    ("bytes_b64", "sha256", "size_bytes", "expected_error"),
    [
        ("abc", "0" * 64, True, "Attachment size_bytes must be a non-negative integer."),
        ("not valid base64", "0" * 64, 3, "Attachment bytes are not valid base64."),
        (
            base64.b64encode(b"payload").decode("ascii"),
            hashlib.sha256(b"payload").hexdigest(),
            999,
            "Attachment byte length does not match the request receipt.",
        ),
        (
            base64.b64encode(b"payload").decode("ascii"),
            "0" * 64,
            len(b"payload"),
            "Attachment SHA256 does not match the request payload.",
        ),
    ],
)
def test_attachment_save_protocol_decode_preserves_error_strings(
    bytes_b64: str,
    sha256: str,
    size_bytes: int | None,
    expected_error: str,
) -> None:
    """Malformed attachment payloads should keep the current runner error text."""
    assert (
        sandbox_proxy_module.decode_attachment_save_bytes(
            bytes_b64=bytes_b64,
            sha256=sha256,
            size_bytes=size_bytes,
        )
        == expected_error
    )


@pytest.mark.parametrize(
    ("response_payload", "expected_error"),
    [
        (
            {"worker_path": "sample.bin", "sha256": "0" * 64},
            "Sandbox save-attachment response is missing its receipt fields.",
        ),
        (
            {"worker_path": "other.bin", "size_bytes": 16, "sha256": "0" * 64},
            "Sandbox save-attachment response path does not match the requested workspace path.",
        ),
        (
            {"worker_path": "sample.bin", "size_bytes": 999, "sha256": "0" * 64},
            "Sandbox save-attachment response size does not match the sent bytes.",
        ),
        (
            {"worker_path": "sample.bin", "size_bytes": 16, "sha256": "0" * 64},
            "Sandbox save-attachment response SHA256 does not match the sent bytes.",
        ),
    ],
)
def test_attachment_save_protocol_receipt_validation_preserves_error_strings(
    response_payload: dict[str, object],
    expected_error: str,
) -> None:
    """Bad worker receipts should keep the current primary-side error text."""
    payload_bytes = b"attachment-bytes"
    result = sandbox_proxy_module._validate_attachment_save_receipt(
        SaveAttachmentResponse(ok=True, **response_payload),
        requested_path="sample.bin",
        byte_count=len(payload_bytes),
        sha256=hashlib.sha256(payload_bytes).hexdigest(),
    )

    assert result == expected_error


def test_attachment_save_protocol_receipt_validation_returns_dataclass() -> None:
    """Valid worker receipts should keep the current receipt field names."""
    payload_bytes = b"attachment-bytes"
    sha256 = hashlib.sha256(payload_bytes).hexdigest()

    receipt = sandbox_proxy_module._validate_attachment_save_receipt(
        SaveAttachmentResponse(ok=True, worker_path="sample.bin", size_bytes=len(payload_bytes), sha256=sha256),
        requested_path="sample.bin",
        byte_count=len(payload_bytes),
        sha256=sha256,
    )

    assert receipt == sandbox_proxy_module.WorkerAttachmentSaveReceipt(
        worker_path="sample.bin",
        size_bytes=len(payload_bytes),
        sha256=sha256,
    )


def _runtime_paths_from_env() -> RuntimePaths:
    # A test's own MINDROOM_STORAGE_PATH export wins, as it does for the runtime.
    storage_path = None if os.environ.get("MINDROOM_STORAGE_PATH") else _ENV_RUNTIME_STORAGE_ROOT
    return resolve_runtime_paths(config_path=Path("config.yaml"), storage_path=storage_path)


def _runs_in_worker(runtime_paths: RuntimePaths, tool_name: str, worker_tools: Collection[str] | None) -> bool:
    """Return whether ``tool_name`` runs in a worker for an agent whose worker tools are ``worker_tools``."""
    return sandbox_proxy_module.tool_placement(runtime_paths, builtin_tool_registry()).runs_in_worker(
        tool_name,
        worker_tools,
    )


def _configure_proxy_runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    proxy_url: str | None,
    proxy_token: str | None = _TEST_AUTH_TOKEN,
    execution_mode: str | None = "all",
    runner_mode: bool = False,
    proxy_tools: set[str] | None = None,
    credential_policy: dict[str, tuple[str, ...]] | None = None,
) -> RuntimePaths:
    if proxy_url is None:
        monkeypatch.delenv("MINDROOM_SANDBOX_PROXY_URL", raising=False)
    else:
        monkeypatch.setenv("MINDROOM_SANDBOX_PROXY_URL", proxy_url)
    if proxy_token is None:
        monkeypatch.delenv("MINDROOM_SANDBOX_PROXY_TOKEN", raising=False)
    else:
        monkeypatch.setenv("MINDROOM_SANDBOX_PROXY_TOKEN", proxy_token)
    if execution_mode is None:
        monkeypatch.delenv("MINDROOM_SANDBOX_EXECUTION_MODE", raising=False)
    else:
        monkeypatch.setenv("MINDROOM_SANDBOX_EXECUTION_MODE", execution_mode)
    monkeypatch.setenv("MINDROOM_SANDBOX_RUNNER_MODE", "true" if runner_mode else "false")
    if proxy_tools is None:
        monkeypatch.delenv("MINDROOM_SANDBOX_PROXY_TOOLS", raising=False)
    else:
        monkeypatch.setenv("MINDROOM_SANDBOX_PROXY_TOOLS", ",".join(sorted(proxy_tools)))
    if credential_policy is None:
        monkeypatch.delenv("MINDROOM_SANDBOX_CREDENTIAL_POLICY_JSON", raising=False)
    else:
        monkeypatch.setenv(
            "MINDROOM_SANDBOX_CREDENTIAL_POLICY_JSON",
            json.dumps({key: list(value) for key, value in credential_policy.items()}),
        )
    return _runtime_paths_from_env()


def _worker_target(
    runtime_paths: RuntimePaths,
    worker_scope: str | None,
    routing_agent_name: str | None,
    execution_identity: ToolExecutionIdentity | None,
    *,
    private_agent_names: frozenset[str] | None = None,
) -> ResolvedWorkerTarget:
    return resolve_worker_target(
        worker_scope,
        routing_agent_name,
        execution_identity=execution_identity,
        tenant_id=runtime_paths.env_value("CUSTOMER_ID"),
        account_id=runtime_paths.env_value("ACCOUNT_ID"),
        private_agent_names=private_agent_names,
    )


def _execute_request(tool_name: str, function_name: str) -> ExecuteRequest:
    return ExecuteRequest(protocol=WORKER_PROTOCOL_VERSION, tool_name=tool_name, function_name=function_name)


class _FakeResponse:
    def __init__(self, payload: object | None = None) -> None:
        self._payload = payload or {"ok": True, "result": "sandbox-result"}

    def raise_for_status(self) -> None:
        return

    def json(self) -> object:
        return self._payload


class _LineResponse:
    """One fake runner response, streamed as the single result line a quick call sends."""

    def __init__(self, response: Any) -> None:  # noqa: ANN401
        self._response = response
        self.status_code = getattr(response, "status_code", 200)

    def raise_for_status(self) -> None:
        self._response.raise_for_status()

    def read(self) -> bytes:
        return b""

    def iter_bytes(self) -> Iterator[bytes]:
        yield json.dumps(self._response.json()).encode() + b"\n"


class _StreamsFromPost:
    """Serve a fake client's streamed execute request from its ``post``, and its health probe as a current runner."""

    def get(self, url: str) -> object:
        assert url.endswith("/healthz"), url
        return _FakeResponse(asdict(worker_health_payload(mindroom_version="test")))

    @contextlib.contextmanager
    def stream(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str],
        timeout: object,
    ) -> Iterator[_LineResponse]:
        del method, timeout
        yield _LineResponse(self.post(url, json=json, headers=headers))


class _TrackingWorkerManager:
    cleanup_locator = None

    def __init__(self) -> None:
        self.touched: list[str] = []
        self.failures: list[tuple[str, str]] = []

    def ensure_worker(self, spec: WorkerSpec, *, now: float | None = None, progress_sink: object = None) -> object:
        del now, progress_sink
        return WorkerHandle(
            worker_id="worker-1",
            worker_key=spec.worker_key,
            endpoint="http://worker/api/sandbox-runner/execute",
            auth_token=_TEST_AUTH_TOKEN,
            status="ready",
            backend_name="kubernetes",
            last_used_at=0.0,
            created_at=0.0,
        )

    def require_compatible_runner(self) -> None:
        """Accept the shared runner's protocol, as a compatible runner would."""

    def touch_worker(self, worker_key: str, *, now: float | None = None) -> object:
        del now
        self.touched.append(worker_key)
        return None

    def record_failure(
        self,
        worker_key: str,
        failure_reason: str,
        *,
        now: float | None = None,
        **_kwargs: object,
    ) -> object:
        del now
        self.failures.append((worker_key, failure_reason))
        return None


def _unleased_primary_worker_manager(runtime_paths: RuntimePaths, **settings: Any) -> WorkerBackend:  # noqa: ANN401
    """Return the primary worker manager these backend settings resolve, holding no lease on it afterwards."""
    with workers_runtime_module.lease_primary_worker_manager(runtime_paths, **settings) as manager:
        return manager


def _build_backend_as(monkeypatch: pytest.MonkeyPatch, configure_name: str, backend_class: Any) -> None:  # noqa: ANN401
    """Keep reading one dedicated backend's real settings, but build ``backend_class`` from them."""
    configure = getattr(workers_runtime_module, configure_name)

    def configure_with_fake_build(
        runtime_paths: RuntimePaths,
        *,
        auth_token: str | None,
        storage_root: Path,
    ) -> ConfiguredWorkerBackend:
        return replace(
            configure(runtime_paths, auth_token=auth_token, storage_root=storage_root),
            build=lambda current_launch: backend_class.build(
                runtime_paths,
                auth_token=auth_token,
                current_launch=current_launch,
                storage_root=storage_root,
            ),
        )

    monkeypatch.setattr(workers_runtime_module, configure_name, configure_with_fake_build)


def _static_worker_manager_lease(manager: object) -> object:
    class _StaticLease:
        def __enter__(self) -> object:
            return manager

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

    return _StaticLease()


class _HttpStatusErrorResponse:
    def __init__(self, url: str, *, status_code: int, payload: dict[str, object] | None = None) -> None:
        self.url = url
        self.status_code = status_code
        self.payload = payload

    def raise_for_status(self) -> None:
        request = httpx.Request("POST", self.url)
        response = (
            httpx.Response(self.status_code, request=request, json=self.payload)
            if self.payload is not None
            else httpx.Response(self.status_code, request=request)
        )
        message = f"{self.status_code} client error"
        raise httpx.HTTPStatusError(message, request=request, response=response)

    def json(self) -> dict[str, object]:
        return self.payload or {}


def _http_status_client_class(*, status_code: int, payload: dict[str, object] | None = None) -> type:
    class _HttpStatusClient(_StreamsFromPost):
        def __init__(self, *, timeout: float) -> None:
            del timeout

        def __enter__(self) -> Self:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return

        def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> _HttpStatusErrorResponse:
            del json, headers
            assert url == "http://worker/api/sandbox-runner/execute"
            return _HttpStatusErrorResponse(url, status_code=status_code, payload=payload)

    return _HttpStatusClient


def _recording_client_class(
    *,
    captured: dict[str, Any] | None = None,
    captured_calls: list[tuple[str, dict[str, Any]]] | None = None,
    responder: Callable[[str, dict[str, Any]], object] | None = None,
    health: Callable[[str], object] | None = None,
) -> type:
    class _FakeClient(_StreamsFromPost):
        def __init__(self, *, timeout: float) -> None:
            if captured is not None:
                captured["timeout"] = timeout

        def get(self, url: str) -> object:
            return super().get(url) if health is None else health(url)

        def __enter__(self) -> Self:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return

        def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> _FakeResponse:
            if captured is not None:
                captured["url"] = url
                captured["json"] = json
                captured["headers"] = headers
            if captured_calls is not None:
                captured_calls.append((url, json))
            payload = responder(url, json) if responder is not None else {"ok": True, "result": "sandbox-result"}
            return _FakeResponse(payload)

    return _FakeClient


def _runner_routes_responder(runner_runtime_paths: RuntimePaths) -> Callable[[str, dict[str, Any]], object]:
    """Serve proxy lease and execute calls the way the runner routes do, with a subprocess child."""

    def responder(url: str, payload: dict[str, Any]) -> object:
        if url.endswith("/leases"):
            lease_request = LeaseRequest.model_validate(payload)
            lease = sandbox_runner_module.sandbox_worker_prep.create_credential_lease(
                **lease_request.model_dump(exclude={"protocol"}),
            )
            return {"lease_id": lease.lease_id, "expires_at": lease.expires_at, "max_uses": lease.uses_remaining}
        request = ExecuteRequest.model_validate(payload)
        credential_overrides = (
            sandbox_runner_module.sandbox_worker_prep.consume_credential_lease(
                request.lease_id,
                tool_name=request.tool_name,
                function_name=request.function_name,
            )
            if request.lease_id is not None
            else {}
        )
        response = sandbox_runner_module._execute_request_subprocess_sync(
            execute_call(
                runner_runtime_paths,
                runner_token=_TEST_AUTH_TOKEN,
                credential_overrides=credential_overrides,
                **request.model_dump(exclude={"protocol"}),
            ),
        )
        return response.model_dump(mode="json")

    return responder


def test_worker_proxy_client_records_worker_success() -> None:
    """Worker proxy HTTP details should live behind one focused client seam."""
    captured: dict[str, Any] = {}
    manager = _TrackingWorkerManager()
    handle = WorkerHandle(
        worker_id="worker-1",
        worker_key="agent:test",
        endpoint="http://worker/api/sandbox-runner/execute",
        auth_token=_TEST_AUTH_TOKEN,
        status="ready",
        backend_name="docker",
        last_used_at=0.0,
        created_at=0.0,
    )

    result = execute_worker_proxy_request(
        config=WorkerProxyClientConfig(
            proxy_url=None,
            proxy_token=None,
            proxy_timeout_seconds=7.0,
            credential_lease_ttl_seconds=60,
            credential_policy={},
            lease_tool_credentials=False,
        ),
        request=_execute_request("shell", "run_shell_command"),
        credentials_manager=None,
        worker_target=None,
        worker_handle=handle,
        worker_manager=manager,
        client_factory=_recording_client_class(captured=captured),
    )

    assert result == "sandbox-result"
    assert captured["url"] == "http://worker/api/sandbox-runner/execute"
    assert captured["headers"] == {"x-mindroom-sandbox-token": _TEST_AUTH_TOKEN}
    assert captured["timeout"] == 7.0
    assert manager.touched == ["agent:test"]
    assert manager.failures == []


def test_cancelled_dedicated_worker_call_stops_at_that_worker() -> None:
    """A dedicated worker's call is stopped at that worker, with the worker's own token."""
    calls: list[tuple[str, dict[str, Any]]] = []
    stopped = threading.Event()

    def responder(url: str, _payload: dict[str, Any]) -> object:
        if url.endswith("/execute/cancel"):
            stopped.set()
            return {"cancelled": True}
        cancellation.cancel()
        assert stopped.wait(10)
        return {"ok": False, "error": "Tool call was cancelled.", "failure_kind": "tool"}

    handle = WorkerHandle(
        worker_id="worker-1",
        worker_key="agent:test",
        endpoint="http://worker/api/sandbox-runner/execute",
        auth_token=_TEST_AUTH_TOKEN,
        status="ready",
        backend_name="docker",
        last_used_at=0.0,
        created_at=0.0,
    )
    cancellation = sandbox_proxy_module.WorkerCallCancellation()
    manager = _TrackingWorkerManager()
    last_post: dict[str, Any] = {}
    client_class = _recording_client_class(captured=last_post, captured_calls=calls, responder=responder)

    with pytest.raises(RuntimeError, match="cancelled"):
        execute_worker_proxy_request(
            config=WorkerProxyClientConfig(
                proxy_url=None,
                proxy_token=None,
                proxy_timeout_seconds=7.0,
                credential_lease_ttl_seconds=60,
                credential_policy={},
                lease_tool_credentials=False,
            ),
            request=_execute_request("shell", "run_shell_command"),
            credentials_manager=None,
            worker_target=None,
            worker_handle=handle,
            worker_manager=manager,
            client_factory=client_class,
            cancellation=cancellation,
        )

    (_, execute), (cancel_url, cancel) = calls
    assert cancel_url == "http://worker/api/sandbox-runner/execute/cancel"
    assert cancel == {"protocol": WORKER_PROTOCOL_VERSION, "request_id": execute["request_id"]}
    assert last_post["headers"] == {"x-mindroom-sandbox-token": _TEST_AUTH_TOKEN}
    # A stopped call is a tool outcome, never a worker failure.
    assert manager.failures == []
    assert manager.touched == ["agent:test"]


def _run_worker_proxy_request_with_exception(
    exception_factory: Callable[[httpx.Request], Exception],
) -> _TrackingWorkerManager:
    manager = _TrackingWorkerManager()
    handle = WorkerHandle(
        worker_id="worker-1",
        worker_key="agent:test",
        endpoint="http://worker/api/sandbox-runner/execute",
        auth_token=_TEST_AUTH_TOKEN,
        status="ready",
        backend_name="kubernetes",
        last_used_at=0.0,
        created_at=0.0,
    )

    class _TimeoutClient(_StreamsFromPost):
        def __init__(self, *, timeout: float) -> None:
            _ = timeout

        def __enter__(self) -> Self:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return

        def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> _FakeResponse:
            _ = json, headers
            request = httpx.Request("POST", url)
            raise exception_factory(request)

    with pytest.raises(httpx.TransportError):
        execute_worker_proxy_request(
            config=WorkerProxyClientConfig(
                proxy_url=None,
                proxy_token=None,
                proxy_timeout_seconds=7.0,
                credential_lease_ttl_seconds=60,
                credential_policy={},
                lease_tool_credentials=False,
            ),
            request=_execute_request("shell", "run_shell_command"),
            credentials_manager=None,
            worker_target=None,
            worker_handle=handle,
            worker_manager=manager,
            client_factory=_TimeoutClient,
        )

    return manager


@pytest.mark.parametrize(
    "exception_factory",
    [
        lambda request: httpx.ConnectError("[Errno 110] Connection timed out", request=request),
        lambda request: httpx.ReadTimeout("timed out", request=request),
        lambda request: httpx.WriteTimeout("timed out", request=request),
    ],
)
def test_worker_proxy_transport_timeout_keeps_ready_worker_alive(
    exception_factory: Callable[[httpx.Request], Exception],
) -> None:
    """A transient proxy transport timeout must not evict the shared worker for sibling streams."""
    manager = _run_worker_proxy_request_with_exception(exception_factory)

    assert manager.touched == ["agent:test"]
    assert manager.failures == []


@pytest.mark.parametrize(
    ("exception_factory", "failure_reason"),
    [
        (
            lambda request: httpx.ConnectError("connection refused", request=request),
            "connection refused",
        ),
        (
            lambda request: httpx.RemoteProtocolError("malformed worker response", request=request),
            "malformed worker response",
        ),
        (
            lambda request: httpx.ReadError("worker read failed", request=request),
            "worker read failed",
        ),
        (
            lambda request: httpx.WriteError("worker write failed", request=request),
            "worker write failed",
        ),
    ],
)
def test_worker_proxy_malformed_transport_errors_record_worker_failure(
    exception_factory: Callable[[httpx.Request], Exception],
    failure_reason: str,
) -> None:
    """Malformed proxy transport failures must evict the broken shared worker."""
    manager = _run_worker_proxy_request_with_exception(exception_factory)

    assert manager.touched == []
    assert manager.failures == [("agent:test", failure_reason)]


def test_proxy_wraps_tool_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tool entrypoints should call the sandbox runner API when proxy mode is enabled."""
    captured: dict[str, Any] = {}

    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="selective",
        proxy_tools={"calculator"},
        credential_policy={},
    )
    monkeypatch.setattr(
        "mindroom.tool_system.sandbox_proxy.httpx.Client",
        _recording_client_class(captured=captured),
    )

    tool = get_tool_by_name("calculator", runtime_paths, worker_target=None, tools=builtin_tool_registry())
    entrypoint = blocking_entrypoint(tool, "add")
    result = entrypoint("1", 2)

    assert result == "sandbox-result"
    assert captured["url"] == "http://sandbox-runner:8765/api/sandbox-runner/execute"
    # The arguments reach the worker as the call sent them, for the worker to validate, with the id a cancelled call
    # stops its request by.
    request_id = captured["json"].pop("request_id")
    assert isinstance(request_id, str)
    assert captured["json"] == {
        "protocol": WORKER_PROTOCOL_VERSION,
        "worker_key": None,
        "worker_scope": None,
        "routing_agent_name": None,
        "execution_identity": None,
        "private_agent_names": None,
        "config_snapshot": None,
        "base_dir": None,
        "tool_name": "calculator",
        "function_name": "add",
        "args": ["1", 2],
        "kwargs": {},
        "lease_id": None,
        "tool_config_overrides": {},
        "tool_init_overrides": {},
        "execution_env": {},
        "environment": {"set_names": [], "unset_names": [], "path_prepend": []},
        "extra_env_passthrough": None,
        "run_as_job": False,
    }
    assert captured["headers"] == {"x-mindroom-sandbox-token": "test-token"}


def test_sandbox_proxy_schema_keeps_mindroom_output_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Proxy wrapping should preserve the augmented output-path schema and forward the kwarg."""
    captured: dict[str, Any] = {}

    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="selective",
        proxy_tools={"calculator"},
        credential_policy={},
    )
    monkeypatch.setattr(
        "mindroom.tool_system.sandbox_proxy.httpx.Client",
        _recording_client_class(captured=captured),
    )

    tool = get_tool_by_name(
        "calculator",
        runtime_paths,
        tool_output_workspace_root=tmp_path,
        worker_target=None,
        tools=builtin_tool_registry(),
    )
    function = toolkit_function(tool, "add")
    assert function is not None

    output_schema = function.parameters["properties"][OUTPUT_PATH_ARGUMENT]
    assert output_schema["description"].startswith("Optional")
    assert output_schema["default"] is None
    assert OUTPUT_PATH_ARGUMENT not in function.parameters["required"]
    entrypoint = blocking_entrypoint(tool, "add")

    result = entrypoint(1, 2, mindroom_output_path="tool-results/add.json")

    assert result == "sandbox-result"
    assert captured["json"]["args"] == [1, 2]
    assert captured["json"]["kwargs"] == {OUTPUT_PATH_ARGUMENT: "tool-results/add.json"}


def test_static_proxy_payload_carries_agent_identity_for_output_redirect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Default static-runner proxy requests should carry enough identity for runner-side wrapping."""
    captured: dict[str, Any] = {}
    execution_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
    )

    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="selective",
        proxy_tools={"calculator"},
        credential_policy={},
    )
    monkeypatch.setattr(
        "mindroom.tool_system.sandbox_proxy.httpx.Client",
        _recording_client_class(captured=captured),
    )

    tool = get_tool_by_name(
        "calculator",
        runtime_paths,
        tool_output_workspace_root=tmp_path,
        worker_target=_worker_target(runtime_paths, None, "general", execution_identity),
        tools=builtin_tool_registry(),
    )
    entrypoint = blocking_entrypoint(tool, "add")

    result = entrypoint(1, 2, mindroom_output_path="tool-results/add.json")

    assert result == "sandbox-result"
    assert captured["json"]["routing_agent_name"] == "general"
    assert captured["json"]["execution_identity"] == asdict(execution_identity)


@pytest.mark.usefixtures("restored_builtin_tools")
def test_sandbox_runner_executes_wrapper_before_to_json_compatible(tmp_path: Path) -> None:
    """Runner-side wrapping should save raw output before proxy response serialization."""
    tool_name = "test_runner_output_redirect"
    marker = "ISSUE200_RUNNER_MARKER"

    class _RunnerRedirectToolkit(Toolkit):
        def __init__(self) -> None:
            super().__init__(name=tool_name, tools=[self.large])

        def large(self) -> dict[str, str]:
            return {"marker": marker * 20}

    @register_tool_with_metadata(
        name=tool_name,
        file_access=ToolFileAccess.NONE,
        display_name="Runner Redirect",
        description="Test-only runner redirect coverage.",
        category=ToolCategory.DEVELOPMENT,
    )
    def _runner_redirect_factory() -> type[_RunnerRedirectToolkit]:
        return _RunnerRedirectToolkit

    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )
    _toolkit, entrypoint = sandbox_runner_module._resolve_entrypoint(
        runtime_paths=runtime_paths,
        config=Config(agents={}, models={}),
        tool_name=tool_name,
        function_name="large",
        tool_output_workspace_root=tmp_path,
        tools=builtin_tool_registry(),
    )

    result = entrypoint(mindroom_output_path="runner/result.json")
    proxy_result = sandbox_runner_module.to_json_compatible(result)

    assert marker in (tmp_path / "runner/result.json").read_text(encoding="utf-8")
    assert marker not in str(proxy_result)
    assert proxy_result["mindroom_tool_output"]["status"] == "saved_to_file"


@pytest.mark.usefixtures("restored_builtin_tools")
@pytest.mark.asyncio
async def test_an_owned_toolkit_routed_to_a_worker_sends_its_arguments_as_sent_and_validates_on_the_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An owned function keeps its schema behind the proxy, sends the model's arguments, and the runner coerces them."""
    captured: dict[str, Any] = {}
    tool_name = "test_proxy_owned_tool"

    class _OwnedTools(OwnedToolkit):
        name = tool_name

        @tool
        def repeat(self, word: str, times: int = 1) -> str:
            """Repeat a word.

            Args:
                word: The word.
                times: How often.

            """
            return word * times

    register_tool_with_metadata(
        name=tool_name,
        file_access=ToolFileAccess.NONE,
        display_name="Owned Proxy Tool",
        description="Test-only owned toolkit behind the worker proxy.",
        category=ToolCategory.DEVELOPMENT,
    )(lambda: _OwnedTools)
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="selective",
        proxy_tools={tool_name},
        credential_policy={},
    )
    monkeypatch.setattr(
        "mindroom.tool_system.sandbox_proxy.httpx.Client",
        _recording_client_class(captured=captured),
    )
    [function] = adapt_toolkit(
        get_tool_by_name(tool_name, runtime_paths, worker_target=None, tools=builtin_tool_registry()),
    ).functions
    outcome = await execute_tool_call(
        function,
        {"word": "ab", "times": "2"},
        ToolCallContext(run_id="run-1", session_id="session-1", user_id=None),
        middleware=(),
    )
    _toolkit, entrypoint = sandbox_runner_module._resolve_entrypoint(
        runtime_paths=runtime_paths,
        config=Config(agents={}, models={}),
        tool_name=tool_name,
        function_name="repeat",
        tools=builtin_tool_registry(),
    )

    assert function.parameters == {
        "type": "object",
        "properties": {
            "word": {"type": "string", "description": "The word."},
            "times": {"type": "integer", "description": "How often."},
        },
        "required": ["word"],
    }
    assert outcome.content == "sandbox-result"
    assert captured["json"]["kwargs"] == {"word": "ab", "times": "2"}
    assert entrypoint(word="ab", times="2") == "abab"


@pytest.mark.usefixtures("restored_builtin_tools")
@pytest.mark.asyncio
async def test_sandbox_runner_runs_the_function_a_primary_run_offers(tmp_path: Path) -> None:
    """A toolkit that registers a sync and an async function of one name runs its async one, as the primary offers it."""
    tool_name = "test_runner_one_function_per_name"

    class _DualToolkit(Toolkit):
        def __init__(self) -> None:
            super().__init__(name=tool_name, tools=[self.lookup])
            self.async_functions["lookup"] = Function(name="lookup", entrypoint=self.alookup)

        def lookup(self) -> str:
            return "sync"

        async def alookup(self) -> str:
            return "async"

    @register_tool_with_metadata(
        name=tool_name,
        file_access=ToolFileAccess.NONE,
        display_name="One Function Per Name",
        description="Test-only toolkit registering both variants of one function.",
        category=ToolCategory.DEVELOPMENT,
    )
    def _dual_factory() -> type[_DualToolkit]:
        return _DualToolkit

    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )
    toolkit, entrypoint = sandbox_runner_module._resolve_entrypoint(
        runtime_paths=runtime_paths,
        config=Config(agents={}, models={}),
        tool_name=tool_name,
        function_name="lookup",
        tools=builtin_tool_registry(),
    )
    [offered] = adapt_toolkit(toolkit).functions

    assert await offered.entrypoint() == "async"
    assert await sandbox_runner_module._call_entrypoint(entrypoint, [], {}) == "async"


def _routed_ping_toolkit(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    responder: Callable[[str, dict[str, Any]], object] | None = None,
) -> Toolkit:
    """Register a toolkit with one synchronous function, route it to a worker, and build it.

    The test that calls it restores the built-in tools through the ``restored_builtin_tools`` fixture.
    """

    class _PingToolkit(Toolkit):
        def __init__(self) -> None:
            super().__init__(name=tool_name, tools=[self.ping])

        def ping(self) -> str:
            return "local"

    register_tool_with_metadata(
        name=tool_name,
        file_access=ToolFileAccess.NONE,
        display_name="Routed Ping",
        description="Test-only toolkit a plugin calls directly.",
        category=ToolCategory.DEVELOPMENT,
    )(lambda: _PingToolkit)
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        execution_mode="selective",
        proxy_tools={tool_name},
        credential_policy={},
    )
    monkeypatch.setattr(
        "mindroom.tool_system.sandbox_proxy.httpx.Client",
        _recording_client_class(responder=responder),
    )
    return get_tool_by_name(tool_name, runtime_paths, worker_target=None, tools=builtin_tool_registry())


def _blocking_requests(count: int) -> tuple[list[threading.Event], list[threading.Event], Callable[..., object]]:
    """Return per-request start and release events, and a responder that blocks each request until its release."""
    starts = [threading.Event() for _ in range(count)]
    releases = [threading.Event() for _ in range(count)]
    order = iter(range(count))
    lock = threading.Lock()

    def respond(_url: str, _payload: dict[str, Any]) -> object:
        with lock:
            index = next(order)
        starts[index].set()
        releases[index].wait(timeout=30)
        return {"ok": True, "result": "sandbox-result"}

    return starts, releases, respond


@pytest.mark.usefixtures("restored_builtin_tools")
@pytest.mark.asyncio
async def test_a_worker_routed_toolkit_function_runs_more_than_once_inside_one_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tool may call a sibling's function twice, and twice at once; the call's owner holds every request."""
    toolkit = _routed_ping_toolkit(monkeypatch, "test_routed_ping_twice")
    ping = toolkit_functions(toolkit)["ping"]
    tracker = SyncToolCompletionTracker(executor=ThreadPoolExecutor(max_workers=2))
    with track_sync_tool_completion(tracker):
        results = [await ping(), await ping(), *await asyncio.gather(ping(), ping())]

    assert results == ["sandbox-result"] * 4
    assert [task.done() for task in tracker.settle()] == [True] * 4


@pytest.mark.usefixtures("restored_builtin_tools")
@pytest.mark.asyncio
async def test_a_toolkit_function_runs_its_request_on_the_threads_of_the_call_that_makes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A nested request takes the capacity its caller's owner set aside, such as a job's own threads."""
    threads: list[str] = []

    def record_thread(_url: str, _payload: dict[str, Any]) -> object:
        threads.append(threading.current_thread().name)
        return {"ok": True, "result": "sandbox-result"}

    toolkit = _routed_ping_toolkit(monkeypatch, "test_routed_ping_in_job", record_thread)
    ping = toolkit_functions(toolkit)["ping"]
    async with owned_sync_tool_completion(JOB_CALL_EXECUTOR):
        result = await ping()

    assert result == "sandbox-result"
    assert threads == ["mindroom-job-call"]


@pytest.mark.usefixtures("restored_builtin_tools")
@pytest.mark.asyncio
async def test_a_synchronous_tool_cannot_run_a_toolkit_function_on_its_own_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A nested call a synchronous tool runs with ``asyncio.run`` fails before its request starts; the owner settles."""
    requests: list[str] = []

    def respond(_url: str, _payload: dict[str, Any]) -> object:
        requests.append(threading.current_thread().name)
        return {"ok": True, "result": "sandbox-result"}

    toolkit = _routed_ping_toolkit(monkeypatch, "test_routed_ping_sync_tool", respond)
    ping = toolkit_functions(toolkit)["ping"]

    def sync_tool() -> str:
        return asyncio.run(ping())

    with pytest.raises(RuntimeError, match="on an event loop of its own"):
        async with owned_sync_tool_completion(JOB_CALL_EXECUTOR):
            await run_sync_tool_entrypoint(sync_tool)

    assert requests == []


@pytest.mark.usefixtures("restored_builtin_tools")
@pytest.mark.asyncio
async def test_a_cancelled_toolkit_function_call_returns_at_once_and_leaves_its_request_to_the_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tool that made the call unwinds promptly, while its call's owner still holds the running request."""
    starts, releases, respond = _blocking_requests(1)
    toolkit = _routed_ping_toolkit(monkeypatch, "test_routed_ping_cancelled", respond)
    tracker = SyncToolCompletionTracker(executor=ThreadPoolExecutor(max_workers=1))
    try:
        ping = toolkit_functions(toolkit)["ping"]
        with track_sync_tool_completion(tracker):
            call = asyncio.create_task(ping())
            await asyncio.to_thread(starts[0].wait, 30)
            call.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                async with asyncio.timeout(2):
                    await call
            [leaf] = tracker.settle()
            held = not leaf.done()
            releases[0].set()
            assert await leaf == "sandbox-result"
    finally:
        releases[0].set()

    assert call.cancelled()
    assert held


@pytest.mark.usefixtures("restored_builtin_tools")
@pytest.mark.asyncio
@pytest.mark.parametrize("returns_first", [0, 1], ids=["first-request-returns-first", "second-request-returns-first"])
async def test_the_owner_of_a_cancelled_call_waits_for_every_request_its_nested_calls_started(
    monkeypatch: pytest.MonkeyPatch,
    returns_first: int,
) -> None:
    """A waiting owner, such as a job's, returns from a cancelled call only after each nested request has returned."""
    starts, releases, respond = _blocking_requests(2)
    toolkit = _routed_ping_toolkit(monkeypatch, "test_routed_ping_owner_waits", respond)
    try:
        ping = toolkit_functions(toolkit)["ping"]

        async def tool() -> list[object]:
            # The second request starts after the first arrives, so arrival order is the order the owner holds them in.
            earlier = asyncio.create_task(ping())
            await asyncio.to_thread(starts[0].wait, 30)
            return await asyncio.gather(earlier, ping())

        async def owned_call() -> list[object]:
            async with owned_sync_tool_completion(ThreadPoolExecutor(max_workers=2)):
                return await tool()

        owner = asyncio.create_task(owned_call())
        for started in starts:
            await asyncio.to_thread(started.wait, 30)
        owner.cancel()
        releases[returns_first].set()
        await asyncio.sleep(0.2)
        waiting_on_other = not owner.done()
        releases[1 - returns_first].set()
        with contextlib.suppress(asyncio.CancelledError):
            async with asyncio.timeout(5):
                await owner
    finally:
        for release in releases:
            release.set()

    assert waiting_on_other
    assert owner.cancelled()


@pytest.mark.usefixtures("restored_builtin_tools")
@pytest.mark.asyncio
async def test_a_plugins_timeout_around_a_toolkit_function_call_fires_on_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tool's own timeout around a nested call ends it when the timeout says, not when the request returns."""
    _starts, releases, respond = _blocking_requests(1)
    toolkit = _routed_ping_toolkit(monkeypatch, "test_routed_ping_timeout", respond)
    tracker = SyncToolCompletionTracker()
    try:
        ping = toolkit_functions(toolkit)["ping"]
        with track_sync_tool_completion(tracker):
            began = time.monotonic()
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.3):
                    await ping()
            elapsed = time.monotonic() - began
    finally:
        releases[0].set()
        await asyncio.gather(*tracker.settle(), return_exceptions=True)

    assert elapsed < 5


@pytest.mark.asyncio
@requires_linux(reason="local worker venv bootstrap is validated on Linux", timeout=180)
async def test_sandbox_runner_save_attachment_writes_worker_workspace(tmp_path: Path) -> None:
    """The runner save endpoint should validate and atomically write bytes under the worker workspace."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_SANDBOX_PROXY_TOKEN": _TEST_AUTH_TOKEN},
    )
    config = Config(agents={"code": AgentConfig(display_name="Code", memory_backend="file")}, models={})
    app = SimpleNamespace(
        state=SimpleNamespace(
            sandbox_runner_context=sandbox_runner_module._SandboxRunnerContext(
                runtime_paths=runtime_paths,
                config=config,
                runner_token=_TEST_AUTH_TOKEN,
            ),
        ),
    )
    payload_bytes = b"worker-bytes"
    sha256 = hashlib.sha256(payload_bytes).hexdigest()

    response = await sandbox_runner_module.save_attachment_to_worker(
        SimpleNamespace(app=app),
        SaveAttachmentRequest(
            protocol=WORKER_PROTOCOL_VERSION,
            worker_key="worker-test",
            attachment_id="att_sample",
            mindroom_output_path="inputs/sample.bin",
            sha256=sha256,
            size_bytes=len(payload_bytes),
            bytes_b64=base64.b64encode(payload_bytes).decode("ascii"),
        ),
    )

    assert response.ok is True
    assert response.worker_path == "inputs/sample.bin"
    saved_path = next(runtime_paths.storage_root.rglob("sample.bin"))
    assert saved_path.read_bytes() == payload_bytes
    assert saved_path.is_relative_to(runtime_paths.storage_root)
    assert stat.S_IMODE(saved_path.stat().st_mode) == 0o600
    assert response.size_bytes == len(payload_bytes)
    assert response.sha256 == sha256


@pytest.mark.asyncio
async def test_sandbox_runner_save_attachment_rejects_sha_mismatch_and_unsafe_path(tmp_path: Path) -> None:
    """The save endpoint should reject mismatched bytes and unsafe output paths without writing files."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_SANDBOX_PROXY_TOKEN": _TEST_AUTH_TOKEN},
    )
    config = Config(agents={"code": AgentConfig(display_name="Code", memory_backend="file")}, models={})
    app = SimpleNamespace(
        state=SimpleNamespace(
            sandbox_runner_context=sandbox_runner_module._SandboxRunnerContext(
                runtime_paths=runtime_paths,
                config=config,
                runner_token=_TEST_AUTH_TOKEN,
            ),
        ),
    )
    request = SimpleNamespace(app=app)
    payload_bytes = b"worker-bytes"
    encoded = base64.b64encode(payload_bytes).decode("ascii")

    mismatch = await sandbox_runner_module.save_attachment_to_worker(
        request,
        SaveAttachmentRequest(
            protocol=WORKER_PROTOCOL_VERSION,
            worker_key="worker-test",
            attachment_id="att_sample",
            mindroom_output_path="inputs/sample.bin",
            sha256="0" * 64,
            size_bytes=len(payload_bytes),
            bytes_b64=encoded,
        ),
    )
    unsafe = await sandbox_runner_module.save_attachment_to_worker(
        request,
        SaveAttachmentRequest(
            protocol=WORKER_PROTOCOL_VERSION,
            worker_key="worker-test",
            attachment_id="att_sample",
            mindroom_output_path="../escape.bin",
            sha256=hashlib.sha256(payload_bytes).hexdigest(),
            size_bytes=len(payload_bytes),
            bytes_b64=encoded,
        ),
    )

    assert mismatch.ok is False
    assert mismatch.failure_kind == "tool"
    assert "SHA256" in (mismatch.error or "")
    assert unsafe.ok is False
    assert unsafe.failure_kind == "tool"
    assert "mindroom_output_path" in (unsafe.error or "")
    assert not list(runtime_paths.storage_root.rglob("sample.bin"))
    assert not list(runtime_paths.storage_root.rglob("escape.bin"))


@pytest.mark.asyncio
async def test_sandbox_runner_save_attachment_rejects_unsafe_path_before_decoding(tmp_path: Path) -> None:
    """Unsafe save paths should be rejected before malformed request bytes are decoded."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_SANDBOX_PROXY_TOKEN": _TEST_AUTH_TOKEN},
    )
    config = Config(agents={"code": AgentConfig(display_name="Code")}, models={})
    app = SimpleNamespace(
        state=SimpleNamespace(
            sandbox_runner_context=sandbox_runner_module._SandboxRunnerContext(
                runtime_paths=runtime_paths,
                config=config,
                runner_token=_TEST_AUTH_TOKEN,
            ),
        ),
    )

    response = await sandbox_runner_module.save_attachment_to_worker(
        SimpleNamespace(app=app),
        SaveAttachmentRequest(
            protocol=WORKER_PROTOCOL_VERSION,
            worker_key="worker-test",
            attachment_id="att_sample",
            mindroom_output_path="../escape.bin",
            sha256="0" * 64,
            size_bytes=10,
            bytes_b64="not valid base64",
        ),
    )

    assert response.ok is False
    assert response.failure_kind == "tool"
    assert "mindroom_output_path" in (response.error or "")


@pytest.mark.asyncio
async def test_sandbox_runner_save_attachment_supports_static_unkeyed_workspace(tmp_path: Path) -> None:
    """Static runner saves should mirror execute output redirection without requiring worker_key."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_SANDBOX_PROXY_TOKEN": _TEST_AUTH_TOKEN},
    )
    config = Config(agents={"code": AgentConfig(display_name="Code", memory_backend="file")}, models={})
    app = SimpleNamespace(
        state=SimpleNamespace(
            sandbox_runner_context=sandbox_runner_module._SandboxRunnerContext(
                runtime_paths=runtime_paths,
                config=config,
                runner_token=_TEST_AUTH_TOKEN,
            ),
        ),
    )
    payload_bytes = b"static-runner"
    sha256 = hashlib.sha256(payload_bytes).hexdigest()

    response = await sandbox_runner_module.save_attachment_to_worker(
        SimpleNamespace(app=app),
        SaveAttachmentRequest(
            protocol=WORKER_PROTOCOL_VERSION,
            routing_agent_name="code",
            attachment_id="att_sample",
            mindroom_output_path="inputs/static.bin",
            sha256=sha256,
            size_bytes=len(payload_bytes),
            bytes_b64=base64.b64encode(payload_bytes).decode("ascii"),
        ),
    )

    workspace_root = agent_workspace_root_path(runtime_paths.storage_root, "code")
    assert response.ok is True
    assert response.worker_path == "inputs/static.bin"
    assert (workspace_root / "inputs" / "static.bin").read_bytes() == payload_bytes


@pytest.mark.usefixtures("restored_builtin_tools")
@pytest.mark.asyncio
async def test_static_runner_redirect_resolves_agent_workspace_without_prepared_worker(tmp_path: Path) -> None:
    """Static runner redirects should rebuild with the routing agent workspace root."""
    tool_name = "test_static_runner_output_redirect"
    marker = "ISSUE200_STATIC_RUNNER_MARKER"

    class _StaticRunnerRedirectToolkit(Toolkit):
        def __init__(self) -> None:
            super().__init__(name=tool_name, tools=[self.large])

        def large(self) -> str:
            return marker * 20

    @register_tool_with_metadata(
        name=tool_name,
        file_access=ToolFileAccess.NONE,
        display_name="Static Runner Redirect",
        description="Test-only static runner redirect coverage.",
        category=ToolCategory.DEVELOPMENT,
    )
    def _static_runner_redirect_factory() -> type[_StaticRunnerRedirectToolkit]:
        return _StaticRunnerRedirectToolkit

    storage_root = tmp_path / "storage"
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=storage_root,
        process_env={},
    )
    config = Config(
        agents={"general": AgentConfig(display_name="General", memory_backend="file")},
        models={},
    )
    call = execute_call(
        runtime_paths,
        config,
        tool_name=tool_name,
        function_name="large",
        kwargs={OUTPUT_PATH_ARGUMENT: "tool-results/static.txt"},
        routing_agent_name="general",
    )
    response = await sandbox_runner_module._execute_request_inprocess(call)

    workspace = agent_workspace_root_path(storage_root, "general")
    output_path = workspace / "tool-results/static.txt"
    assert response.ok is True
    assert marker in output_path.read_text(encoding="utf-8")
    assert marker not in str(response.result)
    assert response.result["mindroom_tool_output"]["status"] == "saved_to_file"


@pytest.mark.usefixtures("restored_builtin_tools")
@pytest.mark.asyncio
async def test_worker_redirect_uses_agent_workspace_not_worker_scratch(tmp_path: Path) -> None:
    """Worker-routed redirects should save where later agent file tools can read them."""
    tool_name = "test_worker_output_redirect"
    marker = "ISSUE200_WORKER_REDIRECT_MARKER"

    class _WorkerRedirectToolkit(Toolkit):
        def __init__(self) -> None:
            super().__init__(name=tool_name, tools=[self.large])

        def large(self) -> dict[str, str]:
            return {"marker": marker}

    @register_tool_with_metadata(
        name=tool_name,
        file_access=ToolFileAccess.NONE,
        display_name="Worker Redirect",
        description="Test-only worker redirect coverage.",
        category=ToolCategory.DEVELOPMENT,
    )
    def _worker_redirect_factory() -> type[_WorkerRedirectToolkit]:
        return _WorkerRedirectToolkit

    storage_root = tmp_path / "storage"
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=storage_root,
        process_env={},
    )
    config = Config(
        agents={"general": AgentConfig(display_name="General", memory_backend="file")},
        models={},
    )
    execution_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
    )
    worker_key = resolve_worker_key("shared", execution_identity, agent_name="general")
    worker_paths = local_worker_state_paths_for_root(tmp_path / "worker-scratch")
    worker_paths.workspace.mkdir(parents=True, exist_ok=True)
    prepared_worker = sandbox_runner_module.sandbox_worker_prep.PreparedWorkerRequest(
        handle=WorkerHandle(
            worker_id="worker-1",
            worker_key=worker_key,
            endpoint="/api/sandbox-runner/execute",
            auth_token=_TEST_AUTH_TOKEN,
            status="ready",
            backend_name="local",
            last_used_at=0.0,
            created_at=0.0,
        ),
        paths=worker_paths,
        base_dir=worker_paths.workspace,
    )
    request = ExecuteRequest(
        protocol=WORKER_PROTOCOL_VERSION,
        tool_name=tool_name,
        function_name="large",
        kwargs={OUTPUT_PATH_ARGUMENT: "tool-results/worker.json"},
        worker_key=worker_key,
        worker_scope="shared",
        routing_agent_name="general",
        execution_identity=serialize_tool_execution_identity(execution_identity),
    )
    # A worker request that names no base_dir acts in its routing agent's workspace, not the worker's scratch.
    route = sandbox_runner_module._PreparedRoute(
        runtime_paths=runtime_paths,
        config=config,
        worker_key=worker_key,
        identity=execution_identity,
        worker=prepared_worker,
        workspace=sandbox_runner_module._route_workspace(
            request,
            prepared_worker,
            runtime_paths=runtime_paths,
            config=config,
            identity=execution_identity,
        ),
    )
    response = await sandbox_runner_module._execute_request_inprocess(
        sandbox_runner_module._ExecuteCall(request=request, route=route, tools=builtin_tool_registry()),
    )

    workspace = agent_workspace_root_path(storage_root, "general")
    output_path = workspace / "tool-results/worker.json"
    assert response.ok is True
    assert marker in output_path.read_text(encoding="utf-8")
    assert not (worker_paths.workspace / "tool-results/worker.json").exists()
    assert response.result["mindroom_tool_output"]["path"] == "tool-results/worker.json"


@pytest.mark.usefixtures("restored_builtin_tools")
def test_proxy_payload_includes_tool_config_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proxy execution should forward authored tool config overrides separately from runtime init overrides."""
    captured: dict[str, Any] = {}
    tool_name = "test_proxy_configured_tool"

    class _ConfiguredToolkit(Toolkit):
        def __init__(self, label: str | None = None) -> None:
            self.label = label
            super().__init__(name=tool_name, tools=[self.ping])

        def ping(self) -> str:
            return self.label or "local"

    @register_tool_with_metadata(
        name=tool_name,
        file_access=ToolFileAccess.NONE,
        display_name="Proxy Configured Tool",
        description="Test-only proxy payload coverage.",
        category=ToolCategory.DEVELOPMENT,
        config_fields=[ConfigField(name="label", label="Label", type="text", required=False)],
    )
    def _configured_tool_factory() -> type[_ConfiguredToolkit]:
        return _ConfiguredToolkit

    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="selective",
        proxy_tools={tool_name},
        credential_policy={},
    )
    monkeypatch.setattr(
        "mindroom.tool_system.sandbox_proxy.httpx.Client",
        _recording_client_class(captured=captured),
    )

    tool = get_tool_by_name(
        tool_name,
        runtime_paths,
        tool_config_overrides={"label": "from-config"},
        worker_target=None,
        tools=builtin_tool_registry(),
    )
    entrypoint = blocking_entrypoint(tool, "ping")
    result = entrypoint()

    assert result == "sandbox-result"
    assert captured["json"]["tool_config_overrides"] == {"label": "from-config"}
    assert captured["json"]["tool_init_overrides"] == {}


def test_proxy_disabled_in_runner_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Runner mode must execute tools locally to avoid proxy recursion."""

    class _ForbiddenClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            msg = "Proxy client should not be used in runner mode."
            raise AssertionError(msg)

    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        execution_mode="all",
        runner_mode=True,
    )
    monkeypatch.setattr("mindroom.tool_system.sandbox_proxy.httpx.Client", _ForbiddenClient)

    tool = get_tool_by_name("calculator", runtime_paths, worker_target=None, tools=builtin_tool_registry())
    entrypoint = blocking_entrypoint(tool, "add")
    result = entrypoint(1, 2)
    assert '"result": 3' in result


@pytest.mark.asyncio
async def test_sync_function_placed_on_the_primary_runs_locally_through_its_async_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sync function's async proxy still runs calls its toolkit places on the primary, without the worker."""
    calls: list[tuple[str, dict[str, Any]]] = []
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="all",
    )
    monkeypatch.setattr(
        "mindroom.tool_system.sandbox_proxy.httpx.Client",
        _recording_client_class(
            captured_calls=calls,
            responder=lambda _url, _payload: {"ok": True, "result": "worker"},
        ),
    )
    caller_threads: list[int] = []

    class PlacedTools(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="calculator", tools=[self.where])

        def where(self, place: str) -> str:
            """Report where the call ran."""
            caller_threads.append(threading.get_ident())
            return f"ran {place}"

        def runs_on_primary(self, function_name: str, arguments: Mapping[str, object]) -> bool:
            return function_name == "where" and arguments.get("place") == "primary"

    placed = PlacedTools()
    toolkit = configure_toolkit_functions(
        placed,
        to_worker=sandbox_proxy_module.worker_stand_ins(
            "calculator",
            placed,
            runtime_paths=runtime_paths,
            credentials_manager=None,
            worker_target=None,
            tools=builtin_tool_registry(),
        ),
    )
    entrypoint = _offered_entrypoint(toolkit, "where")

    assert await entrypoint(place="primary") == "ran primary"
    assert calls == []
    # The sync function ran off the event loop thread.
    assert caller_threads != [threading.get_ident()]
    assert await entrypoint(place="worker") == "worker"
    assert [url for url, _payload in calls] == ["http://sandbox-runner:8765/api/sandbox-runner/execute"]


@pytest.mark.asyncio
async def test_a_placed_call_binds_only_its_own_arguments_and_one_missing_an_argument_fails_there(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Placement reads the call's own arguments, not the output path, and a call it cannot bind reaches no worker."""
    calls: list[tuple[str, dict[str, Any]]] = []
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="all",
    )
    monkeypatch.setattr(
        "mindroom.tool_system.sandbox_proxy.httpx.Client",
        _recording_client_class(captured_calls=calls, responder=lambda _url, _payload: {"ok": True, "result": "w"}),
    )

    class PlacedTools(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="calculator", tools=[self.where])

        def where(self, place: str) -> str:
            """Report where the call ran."""
            return f"ran {place}"

        def runs_on_primary(self, function_name: str, arguments: Mapping[str, object]) -> bool:
            assert function_name == "where"
            assert OUTPUT_PATH_ARGUMENT not in arguments
            return arguments["place"] == "primary"

    placed = PlacedTools()
    toolkit = configure_toolkit_functions(
        placed,
        output_files=ToolOutputFilePolicy(workspace_root=tmp_path),
        to_worker=sandbox_proxy_module.worker_stand_ins(
            "calculator",
            placed,
            runtime_paths=runtime_paths,
            credentials_manager=None,
            worker_target=None,
            tools=builtin_tool_registry(),
        ),
    )
    entrypoint = _offered_entrypoint(toolkit, "where")

    saved = await entrypoint(place="primary", mindroom_output_path="where.txt")
    with pytest.raises(TypeError, match="missing a required argument: 'place'"):
        await entrypoint(mindroom_output_path="where.txt")

    assert saved["mindroom_tool_output"]["status"] == "saved_to_file"
    assert (tmp_path / "where.txt").read_text() == "ran primary"
    assert calls == []


@pytest.mark.asyncio
async def test_a_routed_call_reaches_the_worker_as_sent_which_coerces_and_validates_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The primary forwards the model's arguments unchanged; the worker coerces "3" and rejects what does not validate."""
    primary_paths, _workspace, sent_payloads = _forward_proxy_to_seeded_runner(
        monkeypatch,
        tmp_path,
        proxy_tools=frozenset({"calculator"}),
    )
    tool = get_tool_by_name("calculator", primary_paths, worker_target=None, tools=builtin_tool_registry())
    entrypoint = _offered_entrypoint(tool, "add")

    added = await entrypoint(a="3", b=4)
    with pytest.raises(RuntimeError) as invalid:
        await entrypoint(a="x", b=4)

    assert [payload["kwargs"] for payload in sent_payloads] == [{"a": "3", "b": 4}, {"a": "x", "b": 4}]
    assert json.loads(added) == {"operation": "addition", "result": 7.0}
    assert str(invalid.value).startswith(
        "Sandbox tool execution failed: ValidationError: 1 validation error for CalculatorTools.add\na\n"
        "  Input should be a valid number, unable to parse string as a number",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("a", [3, "x"], ids=["valid-argument", "invalid-argument"])
async def test_a_routed_output_path_is_sent_as_given_and_reported_by_the_worker_before_the_arguments(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    a: object,
) -> None:
    """A non-string path travels to the worker, whose output files answer with its error before validating the rest."""
    sent: list[dict[str, object]] = []
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="selective",
        proxy_tools={"calculator"},
    )
    monkeypatch.setattr(sandbox_proxy_module, "_call_proxy_sync", lambda **call: sent.append(call["kwargs"]))
    tool = get_tool_by_name(
        "calculator",
        runtime_paths,
        tool_output_workspace_root=tmp_path / "primary",
        worker_target=None,
        tools=builtin_tool_registry(),
    )
    await _offered_entrypoint(tool, "add")(a=a, b=4, mindroom_output_path=5)
    _toolkit, worker_entrypoint = sandbox_runner_module._resolve_entrypoint(
        runtime_paths=resolve_runtime_paths(config_path=tmp_path / "config.yaml", process_env={}),
        config=Config(agents={}, models={}),
        tool_name="calculator",
        function_name="add",
        tools=builtin_tool_registry(),
        tool_output_workspace_root=tmp_path / "worker",
    )

    assert sent == [{"a": a, "b": 4, OUTPUT_PATH_ARGUMENT: 5}]
    assert worker_entrypoint(**sent[0]) == {
        "mindroom_tool_output": {
            "status": "error",
            "error": "mindroom_output_path must be a workspace-relative string path.",
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("on_primary", [False, True], ids=["worker", "primary"])
async def test_stopped_sync_tool_call_keeps_its_completion_owner_waiting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    on_primary: bool,
) -> None:
    """A tool catalog still waits for a stopped sync call's thread, as it did before sync functions got async proxies."""
    entered = threading.Event()
    finish = threading.Event()

    def blocking(*_args: object, **_kwargs: object) -> str:
        entered.set()
        assert finish.wait(10)
        return "done"

    class PlacedTools(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="calculator", tools=[self.action])

        def action(self) -> str:
            """Block until released."""
            return blocking()

        def runs_on_primary(self, function_name: str, arguments: Mapping[str, object]) -> bool:
            del function_name, arguments
            return on_primary

    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="all",
    )
    monkeypatch.setattr(sandbox_proxy_module, "_call_proxy_sync", blocking)
    placed = PlacedTools()
    toolkit = configure_toolkit_functions(
        placed,
        to_worker=sandbox_proxy_module.worker_stand_ins(
            "calculator",
            placed,
            runtime_paths=runtime_paths,
            credentials_manager=None,
            worker_target=None,
            tools=builtin_tool_registry(),
        ),
    )
    catalog = await _catalog(tmp_path, [adapt_toolkit(toolkit)])
    call = asyncio.create_task(_events(catalog, "calculator", "action"))
    assert await asyncio.to_thread(entered.wait, 5)
    call.cancel()
    close = asyncio.create_task(catalog.close())
    try:
        done, _pending = await asyncio.wait({close}, timeout=0.2)
        assert not done, "the catalog closed while the stopped call was still running"
    finally:
        finish.set()
        with contextlib.suppress(asyncio.CancelledError):
            await call
        await close


def test_proxy_requests_credential_lease_when_policy_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proxy should create and consume a lease when credential sharing policy allows it."""
    captured_calls: list[tuple[str, dict[str, Any]]] = []

    fake_credentials = FakeCredentialsManager({"openai": {"api_key": "sk-test", "_source": "ui"}})

    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="all",
        credential_policy={"calculator.add": ("openai",)},
    )
    monkeypatch.setattr(
        "mindroom.tool_system.sandbox_proxy.httpx.Client",
        _recording_client_class(
            captured_calls=captured_calls,
            responder=lambda url, _json: (
                {"lease_id": "lease-123", "expires_at": 123.0, "max_uses": 1}
                if url.endswith("/leases")
                else {"ok": True, "result": "proxied"}
            ),
        ),
    )

    tool = get_tool_by_name(
        "calculator",
        runtime_paths,
        credentials_manager=fake_credentials,
        worker_target=None,
        tools=builtin_tool_registry(),
    )
    entrypoint = blocking_entrypoint(tool, "add")
    result = entrypoint(1, 2)

    assert result == "proxied"
    assert len(captured_calls) == 2

    lease_url, lease_payload = captured_calls[0]
    assert lease_url.endswith("/api/sandbox-runner/leases")
    assert lease_payload["credential_overrides"] == {"api_key": "sk-test"}

    execute_url, execute_payload = captured_calls[1]
    assert execute_url.endswith("/api/sandbox-runner/execute")
    assert execute_payload["lease_id"] == "lease-123"


@pytest.mark.parametrize(
    ("leased_service", "expected_token"),
    [
        # The called tool itself: a scoped call leases its primary-owned settings on every backend.
        ("calculator", "primary"),
        ("github", "primary"),
        ("postgres", "primary"),
        # A model provider service is a tool too, so its settings are primary-owned.
        ("openai", "primary"),
        # A service that configures no tool stays in the worker store.
        ("github_private", "worker"),
    ],
)
def test_scoped_proxy_calls_lease_tool_settings_from_primary_stores(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    leased_service: str,
    expected_token: str,
) -> None:
    """Scoped routed calls receive primary-owned tool settings, never the worker-writable copies."""
    captured_calls: list[tuple[str, dict[str, Any]]] = []
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        execution_mode="all",
        credential_policy={} if leased_service == "calculator" else {"calculator.add": (leased_service,)},
    )
    manager = CredentialsManager(tmp_path / "credentials")
    target = resolve_worker_target("shared", "alpha", None, tenant_id="test-tenant")
    assert target.worker_key is not None
    manager.for_primary_runtime_agent_scope("alpha").save_credentials(leased_service, {"access_token": "primary"})
    manager.for_worker(target.worker_key).save_credentials(leased_service, {"access_token": "worker"})
    monkeypatch.setattr(
        "mindroom.tool_system.sandbox_proxy.httpx.Client",
        _recording_client_class(
            captured_calls=captured_calls,
            responder=lambda url, _json: (
                {"lease_id": "lease-123", "expires_at": 123.0, "max_uses": 1}
                if url.endswith("/leases")
                else {"ok": True, "result": "proxied"}
            ),
        ),
    )

    tool = get_tool_by_name(
        "calculator",
        runtime_paths,
        credentials_manager=manager,
        runs_in_worker=_runs_in_worker(runtime_paths, "calculator", ["calculator"]),
        worker_target=target,
        tools=builtin_tool_registry(),
    )
    entrypoint = blocking_entrypoint(tool, "add")
    assert entrypoint(1, 2) == "proxied"

    lease_url, lease_payload = captured_calls[0]
    assert lease_url.endswith("/leases")
    assert lease_payload["credential_overrides"] == {"access_token": expected_token}


@pytest.mark.parametrize(
    ("credential_policy", "expected_overrides"),
    [
        (None, {"api_key": "saved-key", "region": "eu"}),
        ({"calculator.add": ("openai",)}, {"api_key": "policy-key", "region": "eu"}),
    ],
)
def test_static_runner_proxy_leases_the_tool_own_saved_settings(
    monkeypatch: pytest.MonkeyPatch,
    credential_policy: dict[str, tuple[str, ...]] | None,
    expected_overrides: dict[str, str],
) -> None:
    """The shared runner has no credential store, so the primary leases the tool's saved settings per call."""
    captured_calls: list[tuple[str, dict[str, Any]]] = []
    fake_credentials = FakeCredentialsManager(
        {
            "calculator": {"api_key": "saved-key", "region": "eu", "_source": "ui"},
            "openai": {"api_key": "policy-key", "_source": "ui"},
        },
    )
    monkeypatch.delenv("MINDROOM_WORKER_BACKEND", raising=False)
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        execution_mode="all",
        credential_policy=credential_policy,
    )
    monkeypatch.setattr(
        "mindroom.tool_system.sandbox_proxy.httpx.Client",
        _recording_client_class(
            captured_calls=captured_calls,
            responder=lambda url, _json: (
                {"lease_id": "lease-123", "expires_at": 123.0, "max_uses": 1}
                if url.endswith("/leases")
                else {"ok": True, "result": "proxied"}
            ),
        ),
    )

    tool = get_tool_by_name(
        "calculator",
        runtime_paths,
        credentials_manager=fake_credentials,
        worker_target=None,
        tools=builtin_tool_registry(),
    )
    entrypoint = blocking_entrypoint(tool, "add")

    assert entrypoint(1, 2) == "proxied"
    assert [url.rsplit("/", 1)[-1] for url, _payload in captured_calls] == ["leases", "execute"]
    assert captured_calls[0][1]["credential_overrides"] == expected_overrides
    assert captured_calls[1][1]["lease_id"] == "lease-123"


@pytest.mark.usefixtures("restored_builtin_tools")
def test_a_setting_from_the_primary_env_file_never_reaches_the_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """The primary builds a routed tool with its `.env` setting, but only saved settings travel with the call."""
    captured_calls: list[tuple[str, dict[str, Any]]] = []
    tool_name = "test_env_file_setting_tool"

    class _EnvSettingToolkit(Toolkit):
        def __init__(self, token: str | None = None, label: str | None = None) -> None:
            self.token = token
            self.label = label
            super().__init__(name=tool_name, tools=[self.ping])

        def ping(self) -> str:
            return "local"

    @register_tool_with_metadata(
        name=tool_name,
        file_access=ToolFileAccess.NONE,
        display_name="Env File Setting Tool",
        description="Test-only coverage of `.env` settings on routed calls.",
        category=ToolCategory.DEVELOPMENT,
        config_fields=[
            ConfigField(name="token", label="Token", type="password", required=False, env=("PROBE_TOOL_TOKEN",)),
            ConfigField(name="label", label="Label", type="text", required=False),
        ],
    )
    def _env_setting_tool_factory() -> type[_EnvSettingToolkit]:
        return _EnvSettingToolkit

    monkeypatch.delenv("MINDROOM_WORKER_BACKEND", raising=False)
    monkeypatch.delenv("PROBE_TOOL_TOKEN", raising=False)
    runtime_paths = replace(
        _configure_proxy_runtime(
            monkeypatch,
            proxy_url="http://sandbox-runner:8765",
            execution_mode="selective",
            proxy_tools={tool_name},
            credential_policy={},
        ),
        env_file_values={"PROBE_TOOL_TOKEN": "env-file-secret"},
    )
    monkeypatch.setattr(
        "mindroom.tool_system.sandbox_proxy.httpx.Client",
        _recording_client_class(
            captured_calls=captured_calls,
            responder=lambda url, _json: (
                {"lease_id": "lease-123", "expires_at": 123.0, "max_uses": 1}
                if url.endswith("/leases")
                else {"ok": True, "result": "proxied"}
            ),
        ),
    )
    credentials = FakeCredentialsManager({tool_name: {"label": "saved-label", "_source": "ui"}})

    tool = get_tool_by_name(
        tool_name,
        runtime_paths,
        tools=builtin_tool_registry(),
        credentials_manager=credentials,
        worker_target=None,
    )
    assert (tool.token, tool.label) == ("env-file-secret", "saved-label")
    assert blocking_entrypoint(tool, "ping")() == "proxied"

    assert [url.rsplit("/", 1)[-1] for url, _payload in captured_calls] == ["leases", "execute"]
    assert captured_calls[0][1]["credential_overrides"] == {"label": "saved-label"}
    assert "env-file-secret" not in json.dumps(captured_calls)


def test_a_refused_lease_reports_the_runner_own_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """A runner that refuses a lease in its typed answer fails the call with its reason, and no call runs."""
    captured_calls: list[tuple[str, dict[str, Any]]] = []
    fake_credentials = FakeCredentialsManager({"calculator": {"api_key": "saved-key", "_source": "ui"}})
    monkeypatch.delenv("MINDROOM_WORKER_BACKEND", raising=False)
    runtime_paths = _configure_proxy_runtime(monkeypatch, proxy_url="http://sandbox-runner:8765", execution_mode="all")
    refusal = WorkerFailure(error="The primary speaks worker protocol 6, this worker 7.", failure_kind="worker")
    monkeypatch.setattr(
        "mindroom.tool_system.sandbox_proxy.httpx.Client",
        _recording_client_class(captured_calls=captured_calls, responder=lambda _url, _json: refusal.model_dump()),
    )

    tool = get_tool_by_name(
        "calculator",
        runtime_paths,
        credentials_manager=fake_credentials,
        worker_target=None,
        tools=builtin_tool_registry(),
    )
    entrypoint = blocking_entrypoint(tool, "add")

    with pytest.raises(RuntimeError, match=re.escape(refusal.error)):
        entrypoint(1, 2)
    assert [url.rsplit("/", 1)[-1] for url, _payload in captured_calls] == ["leases"]


@pytest.mark.parametrize(
    ("worker_backend", "expected"),
    [(None, True), ("static_runner", True), ("docker", False), ("kubernetes", False)],
)
def test_only_the_static_runner_leases_tool_own_saved_settings(
    monkeypatch: pytest.MonkeyPatch,
    worker_backend: str | None,
    expected: bool,
) -> None:
    """Only the static runner leases every tool's saved settings; dedicated workers lease only primary-owned ones."""
    if worker_backend is None:
        monkeypatch.delenv("MINDROOM_WORKER_BACKEND", raising=False)
    else:
        monkeypatch.setenv("MINDROOM_WORKER_BACKEND", worker_backend)
    runtime_paths = _configure_proxy_runtime(monkeypatch, proxy_url="http://sandbox-runner:8765")

    config = sandbox_proxy_module._worker_proxy_client_config(
        sandbox_proxy_module.sandbox_proxy_config(runtime_paths),
        runtime_paths,
    )

    assert config.lease_tool_credentials is expected


@pytest.mark.parametrize(
    ("worker_scope", "requester_id", "expected_key"),
    [
        (None, "@alice:example.org", "global-key"),
        ("shared", "@alice:example.org", "alpha-key"),
        ("user", "@alice:example.org", "alice-key"),
        ("user_agent", "@alice:example.org", "alice-alpha-key"),
        ("user", "@bob:example.org", None),
    ],
)
def test_dedicated_worker_calls_lease_the_callers_own_tool_settings(
    tmp_path: Path,
    worker_scope: str | None,
    requester_id: str,
    expected_key: str | None,
) -> None:
    """Dedicated-worker calls lease only their own scope's primary settings; unscoped calls lease the global ones."""
    captured_calls: list[tuple[str, dict[str, Any]]] = []
    handle = WorkerHandle(
        worker_id="worker-1",
        worker_key="agent:test",
        endpoint="http://worker/api/sandbox-runner/execute",
        auth_token=_TEST_AUTH_TOKEN,
        status="ready",
        backend_name="kubernetes",
        last_used_at=0.0,
        created_at=0.0,
    )
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", process_env={})
    manager = CredentialsManager(tmp_path / "credentials")
    manager.save_credentials("calculator", {"api_key": "global-key"})
    manager.for_primary_runtime_agent_scope("alpha").save_credentials("calculator", {"api_key": "alpha-key"})
    manager.for_primary_runtime_scope("@alice:example.org", None).save_credentials(
        "calculator",
        {"api_key": "alice-key"},
    )
    manager.for_primary_runtime_scope("@alice:example.org", "alpha").save_credentials(
        "calculator",
        {"api_key": "alice-alpha-key"},
    )
    identity = ToolExecutionIdentity("matrix", "alpha", requester_id, None, None, None, None)
    target = resolve_worker_target(worker_scope, "alpha", identity, tenant_id="test-tenant")

    result = execute_worker_proxy_request(
        config=WorkerProxyClientConfig(
            proxy_url=None,
            proxy_token=None,
            proxy_timeout_seconds=7.0,
            credential_lease_ttl_seconds=60,
            credential_policy={},
            lease_tool_credentials=False,
        ),
        request=_execute_request("calculator", "add"),
        credentials_manager=manager,
        worker_target=target,
        worker_handle=handle,
        worker_manager=_TrackingWorkerManager(),
        client_factory=_recording_client_class(
            captured_calls=captured_calls,
            responder=lambda url, _json: (
                {"lease_id": "lease-123", "expires_at": 123.0, "max_uses": 1}
                if url.endswith("/leases")
                else {"ok": True, "result": "sandbox-result"}
            ),
        ),
        primary_built_service=functools.partial(
            sandbox_proxy_module.primary_owns_tool_settings,
            tools=builtin_tool_registry(),
            runtime_paths=runtime_paths,
        ),
    )

    assert result == "sandbox-result"
    if expected_key is None:
        assert [url for url, _payload in captured_calls] == ["http://worker/api/sandbox-runner/execute"]
        assert captured_calls[0][1]["lease_id"] is None
    else:
        assert [url.rsplit("/", 1)[-1] for url, _payload in captured_calls] == ["leases", "execute"]
        assert captured_calls[0][1]["credential_overrides"] == {"api_key": expected_key}
        assert captured_calls[1][1]["lease_id"] == "lease-123"
    assert [payload["protocol"] for _url, payload in captured_calls] == [WORKER_PROTOCOL_VERSION] * len(captured_calls)


def test_scoped_calls_do_not_lease_settings_the_primary_does_not_own(tmp_path: Path) -> None:
    """Unknown names keep the worker's own store, so no lease is sent back to it."""
    tool_name = "unregistered_tool"
    captured_calls: list[tuple[str, dict[str, Any]]] = []
    handle = WorkerHandle(
        worker_id="worker-1",
        worker_key="agent:test",
        endpoint="http://worker/api/sandbox-runner/execute",
        auth_token=_TEST_AUTH_TOKEN,
        status="ready",
        backend_name="kubernetes",
        last_used_at=0.0,
        created_at=0.0,
    )
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", process_env={})
    manager = CredentialsManager(tmp_path / "credentials")
    target = resolve_worker_target("shared", "alpha", None, tenant_id="test-tenant")
    assert target.worker_key is not None
    manager.for_worker(target.worker_key).save_credentials(tool_name, {"api_key": "worker-planted"})

    execute_worker_proxy_request(
        config=WorkerProxyClientConfig(
            proxy_url=None,
            proxy_token=None,
            proxy_timeout_seconds=7.0,
            credential_lease_ttl_seconds=60,
            credential_policy={},
            lease_tool_credentials=False,
        ),
        request=_execute_request(tool_name, "run"),
        credentials_manager=manager,
        worker_target=target,
        worker_handle=handle,
        worker_manager=_TrackingWorkerManager(),
        client_factory=_recording_client_class(captured_calls=captured_calls),
        primary_built_service=functools.partial(
            sandbox_proxy_module.primary_owns_tool_settings,
            tools=builtin_tool_registry(),
            runtime_paths=runtime_paths,
        ),
    )

    assert [url for url, _payload in captured_calls] == ["http://worker/api/sandbox-runner/execute"]


def test_save_attachment_to_worker_posts_with_worker_token_and_size_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attachment worker saves should use the selected worker handle and reject oversized payloads locally."""
    captured: dict[str, Any] = {}
    manager = _TrackingWorkerManager()
    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", "kubernetes")
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url=None,
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="selective",
        proxy_tools={"file"},
    )
    execution_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
    )
    worker_target = _worker_target(runtime_paths, "shared", "code", execution_identity)
    payload_bytes = b"attachment-bytes"
    sha256 = hashlib.sha256(payload_bytes).hexdigest()

    monkeypatch.setattr(
        sandbox_proxy_module,
        "lease_primary_worker_manager",
        lambda *_args, **_kwargs: _static_worker_manager_lease(manager),
    )
    monkeypatch.setattr(
        "mindroom.tool_system.sandbox_proxy.httpx.Client",
        _recording_client_class(
            captured=captured,
            responder=lambda _url, _json: {
                "ok": True,
                "worker_path": "sample.bin",
                "size_bytes": len(payload_bytes),
                "sha256": sha256,
            },
        ),
    )

    receipt = sandbox_proxy_module.save_attachment_to_worker(
        runtime_paths=runtime_paths,
        runtime_config=None,
        worker_target=worker_target,
        workspace_root=runtime_paths.storage_root / "agents" / "code" / "workspace",
        attachment_id="att_sample",
        mindroom_output_path="sample.bin",
        payload_bytes=payload_bytes,
    )

    assert receipt is not None
    assert receipt.worker_path == "sample.bin"
    assert captured["url"] == "http://worker/api/sandbox-runner/save-attachment"
    assert captured["headers"] == {"x-mindroom-sandbox-token": _TEST_AUTH_TOKEN}
    request_json = captured["json"]
    assert request_json["attachment_id"] == "att_sample"
    assert request_json["mindroom_output_path"] == "sample.bin"
    assert request_json["worker_key"] == worker_target.worker_key
    assert base64.b64decode(request_json["bytes_b64"]) == payload_bytes
    assert request_json["sha256"] == sha256
    assert request_json["protocol"] == WORKER_PROTOCOL_VERSION
    # The attachments tool's workspace travels relative to the storage root the worker mounts.
    assert request_json["base_dir"] == "agents/code/workspace"
    assert "mime_type" not in request_json
    assert "filename" not in request_json
    assert manager.touched == [worker_target.worker_key]

    monkeypatch.setenv("MINDROOM_ATTACHMENT_INLINE_SAVE_MAX_BYTES", "3")
    with pytest.raises(RuntimeError, match="att_sample"):
        sandbox_proxy_module.save_attachment_to_worker(
            runtime_paths=_runtime_paths_from_env(),
            runtime_config=None,
            worker_target=worker_target,
            workspace_root=None,
            attachment_id="att_sample",
            mindroom_output_path="sample.bin",
            payload_bytes=payload_bytes,
        )


def test_inline_attachment_byte_limit_fits_every_retained_attachment_by_default(tmp_path: Path) -> None:
    """Every attachment MindRoom retains can reach a worker, and the worker can write it."""
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", process_env={})

    assert sandbox_proxy_module.inline_attachment_byte_limit(runtime_paths) == RETAINED_MEDIA_MAX_BYTES
    assert ToolOutputFilePolicy.from_runtime(tmp_path, runtime_paths).max_bytes >= RETAINED_MEDIA_MAX_BYTES


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("3", 3),
        ("not-a-number", RETAINED_MEDIA_MAX_BYTES),
        ("0", RETAINED_MEDIA_MAX_BYTES),
        ("-1", RETAINED_MEDIA_MAX_BYTES),
    ],
)
def test_inline_attachment_byte_limit_honors_valid_operator_overrides(
    tmp_path: Path,
    configured: str,
    expected: int,
) -> None:
    """A positive setting replaces the default; anything else falls back to it."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        process_env={"MINDROOM_ATTACHMENT_INLINE_SAVE_MAX_BYTES": configured},
    )

    assert sandbox_proxy_module.inline_attachment_byte_limit(runtime_paths) == expected


@pytest.mark.parametrize(
    ("process_env", "env_file", "expected"),
    [
        ({}, "MINDROOM_ATTACHMENT_INLINE_SAVE_MAX_BYTES=5\n", 5),
        ({"MINDROOM_ATTACHMENT_INLINE_SAVE_MAX_BYTES": "7"}, "MINDROOM_ATTACHMENT_INLINE_SAVE_MAX_BYTES=5\n", 7),
        ({}, "", RETAINED_MEDIA_MAX_BYTES),
    ],
    ids=["env_file", "process_env_wins", "live_environ_ignored"],
)
def test_inline_attachment_byte_limit_reads_the_runtime_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    process_env: dict[str, str],
    env_file: str,
    expected: int,
) -> None:
    """The limit resolves like every runtime setting: the runtime's process env, then its `.env`, then the default."""
    monkeypatch.setenv("MINDROOM_ATTACHMENT_INLINE_SAVE_MAX_BYTES", "11")
    (tmp_path / ".env").write_text(env_file, encoding="utf-8")
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", process_env=process_env)

    assert sandbox_proxy_module.inline_attachment_byte_limit(runtime_paths) == expected


def test_view_file_from_worker_posts_and_decodes_bounded_media(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Primary file viewing should route to the selected worker and restore image bytes."""
    captured: dict[str, Any] = {}
    manager = _TrackingWorkerManager()
    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", "kubernetes")
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url=None,
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="selective",
        proxy_tools={"file"},
    )
    execution_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
    )
    worker_target = _worker_target(runtime_paths, "shared", "code", execution_identity)
    envelope = encode_media_result(
        ToolValue(content='{"view_status":"ready"}', images=(Image(content=b"png", mime_type="image/png"),)),
    )
    monkeypatch.setattr(
        sandbox_proxy_module,
        "lease_primary_worker_manager",
        lambda *_args, **_kwargs: _static_worker_manager_lease(manager),
    )
    monkeypatch.setattr(
        "mindroom.tool_system.sandbox_proxy.httpx.Client",
        _recording_client_class(
            captured=captured,
            responder=lambda _url, _json: {"ok": True, "result": envelope},
        ),
    )

    workspace_root = runtime_paths.storage_root / "agents" / "code" / "workspace"
    result = sandbox_proxy_module.view_file_from_worker(
        runtime_paths=runtime_paths,
        runtime_config=None,
        worker_target=worker_target,
        workspace_root=workspace_root,
        path=str(workspace_root / "plots" / "result.png"),
    )

    assert result is not None
    assert result.images
    assert result.images[0].content == b"png"
    assert captured["url"] == "http://worker/api/sandbox-runner/view-file"
    assert captured["json"]["path"] == "plots/result.png"
    assert captured["json"]["worker_key"] == worker_target.worker_key
    assert captured["json"]["protocol"] == WORKER_PROTOCOL_VERSION
    assert captured["json"]["base_dir"] == "agents/code/workspace"
    assert "tool_init_overrides" not in captured["json"]
    assert manager.touched == [worker_target.worker_key]


@pytest.mark.asyncio
@pytest.mark.parametrize("non_object", [True, False])
async def test_view_file_reports_malformed_worker_responses(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    non_object: bool,
) -> None:
    """Malformed worker replies remain structured file-view errors at the public tool boundary."""
    from mindroom.custom_tools.attachments import AttachmentTools  # noqa: PLC0415

    manager = _TrackingWorkerManager()
    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", "kubernetes")
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url=None,
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="selective",
        proxy_tools={"file"},
    )
    execution_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
    )
    worker_target = _worker_target(runtime_paths, "shared", "code", execution_identity)
    reply = ["unexpected"] if non_object else {"ok": True, "result": encode_media_result({"value": "not media"})}
    monkeypatch.setattr(
        sandbox_proxy_module,
        "lease_primary_worker_manager",
        lambda *_args, **_kwargs: _static_worker_manager_lease(manager),
    )
    monkeypatch.setattr(
        "mindroom.tool_system.sandbox_proxy.httpx.Client",
        _recording_client_class(captured={}, responder=lambda _url, _json: reply),
    )
    toolkit = AttachmentTools(runtime_paths=runtime_paths, worker_target=worker_target, workspace_in_worker=True)

    with tool_runtime_context(_tool_context(tmp_path)):
        result = await toolkit.view_file(path="plots/result.png")

    assert not result.images
    receipt = json.loads(result.content)
    assert receipt["view_status"] == "error"
    assert receipt["path"] == "plots/result.png"
    assert receipt["message"] == (
        "Sandbox view-file returned a malformed response: 1 invalid field(s)."
        if non_object
        else "Sandbox view-file returned a non-media result."
    )


@pytest.mark.parametrize(
    ("response_payload", "expected_error"),
    [
        (
            {"ok": True, "worker_path": "sample.bin", "size_bytes": 999, "sha256": "0" * 64},
            "size does not match",
        ),
        (
            {"ok": True, "worker_path": "sample.bin", "size_bytes": 16, "sha256": "0" * 64},
            "SHA256 does not match",
        ),
        (
            {"ok": True, "worker_path": "sample.bin", "size_bytes": "sixteen", "sha256": "0" * 64},
            "malformed response",
        ),
        (
            {"ok": True, "worker_path": "sample.bin", "size_bytes": 16},
            "missing its receipt fields",
        ),
        (
            {"ok": True, "worker_path": "sample.bin", "size_bytes": -1, "sha256": "0" * 64},
            "missing its receipt fields",
        ),
    ],
)
def test_save_attachment_to_worker_rejects_bad_receipts(
    monkeypatch: pytest.MonkeyPatch,
    response_payload: dict[str, object],
    expected_error: str,
) -> None:
    """Primary saves should verify worker receipts against the bytes sent."""
    manager = _TrackingWorkerManager()
    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", "kubernetes")
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url=None,
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="selective",
        proxy_tools={"file"},
    )
    execution_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
    )
    worker_target = _worker_target(runtime_paths, "shared", "code", execution_identity)

    monkeypatch.setattr(
        sandbox_proxy_module,
        "lease_primary_worker_manager",
        lambda *_args, **_kwargs: _static_worker_manager_lease(manager),
    )
    monkeypatch.setattr(
        "mindroom.tool_system.sandbox_proxy.httpx.Client",
        _recording_client_class(responder=lambda _url, _json: response_payload),
    )

    with pytest.raises(RuntimeError, match=expected_error):
        sandbox_proxy_module.save_attachment_to_worker(
            runtime_paths=runtime_paths,
            runtime_config=None,
            worker_target=worker_target,
            workspace_root=None,
            attachment_id="att_sample",
            mindroom_output_path="sample.bin",
            payload_bytes=b"attachment-bytes",
        )

    assert manager.failures
    assert manager.touched == []


def test_save_attachment_to_worker_request_failure_does_not_record_worker_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Structured tool/request failures from save-attachment should not poison worker health."""
    manager = _TrackingWorkerManager()
    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", "kubernetes")
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url=None,
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="selective",
        proxy_tools={"file"},
    )
    execution_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
    )
    worker_target = _worker_target(runtime_paths, "shared", "code", execution_identity)

    monkeypatch.setattr(
        sandbox_proxy_module,
        "lease_primary_worker_manager",
        lambda *_args, **_kwargs: _static_worker_manager_lease(manager),
    )
    monkeypatch.setattr(
        "mindroom.tool_system.sandbox_proxy.httpx.Client",
        _recording_client_class(
            responder=lambda _url, _json: {
                "ok": False,
                "error": "mindroom_output_path must stay inside the workspace.",
                "failure_kind": "tool",
            },
        ),
    )

    with pytest.raises(RuntimeError, match="mindroom_output_path"):
        sandbox_proxy_module.save_attachment_to_worker(
            runtime_paths=runtime_paths,
            runtime_config=None,
            worker_target=worker_target,
            workspace_root=None,
            attachment_id="att_sample",
            mindroom_output_path="../escape.bin",
            payload_bytes=b"attachment-bytes",
        )

    assert manager.failures == []
    assert manager.touched == [worker_target.worker_key]


def _sandbox_proxy_test_metadata(
    name: str,
    *,
    default_execution_target: ToolExecutionTarget = ToolExecutionTarget.PRIMARY,
    consumes_workspace_paths: bool = False,
    requires_primary_runtime: bool = False,
    requires_room_context: bool = False,
) -> ToolMetadata:
    return ToolMetadata(
        name=name,
        file_access=ToolFileAccess.NONE,
        display_name=name,
        description=name,
        category=ToolCategory.DEVELOPMENT,
        default_execution_target=default_execution_target,
        consumes_workspace_paths=consumes_workspace_paths,
        requires_primary_runtime=requires_primary_runtime,
        requires_room_context=requires_room_context,
    )


def test_default_proxy_routing_uses_worker_execution_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default fail-closed proxy routing should come from tool metadata, not a hardcoded name set."""
    tool_name = "metadata_worker_execution_test"
    register_test_tool(
        monkeypatch,
        _sandbox_proxy_test_metadata(
            tool_name,
            default_execution_target=ToolExecutionTarget.WORKER,
        ),
    )
    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", "kubernetes")
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url=None,
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode=None,
    )

    assert _runs_in_worker(runtime_paths, tool_name, None) is True


def test_declared_primary_runtime_requirement_cannot_be_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    """A catalog declaration must win over global and per-agent worker routing requests."""
    tool_name = "declared_primary_runtime_test"
    register_test_tool(
        monkeypatch,
        _sandbox_proxy_test_metadata(tool_name, requires_primary_runtime=True),
    )
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox:8765",
        execution_mode="all",
    )

    assert builtin_tool_registry().entries[tool_name].requires_primary_runtime is True
    assert _runs_in_worker(runtime_paths, tool_name, None) is False
    assert _runs_in_worker(runtime_paths, tool_name, [tool_name]) is False


@pytest.mark.parametrize(
    ("requires_primary_runtime", "requires_room_context", "runner_mode", "expected"),
    [
        pytest.param(True, False, False, True, id="primary-runtime"),
        pytest.param(False, True, False, True, id="room-context"),
        pytest.param(False, False, False, True, id="worker-routable"),
        pytest.param(False, False, True, False, id="inside-worker"),
    ],
)
def test_primary_owns_settings_of_every_registered_tool(
    monkeypatch: pytest.MonkeyPatch,
    requires_primary_runtime: bool,
    requires_room_context: bool,
    runner_mode: bool,
    expected: bool,
) -> None:
    """The primary owns every tool's settings, wherever its calls run; a worker runtime owns its own."""
    register_test_tool(
        monkeypatch,
        _sandbox_proxy_test_metadata(
            "ownership_test",
            requires_primary_runtime=requires_primary_runtime,
            requires_room_context=requires_room_context,
        ),
    )
    runtime_paths = _configure_proxy_runtime(monkeypatch, proxy_url="http://sandbox:8765", runner_mode=runner_mode)

    assert (
        sandbox_proxy_module.primary_owns_tool_settings(
            "ownership_test",
            builtin_tool_registry(),
            runtime_paths=runtime_paths,
        )
        is expected
    )
    assert not sandbox_proxy_module.primary_owns_tool_settings(
        "unregistered_test",
        builtin_tool_registry(),
        runtime_paths=runtime_paths,
    )


@pytest.mark.parametrize("tool_name", ["openai", "groq"])
def test_primary_built_provider_tool_ignores_worker_written_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    tool_name: str,
) -> None:
    """A tool named like a model provider is still built by the primary, so the worker store never configures it."""
    runtime_paths = _configure_proxy_runtime(monkeypatch, proxy_url="http://sandbox:8765")
    manager = CredentialsManager(tmp_path / "credentials")
    target = resolve_worker_target("shared", "alpha", None, tenant_id="test-tenant")
    assert target.worker_key is not None
    manager.for_primary_runtime_agent_scope("alpha").save_credentials(tool_name, {"api_key": "operator-key"})
    manager.for_worker(target.worker_key).save_credentials(tool_name, {"api_key": "worker-planted"})
    captured: dict[str, object] = {}

    class _FakeProviderTools(OwnedToolkit):
        def __init__(self, *, api_key: str | None = None, **_: object) -> None:
            captured["api_key"] = api_key
            super().__init__(name=tool_name)

    replace_test_tool_factory(monkeypatch, tool_name, lambda: _FakeProviderTools)

    get_tool_by_name(
        tool_name,
        runtime_paths,
        credentials_manager=manager,
        runs_in_worker=False,
        worker_target=target,
        tools=builtin_tool_registry(),
    )

    assert captured["api_key"] == "operator-key"


def test_primary_default_tool_remains_explicitly_worker_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A primary default alone must not become a non-overridable primary-runtime restriction."""
    tool_name = "primary_default_override_test"
    register_test_tool(monkeypatch, _sandbox_proxy_test_metadata(tool_name))
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox:8765",
        execution_mode="off",
    )

    assert builtin_tool_registry().entries[tool_name].default_execution_target is ToolExecutionTarget.PRIMARY
    assert builtin_tool_registry().entries[tool_name].requires_primary_runtime is False
    assert _runs_in_worker(runtime_paths, tool_name, [tool_name]) is True


def _attachments_use_worker(runtime_paths: RuntimePaths, *, tools: list[str], worker_tools: list[str]) -> bool:
    """Return whether attachments of an agent with ``tools`` and ``worker_tools`` save where its workspace tools run."""
    config = Config(
        agents={"code": AgentConfig(display_name="Code", tools=tools, worker_tools=worker_tools)},
        models={},
    )
    agent_tools = resolve_agent_tools(config, "code", builtin_tool_registry())
    return agent_tools.workspace_in_worker(
        agent_tools.tool_names,
        sandbox_proxy_module.tool_placement(runtime_paths, builtin_tool_registry()),
    )


def test_attachment_save_uses_workspace_consumer_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """Attachment saves should follow workspace-consumer metadata, not a hardcoded name set."""
    consumer_tool = "metadata_workspace_consumer_test"
    non_consumer_tool = "metadata_non_workspace_consumer_test"
    for tool_name, consumes_workspace_paths in ((consumer_tool, True), (non_consumer_tool, False)):
        metadata = _sandbox_proxy_test_metadata(
            tool_name,
            default_execution_target=ToolExecutionTarget.WORKER,
            consumes_workspace_paths=consumes_workspace_paths,
        )
        register_test_tool(monkeypatch, replace(metadata, factory=lambda: Toolkit))
    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", "kubernetes")
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url=None,
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="off",
    )
    tools = [consumer_tool, non_consumer_tool]

    assert _attachments_use_worker(runtime_paths, tools=tools, worker_tools=[consumer_tool]) is True
    assert _attachments_use_worker(runtime_paths, tools=tools, worker_tools=[non_consumer_tool]) is False


@pytest.mark.parametrize(
    "worker_tools",
    [["coding"], ["docker"], ["python"], ["shell", "coding"], ["browser_mcp"]],
)
def test_attachment_save_uses_worker_for_worker_routed_workspace_consumers(
    monkeypatch: pytest.MonkeyPatch,
    worker_tools: list[str],
) -> None:
    """Attachment saves follow the agent's own worker-routed workspace consumers, not only file workspaces.

    A ``worker_tools`` entry naming a workspace consumer the agent does not have sends nothing to the worker.
    """
    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", "kubernetes")
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url=None,
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="off",
    )
    tools = [*worker_tools, "calculator"]

    assert _attachments_use_worker(runtime_paths, tools=tools, worker_tools=worker_tools) is True
    assert _attachments_use_worker(runtime_paths, tools=tools, worker_tools=["calculator"]) is False
    assert _attachments_use_worker(runtime_paths, tools=["calculator"], worker_tools=worker_tools) is False


def test_save_attachment_to_static_runner_posts_without_worker_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Static proxy saves should use the shared runner endpoint without forcing a worker key."""
    captured: dict[str, Any] = {}
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="selective",
        proxy_tools={"file"},
    )
    execution_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
    )
    worker_target = _worker_target(runtime_paths, None, "code", execution_identity)
    payload_bytes = b"attachment-bytes"
    sha256 = hashlib.sha256(payload_bytes).hexdigest()

    monkeypatch.setattr(
        "mindroom.tool_system.sandbox_proxy.httpx.Client",
        _recording_client_class(
            captured=captured,
            responder=lambda _url, _json: {
                "ok": True,
                "worker_path": "sample.bin",
                "size_bytes": len(payload_bytes),
                "sha256": sha256,
            },
        ),
    )

    workspace_root = runtime_paths.storage_root / "agents" / "code" / "workspace"
    receipt = sandbox_proxy_module.save_attachment_to_worker(
        runtime_paths=runtime_paths,
        runtime_config=None,
        worker_target=worker_target,
        workspace_root=workspace_root,
        attachment_id="att_sample",
        mindroom_output_path="sample.bin",
        payload_bytes=payload_bytes,
    )

    assert receipt is not None
    assert captured["url"] == "http://sandbox-runner:8765/api/sandbox-runner/save-attachment"
    assert captured["json"]["worker_key"] is None
    assert captured["json"]["routing_agent_name"] == "code"
    # A request that names no worker keeps the workspace in this primary's own spelling.
    assert captured["json"]["base_dir"] == str(workspace_root)


def test_get_tool_by_name_rejects_unsafe_tool_init_overrides() -> None:
    """Tool init overrides should allow only the explicit safe whitelist."""
    with pytest.raises(ToolInitOverrideError, match="api_key"):
        get_tool_by_name(
            "openai",
            _runtime_paths_from_env(),
            tool_init_overrides={"api_key": "sk-test"},
            worker_target=None,
            tools=builtin_tool_registry(),
        )


def test_get_tool_by_name_rejects_invalid_base_dir_override_type() -> None:
    """base_dir overrides should be validated before toolkit construction."""
    with pytest.raises(ToolInitOverrideError, match="base_dir"):
        get_tool_by_name(
            "coding",
            _runtime_paths_from_env(),
            tool_init_overrides={"base_dir": {"bad": "value"}},
            worker_target=None,
            tools=builtin_tool_registry(),
        )


def test_get_tool_by_name_loads_persisted_non_secret_file_config(tmp_path: Path) -> None:
    """Persisted plain config should still hydrate SetupType.NONE tools during rebuilds."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("models: {}\nagents: {}\n", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={},
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    save_scoped_credentials(
        "file",
        {
            "base_dir": str(workspace),
            "enable_delete_file": True,
        },
        credentials_manager=credentials_manager,
        worker_target=None,
    )

    tool = get_tool_by_name("file", runtime_paths, worker_target=None, tools=builtin_tool_registry())

    assert tool.base_dir == workspace.resolve()
    assert "delete_file" in tool.functions


def test_get_tool_by_name_builds_google_bigquery_from_scoped_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Google BigQuery should be configured from persisted tool credentials."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("models: {}\nagents: {}\n", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={},
    )
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    save_scoped_credentials(
        "google_bigquery",
        {
            "dataset": "demo_dataset",
            "project": "demo-project",
            "location": "us-central1",
        },
        credentials_manager=credentials_manager,
        worker_target=None,
    )
    captured: dict[str, object] = {}

    class _FakeGoogleBigQueryTools(OwnedToolkit):
        def __init__(
            self,
            *,
            dataset: str,
            project: str,
            location: str,
            runtime_paths: RuntimePaths,
            credentials_manager: CredentialsManager,
            worker_target: object | None,
            **_: object,
        ) -> None:
            self.dataset = dataset
            self.project = project
            self.location = location
            captured["runtime_paths"] = runtime_paths
            captured["credentials_manager"] = credentials_manager
            captured["worker_target"] = worker_target
            super().__init__(name="google_bigquery")

    replace_test_tool_factory(monkeypatch, "google_bigquery", lambda: _FakeGoogleBigQueryTools)

    tool = get_tool_by_name("google_bigquery", runtime_paths, worker_target=None, tools=builtin_tool_registry())

    assert tool.dataset == "demo_dataset"
    assert tool.project == "demo-project"
    assert tool.location == "us-central1"
    assert captured["runtime_paths"] is runtime_paths
    assert captured["credentials_manager"] is not None
    assert captured["worker_target"] is None


def test_primary_built_tool_ignores_worker_written_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A tool the primary builds for an agent reads that agent's primary settings, never its worker store."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_SANDBOX_PROXY_URL": "http://sandbox:8765", "MINDROOM_SANDBOX_PROXY_TOKEN": "token"},
    )
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    target = resolve_worker_target("shared", "alpha", None, tenant_id="test-tenant")
    assert target.worker_key is not None
    settings = {"db_name": "demo", "user": "alpha"}
    credentials_manager.for_primary_runtime_agent_scope("alpha").save_credentials(
        "postgres",
        {**settings, "host": "primary.example.test"},
    )
    credentials_manager.for_worker(target.worker_key).save_credentials(
        "postgres",
        {**settings, "host": "worker.example.test"},
    )
    captured: dict[str, object] = {}

    class _FakePostgresTools(OwnedToolkit):
        def __init__(self, *, host: str, **_: object) -> None:
            captured["host"] = host
            super().__init__(name="postgres")

    replace_test_tool_factory(monkeypatch, "postgres", lambda: _FakePostgresTools)

    get_tool_by_name(
        "postgres",
        runtime_paths,
        credentials_manager=credentials_manager,
        runs_in_worker=False,
        worker_target=target,
        tools=builtin_tool_registry(),
    )

    assert captured["host"] == "primary.example.test"


def test_routed_browser_ignores_worker_planted_desktop_target(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A worker-routed browser still runs desktop calls in the primary, so worker settings must not configure it."""
    runtime_paths = _configure_proxy_runtime(monkeypatch, proxy_url="http://sandbox:8765", execution_mode="all")
    manager = CredentialsManager(tmp_path / "credentials")
    target = resolve_worker_target("shared", "alpha", None, tenant_id="test-tenant")
    assert target.worker_key is not None
    manager.for_worker(target.worker_key).save_credentials(
        "browser",
        {
            "default_target": "desktop",
            "device_user_id": "@attacker:example.test",
            "device_id": "PLANTED",
            "device_ed25519": "planted-key",
        },
    )

    tool = get_tool_by_name(
        "browser",
        runtime_paths,
        credentials_manager=manager,
        runs_in_worker=_runs_in_worker(runtime_paths, "browser", ["browser"]),
        worker_target=target,
        tools=builtin_tool_registry(),
    )

    assert _runs_in_worker(runtime_paths, "browser", ["browser"])
    assert tool._default_target == "host"
    assert tool._desktop_target is None


def test_a_worker_routed_browser_toolkit_is_collected_once_its_functions_are_released(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A routed toolkit whose calls the primary may keep, as the browser's desktop calls stay, outlives no run."""
    runtime_paths = _configure_proxy_runtime(monkeypatch, proxy_url="http://sandbox:8765", execution_mode="all")
    tool = get_tool_by_name(
        "browser",
        runtime_paths,
        runs_in_worker=True,
        worker_target=None,
        tools=builtin_tool_registry(),
    )
    assert isinstance(tool, SupportsPrimaryCallPlacement)
    functions = adapt_toolkit(tool).functions
    released = weakref.ref(tool)
    released_function = weakref.ref(functions[0].entrypoint)

    del tool, functions
    gc.collect()

    assert released() is None
    assert released_function() is None


def test_get_tool_by_name_does_not_expose_runtime_env_to_direct_python_execution(tmp_path: Path) -> None:
    """Direct in-process Python execution should not emulate committed runtime env."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-6-astra\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={
            "MINDROOM_UNSAFE_ALLOW_LOCAL_EXECUTION_TOOLS": "true",
            "OPENAI_BASE_URL": "http://example.invalid/v1",
            "MINDROOM_NAMESPACE": "alpha1234",
        },
    )

    tool = get_tool_by_name("python", runtime_paths, worker_target=None, tools=builtin_tool_registry())
    entrypoint = blocking_entrypoint(tool, "run_python_code")

    result = entrypoint(
        'import os\nresult = {"openai_base_url": os.environ.get("OPENAI_BASE_URL"), "namespace": os.environ.get("MINDROOM_NAMESPACE"), "storage": os.environ.get("MINDROOM_STORAGE_PATH")}',
        "result",
    )

    assert ast.literal_eval(result) == {
        "openai_base_url": None,
        "namespace": None,
        "storage": None,
    }


def test_get_tool_by_name_does_not_expose_runtime_env_to_file_backed_python_execution(tmp_path: Path) -> None:
    """Direct file-backed Python execution should also avoid runtime env emulation."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-6-astra\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={
            "MINDROOM_UNSAFE_ALLOW_LOCAL_EXECUTION_TOOLS": "true",
            "OPENAI_BASE_URL": "http://example.invalid/v1",
            "MINDROOM_NAMESPACE": "alpha1234",
        },
    )

    tool = get_tool_by_name(
        "python",
        runtime_paths,
        tool_init_overrides={"base_dir": str(tmp_path)},
        worker_target=None,
        tools=builtin_tool_registry(),
    )
    save_entrypoint = blocking_entrypoint(tool, "save_to_file_and_run")
    run_file_entrypoint = blocking_entrypoint(tool, "run_python_file_return_variable")

    code = (
        "import os\n"
        'result = {"openai_base_url": os.environ.get("OPENAI_BASE_URL"), '
        '"namespace": os.environ.get("MINDROOM_NAMESPACE"), '
        '"storage": os.environ.get("MINDROOM_STORAGE_PATH")}'
    )
    save_result = save_entrypoint("runtime_values.py", code, "result")
    run_result = run_file_entrypoint("runtime_values.py", "result")
    expected = {
        "openai_base_url": None,
        "namespace": None,
        "storage": None,
    }

    assert ast.literal_eval(save_result) == expected
    assert ast.literal_eval(run_result) == expected


def test_shell_subprocess_path_keeps_existing_path_without_prepend() -> None:
    """Shell PATH normalization should preserve the base PATH when nothing is prepended."""
    assert subprocess_path_with_prepends("/usr/local/bin:/usr/bin:/bin") == "/usr/local/bin:/usr/bin:/bin"


def test_shell_subprocess_path_uses_only_prepend_entries_for_empty_path() -> None:
    """Configured path entries should still be available when PATH is empty."""
    assert (
        subprocess_path_with_prepends(
            "",
            prepend_entries=("/opt/custom/bin", "/opt/worker/bin"),
        )
        == "/opt/custom/bin:/opt/worker/bin"
    )


def test_shell_subprocess_path_prepends_configured_entries_and_dedupes() -> None:
    """Configured path entries should stay first without duplicating existing PATH entries."""
    assert (
        subprocess_path_with_prepends(
            "/usr/bin:/opt/existing/bin:/bin",
            prepend_entries=("/opt/custom/bin", "/opt/existing/bin"),
        )
        == "/opt/custom/bin:/opt/existing/bin:/usr/bin:/bin"
    )


def test_shell_subprocess_env_path_passthrough_without_prepend(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Shell PATH normalization should preserve the runtime PATH by default."""
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-6-astra\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={},
    )

    runtime_env = dict(shell_execution_runtime_env_values(runtime_paths))
    assert (
        shell_tool_module._shell_subprocess_env(runtime_env, call_environment=ToolEnvironment())["PATH"]
        == "/usr/local/bin:/usr/bin"
    )


def test_shell_subprocess_env_prefers_explicit_base_process_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shell subprocess env should come from the explicit request snapshot, not live runner state."""
    monkeypatch.setenv("PATH", "/runner/bin")
    monkeypatch.setenv("HOME", "/runner-home")

    env = shell_tool_module._shell_subprocess_env(
        {},
        call_environment=ToolEnvironment(),
        base_process_env={"PATH": "/request/bin", "HOME": "/request-home"},
    )

    assert env["PATH"] == "/request/bin"
    assert env["HOME"] == "/request-home"


def test_shell_subprocess_env_forces_vendor_telemetry_over_runtime_env() -> None:
    """Shell subprocesses should not let request-scoped env re-enable vendor telemetry."""
    runtime_env = dict.fromkeys(VENDOR_TELEMETRY_ENV_VALUES, "enabled")

    env = shell_tool_module._shell_subprocess_env(runtime_env, call_environment=ToolEnvironment())

    for name, value in VENDOR_TELEMETRY_ENV_VALUES.items():
        assert env[name] == value


def test_subprocess_env_for_request_forces_vendor_telemetry_over_execution_env() -> None:
    """Sandbox subprocess env should not let execution overlays re-enable vendor telemetry."""
    base_env = dict.fromkeys(VENDOR_TELEMETRY_ENV_VALUES, "base-enabled")
    execution_env = dict.fromkeys(VENDOR_TELEMETRY_ENV_VALUES, "request-enabled")
    execution_env["MINDROOM_KEEP"] = "from-request"

    env = sandbox_exec_module.subprocess_env_for_request(base_env, execution_env)

    assert env is not None
    assert env["MINDROOM_KEEP"] == "from-request"
    for name, value in VENDOR_TELEMETRY_ENV_VALUES.items():
        assert env[name] == value


def test_shell_subprocess_env_preserves_workspace_home_contract_names() -> None:
    """Workspace-home env names should survive into the shell command process."""
    env = shell_tool_module._shell_subprocess_env(
        {
            "HOME": "/env-home",
            "MINDROOM_AGENT_WORKSPACE": "/env-agent-workspace",
            "PIP_CACHE_DIR": "/env-pip-cache",
            "PYTHONPYCACHEPREFIX": "/env-pycache",
            "UV_CACHE_DIR": "/env-uv-cache",
            "VIRTUAL_ENV": "/env-venv",
            "XDG_CACHE_HOME": "/env-cache",
            "XDG_CONFIG_HOME": "/env-config",
            "XDG_DATA_HOME": "/env-data",
            "XDG_STATE_HOME": "/env-state",
        },
        call_environment=ToolEnvironment(),
        base_process_env={
            "HOME": "/workspace",
            "MINDROOM_AGENT_WORKSPACE": "/workspace",
            "PIP_CACHE_DIR": "/worker-cache/pip",
            "PYTHONPYCACHEPREFIX": "/worker-cache/pycache",
            "UV_CACHE_DIR": "/worker-cache/uv",
            "VIRTUAL_ENV": "/worker-venv",
            "XDG_CACHE_HOME": "/worker-cache",
            "XDG_CONFIG_HOME": "/workspace/.config",
            "XDG_DATA_HOME": "/workspace/.local/share",
            "XDG_STATE_HOME": "/workspace/.local/state",
        },
    )

    assert env["HOME"] == "/workspace"
    assert env["MINDROOM_AGENT_WORKSPACE"] == "/workspace"
    assert env["XDG_CONFIG_HOME"] == "/workspace/.config"
    assert env["XDG_DATA_HOME"] == "/workspace/.local/share"
    assert env["XDG_STATE_HOME"] == "/workspace/.local/state"
    assert env["XDG_CACHE_HOME"] == "/worker-cache"
    assert env["PIP_CACHE_DIR"] == "/worker-cache/pip"
    assert env["UV_CACHE_DIR"] == "/worker-cache/uv"
    assert env["PYTHONPYCACHEPREFIX"] == "/worker-cache/pycache"
    assert env["VIRTUAL_ENV"] == "/worker-venv"


def test_shell_subprocess_env_prefers_runtime_env_when_no_workspace_contract() -> None:
    """Direct shell runtime env should not be overwritten by ordinary process env."""
    env = shell_tool_module._shell_subprocess_env(
        {
            "HOME": "/runtime-home",
            "XDG_CONFIG_HOME": "/runtime-config",
            "VIRTUAL_ENV": "/runtime-venv",
        },
        call_environment=ToolEnvironment(),
        base_process_env={
            "HOME": "/process-home",
            "XDG_CONFIG_HOME": "/process-config",
            "VIRTUAL_ENV": "/process-venv",
        },
    )

    assert env["HOME"] == "/runtime-home"
    assert env["XDG_CONFIG_HOME"] == "/runtime-config"
    assert env["VIRTUAL_ENV"] == "/runtime-venv"


def test_execution_env_payload_denies_provider_env_by_default_in_isolated_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Isolated execution should not inherit provider env or arbitrary runtime `.env` values by default."""
    monkeypatch.setenv("OPENAI_API_KEY", "env-openai-key")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-6-astra\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "MINDROOM_NAMESPACE=alpha1234\nTEST_EXECUTION_ENV=visible-in-shell\nOPENAI_BASE_URL=http://example.invalid/v1\nCUSTOM_API_TOKEN=custom-secret\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env=dict(os.environ),
    )

    execution_env = sandbox_proxy_module._execution_env_payload("python", runtime_paths=runtime_paths)

    assert execution_env is not None
    assert execution_env["MINDROOM_NAMESPACE"] == "alpha1234"
    assert "TEST_EXECUTION_ENV" not in execution_env
    assert "OPENAI_API_KEY" not in execution_env
    assert "OPENAI_BASE_URL" not in execution_env
    assert "CUSTOM_API_TOKEN" not in execution_env


def test_worker_env_excludes_openai_api_key_unless_extra_env_passthrough(
    tmp_path: Path,
) -> None:
    """Isolated worker startup env should not inherit provider API keys by default."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={
            "OPENAI_API_KEY": "sk-primary",
            "ANTHROPIC_API_KEY": "sk-ant-primary",
        },
    )

    worker_paths = isolated_runtime_paths(runtime_paths)
    shell_env = build_execution_tool_env(
        "shell",
        worker_paths,
        extra_env_passthrough="OPENAI_API_KEY",
        shell_process_env=runtime_paths.process_env,
    )

    assert worker_paths.env_value("OPENAI_API_KEY") is None
    assert worker_paths.env_value("ANTHROPIC_API_KEY") is None
    assert shell_env["OPENAI_API_KEY"] == "sk-primary"


def test_worker_env_includes_extra_env_passthrough(tmp_path: Path) -> None:
    """Explicit shell passthrough should still expose selected process env values."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={
            "MY_VAR": "visible",
            "OTHER_VAR": "hidden",
        },
    )

    worker_paths = isolated_runtime_paths(runtime_paths)
    shell_env = build_execution_tool_env(
        "shell",
        worker_paths,
        extra_env_passthrough="MY_VAR",
        shell_process_env=runtime_paths.process_env,
    )

    assert worker_paths.env_value("MY_VAR") is None
    assert shell_env["MY_VAR"] == "visible"
    assert "OTHER_VAR" not in shell_env


@pytest.mark.asyncio
async def test_get_tool_by_name_exposes_runtime_env_to_shell_execution(tmp_path: Path) -> None:
    """Direct shell execution should inherit committed runtime env values from the runtime `.env`."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-6-astra\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("TEST_EXECUTION_ENV=visible-in-shell\n", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={},
    )

    tool = get_tool_by_name(
        "shell",
        runtime_paths,
        runs_in_worker=False,
        worker_target=None,
        tools=builtin_tool_registry(),
    )
    entrypoint = _offered_entrypoint(tool, "run_shell_command")

    result = await entrypoint(
        [
            sys.executable,
            "-c",
            'import os, sys; sys.stdout.write(os.environ.get("TEST_EXECUTION_ENV", ""))',
        ],
    )

    assert result == "visible-in-shell"


@pytest.mark.asyncio
async def test_local_shell_exposes_configured_extra_parent_env_without_leaking_control_secrets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Direct shell execution should allow explicit extra passthrough without leaking control secrets."""
    monkeypatch.setenv("GITEA_TOKEN", "visible-gitea-token")
    monkeypatch.setenv("MINDROOM_SANDBOX_PROXY_TOKEN", "runner-secret")
    monkeypatch.setenv("CI_JOB_TOKEN", "ci-secret")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-6-astra\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("TEST_EXECUTION_ENV=visible-in-shell\n", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env=dict(os.environ),
    )
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    save_scoped_credentials(
        "shell",
        {"extra_env_passthrough": "GITEA_*, MINDROOM_*, CI_JOB_TOKEN"},
        credentials_manager=credentials_manager,
        worker_target=None,
    )

    tool = get_tool_by_name(
        "shell",
        runtime_paths,
        runs_in_worker=False,
        worker_target=None,
        tools=builtin_tool_registry(),
    )
    entrypoint = _offered_entrypoint(tool, "run_shell_command")

    result = await entrypoint(
        [
            "bash",
            "-lc",
            "printf '%s' \"$GITEA_TOKEN|$MINDROOM_SANDBOX_PROXY_TOKEN|$CI_JOB_TOKEN|$TEST_EXECUTION_ENV\"",
        ],
    )

    assert result == "visible-gitea-token||ci-secret|visible-in-shell"


@pytest.mark.asyncio
async def test_local_shell_does_not_expose_extra_parent_env_without_configuration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Direct shell execution should not expose extra parent env without explicit config."""
    monkeypatch.setenv("WHISPER_URL", "https://whisper.example")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-6-astra\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("TEST_EXECUTION_ENV=visible-in-shell\n", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env=dict(os.environ),
    )

    tool = get_tool_by_name(
        "shell",
        runtime_paths,
        runs_in_worker=False,
        worker_target=None,
        tools=builtin_tool_registry(),
    )
    entrypoint = _offered_entrypoint(tool, "run_shell_command")

    result = await entrypoint(["bash", "-lc", "printf '%s' \"$WHISPER_URL|$TEST_EXECUTION_ENV\""])

    assert result == "|visible-in-shell"


@pytest.mark.asyncio
async def test_local_shell_prepends_configured_path_entries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Direct shell execution should prepend configured PATH entries without losing runtime PATH."""
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin:/bin")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-6-astra\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=config_path.parent / "storage",
        process_env=dict(os.environ),
    )
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    save_scoped_credentials(
        "shell",
        {"shell_path_prepend": "/opt/custom/bin, /opt/worker/bin"},
        credentials_manager=credentials_manager,
        worker_target=None,
    )

    shell_tool = get_tool_by_name(
        "shell",
        runtime_paths,
        runs_in_worker=False,
        worker_target=None,
        tools=builtin_tool_registry(),
    )
    shell_entrypoint = _offered_entrypoint(shell_tool, "run_shell_command")
    result = await shell_entrypoint(
        [
            sys.executable,
            "-c",
            'import os, sys; sys.stdout.write(os.environ.get("PATH", ""))',
        ],
    )

    assert result.startswith("/opt/custom/bin:/opt/worker/bin:")
    assert result.endswith("/usr/local/bin:/usr/bin:/bin")


@pytest.mark.asyncio
async def test_proxy_forwards_configured_shell_execution_env_only_for_execution_tools(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Sandbox proxy should forward shell execution env and passthrough config from stored tool settings."""
    captured: dict[str, Any] = {}
    _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="all",
        credential_policy={},
    )
    captured_calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "mindroom.tool_system.sandbox_proxy.httpx.Client",
        _recording_client_class(
            captured=captured,
            captured_calls=captured_calls,
            responder=lambda url, _json: (
                {"lease_id": "lease-123", "expires_at": 123.0, "max_uses": 1}
                if url.endswith("/leases")
                else {"ok": True, "result": "sandbox-result"}
            ),
        ),
    )
    monkeypatch.setenv("GITEA_TOKEN", "visible-gitea-token")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-6-astra\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    config_path.with_name(".env").write_text("TEST_EXECUTION_ENV=visible-in-shell\n", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=config_path.parent / "storage",
        process_env=dict(os.environ),
    )
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    save_scoped_credentials(
        "shell",
        {
            "extra_env_passthrough": "GITEA_*",
            "shell_path_prepend": "/opt/custom/bin",
        },
        credentials_manager=credentials_manager,
        worker_target=None,
    )

    shell_tool = get_tool_by_name("shell", runtime_paths, worker_target=None, tools=builtin_tool_registry())
    shell_entrypoint = _offered_entrypoint(shell_tool, "run_shell_command")
    result = await shell_entrypoint(["bash", "-lc", "printf '%s' \"$TEST_EXECUTION_ENV\""])

    assert result == "sandbox-result"
    assert captured_calls[0][1]["credential_overrides"] == {
        "extra_env_passthrough": "GITEA_*",
        "shell_path_prepend": "/opt/custom/bin",
    }
    assert captured["json"]["lease_id"] == "lease-123"
    assert captured["json"]["extra_env_passthrough"] == "GITEA_*"
    assert captured["json"]["tool_init_overrides"]["shell_path_prepend"] == "/opt/custom/bin"
    assert "TEST_EXECUTION_ENV" not in captured["json"]["execution_env"]
    assert captured["json"]["execution_env"]["GITEA_TOKEN"] == "visible-gitea-token"  # noqa: S105
    assert "MINDROOM_SANDBOX_PROXY_TOKEN" not in captured["json"]["execution_env"]

    captured.clear()
    calculator = get_tool_by_name("calculator", runtime_paths, worker_target=None, tools=builtin_tool_registry())
    await _offered_entrypoint(calculator, "add")(1, 2)

    assert captured["json"]["execution_env"] == {}


@pytest.mark.asyncio
async def test_proxy_shell_extra_env_passthrough_survives_sandbox_runner_rebuild(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Configured shell passthrough should survive proxy forwarding and runner subprocess rebuilds."""
    _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="all",
        credential_policy={},
    )
    monkeypatch.setenv("MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE", "subprocess")
    monkeypatch.setenv("GITEA_TOKEN", "visible-gitea-token")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-6-astra\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    config_path.with_name(".env").write_text("TEST_EXECUTION_ENV=visible-in-shell\n", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=config_path.parent / "storage",
        process_env=dict(os.environ),
    )
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    save_scoped_credentials(
        "shell",
        {"extra_env_passthrough": "GITEA_*"},
        credentials_manager=credentials_manager,
        worker_target=None,
    )

    # The runner has its own storage, so saved settings can reach it only through the primary's lease.
    runner_runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "runner-storage",
        process_env=dict(os.environ),
    )
    monkeypatch.setattr(
        "mindroom.tool_system.sandbox_proxy.httpx.Client",
        _recording_client_class(responder=_runner_routes_responder(runner_runtime_paths)),
    )

    shell_tool = get_tool_by_name("shell", runtime_paths, worker_target=None, tools=builtin_tool_registry())
    shell_entrypoint = _offered_entrypoint(shell_tool, "run_shell_command")
    result = await shell_entrypoint(
        [
            "bash",
            "-lc",
            "printf '%s' \"$GITEA_TOKEN|$TEST_EXECUTION_ENV\"",
        ],
    )

    assert result == "visible-gitea-token|"


@pytest.mark.asyncio
async def test_proxy_shell_path_prepend_survives_sandbox_runner_rebuild(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Configured shell_path_prepend should survive proxy forwarding and runner rebuilds."""
    _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="all",
        credential_policy={},
    )
    monkeypatch.setenv("MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE", "subprocess")
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin:/bin")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-6-astra\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=config_path.parent / "storage",
        process_env=dict(os.environ),
    )
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    save_scoped_credentials(
        "shell",
        {"shell_path_prepend": "/opt/custom/bin, /opt/worker/bin"},
        credentials_manager=credentials_manager,
        worker_target=None,
    )

    # The runner has its own storage, so saved settings can reach it only through the primary's lease.
    runner_runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "runner-storage",
        process_env=dict(os.environ),
    )
    monkeypatch.setattr(
        "mindroom.tool_system.sandbox_proxy.httpx.Client",
        _recording_client_class(responder=_runner_routes_responder(runner_runtime_paths)),
    )

    shell_tool = get_tool_by_name("shell", runtime_paths, worker_target=None, tools=builtin_tool_registry())
    shell_entrypoint = _offered_entrypoint(shell_tool, "run_shell_command")
    result = await shell_entrypoint(
        [
            sys.executable,
            "-c",
            'import os, sys; sys.stdout.write(os.environ.get("PATH", ""))',
        ],
    )

    assert result.startswith("/opt/custom/bin:/opt/worker/bin:")
    assert result.endswith("/usr/local/bin:/usr/bin:/bin")


def test_dedicated_worker_runtime_config_resolves_include_tags(tmp_path: Path) -> None:
    """Dedicated workers must load configs split across !include files."""
    (tmp_path / "models.yaml").write_text(
        "default:\n  provider: openai\n  id: gpt-6-astra\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models: !include models.yaml\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=config_path.parent / "storage",
        process_env={},
    )
    config = sandbox_runner_module._dedicated_worker_runtime_config_or_empty(
        runtime_paths,
        {"shell": ToolValidationInfo(name="shell")},
    )

    assert config.models["default"].id == "gpt-6-astra"


@pytest.mark.asyncio
async def test_inprocess_runner_shell_uses_request_scoped_extra_env_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """In-process runner rebuilds should source shell passthrough from request-scoped runtime env."""
    monkeypatch.setenv("GITEA_TOKEN", "ambient-gitea-token")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-6-astra\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    config_path.with_name(".env").write_text("TEST_EXECUTION_ENV=visible-in-shell\n", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=config_path.parent / "storage",
        process_env={},
    )
    config = sandbox_runner_module._runtime_config_or_empty(runtime_paths)
    call = execute_call(
        runtime_paths,
        config,
        tool_name="shell",
        function_name="run_shell_command",
        args=[
            [
                "bash",
                "-lc",
                "printf '%s' \"$GITEA_TOKEN|$TEST_EXECUTION_ENV\"",
            ],
        ],
        execution_env={"GITEA_TOKEN": "request-gitea-token"},
        extra_env_passthrough="GITEA_*",
    )

    response = await sandbox_runner_module._execute_request_inprocess(call)

    assert response.ok is True
    assert response.result == "request-gitea-token|"


def test_proxy_holds_manager_lease_through_success_bookkeeping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dedicated worker success bookkeeping should run before the manager lease is released."""
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="all",
    )
    events: list[str] = []
    lease_active = False
    worker_handle = WorkerHandle(
        worker_id="worker-1",
        worker_key="worker-key",
        endpoint="http://worker/api/sandbox-runner/execute",
        auth_token=_TEST_AUTH_TOKEN,
        status="ready",
        backend_name="docker",
        last_used_at=1.0,
        created_at=0.0,
    )

    class _FakeWorkerManager:
        cleanup_locator = None

        def touch_worker(self, worker_key: str, *, now: float | None = None) -> WorkerHandle | None:
            nonlocal lease_active
            _ = now
            assert lease_active is True
            assert worker_key == "worker-key"
            events.append("touch")
            return worker_handle

        def record_failure(
            self,
            worker_key: str,
            failure_reason: str,
            *,
            now: float | None = None,
            **_kwargs: object,
        ) -> WorkerHandle:
            _ = now
            msg = f"unexpected failure bookkeeping for {worker_key}: {failure_reason}"
            raise AssertionError(msg)

    worker_manager = _FakeWorkerManager()

    class _FakeLease:
        def __enter__(self) -> _FakeWorkerManager:
            nonlocal lease_active
            lease_active = True
            events.append("enter")
            return worker_manager

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            nonlocal lease_active
            lease_active = False
            events.append("exit")

    def _fake_worker_route(
        *,
        runtime_paths: RuntimePaths,
        runtime_config: Config | None,
        tool_name: str,
        function_name: str,
        worker_target: ResolvedWorkerTarget | None,
        worker_manager: object | None = None,
        base_dir: str | None,
        progress_sink: object = None,
    ) -> tuple[WorkerRoute, WorkerHandle | None]:
        _ = runtime_config, worker_target, base_dir, progress_sink
        assert runtime_paths is not None
        assert tool_name == "calculator"
        assert function_name == "add"
        assert worker_manager is worker_manager_ref
        events.append("build")
        return WorkerRoute(protocol=WORKER_PROTOCOL_VERSION, worker_key="worker-key"), worker_handle

    def _fake_lease_primary_worker_manager(*_args: object, **_kwargs: object) -> _FakeLease:
        return _FakeLease()

    worker_manager_ref = worker_manager
    monkeypatch.setattr(sandbox_proxy_module, "lease_primary_worker_manager", _fake_lease_primary_worker_manager)
    monkeypatch.setattr(sandbox_proxy_module, "_worker_route", _fake_worker_route)
    monkeypatch.setattr(
        "mindroom.tool_system.sandbox_proxy.httpx.Client",
        _recording_client_class(
            responder=lambda _url, _json: {"ok": True, "result": "proxied"},
        ),
    )

    result = call_proxy_sync_for_test(
        runtime_paths=runtime_paths,
        tool_name="calculator",
        function_name="add",
        args=(1, 2),
        kwargs={},
        credentials_manager=None,
    )

    assert result == "proxied"
    assert events == ["enter", "build", "touch", "exit"]


def test_proxy_holds_manager_lease_through_failure_bookkeeping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dedicated worker failure bookkeeping should run before the manager lease is released."""
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="all",
    )
    events: list[str] = []
    lease_active = False
    worker_handle = WorkerHandle(
        worker_id="worker-1",
        worker_key="worker-key",
        endpoint="http://worker/api/sandbox-runner/execute",
        auth_token=_TEST_AUTH_TOKEN,
        status="ready",
        backend_name="docker",
        last_used_at=1.0,
        created_at=0.0,
    )

    class _FakeWorkerManager:
        cleanup_locator = None

        def touch_worker(self, worker_key: str, *, now: float | None = None) -> WorkerHandle | None:
            _ = now
            msg = f"unexpected touch bookkeeping for {worker_key}"
            raise AssertionError(msg)

        def record_failure(
            self,
            worker_key: str,
            failure_reason: str,
            *,
            now: float | None = None,
            **_kwargs: object,
        ) -> WorkerHandle:
            nonlocal lease_active
            _ = now
            assert lease_active is True
            assert worker_key == "worker-key"
            assert failure_reason == "boom"
            events.append("record_failure")
            return worker_handle

    worker_manager = _FakeWorkerManager()

    class _FakeLease:
        def __enter__(self) -> _FakeWorkerManager:
            nonlocal lease_active
            lease_active = True
            events.append("enter")
            return worker_manager

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            nonlocal lease_active
            lease_active = False
            events.append("exit")

    def _fake_worker_route(
        *,
        runtime_paths: RuntimePaths,
        runtime_config: Config | None,
        tool_name: str,
        function_name: str,
        worker_target: ResolvedWorkerTarget | None,
        worker_manager: object | None = None,
        base_dir: str | None,
        progress_sink: object = None,
    ) -> tuple[WorkerRoute, WorkerHandle | None]:
        _ = runtime_paths, runtime_config, worker_target, base_dir, progress_sink
        assert tool_name == "calculator"
        assert function_name == "add"
        assert worker_manager is worker_manager_ref
        events.append("build")
        return WorkerRoute(protocol=WORKER_PROTOCOL_VERSION, worker_key="worker-key"), worker_handle

    def _fake_lease_primary_worker_manager(*_args: object, **_kwargs: object) -> _FakeLease:
        return _FakeLease()

    worker_manager_ref = worker_manager
    monkeypatch.setattr(sandbox_proxy_module, "lease_primary_worker_manager", _fake_lease_primary_worker_manager)
    monkeypatch.setattr(sandbox_proxy_module, "_worker_route", _fake_worker_route)
    monkeypatch.setattr(
        "mindroom.tool_system.sandbox_proxy.httpx.Client",
        _recording_client_class(
            responder=lambda _url, _json: {"ok": False, "error": "boom"},
        ),
    )

    with pytest.raises(RuntimeError, match="boom"):
        call_proxy_sync_for_test(
            runtime_paths=runtime_paths,
            tool_name="calculator",
            function_name="add",
            args=(1, 2),
            kwargs={},
            credentials_manager=None,
        )

    assert events == ["enter", "build", "record_failure", "exit"]


def test_proxy_requires_shared_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proxy mode should fail closed when no shared token is configured."""

    class _FakeClient(_StreamsFromPost):
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return

        def __enter__(self) -> Self:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return

        def post(self, *_args: object, **_kwargs: object) -> None:
            msg = "Proxy client should not make requests without a shared token."
            raise AssertionError(msg)

    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        proxy_token=None,
        execution_mode="all",
    )
    monkeypatch.setattr("mindroom.tool_system.sandbox_proxy.httpx.Client", _FakeClient)

    tool = get_tool_by_name("calculator", runtime_paths, worker_target=None, tools=builtin_tool_registry())
    entrypoint = blocking_entrypoint(tool, "add")
    with pytest.raises(RuntimeError, match="MINDROOM_SANDBOX_PROXY_TOKEN"):
        entrypoint(1, 2)


def test_proxy_prefers_worker_scoped_credentials_for_worker_routed_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Worker-routed credential leases should prefer credentials stored in the resolved worker scope."""
    captured_calls: list[tuple[str, dict[str, Any]]] = []

    class _FakeResponse:
        def __init__(self, data: dict[str, object]) -> None:
            self._data = data

        def raise_for_status(self) -> None:
            return

        def json(self) -> dict[str, object]:
            return self._data

    class _FakeClient(_StreamsFromPost):
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        def __enter__(self) -> Self:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return

        def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> _FakeResponse:
            _ = headers
            captured_calls.append((url, json))
            if url.endswith("/leases"):
                return _FakeResponse({"lease_id": "lease-123", "expires_at": 123.0, "max_uses": 1})
            return _FakeResponse({"ok": True, "result": "proxied"})

    execution_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
    )
    worker_key = resolve_worker_key("user", execution_identity, agent_name="code")
    assert worker_key is not None
    fake_credentials = FakeCredentialsManager(
        {"github_private": {"api_key": "shared-key", "_source": "ui"}},
        worker_managers={
            worker_key: FakeCredentialsManager({"github_private": {"api_key": "worker-key", "_source": "ui"}}),
        },
    )

    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="off",
        credential_policy={"calculator.add": ("github_private",)},
    )
    monkeypatch.setattr("mindroom.tool_system.sandbox_proxy.httpx.Client", _FakeClient)

    tool = get_tool_by_name(
        "calculator",
        runtime_paths,
        credentials_manager=fake_credentials,
        runs_in_worker=_runs_in_worker(runtime_paths, "calculator", ["calculator"]),
        worker_target=_worker_target(runtime_paths, "user", "code", execution_identity),
        tools=builtin_tool_registry(),
    )
    entrypoint = blocking_entrypoint(tool, "add")
    result = entrypoint(1, 2)

    assert result == "proxied"
    lease_url, lease_payload = captured_calls[0]
    assert lease_url.endswith("/api/sandbox-runner/leases")
    assert lease_payload["credential_overrides"] == {"api_key": "worker-key"}


@pytest.mark.parametrize("built_without_config", [False, True], ids=["config-without-grants", "no-config"])
def test_proxy_worker_routed_lease_skips_non_grantable_shared_credentials(
    monkeypatch: pytest.MonkeyPatch,
    built_without_config: bool,
) -> None:
    """Worker-routed leases expose no shared credential the build config does not grant, and none without one."""
    captured_calls: list[tuple[str, dict[str, Any]]] = []

    execution_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
    )
    fake_credentials = FakeCredentialsManager({"openai": {"api_key": "shared-key", "_source": "ui"}})

    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="off",
        credential_policy={"calculator.add": ("openai",)},
    )
    monkeypatch.setattr(
        "mindroom.tool_system.sandbox_proxy.httpx.Client",
        _recording_client_class(
            captured_calls=captured_calls,
            responder=lambda _url, _json: {"ok": True, "result": "proxied"},
        ),
    )

    tool = get_tool_by_name(
        "calculator",
        runtime_paths,
        credentials_manager=fake_credentials,
        runs_in_worker=_runs_in_worker(runtime_paths, "calculator", ["calculator"]),
        worker_target=_worker_target(runtime_paths, "user", "code", execution_identity),
        runtime_config=(
            None
            if built_without_config
            else Config(agents={"code": AgentConfig(display_name="Code", worker_scope="user")}, models={})
        ),
        tools=builtin_tool_registry(),
    )
    entrypoint = blocking_entrypoint(tool, "add")

    result = entrypoint(1, 2)

    assert result == "proxied"
    assert len(captured_calls) == 1
    execute_url, execute_payload = captured_calls[0]
    assert execute_url.endswith("/api/sandbox-runner/execute")
    assert execute_payload["lease_id"] is None


def test_proxy_worker_routed_lease_uses_build_config_grants_without_tool_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gateway calls bind no tool context, and their leases still include the shared settings their build config grants."""
    captured_calls: list[tuple[str, dict[str, Any]]] = []
    execution_identity = ToolExecutionIdentity(
        channel="mcp",
        agent_name="code",
        requester_id="@alice:example.org",
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    fake_credentials = FakeCredentialsManager({"openai": {"api_key": "shared-key", "_source": "ui"}})
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="off",
        credential_policy={"calculator.add": ("openai",)},
    )
    monkeypatch.setattr(
        "mindroom.tool_system.sandbox_proxy.httpx.Client",
        _recording_client_class(
            captured_calls=captured_calls,
            responder=lambda url, _json: (
                {"lease_id": "lease-123", "expires_at": 123.0, "max_uses": 1}
                if url.endswith("/leases")
                else {"ok": True, "result": "proxied"}
            ),
        ),
    )
    config = Config(
        agents={"code": AgentConfig(display_name="Code", worker_scope="user")},
        defaults={"worker_grantable_credentials": ["openai"]},
        models={},
    )
    tool = get_tool_by_name(
        "calculator",
        runtime_paths,
        credentials_manager=fake_credentials,
        runs_in_worker=_runs_in_worker(runtime_paths, "calculator", ["calculator"]),
        worker_target=_worker_target(runtime_paths, "user", "code", execution_identity),
        runtime_config=config,
        tools=builtin_tool_registry(),
    )
    entrypoint = blocking_entrypoint(tool, "add")

    with tool_runtime_context(None):
        result = entrypoint(1, 2)

    assert result == "proxied"
    lease_url, lease_payload = captured_calls[0]
    assert lease_url.endswith("/api/sandbox-runner/leases")
    assert lease_payload["credential_overrides"] == {"api_key": "shared-key"}


def test_proxy_includes_worker_routing_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Worker-routed tool calls should include scope, key, and execution identity."""
    captured: dict[str, Any] = {}

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return

        def json(self) -> dict[str, object]:
            return {"ok": True, "result": "sandbox-result"}

    class _FakeClient(_StreamsFromPost):
        def __init__(self, *, timeout: float) -> None:
            captured["timeout"] = timeout

        def __enter__(self) -> Self:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return

        def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> _FakeResponse:
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _FakeResponse()

    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="off",
        credential_policy={},
    )
    monkeypatch.setattr("mindroom.tool_system.sandbox_proxy.httpx.Client", _FakeClient)

    execution_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="shared_agent",
        requester_id="alice",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
        transport_agent_name="shared_agent",
    )

    tool = get_tool_by_name(
        "calculator",
        runtime_paths,
        runs_in_worker=_runs_in_worker(runtime_paths, "calculator", ["calculator"]),
        worker_target=_worker_target(
            runtime_paths,
            "user_agent",
            "code",
            execution_identity,
            private_agent_names=frozenset({"code"}),
        ),
        tools=builtin_tool_registry(),
    )
    entrypoint = blocking_entrypoint(tool, "add")
    expected_worker_key = resolve_worker_key("user_agent", execution_identity, agent_name="code")
    assert expected_worker_key is not None

    runtime_context = make_test_tool_runtime_context(
        agent_name="code",
        target=MessageTarget.resolve(
            room_id="!room:example.org",
            thread_id="$thread",
            reply_to_event_id=None,
        ),
        requester_id="alice",
        client=object(),
        config=Config(
            agents={
                "code": AgentConfig(
                    display_name="Code",
                    private=AgentPrivateConfig(per="user_agent"),
                ),
            },
            models={},
        ),
        runtime_paths=runtime_paths,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
    )

    with tool_runtime_context(runtime_context):
        result = entrypoint(1, 2)

    assert result == "sandbox-result"
    assert captured["headers"] == {"x-mindroom-sandbox-token": "test-token"}
    assert captured["json"]["worker_scope"] == "user_agent"
    assert captured["json"]["worker_key"] == expected_worker_key
    assert captured["json"]["execution_identity"] == {
        "channel": "matrix",
        "agent_name": "shared_agent",
        "requester_id": "alice",
        "room_id": "!room:example.org",
        "thread_id": "$thread",
        "resolved_thread_id": "$thread",
        "session_id": "session-1",
        "tenant_id": None,
        "account_id": None,
        "transport_agent_name": "shared_agent",
    }
    assert captured["json"]["private_agent_names"] == ["code"]


def test_proxy_user_agent_shared_agent_sends_explicit_empty_private_visibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shared user-agent workers should still send explicit empty private visibility."""
    captured: dict[str, Any] = {}

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return

        def json(self) -> dict[str, object]:
            return {"ok": True, "result": "sandbox-result"}

    class _FakeClient(_StreamsFromPost):
        def __init__(self, *, timeout: float) -> None:
            captured["timeout"] = timeout

        def __enter__(self) -> Self:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return

        def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> _FakeResponse:
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _FakeResponse()

    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="off",
        credential_policy={},
    )
    monkeypatch.setattr("mindroom.tool_system.sandbox_proxy.httpx.Client", _FakeClient)

    execution_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="shared_agent",
        requester_id="alice",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
    )

    tool = get_tool_by_name(
        "calculator",
        runtime_paths,
        runs_in_worker=_runs_in_worker(runtime_paths, "calculator", ["calculator"]),
        worker_target=_worker_target(
            runtime_paths,
            "user_agent",
            "code",
            execution_identity,
            private_agent_names=frozenset(),
        ),
        tools=builtin_tool_registry(),
    )
    entrypoint = blocking_entrypoint(tool, "add")
    expected_worker_key = resolve_worker_key("user_agent", execution_identity, agent_name="code")
    assert expected_worker_key is not None

    runtime_context = make_test_tool_runtime_context(
        agent_name="code",
        target=MessageTarget.resolve(
            room_id="!room:example.org",
            thread_id="$thread",
            reply_to_event_id=None,
        ),
        requester_id="alice",
        client=object(),
        config=Config(
            agents={
                "code": AgentConfig(
                    display_name="Code",
                    worker_scope="user_agent",
                ),
            },
            models={},
        ),
        runtime_paths=runtime_paths,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
    )

    with tool_runtime_context(runtime_context):
        result = entrypoint(1, 2)

    assert result == "sandbox-result"
    assert captured["json"]["worker_key"] == expected_worker_key
    assert captured["json"]["private_agent_names"] == []


@pytest.mark.parametrize(
    ("env", "identity_tenant_id", "identity_account_id", "expected_worker_key"),
    [
        ({}, None, None, "v1:default:unscoped:code"),
        ({"CUSTOMER_ID": "tenant-a", "ACCOUNT_ID": "account-a"}, None, None, "v1:tenant-a:unscoped:code"),
        ({"ACCOUNT_ID": "account-b"}, None, None, "v1:account-b:unscoped:code"),
        ({}, "tenant-c", None, "v1:tenant-c:unscoped:code"),
        ({}, None, "account-d", "v1:account-d:unscoped:code"),
    ],
    ids=["default", "runtime-tenant", "runtime-account", "identity-tenant", "identity-account"],
)
def test_unscoped_dedicated_worker_payload_reconciles_via_ensure_worker(
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
    identity_tenant_id: str | None,
    identity_account_id: str | None,
    expected_worker_key: str,
) -> None:
    """Dedicated unscoped routing reconciles through ensure_worker under a key that keeps the runtime or identity tenant."""

    class _FakeWorkerManager:
        def __init__(self, ensured_handle: WorkerHandle) -> None:
            self.ensured_handle = ensured_handle
            self.ensure_calls: list[str] = []

        def ensure_worker(
            self,
            spec: WorkerSpec,
            *,
            now: float | None = None,
            progress_sink: object = None,
        ) -> WorkerHandle:
            _ = now, progress_sink
            self.ensure_calls.append(spec.worker_key)
            return self.ensured_handle

    refreshed_handle = WorkerHandle(
        worker_id="worker-a",
        worker_key="worker-a",
        endpoint="http://sandbox-runner:8765/api/sandbox-runner/execute",
        auth_token=_TEST_AUTH_TOKEN,
        status="ready",
        backend_name="docker",
        last_used_at=20.0,
        created_at=10.0,
    )
    for name in ("CUSTOMER_ID", "ACCOUNT_ID"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="off",
        credential_policy={},
    )
    fake_manager = _FakeWorkerManager(refreshed_handle)
    monkeypatch.setattr(sandbox_proxy_module, "worker_backend_kind", lambda _paths: WorkerBackendKind.DOCKER)
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id=None,
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
        tenant_id=identity_tenant_id,
        account_id=identity_account_id,
    )

    worker_target = _worker_target(runtime_paths, None, "code", identity)
    assert worker_target.execution_identity is not None
    route, worker_handle = sandbox_proxy_module._worker_route(
        runtime_paths=runtime_paths,
        runtime_config=None,
        tool_name="shell",
        function_name="run_shell_command",
        worker_target=worker_target,
        worker_manager=fake_manager,
        base_dir=None,
    )

    # The dedicated worker serves the call on behalf of its caller, so the caller's identity travels too.
    assert route == WorkerRoute(
        protocol=WORKER_PROTOCOL_VERSION,
        worker_key=expected_worker_key,
        routing_agent_name="code",
        execution_identity=serialize_tool_execution_identity(worker_target.execution_identity),
    )
    assert worker_handle is refreshed_handle
    assert fake_manager.ensure_calls == [expected_worker_key]


def test_scoped_dedicated_worker_payload_preserves_resolved_worker_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dedicated worker reconciliation should pass the resolved worker key through unchanged."""

    class _FakeWorkerManager:
        def __init__(self, ensured_handle: WorkerHandle) -> None:
            self.ensured_handle = ensured_handle
            self.ensure_calls: list[str] = []

        def ensure_worker(
            self,
            spec: WorkerSpec,
            *,
            now: float | None = None,
            progress_sink: object = None,
        ) -> WorkerHandle:
            _ = now, progress_sink
            self.ensure_calls.append(spec.worker_key)
            return self.ensured_handle

    ensured_handle = WorkerHandle(
        worker_id="worker-b",
        worker_key="worker-b",
        endpoint="http://sandbox-runner:8765/api/sandbox-runner/execute",
        auth_token=_TEST_AUTH_TOKEN,
        status="idle",
        backend_name="docker",
        last_used_at=15.0,
        created_at=10.0,
    )
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="off",
        credential_policy={},
    )
    fake_manager = _FakeWorkerManager(ensured_handle)
    execution_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
    )
    expected_worker_key = resolve_worker_key("user", execution_identity, agent_name="code")
    assert expected_worker_key is not None

    route, worker_handle = sandbox_proxy_module._worker_route(
        runtime_paths=runtime_paths,
        runtime_config=None,
        tool_name="shell",
        function_name="run_shell_command",
        worker_target=_worker_target(runtime_paths, "user", "code", execution_identity),
        worker_manager=fake_manager,
        base_dir=None,
    )

    assert route.worker_key == expected_worker_key
    assert route.worker_scope == "user"
    assert worker_handle is ensured_handle
    assert fake_manager.ensure_calls == [expected_worker_key]


def test_proxy_surfaces_runner_http_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proxy should preserve sandbox-runner detail messages on HTTP failures."""

    class _FakeResponse:
        def raise_for_status(self) -> None:
            request = httpx.Request("POST", "http://sandbox-runner:8765/api/sandbox-runner/execute")
            response = httpx.Response(
                400,
                request=request,
                json={"detail": "base_dir must stay inside a visible workspace or the worker root"},
            )
            message = "bad request"
            raise httpx.HTTPStatusError(message, request=request, response=response)

        def json(self) -> dict[str, object]:
            msg = "Success payload should not be parsed for HTTP failures."
            raise AssertionError(msg)

    class _FakeClient(_StreamsFromPost):
        def __init__(self, *, timeout: float) -> None:
            _ = timeout

        def __enter__(self) -> Self:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return

        def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> _FakeResponse:
            _ = url, json, headers
            return _FakeResponse()

    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="all",
        credential_policy={},
    )
    monkeypatch.setattr("mindroom.tool_system.sandbox_proxy.httpx.Client", _FakeClient)

    tool = get_tool_by_name("calculator", runtime_paths, worker_target=None, tools=builtin_tool_registry())
    entrypoint = blocking_entrypoint(tool, "add")
    with pytest.raises(RuntimeError, match="base_dir must stay inside a visible workspace or the worker root"):
        entrypoint(1, 2)


@pytest.mark.usefixtures("current_static_runner")
def test_static_sandbox_runner_backend_reuses_worker_handle_identity() -> None:
    """The current shared sandbox-runner provider should return stable handle identity per worker key."""
    backend = StaticSandboxRunnerBackend(
        api_root="http://sandbox-runner:8765",
        auth_token=_TEST_AUTH_TOKEN,
        idle_timeout_seconds=60.0,
    )

    first = backend.ensure_worker(WorkerSpec("worker-a"), now=10.0)
    second = backend.ensure_worker(WorkerSpec("worker-a"), now=20.0)

    assert first.worker_id == second.worker_id
    assert second.worker_key == "worker-a"
    assert second.endpoint == "http://sandbox-runner:8765/api/sandbox-runner/execute"
    assert second.auth_token == _TEST_AUTH_TOKEN
    assert second.backend_name == "static_runner"
    assert second.startup_count == 1
    assert second.last_used_at == 20.0


@pytest.mark.usefixtures("current_static_runner")
def test_static_sandbox_runner_backend_marks_idle_workers() -> None:
    """Idle cleanup on the static provider should preserve worker identity while changing lifecycle state."""
    backend = StaticSandboxRunnerBackend(
        api_root="http://sandbox-runner:8765",
        auth_token=_TEST_AUTH_TOKEN,
        idle_timeout_seconds=5.0,
    )
    backend.ensure_worker(WorkerSpec("worker-a"), now=0.0)

    cleaned_workers = backend.cleanup_idle_workers(now=10.0)

    assert len(cleaned_workers) == 1
    assert cleaned_workers[0].worker_key == "worker-a"
    assert cleaned_workers[0].status == "idle"


def _runner_health_answers(
    monkeypatch: pytest.MonkeyPatch,
    *answers: dict[str, object] | int | Exception,
) -> list[str]:
    """Answer the static runner's health probes in order with a payload, an error status, or an exception.

    Returns the probed runner roots.
    """
    probed: list[str] = []
    pending = list(answers)

    def answer(runner_root: str) -> httpx.Response:
        probed.append(runner_root)
        next_answer = pending.pop(0)
        if isinstance(next_answer, Exception):
            raise next_answer
        request = httpx.Request("GET", f"{runner_root}/healthz")
        if isinstance(next_answer, int):
            return httpx.Response(next_answer, json={"detail": "unavailable"}, request=request)
        return httpx.Response(200, json=next_answer, request=request)

    monkeypatch.setattr(static_runner_module, "_read_runner_health", answer)
    return probed


_STALE_RUNNER_HEALTH = {"status": "ok", "mindroom_version": "2026.10.612", "worker_protocol": 4}
_CURRENT_RUNNER_HEALTH = {"status": "ok", "mindroom_version": "test", "worker_protocol": WORKER_PROTOCOL_VERSION}


def test_static_runner_refuses_a_runner_on_another_worker_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    """A shared runner from another release is refused with the dedicated backends' compatibility error."""
    probed = _runner_health_answers(monkeypatch, _STALE_RUNNER_HEALTH)
    backend = StaticSandboxRunnerBackend(api_root="http://sandbox-runner:8765", auth_token=_TEST_AUTH_TOKEN)

    with pytest.raises(WorkerBackendError) as exc_info:
        backend.ensure_worker(WorkerSpec("worker-a"))

    assert probed == ["http://sandbox-runner:8765"]
    assert str(exc_info.value) == (
        "Sandbox runner at 'http://sandbox-runner:8765' is incompatible with this MindRoom runtime: "
        f"expected worker protocol {WORKER_PROTOCOL_VERSION}, got 4 (worker MindRoom version 2026.10.612). "
        "Use a worker image built for this MindRoom release. "
        "Restart the runner on this release, pulling its image again when it runs a tag such as ':latest'."
    )
    assert backend.list_workers() == []


def test_static_runner_asks_once_and_again_after_a_failed_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """The runner is probed at first use, not on every call, and again once a call to it has failed."""
    probed = _runner_health_answers(
        monkeypatch,
        httpx.ConnectError("Connection refused"),
        503,
        _CURRENT_RUNNER_HEALTH,
        _STALE_RUNNER_HEALTH,
    )
    backend = StaticSandboxRunnerBackend(api_root="http://sandbox-runner:8765", auth_token=_TEST_AUTH_TOKEN)

    with pytest.raises(WorkerBackendError, match="did not answer its health check: Connection refused"):
        backend.ensure_worker(WorkerSpec("worker-a"))
    with pytest.raises(WorkerBackendError, match="did not answer its health check: Server error '503"):
        backend.ensure_worker(WorkerSpec("worker-a"))
    backend.ensure_worker(WorkerSpec("worker-a"))
    backend.ensure_worker(WorkerSpec("worker-b"))
    assert len(probed) == 3

    backend.record_failure("worker-a", "Connection reset by peer")
    with pytest.raises(WorkerBackendError, match="got 4"):
        backend.ensure_worker(WorkerSpec("worker-b"))
    assert len(probed) == 4


def test_static_runner_asks_again_after_a_failure_recorded_during_its_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure recorded while a probe is in flight keeps that probe's answer from skipping the next probe."""
    backend = StaticSandboxRunnerBackend(api_root="http://sandbox-runner:8765", auth_token=_TEST_AUTH_TOKEN)
    probed: list[str] = []

    def answer_after_a_concurrent_failure(runner_root: str) -> httpx.Response:
        probed.append(runner_root)
        if len(probed) == 1:
            # Another call to the runner fails while this probe waits for its answer.
            backend.record_failure("worker-b", "Connection reset by peer")
        return httpx.Response(
            200,
            json=_CURRENT_RUNNER_HEALTH,
            request=httpx.Request("GET", f"{runner_root}/healthz"),
        )

    monkeypatch.setattr(static_runner_module, "_read_runner_health", answer_after_a_concurrent_failure)

    backend.ensure_worker(WorkerSpec("worker-a"))
    backend.ensure_worker(WorkerSpec("worker-a"))
    backend.ensure_worker(WorkerSpec("worker-a"))

    assert len(probed) == 2


@pytest.mark.parametrize(
    ("proxy_url", "proxy_token", "missing_setting"),
    [
        (None, _TEST_AUTH_TOKEN, "MINDROOM_SANDBOX_PROXY_URL"),
        ("http://sandbox-runner:8765", None, "MINDROOM_SANDBOX_PROXY_TOKEN"),
    ],
)
def test_unscoped_call_to_an_unconfigured_runner_names_the_missing_setting(
    monkeypatch: pytest.MonkeyPatch,
    proxy_url: str | None,
    proxy_token: str | None,
    missing_setting: str,
) -> None:
    """Without a runner URL or token the call fails naming that setting, before any health probe is sent."""
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url=proxy_url,
        proxy_token=proxy_token,
        execution_mode="all",
        credential_policy={},
    )

    entrypoint = blocking_entrypoint(
        get_tool_by_name("calculator", runtime_paths, worker_target=None, tools=builtin_tool_registry()),
        "add",
    )
    with pytest.raises(WorkerBackendError) as exc_info:
        entrypoint(1, 2)

    assert str(exc_info.value) == f"{missing_setting} must be set when sandbox proxying is enabled."


def test_unscoped_call_never_reaches_a_runner_on_another_worker_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unscoped call, which goes to the shared runner without a worker handle, is refused before it is sent."""
    captured: dict[str, Any] = {}
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        execution_mode="selective",
        proxy_tools={"calculator"},
        credential_policy={},
    )
    monkeypatch.setattr("mindroom.tool_system.sandbox_proxy.httpx.Client", _recording_client_class(captured=captured))
    probed = _runner_health_answers(monkeypatch, _STALE_RUNNER_HEALTH)

    entrypoint = blocking_entrypoint(
        get_tool_by_name("calculator", runtime_paths, worker_target=None, tools=builtin_tool_registry()),
        "add",
    )
    with pytest.raises(WorkerBackendError, match="is incompatible with this MindRoom runtime"):
        entrypoint(1, 2)

    assert probed == ["http://sandbox-runner:8765"]
    assert captured == {}


def test_worker_manager_singleton_creation_is_thread_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Concurrent proxy requests should not build multiple static worker managers for one config."""
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner-thread-safe:8765",
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode=None,
    )
    proxy_config = sandbox_proxy_module.sandbox_proxy_config(runtime_paths)
    monkeypatch.delenv("MINDROOM_WORKER_BACKEND", raising=False)

    first_init_started = threading.Event()
    allow_first_init_to_finish = threading.Event()
    init_count_lock = threading.Lock()
    init_count = 0
    managers: list[object] = []
    exceptions: list[Exception] = []

    class FakeBackend:
        backend_name = "fake_static_backend"
        idle_timeout_seconds = 60.0

        def __init__(self, *, api_root: str, auth_token: str | None) -> None:
            del api_root, auth_token
            nonlocal init_count
            with init_count_lock:
                init_count += 1
                call_number = init_count
            if call_number == 1:
                first_init_started.set()
                assert allow_first_init_to_finish.wait(timeout=1.0)

        def shutdown(self) -> None:
            return None

    def load_manager() -> None:
        try:
            with sandbox_proxy_module._lease_worker_manager(runtime_paths, proxy_config) as manager:
                managers.append(manager)
        except Exception as exc:  # pragma: no cover - surfaced by assertion below
            exceptions.append(exc)

    monkeypatch.setattr(workers_runtime_module, "StaticSandboxRunnerBackend", FakeBackend)

    first_thread = threading.Thread(target=load_manager)
    second_thread = threading.Thread(target=load_manager)

    first_thread.start()
    assert first_init_started.wait(timeout=1.0)
    second_thread.start()
    assert init_count == 1
    allow_first_init_to_finish.set()
    first_thread.join(timeout=1.0)
    second_thread.join(timeout=1.0)

    assert exceptions == []
    assert init_count == 1
    assert len(managers) == 2
    assert managers[0] is managers[1]


def test_docker_worker_manager_rebuilds_when_runtime_storage_path_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Changing the runtime storage path should rebuild the Docker worker manager with the new root."""
    workers_runtime_module.reset_primary_worker_manager()
    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", "docker")
    monkeypatch.setenv("MINDROOM_DOCKER_WORKER_IMAGE", "ghcr.io/mindroom-ai/mindroom:latest")

    built_storage_paths: list[Path] = []
    built_runtime_paths: list[RuntimePaths] = []

    class _FakeDockerBackend:
        backend_name = "docker"
        idle_timeout_seconds = 60.0

        @classmethod
        def build(
            cls,
            runtime_paths: RuntimePaths,
            *,
            auth_token: str | None,
            current_launch: Callable[[], WorkerLaunchConfig],
            storage_root: Path,
        ) -> _FakeDockerBackend:
            del current_launch
            assert auth_token == _TEST_AUTH_TOKEN
            built_storage_paths.append(storage_root)
            built_runtime_paths.append(runtime_paths)
            return cls()

        def shutdown(self) -> None:
            return None

    _build_backend_as(monkeypatch, "configure_docker_worker_backend", _FakeDockerBackend)

    config_path = tmp_path / "config.yaml"
    first_storage_path = (tmp_path / "runtime-a").resolve()
    second_storage_path = (tmp_path / "runtime-b").resolve()
    first_runtime_paths, second_runtime_paths = (
        resolve_runtime_paths(
            config_path=config_path,
            storage_path=storage_path,
            process_env={
                "MINDROOM_WORKER_BACKEND": "docker",
                "MINDROOM_DOCKER_WORKER_IMAGE": "ghcr.io/mindroom-ai/mindroom:latest",
            },
        )
        for storage_path in (first_storage_path, second_storage_path)
    )

    try:
        first_manager = _unleased_primary_worker_manager(
            first_runtime_paths,
            proxy_url=None,
            proxy_token=_TEST_AUTH_TOKEN,
        )

        second_manager = _unleased_primary_worker_manager(
            second_runtime_paths,
            proxy_url=None,
            proxy_token=_TEST_AUTH_TOKEN,
        )

        assert built_storage_paths == [first_storage_path, second_storage_path]
        assert built_runtime_paths == [first_runtime_paths, second_runtime_paths]
        assert first_manager is not second_manager
    finally:
        workers_runtime_module.reset_primary_worker_manager()


def test_shutdown_primary_worker_manager_resets_cached_runtime_manager() -> None:
    """Shutting down the primary manager should tear down the cached manager."""
    workers_runtime_module.reset_primary_worker_manager()
    shutdown_calls: list[str] = []

    class _FakeWorkerManager:
        def __init__(self, name: str) -> None:
            self._name = name

        def shutdown(self) -> None:
            shutdown_calls.append(self._name)

    workers_runtime_module._PRIMARY_WORKER_MANAGER_ENTRY = workers_runtime_module._WorkerManagerEntry(
        manager=_FakeWorkerManager("cached"),
        config_signature=("docker", "cached"),
    )

    workers_runtime_module.shutdown_primary_worker_manager(timeout_seconds=0.0)

    assert shutdown_calls == ["cached"]
    assert workers_runtime_module._PRIMARY_WORKER_MANAGER_ENTRY is None
    assert workers_runtime_module._RETIRED_PRIMARY_WORKER_MANAGER_ENTRIES == []


def test_explicit_primary_worker_reset_reopens_construction_after_shutdown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Final shutdown rejects new builds until an explicit process reset establishes a new epoch."""
    workers_runtime_module.reset_primary_worker_manager()

    class _FakeWorkerManager:
        def shutdown(self) -> None:
            return None

    monkeypatch.setattr(
        workers_runtime_module,
        "_configure_primary_worker_backend",
        lambda *_args, **_kwargs: ConfiguredWorkerBackend(
            signature=("reset-generation",),
            cleanup_signature=("reset-generation",),
            build=lambda _current_launch: _FakeWorkerManager(),
        ),
    )
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)

    try:
        workers_runtime_module.shutdown_primary_worker_manager(timeout_seconds=0.0)
        with pytest.raises(WorkerBackendError, match="shut down"):
            _unleased_primary_worker_manager(
                runtime_paths,
                proxy_url=None,
                proxy_token=None,
            )

        workers_runtime_module.reset_primary_worker_manager()
        manager = _unleased_primary_worker_manager(
            runtime_paths,
            proxy_url=None,
            proxy_token=None,
        )

        assert isinstance(manager, _FakeWorkerManager)
    finally:
        workers_runtime_module.reset_primary_worker_manager()


def test_superseded_worker_manager_disposes_when_its_last_lease_releases(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A superseded manager shuts down exactly once when its final borrower releases it."""
    workers_runtime_module.reset_primary_worker_manager()
    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", "docker")
    monkeypatch.setenv("MINDROOM_DOCKER_WORKER_IMAGE", "ghcr.io/mindroom-ai/mindroom:latest")

    build_order: list[str] = []

    class _FakeDockerBackend:
        backend_name = "docker"
        idle_timeout_seconds = 60.0

        @classmethod
        def build(
            cls,
            runtime_paths: RuntimePaths,
            *,
            auth_token: str | None,
            current_launch: Callable[[], WorkerLaunchConfig],
            storage_root: Path,
        ) -> _FakeDockerBackend:
            del current_launch
            assert auth_token == _TEST_AUTH_TOKEN
            assert runtime_paths.storage_root == storage_root
            build_order.append(str(storage_root))
            backend = cls()
            backend.shutdown_calls = 0
            return backend

        def shutdown(self) -> None:
            self.shutdown_calls += 1

    _build_backend_as(monkeypatch, "configure_docker_worker_backend", _FakeDockerBackend)

    first_storage_path = (tmp_path / "runtime-a").resolve()
    second_storage_path = (tmp_path / "runtime-b").resolve()
    first_runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config-a.yaml", storage_path=first_storage_path)
    second_runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config-b.yaml",
        storage_path=second_storage_path,
    )

    with workers_runtime_module.lease_primary_worker_manager(
        first_runtime_paths,
        proxy_url=None,
        proxy_token=_TEST_AUTH_TOKEN,
    ) as first_manager:
        second_manager = _unleased_primary_worker_manager(
            second_runtime_paths,
            proxy_url=None,
            proxy_token=_TEST_AUTH_TOKEN,
        )
        repeated_second_manager = _unleased_primary_worker_manager(
            second_runtime_paths,
            proxy_url=None,
            proxy_token=_TEST_AUTH_TOKEN,
        )
        assert build_order == [str(first_storage_path), str(second_storage_path)]
        assert first_manager is not second_manager
        assert second_manager is repeated_second_manager
        assert first_manager.shutdown_calls == 0
        assert second_manager.shutdown_calls == 0

    assert first_manager.shutdown_calls == 1
    assert build_order == [str(first_storage_path), str(second_storage_path)]
    workers_runtime_module.reset_primary_worker_manager()
    assert first_manager.shutdown_calls == 1
    assert second_manager.shutdown_calls == 1


def test_shutdown_primary_worker_manager_keeps_leased_retired_manager_tracked_until_release(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A timed-out shutdown should keep leased retired managers so lease release can drain them."""
    workers_runtime_module.reset_primary_worker_manager()
    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", "docker")
    monkeypatch.setenv("MINDROOM_DOCKER_WORKER_IMAGE", "ghcr.io/mindroom-ai/mindroom:latest")

    class _FakeDockerBackend:
        backend_name = "docker"
        idle_timeout_seconds = 60.0

        @classmethod
        def build(
            cls,
            runtime_paths: RuntimePaths,
            *,
            auth_token: str | None,
            current_launch: Callable[[], WorkerLaunchConfig],
            storage_root: Path,
        ) -> _FakeDockerBackend:
            del current_launch
            assert auth_token == _TEST_AUTH_TOKEN
            assert runtime_paths.storage_root == storage_root
            backend = cls()
            backend.shutdown_calls = 0
            return backend

        def shutdown(self) -> None:
            self.shutdown_calls += 1

    _build_backend_as(monkeypatch, "configure_docker_worker_backend", _FakeDockerBackend)

    storage_path = (tmp_path / "runtime-a").resolve()
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config-a.yaml", storage_path=storage_path)
    lease = workers_runtime_module.lease_primary_worker_manager(
        runtime_paths,
        proxy_url=None,
        proxy_token=_TEST_AUTH_TOKEN,
    )
    manager = lease.manager

    workers_runtime_module.shutdown_primary_worker_manager(timeout_seconds=0.0)

    assert workers_runtime_module._PRIMARY_WORKER_MANAGER_ENTRY is None
    assert [lease._entry] == workers_runtime_module._RETIRED_PRIMARY_WORKER_MANAGER_ENTRIES
    assert manager.shutdown_calls == 0

    lease.release()

    assert manager.shutdown_calls == 1
    assert workers_runtime_module._RETIRED_PRIMARY_WORKER_MANAGER_ENTRIES == []


def test_docker_worker_manager_preserves_cached_manager_when_new_build_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed build for a new signature should not evict the last known-good manager."""
    workers_runtime_module.reset_primary_worker_manager()
    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", "docker")
    monkeypatch.setenv("MINDROOM_DOCKER_WORKER_IMAGE", "ghcr.io/mindroom-ai/mindroom:latest")

    first_storage_path = (tmp_path / "runtime-a").resolve()
    second_storage_path = (tmp_path / "runtime-b").resolve()
    build_order: list[str] = []

    class _FakeDockerBackend:
        backend_name = "docker"
        idle_timeout_seconds = 60.0

        @classmethod
        def build(
            cls,
            runtime_paths: RuntimePaths,
            *,
            auth_token: str | None,
            current_launch: Callable[[], WorkerLaunchConfig],
            storage_root: Path,
        ) -> _FakeDockerBackend:
            del current_launch
            assert auth_token == _TEST_AUTH_TOKEN
            assert runtime_paths.storage_root == storage_root
            build_order.append(str(storage_root))
            if storage_root == second_storage_path:
                msg = "boom"
                raise WorkerBackendError(msg)
            backend = cls()
            backend.shutdown_calls = 0
            return backend

        def shutdown(self) -> None:
            self.shutdown_calls += 1

    _build_backend_as(monkeypatch, "configure_docker_worker_backend", _FakeDockerBackend)

    first_runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config-a.yaml", storage_path=first_storage_path)
    second_runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config-b.yaml",
        storage_path=second_storage_path,
    )

    first_manager = _unleased_primary_worker_manager(
        first_runtime_paths,
        proxy_url=None,
        proxy_token=_TEST_AUTH_TOKEN,
    )

    with pytest.raises(WorkerBackendError, match="boom"):
        _unleased_primary_worker_manager(
            second_runtime_paths,
            proxy_url=None,
            proxy_token=_TEST_AUTH_TOKEN,
        )

    repeated_first_manager = _unleased_primary_worker_manager(
        first_runtime_paths,
        proxy_url=None,
        proxy_token=_TEST_AUTH_TOKEN,
    )

    assert build_order == [str(first_storage_path), str(second_storage_path)]
    assert repeated_first_manager is first_manager
    assert first_manager.shutdown_calls == 0
    workers_runtime_module.reset_primary_worker_manager()
    assert first_manager.shutdown_calls == 1


def test_docker_worker_manager_replacement_disposes_previous_once_despite_shutdown_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The final lease release disposes a superseded manager exactly once even on failure."""
    workers_runtime_module.reset_primary_worker_manager()
    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", "docker")
    monkeypatch.setenv("MINDROOM_DOCKER_WORKER_IMAGE", "ghcr.io/mindroom-ai/mindroom:latest")

    class _FakeDockerBackend:
        backend_name = "docker"
        idle_timeout_seconds = 60.0

        @classmethod
        def build(
            cls,
            runtime_paths: RuntimePaths,
            *,
            auth_token: str | None,
            current_launch: Callable[[], WorkerLaunchConfig],
            storage_root: Path,
        ) -> _FakeDockerBackend:
            del current_launch
            assert auth_token == _TEST_AUTH_TOKEN
            assert runtime_paths.storage_root == storage_root
            backend = cls()
            backend.shutdown_calls = 0
            backend.raise_on_shutdown = False
            return backend

        def shutdown(self) -> None:
            self.shutdown_calls += 1
            if self.raise_on_shutdown:
                msg = "old shutdown boom"
                raise WorkerBackendError(msg)

    _build_backend_as(monkeypatch, "configure_docker_worker_backend", _FakeDockerBackend)

    first_storage_path = (tmp_path / "runtime-a").resolve()
    second_storage_path = (tmp_path / "runtime-b").resolve()
    first_runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config-a.yaml", storage_path=first_storage_path)
    second_runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config-b.yaml",
        storage_path=second_storage_path,
    )

    with workers_runtime_module.lease_primary_worker_manager(
        first_runtime_paths,
        proxy_url=None,
        proxy_token=_TEST_AUTH_TOKEN,
    ) as first_manager:
        first_manager.raise_on_shutdown = True

        second_manager = _unleased_primary_worker_manager(
            second_runtime_paths,
            proxy_url=None,
            proxy_token=_TEST_AUTH_TOKEN,
        )
        repeated_second_manager = _unleased_primary_worker_manager(
            second_runtime_paths,
            proxy_url=None,
            proxy_token=_TEST_AUTH_TOKEN,
        )

        assert second_manager is repeated_second_manager
        assert first_manager.shutdown_calls == 0
        assert second_manager.shutdown_calls == 0

    assert first_manager.shutdown_calls == 1
    assert (
        _unleased_primary_worker_manager(
            second_runtime_paths,
            proxy_url=None,
            proxy_token=_TEST_AUTH_TOKEN,
        )
        is second_manager
    )

    first_manager.raise_on_shutdown = False
    workers_runtime_module.reset_primary_worker_manager()
    assert first_manager.shutdown_calls == 1
    assert second_manager.shutdown_calls == 1


def test_docker_worker_manager_build_does_not_hold_cache_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Slow manager construction must not block unrelated cache-lock reads."""
    workers_runtime_module.reset_primary_worker_manager()
    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", "docker")
    monkeypatch.setenv("MINDROOM_DOCKER_WORKER_IMAGE", "ghcr.io/mindroom-ai/mindroom:latest")

    build_started = threading.Event()
    allow_build = threading.Event()

    class _FakeDockerBackend:
        backend_name = "docker"
        idle_timeout_seconds = 60.0

        @classmethod
        def build(
            cls,
            runtime_paths: RuntimePaths,
            *,
            auth_token: str | None,
            current_launch: Callable[[], WorkerLaunchConfig],
            storage_root: Path,
        ) -> _FakeDockerBackend:
            del current_launch
            _ = runtime_paths, storage_root
            assert auth_token == _TEST_AUTH_TOKEN
            build_started.set()
            assert allow_build.wait(timeout=5.0)
            return cls()

        def shutdown(self) -> None:
            return None

    _build_backend_as(monkeypatch, "configure_docker_worker_backend", _FakeDockerBackend)

    storage_path = (tmp_path / "runtime-a").resolve()
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config-a.yaml", storage_path=storage_path)
    thread_error: list[BaseException] = []

    def _build_manager() -> None:
        try:
            _unleased_primary_worker_manager(
                runtime_paths,
                proxy_url=None,
                proxy_token=_TEST_AUTH_TOKEN,
            )
        except BaseException as exc:  # pragma: no cover - surfaced by assertion below
            thread_error.append(exc)

    build_thread = threading.Thread(target=_build_manager)
    build_thread.start()
    try:
        assert build_started.wait(timeout=5.0)
        assert workers_runtime_module._PRIMARY_WORKER_MANAGER_CONDITION.acquire(timeout=0.5)
        workers_runtime_module._PRIMARY_WORKER_MANAGER_CONDITION.release()
    finally:
        allow_build.set()
        build_thread.join(timeout=5.0)

    assert thread_error == []
    assert not build_thread.is_alive()
    workers_runtime_module.reset_primary_worker_manager()


def test_docker_worker_manager_replacement_disposes_unleased_generation_before_return(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An unleased superseded manager is disposed before replacement acquisition returns."""
    workers_runtime_module.reset_primary_worker_manager()
    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", "docker")
    monkeypatch.setenv("MINDROOM_DOCKER_WORKER_IMAGE", "ghcr.io/mindroom-ai/mindroom:latest")

    shutdown_started = threading.Event()
    allow_shutdown = threading.Event()

    class _FakeDockerBackend:
        backend_name = "docker"
        idle_timeout_seconds = 60.0

        @classmethod
        def build(
            cls,
            runtime_paths: RuntimePaths,
            *,
            auth_token: str | None,
            current_launch: Callable[[], WorkerLaunchConfig],
            storage_root: Path,
        ) -> _FakeDockerBackend:
            del current_launch
            assert auth_token == _TEST_AUTH_TOKEN
            assert runtime_paths.storage_root == storage_root
            backend = cls()
            backend.block_shutdown = False
            return backend

        def shutdown(self) -> None:
            if not self.block_shutdown:
                return
            shutdown_started.set()
            assert allow_shutdown.wait(timeout=5.0)

    _build_backend_as(monkeypatch, "configure_docker_worker_backend", _FakeDockerBackend)

    first_storage_path = (tmp_path / "runtime-a").resolve()
    second_storage_path = (tmp_path / "runtime-b").resolve()
    first_runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config-a.yaml", storage_path=first_storage_path)
    second_runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config-b.yaml",
        storage_path=second_storage_path,
    )

    first_manager = _unleased_primary_worker_manager(
        first_runtime_paths,
        proxy_url=None,
        proxy_token=_TEST_AUTH_TOKEN,
    )
    first_manager.block_shutdown = True

    thread_result: dict[str, object] = {}

    def _replace_manager() -> None:
        thread_result["manager"] = _unleased_primary_worker_manager(
            second_runtime_paths,
            proxy_url=None,
            proxy_token=_TEST_AUTH_TOKEN,
        )

    replacement_thread = threading.Thread(target=_replace_manager)
    replacement_thread.start()
    assert shutdown_started.wait(timeout=1.0)
    assert replacement_thread.is_alive()
    allow_shutdown.set()
    replacement_thread.join(timeout=5.0)
    assert not replacement_thread.is_alive()

    second_manager = _unleased_primary_worker_manager(
        second_runtime_paths,
        proxy_url=None,
        proxy_token=_TEST_AUTH_TOKEN,
    )

    assert thread_result["manager"] is second_manager
    workers_runtime_module.reset_primary_worker_manager()


def test_docker_worker_manager_rebuilds_when_runtime_shared_credentials_path_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Changing the runtime shared-credentials path should rebuild the Docker worker manager."""
    workers_runtime_module.reset_primary_worker_manager()
    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", "docker")
    monkeypatch.setenv("MINDROOM_DOCKER_WORKER_IMAGE", "ghcr.io/mindroom-ai/mindroom:latest")

    built_shared_paths: list[str | None] = []

    class _FakeDockerBackend:
        backend_name = "docker"
        idle_timeout_seconds = 60.0

        @classmethod
        def build(
            cls,
            runtime_paths: RuntimePaths,
            *,
            auth_token: str | None,
            current_launch: Callable[[], WorkerLaunchConfig],
            storage_root: Path,
        ) -> _FakeDockerBackend:
            del current_launch
            assert auth_token == _TEST_AUTH_TOKEN
            assert storage_root == tmp_path.resolve()
            built_shared_paths.append(runtime_paths.env_value("MINDROOM_SHARED_CREDENTIALS_PATH"))
            return cls()

        def shutdown(self) -> None:
            return None

    _build_backend_as(monkeypatch, "configure_docker_worker_backend", _FakeDockerBackend)

    config_path = tmp_path / "config.yaml"
    first_runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path,
        process_env={
            "MINDROOM_WORKER_BACKEND": "docker",
            "MINDROOM_DOCKER_WORKER_IMAGE": "ghcr.io/mindroom-ai/mindroom:latest",
            "MINDROOM_SHARED_CREDENTIALS_PATH": str((tmp_path / "shared-a").resolve()),
        },
    )
    second_runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path,
        process_env={
            "MINDROOM_WORKER_BACKEND": "docker",
            "MINDROOM_DOCKER_WORKER_IMAGE": "ghcr.io/mindroom-ai/mindroom:latest",
            "MINDROOM_SHARED_CREDENTIALS_PATH": str((tmp_path / "shared-b").resolve()),
        },
    )

    first_manager = _unleased_primary_worker_manager(
        first_runtime_paths,
        proxy_url=None,
        proxy_token=_TEST_AUTH_TOKEN,
    )
    second_manager = _unleased_primary_worker_manager(
        second_runtime_paths,
        proxy_url=None,
        proxy_token=_TEST_AUTH_TOKEN,
    )

    assert built_shared_paths == [
        str((tmp_path / "shared-a").resolve()),
        str((tmp_path / "shared-b").resolve()),
    ]
    assert first_manager is not second_manager
    workers_runtime_module.reset_primary_worker_manager()


def test_docker_worker_manager_rebuilds_when_committed_runtime_env_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Changing committed worker runtime env should rebuild the Docker worker manager."""
    workers_runtime_module.reset_primary_worker_manager()
    built_browser_paths: list[str | None] = []

    class _FakeDockerBackend:
        backend_name = "docker"
        idle_timeout_seconds = 60.0

        @classmethod
        def build(
            cls,
            runtime_paths: RuntimePaths,
            *,
            auth_token: str | None,
            current_launch: Callable[[], WorkerLaunchConfig],
            storage_root: Path,
        ) -> _FakeDockerBackend:
            del current_launch
            assert auth_token == _TEST_AUTH_TOKEN
            assert storage_root == tmp_path.resolve()
            built_browser_paths.append(runtime_paths.env_value("BROWSER_EXECUTABLE_PATH"))
            return cls()

        def shutdown(self) -> None:
            return None

    _build_backend_as(monkeypatch, "configure_docker_worker_backend", _FakeDockerBackend)

    config_path = tmp_path / "config.yaml"
    first_runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path,
        process_env={
            "MINDROOM_WORKER_BACKEND": "docker",
            "MINDROOM_DOCKER_WORKER_IMAGE": "ghcr.io/mindroom-ai/mindroom:latest",
            "BROWSER_EXECUTABLE_PATH": "/usr/bin/a",
        },
    )
    second_runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path,
        process_env={
            "MINDROOM_WORKER_BACKEND": "docker",
            "MINDROOM_DOCKER_WORKER_IMAGE": "ghcr.io/mindroom-ai/mindroom:latest",
            "BROWSER_EXECUTABLE_PATH": "/usr/bin/b",
        },
    )

    first_manager = _unleased_primary_worker_manager(
        first_runtime_paths,
        proxy_url=None,
        proxy_token=_TEST_AUTH_TOKEN,
    )
    second_manager = _unleased_primary_worker_manager(
        second_runtime_paths,
        proxy_url=None,
        proxy_token=_TEST_AUTH_TOKEN,
    )

    assert built_browser_paths == ["/usr/bin/a", "/usr/bin/b"]
    assert first_manager is not second_manager
    workers_runtime_module.reset_primary_worker_manager()


def test_kubernetes_worker_manager_reuse_ignores_later_disk_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Manager lookups depend on backend settings only, so a broken on-disk edit cannot replace the manager."""
    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", "kubernetes")
    monkeypatch.setenv("MINDROOM_KUBERNETES_WORKER_IMAGE", "ghcr.io/mindroom-ai/mindroom:latest")
    monkeypatch.setenv("MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME", "mindroom-storage")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-6-astra\nrouter:\n  model: default\nagents: {}\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env=dict(os.environ),
    )

    class FakeKubernetesBackend:
        cleanup_locator = None
        backend_name = "kubernetes"
        idle_timeout_seconds = 60.0

        @classmethod
        def build(
            cls,
            runtime_paths: RuntimePaths,
            *,
            auth_token: str | None,
            current_launch: Callable[[], WorkerLaunchConfig],
            storage_root: Path,
        ) -> Self:
            del runtime_paths, auth_token, current_launch, storage_root
            return cls()

        def shutdown(self) -> None:
            return None

    _build_backend_as(monkeypatch, "configure_kubernetes_worker_backend", FakeKubernetesBackend)

    first_manager = _unleased_primary_worker_manager(
        runtime_paths,
        proxy_url=None,
        proxy_token=_TEST_AUTH_TOKEN,
    )
    config_path.write_text("models: [\n", encoding="utf-8")
    second_manager = _unleased_primary_worker_manager(
        runtime_paths,
        proxy_url=None,
        proxy_token=_TEST_AUTH_TOKEN,
    )

    assert first_manager is second_manager


def test_proxy_leases_worker_manager_from_backend_settings_and_grants_from_build_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A call leases the manager from backend settings, and its credential grants come from its build config."""
    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", "kubernetes")
    monkeypatch.setenv("MINDROOM_KUBERNETES_WORKER_IMAGE", "ghcr.io/mindroom-ai/mindroom:latest")
    monkeypatch.setenv("MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME", "mindroom-storage")
    monkeypatch.setenv("MINDROOM_SANDBOX_PROXY_URL", "http://sandbox-runner:8765")
    monkeypatch.setenv("MINDROOM_SANDBOX_PROXY_TOKEN", _TEST_AUTH_TOKEN)
    monkeypatch.setenv("MINDROOM_SANDBOX_EXECUTION_MODE", "all")
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env=dict(os.environ),
    )
    runtime_config = Config(defaults={"worker_grantable_credentials": ["gmail"]})
    lease_kwargs: dict[str, object] = {}
    request_kwargs: dict[str, object] = {}
    fake_worker_manager = object()

    def _fake_lease_primary_worker_manager(*_args: object, **kwargs: object) -> object:
        lease_kwargs.update(kwargs)
        return _static_worker_manager_lease(fake_worker_manager)

    def _fake_execute_worker_proxy_request(**kwargs: object) -> object:
        request_kwargs.update(kwargs)
        return "proxied"

    monkeypatch.setattr(sandbox_proxy_module, "lease_primary_worker_manager", _fake_lease_primary_worker_manager)
    monkeypatch.setattr(
        sandbox_proxy_module,
        "_worker_route",
        lambda **_kwargs: (WorkerRoute(protocol=WORKER_PROTOCOL_VERSION), None),
    )
    monkeypatch.setattr(sandbox_proxy_module, "execute_worker_proxy_request", _fake_execute_worker_proxy_request)

    result = call_proxy_sync_for_test(
        runtime_paths=runtime_paths,
        tool_name="calculator",
        function_name="add",
        args=(1, 2),
        kwargs={},
        credentials_manager=None,
        runtime_config=runtime_config,
    )

    assert result == "proxied"
    assert lease_kwargs == {
        "proxy_url": "http://sandbox-runner:8765",
        "proxy_token": _TEST_AUTH_TOKEN,
    }
    assert request_kwargs["worker_manager"] is fake_worker_manager
    assert request_kwargs["worker_grantable_credentials"] == frozenset({"gmail"})


# Credentials and tenant settings in fields no secret-name heuristic recognizes; none may reach a runner.
_LIVE_CONFIG_SECRETS = (
    "sk-live-model-key",
    "live-model-extra-kwarg-secret",
    "live-mcp-env-secret",
    "live-mcp-arg-secret",
    "live-mcp-url-secret",
    "live-mcp-header-secret",
    "live-journal-dsn-secret",
    "live-git-url-secret",
    "live-private-git-url-secret",
    "live-plugin-setting-secret",
    "live-tool-override-secret",
    "live-embedder-secret",
    "live-agent-instructions",
    "@live-owner:example.org",
)


def _live_primary_config(runtime_paths: RuntimePaths) -> Config:
    """Return a primary config whose `mind` agent and secrets exist only in the live config."""
    return Config.validate_with_runtime(
        {
            "models": {
                "default": {
                    "provider": "openai",
                    "id": "gpt-6-astra",
                    "api_key": "sk-live-model-key",
                    "extra_kwargs": {"default_headers": {"X-Upstream-Key": "live-model-extra-kwarg-secret"}},
                },
            },
            "router": {"model": "default"},
            "mcp_servers": {
                "files": {
                    "transport": "stdio",
                    "command": "npx",
                    "args": ["--api-key", "live-mcp-arg-secret"],
                    "env": {"AWS_SECRET_ACCESS_KEY": "live-mcp-env-secret"},
                },
                "remote": {
                    "transport": "streamable-http",
                    "url": "https://user:live-mcp-url-secret@mcp.example.org/mcp",
                    "headers": {"X-Upstream-Key": "live-mcp-header-secret"},
                },
            },
            "event_journal": {
                "backend": "postgres",
                "database_url": "postgresql://mindroom:live-journal-dsn-secret@db/x",
            },
            "knowledge_bases": {
                "docs": {
                    "path": "./docs",
                    "git": {"repo_url": "https://oauth2:live-git-url-secret@git.example.org/docs.git"},
                },
            },
            "plugins": [
                {"path": "./plugins/live", "enabled": False, "settings": {"key": "live-plugin-setting-secret"}},
            ],
            "memory": {
                "backend": "mem0",
                "embedder": {"provider": "openai", "config": {"api_key": "live-embedder-secret"}},
            },
            "administrators": ["@live-owner:example.org"],
            "agents": {
                "mind": {
                    "display_name": "Mind",
                    "memory_backend": "file",
                    "instructions": ["live-agent-instructions"],
                    "tools": [
                        "shell",
                        {"custom_api": {"base_url": "https://user:live-tool-override-secret@api.example.org"}},
                    ],
                },
                "vault": {
                    "display_name": "Vault",
                    "private": {
                        "per": "user",
                        "knowledge": {
                            "path": "notes",
                            "git": {"repo_url": "https://oauth2:live-private-git-url-secret@git.example.org/notes.git"},
                        },
                    },
                },
            },
        },
        runtime_paths,
    )


def _assert_no_live_config_secrets(sent: object) -> None:
    body = json.dumps(sent)
    assert [secret for secret in _LIVE_CONFIG_SECRETS if secret in body] == []


def _mind_tool_runtime_context(runtime_paths: RuntimePaths, config: Config) -> object:
    return make_test_tool_runtime_context(
        agent_name="mind",
        target=MessageTarget.resolve(room_id="!room:example.org", thread_id=None, reply_to_event_id=None),
        requester_id="@alice:example.org",
        client=object(),
        config=config,
        runtime_paths=runtime_paths,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
    )


@pytest.mark.parametrize(
    ("worker_backend", "sends_snapshot"),
    [("static_runner", True), ("kubernetes", True), ("docker", False)],
)
def test_proxy_sends_live_config_snapshot_without_secrets(
    monkeypatch: pytest.MonkeyPatch,
    worker_backend: str,
    sends_snapshot: bool,
) -> None:
    """Runners mount no config, so each call carries only the live config fields they resolve agents from."""
    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", worker_backend)
    runtime_paths = _configure_proxy_runtime(monkeypatch, proxy_url="http://sandbox-runner:8766")
    live_config = _live_primary_config(runtime_paths)
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        sandbox_proxy_module,
        "lease_primary_worker_manager",
        lambda *_args, **_kwargs: _static_worker_manager_lease(_TrackingWorkerManager()),
    )
    monkeypatch.setattr("mindroom.tool_system.sandbox_proxy.httpx.Client", _recording_client_class(captured=captured))

    call_proxy_sync_for_test(
        runtime_paths=runtime_paths,
        tool_name="calculator",
        function_name="add",
        args=(1, 2),
        kwargs={},
        credentials_manager=None,
        runtime_config=live_config,
        worker_target=_worker_target(runtime_paths, None, "mind", _MIND_EXECUTION_IDENTITY),
    )

    assert (captured["json"]["config_snapshot"] is not None) is sends_snapshot
    _assert_no_live_config_secrets(captured["json"])
    if sends_snapshot:
        assert captured["json"]["config_snapshot"] == {
            "agents": {
                "mind": {"display_name": "Mind", "memory_backend": "file", "tools": ["shell", "custom_api"]},
                "vault": {"display_name": "Vault", "private": {"per": "user", "knowledge": {"path": "notes"}}},
            },
            "plugins": [{"path": "./plugins/live", "enabled": False}],
            "memory": {"backend": "mem0"},
            "knowledge_bases": {"docs": {"path": "./docs"}},
        }


def _forward_proxy_to_seeded_runner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    runner_execution_mode: str = "inprocess",
    proxy_tools: frozenset[str] = frozenset({"shell"}),
) -> tuple[RuntimePaths, Path, list[dict[str, Any]]]:
    """Forward the primary's proxy calls to a real runner app whose seed config lacks the live `mind` agent."""
    storage_root = tmp_path / "storage"
    seed_config_path = tmp_path / "seed" / "config.yaml"
    seed_config_path.parent.mkdir()
    seed_config_path.write_text("models: {}\nagents: {}\n", encoding="utf-8")
    runner_paths = resolve_runtime_paths(
        config_path=seed_config_path,
        storage_path=storage_root,
        process_env={
            "MINDROOM_SANDBOX_RUNNER_MODE": "true",
            "MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE": runner_execution_mode,
        },
    )
    sandbox_runner_module.initialize_sandbox_runner_app(sandbox_runner_app, runner_paths, runner_token=_TEST_AUTH_TOKEN)
    runner = TestClient(sandbox_runner_app)
    monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(storage_root))
    primary_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8766",
        execution_mode="selective",
        proxy_tools=set(proxy_tools),
    )
    sent_payloads: list[dict[str, Any]] = []

    def forward_to_runner(url: str, payload: dict[str, Any]) -> object:
        sent_payloads.append(payload)
        # The runner is another process, so it sees nothing of the primary's context.
        response = contextvars.Context().run(
            runner.post,
            url.removeprefix("http://sandbox-runner:8766"),
            json=payload,
            headers={"x-mindroom-sandbox-token": _TEST_AUTH_TOKEN},
        )
        # A call slowed past the runner's progress or keepalive interval streams those lines before its result line.
        # Split on newlines alone, as the primary's client does, since JSON text may hold other Unicode line separators.
        return json.loads(response.text.rstrip("\n").rpartition("\n")[2])

    monkeypatch.setattr(
        "mindroom.tool_system.sandbox_proxy.httpx.Client",
        _recording_client_class(
            responder=forward_to_runner,
            health=lambda url: runner.get(url.removeprefix("http://sandbox-runner:8766")),
        ),
    )
    return primary_paths, agent_workspace_root_path(storage_root, "mind"), sent_payloads


_MIND_EXECUTION_IDENTITY = ToolExecutionIdentity(
    channel="matrix",
    agent_name="mind",
    requester_id="@alice:example.org",
    room_id="!room:example.org",
    thread_id=None,
    resolved_thread_id=None,
    session_id="session-1",
)


@pytest.mark.asyncio
async def test_static_runner_runs_shell_for_agent_added_after_seeding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A proxied shell call for an agent missing from the runner's seed config runs in that agent's workspace."""
    primary_paths, workspace, sent_payloads = _forward_proxy_to_seeded_runner(monkeypatch, tmp_path)
    live_config = _live_primary_config(primary_paths)
    (workspace / ".mindroom").mkdir(parents=True)
    (workspace / ".mindroom" / "worker-env.sh").write_text("export MIND_HOOK=from-mind-workspace\n", encoding="utf-8")
    tool = get_tool_by_name(
        "shell",
        primary_paths,
        runtime_config=live_config,
        tool_init_overrides={"base_dir": str(workspace)},
        worker_target=_worker_target(primary_paths, None, "mind", _MIND_EXECUTION_IDENTITY),
        tools=builtin_tool_registry(),
    )
    entrypoint = _offered_entrypoint(tool, "run_shell_command")

    with tool_runtime_context(_mind_tool_runtime_context(primary_paths, live_config)):
        result = await entrypoint(["bash", "-c", 'printf "%s" "$MIND_HOOK"'])

    assert isinstance(result, str)
    assert result.endswith("from-mind-workspace")
    _assert_no_live_config_secrets(sent_payloads)


@pytest.mark.asyncio
async def test_minimal_cli_environment_reaches_the_agents_worker_shell(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A minimal response's CLI environment travels with each command into the agent's ordinary worker shell."""
    primary_paths, workspace, sent_payloads = _forward_proxy_to_seeded_runner(monkeypatch, tmp_path)
    live_config = _live_primary_config(primary_paths)
    workspace.mkdir(parents=True)
    tool = get_tool_by_name(
        "shell",
        primary_paths,
        runtime_config=live_config,
        tool_init_overrides={"base_dir": str(workspace)},
        worker_target=_worker_target(primary_paths, None, "mind", _MIND_EXECUTION_IDENTITY),
        tools=builtin_tool_registry(),
    )
    entrypoint = _offered_entrypoint(tool, "run_shell_command")
    command = [
        "bash",
        "-c",
        'printf "%s|%s|%s" "$MINDROOM_AGENT_CLI_URL" "$MINDROOM_AGENT_CLI_TOKEN" "$MINDROOM_AGENT_CLI_WINDOW"',
    ]

    with tool_runtime_context(_mind_tool_runtime_context(primary_paths, live_config)):
        with bound_agent_cli_shell_env(
            AgentCliShellEnv("http://host.docker.internal:8765", "response-grant"),
            window="bash-1",
        ):
            minimal = await entrypoint(command)
        ordinary = await entrypoint(command)

    assert isinstance(minimal, str)
    assert minimal.endswith("http://host.docker.internal:8765|response-grant|bash-1")
    assert isinstance(ordinary, str)
    assert ordinary.endswith("||")
    assert "response-grant" not in json.dumps(sent_payloads[-1])


def _shell_call(pid_file: Path) -> tuple[str, str, tuple[object, ...], dict[str, object]]:
    return "shell", "run_shell_command", (["bash", "-c", f"echo $$ > {pid_file}; exec sleep 30"],), {"timeout": 60}


def _python_call(pid_file: Path) -> tuple[str, str, tuple[object, ...], dict[str, object]]:
    code = f"import os, pathlib, time\npathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\ntime.sleep(30)"
    return "python", "run_python_code", (code,), {}


@requires_linux()
@pytest.mark.asyncio
@pytest.mark.parametrize("runner_execution_mode", ["inprocess", "subprocess", "forkserver"])
@pytest.mark.parametrize("worker_call", [_shell_call, _python_call], ids=["async-shell", "sync-python"])
async def test_cancelling_a_worker_call_stops_its_work(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    runner_execution_mode: str,
    worker_call: Callable[[Path], tuple[str, str, tuple[object, ...], dict[str, object]]],
) -> None:
    """Stopping a response stops the work its worker call started, whether the tool function is async or sync."""
    pid_file = tmp_path / "work.pid"
    tool_name, function_name, args, kwargs = worker_call(pid_file)
    primary_paths, workspace, _sent_payloads = _forward_proxy_to_seeded_runner(
        monkeypatch,
        tmp_path,
        runner_execution_mode=runner_execution_mode,
        proxy_tools=frozenset({tool_name}),
    )
    live_config = _live_primary_config(primary_paths)
    workspace.mkdir(parents=True)
    tool = get_tool_by_name(
        tool_name,
        primary_paths,
        runtime_config=live_config,
        tool_init_overrides={"base_dir": str(workspace)},
        worker_target=_worker_target(primary_paths, None, "mind", _MIND_EXECUTION_IDENTITY),
        tools=builtin_tool_registry(),
    )
    # A function that runs in a worker is offered as its async stand-in.
    entrypoint = _offered_entrypoint(tool, function_name)
    assert inspect.iscoroutinefunction(entrypoint)

    with tool_runtime_context(_mind_tool_runtime_context(primary_paths, live_config)):
        call = asyncio.create_task(entrypoint(*args, **kwargs))
        async with asyncio.timeout(30):
            while not pid_file.exists() or not pid_file.read_text().strip():  # noqa: ASYNC110 - the worker command signals only through its pid file
                await asyncio.sleep(0.05)
        pid = int(pid_file.read_text())
        # Work that ran inside this test process could not be killed separately.
        assert pid != os.getpid()
        call.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await call

    try:
        await assert_linux_pid_not_running(pid)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


def _proxied_shell(
    monkeypatch: pytest.MonkeyPatch,
    responder: Callable[[str, dict[str, Any]], object],
) -> tuple[Callable[..., Any], list[tuple[str, dict[str, Any]]]]:
    calls: list[tuple[str, dict[str, Any]]] = []
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="selective",
        proxy_tools={"shell"},
    )
    monkeypatch.setattr(
        "mindroom.tool_system.sandbox_proxy.httpx.Client",
        _recording_client_class(captured_calls=calls, responder=responder),
    )
    entrypoint = _offered_entrypoint(
        get_tool_by_name("shell", runtime_paths, worker_target=None, tools=builtin_tool_registry()),
        "run_shell_command",
    )
    return entrypoint, calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "function_name", "arguments", "expected_timeout"),
    [
        ("shell", "run_shell_command", {"args": "make", "timeout": 600}, 630.0),
        ("shell", "run_shell_command", {"args": "make"}, 150.0),
        ("browser", "browser_control", {"action": "status"}, 150.0),
    ],
)
async def test_a_worker_call_waits_as_long_as_its_runner(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    function_name: str,
    arguments: dict[str, object],
    expected_timeout: float,
) -> None:
    """The primary waits for a worker call as long as the runner lets it run, so the call's own answer arrives."""
    captured: dict[str, Any] = {}
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="selective",
        proxy_tools={tool_name},
    )
    monkeypatch.setattr("mindroom.tool_system.sandbox_proxy.httpx.Client", _recording_client_class(captured=captured))
    entrypoint = _offered_entrypoint(
        get_tool_by_name(tool_name, runtime_paths, worker_target=None, tools=builtin_tool_registry()),
        function_name,
    )

    await entrypoint(**arguments)

    assert captured["timeout"] == expected_timeout


def test_only_the_browser_tool_waits_for_its_own_time_limit() -> None:
    """Native browser MCP calls have no limit of their own, so they keep the configured timeouts."""
    assert browser_request_budget_seconds("browser", "browser_control") == 150.0
    assert browser_request_budget_seconds("browser_mcp", "browser_snapshot") == 0.0


@pytest.mark.asyncio
async def test_cancelled_worker_call_asks_the_runner_to_stop_that_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancelling a dispatched call returns at once and posts the call's request ID to the runner's cancel route."""
    dispatched = threading.Event()
    stopped = threading.Event()
    runner_answers = threading.Event()

    def responder(url: str, _payload: dict[str, Any]) -> object:
        if url.endswith("/execute/cancel"):
            stopped.set()
            # The caller must not wait for the runner to answer its stop request.
            assert runner_answers.wait(10)
            return {"cancelled": True}
        dispatched.set()
        assert stopped.wait(10)
        return {"ok": False, "error": "Tool call was cancelled.", "failure_kind": "tool"}

    entrypoint, calls = _proxied_shell(monkeypatch, responder)
    call = asyncio.create_task(entrypoint("sleep 30"))
    assert await asyncio.to_thread(dispatched.wait, 10)
    call.cancel()
    cancelled_at = time.monotonic()
    try:
        with pytest.raises(asyncio.CancelledError):
            await call
        # A stop request sent on the event loop would block it until the runner answered.
        assert time.monotonic() - cancelled_at < 2
    finally:
        runner_answers.set()

    assert await asyncio.to_thread(stopped.wait, 10)
    (_, execute), (cancel_url, cancel) = [(url, payload) for url, payload in calls if "/execute" in url]
    assert cancel_url == "http://sandbox-runner:8765/api/sandbox-runner/execute/cancel"
    assert cancel == {"protocol": WORKER_PROTOCOL_VERSION, "request_id": execute["request_id"]}


@pytest.mark.asyncio
async def test_worker_call_cancelled_before_dispatch_is_never_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A call cancelled while its worker is still being prepared never reaches the worker."""
    entrypoint, calls = _proxied_shell(monkeypatch, lambda _url, _payload: {"ok": True, "result": "ran"})
    preparing = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    worker_route = sandbox_proxy_module._worker_route
    execute = sandbox_proxy_module.execute_worker_proxy_request

    def slow_routing(**kwargs: object) -> object:
        preparing.set()
        assert release.wait(10)
        return worker_route(**kwargs)

    def tracked_execute(**kwargs: object) -> object:
        try:
            return execute(**kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(sandbox_proxy_module, "_worker_route", slow_routing)
    monkeypatch.setattr(sandbox_proxy_module, "execute_worker_proxy_request", tracked_execute)
    call = asyncio.create_task(entrypoint("echo never"))
    assert await asyncio.to_thread(preparing.wait, 10)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    release.set()

    assert await asyncio.to_thread(finished.wait, 10)
    assert not [url for url, _payload in calls if url.endswith("/execute")]


def test_static_runner_saves_attachment_for_agent_added_after_seeding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An attachment save for an agent missing from the runner's seed config lands in that agent's workspace."""
    primary_paths, workspace, sent_payloads = _forward_proxy_to_seeded_runner(monkeypatch, tmp_path)
    live_config = _live_primary_config(primary_paths)
    payload_bytes = b"live-agent"

    receipt = sandbox_proxy_module.save_attachment_to_worker(
        runtime_paths=primary_paths,
        runtime_config=live_config,
        worker_target=_worker_target(primary_paths, None, "mind", _MIND_EXECUTION_IDENTITY),
        workspace_root=workspace,
        attachment_id="att_sample",
        mindroom_output_path="inputs/live.bin",
        payload_bytes=payload_bytes,
    )

    assert receipt is not None
    assert (workspace / "inputs" / "live.bin").read_bytes() == payload_bytes
    assert sent_payloads[0]["config_snapshot"] is not None
    _assert_no_live_config_secrets(sent_payloads)


def test_static_runner_views_file_for_agent_added_after_seeding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A file view for an agent missing from the runner's seed config reads from that agent's workspace."""
    primary_paths, workspace, sent_payloads = _forward_proxy_to_seeded_runner(monkeypatch, tmp_path)
    live_config = _live_primary_config(primary_paths)
    workspace.mkdir(parents=True)
    PILImage.new("RGB", (8, 6), "red").save(workspace / "plot.png", format="PNG")

    result = sandbox_proxy_module.view_file_from_worker(
        runtime_paths=primary_paths,
        runtime_config=live_config,
        worker_target=_worker_target(primary_paths, None, "mind", _MIND_EXECUTION_IDENTITY),
        workspace_root=None,
        path="plot.png",
    )

    assert result is not None
    assert result.images
    assert result.images[0].mime_type == "image/png"
    assert sent_payloads[0]["config_snapshot"] is not None
    _assert_no_live_config_secrets(sent_payloads)


def test_explicit_worker_tools_can_use_kubernetes_backend_without_proxy_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Worker-routed tools should stay proxy-enabled when the Kubernetes backend provides worker handles directly."""
    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", "kubernetes")
    monkeypatch.setenv("MINDROOM_KUBERNETES_WORKER_IMAGE", "ghcr.io/mindroom-ai/mindroom:latest")
    monkeypatch.setenv("MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME", "mindroom-storage")
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url=None,
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="off",
    )

    assert _runs_in_worker(runtime_paths, "shell", ["shell"]) is True
    assert _runs_in_worker(runtime_paths, "shell", None) is False


def test_kubernetes_backend_keeps_unscoped_env_routing_enabled_without_proxy_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unscoped agents should still route through dedicated workers on the Kubernetes backend."""
    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", "kubernetes")
    monkeypatch.setenv("MINDROOM_KUBERNETES_WORKER_IMAGE", "ghcr.io/mindroom-ai/mindroom:latest")
    monkeypatch.setenv("MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME", "mindroom-storage")
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url=None,
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="selective",
        proxy_tools={"shell"},
    )

    assert _runs_in_worker(runtime_paths, "shell", None) is True
    assert _runs_in_worker(runtime_paths, "calculator", None) is False


def test_kubernetes_backend_keeps_wrapping_when_required_config_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kubernetes routing should stay enabled so misconfiguration fails closed at call time."""
    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", "kubernetes")
    monkeypatch.delenv("MINDROOM_KUBERNETES_WORKER_IMAGE", raising=False)
    monkeypatch.delenv("MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME", raising=False)
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url=None,
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode=None,
    )

    assert _runs_in_worker(runtime_paths, "shell", ["shell"]) is True


def test_kubernetes_backend_keeps_wrapping_when_proxy_token_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kubernetes routing should stay enabled so missing auth fails closed at call time."""
    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", "kubernetes")
    monkeypatch.setenv("MINDROOM_KUBERNETES_WORKER_IMAGE", "ghcr.io/mindroom-ai/mindroom:latest")
    monkeypatch.setenv("MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME", "mindroom-storage")
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url=None,
        proxy_token=None,
        execution_mode=None,
    )

    assert _runs_in_worker(runtime_paths, "shell", ["shell"]) is True


@pytest.mark.asyncio
async def test_kubernetes_backend_misconfiguration_raises_instead_of_running_locally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Misconfigured Kubernetes worker routing should raise rather than executing in the primary runtime."""
    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", "kubernetes")
    monkeypatch.delenv("MINDROOM_KUBERNETES_WORKER_IMAGE", raising=False)
    monkeypatch.delenv("MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME", raising=False)
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url=None,
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="off",
    )
    runtime_config = load_config(runtime_paths)

    tool = get_tool_by_name(
        "shell",
        runtime_paths,
        runs_in_worker=_runs_in_worker(runtime_paths, "shell", ["shell"]),
        worker_target=_worker_target(runtime_paths, None, "code", None),
        tools=builtin_tool_registry(),
    )
    entrypoint = _offered_entrypoint(tool, "run_shell_command")

    runtime_context = make_test_tool_runtime_context(
        agent_name="code",
        target=MessageTarget.resolve(
            room_id="!room:example.org",
            thread_id=None,
            reply_to_event_id=None,
        ),
        requester_id="@user:example.org",
        client=object(),
        config=runtime_config,
        runtime_paths=runtime_paths,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
    )

    with (
        tool_runtime_context(runtime_context),
        pytest.raises(
            WorkerBackendError,
            match="MINDROOM_KUBERNETES_WORKER_IMAGE",
        ),
    ):
        await entrypoint("pwd")


@pytest.mark.asyncio
async def test_sync_worker_routed_tool_surfaces_progress_in_real_async_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A sync worker-routed tool's async proxy emits worker progress before the call result resolves."""
    release_execute = threading.Event()
    execution_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
    )

    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="off",
    )

    class _FakeWorkerManager:
        cleanup_locator = None

        def ensure_worker(self, spec: WorkerSpec, *, now: float | None = None, progress_sink: object = None) -> object:
            del now
            assert progress_sink is not None
            progress_sink(
                WorkerReadyProgress(
                    phase="cold_start",
                    worker_key=spec.worker_key,
                    backend_name="kubernetes",
                    elapsed_seconds=1.0,
                ),
            )
            return WorkerHandle(
                worker_id="worker-1",
                worker_key=spec.worker_key,
                endpoint="http://worker/api/sandbox-runner/execute",
                auth_token=_TEST_AUTH_TOKEN,
                status="ready",
                backend_name="kubernetes",
                last_used_at=0.0,
                created_at=0.0,
            )

        def touch_worker(self, worker_key: str, *, now: float | None = None) -> object:
            del worker_key, now
            return None

    class _BlockingClient(_StreamsFromPost):
        def __init__(self, *, timeout: float) -> None:
            del timeout

        def __enter__(self) -> Self:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return

        def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> _FakeResponse:
            del json, headers
            assert url == "http://worker/api/sandbox-runner/execute"
            assert release_execute.wait(timeout=10.0)
            return _FakeResponse({"ok": True, "result": "proxied"})

    tool = get_tool_by_name(
        "file",
        runtime_paths,
        tool_init_overrides={"base_dir": str(tmp_path)},
        runs_in_worker=_runs_in_worker(runtime_paths, "file", ["file"]),
        worker_target=_worker_target(runtime_paths, "shared", "code", execution_identity),
        tools=builtin_tool_registry(),
    )
    # The sync function runs through its async proxy in async runs, so it can be stopped.
    assert inspect.iscoroutinefunction(_offered_entrypoint(tool, "read_file"))
    middleware = detached_policy(
        HookRegistry.empty(),
        config=Config(agents={"code": AgentConfig(display_name="Code")}, models={}),
        runtime_paths=runtime_paths,
    )
    read_file = next(function for function in adapt_toolkit(tool).functions if function.name == "read_file")

    progress_queue: asyncio.Queue[object] = asyncio.Queue()
    with (
        patch.object(
            sandbox_proxy_module,
            "lease_primary_worker_manager",
            lambda *_args, **_kwargs: _static_worker_manager_lease(_FakeWorkerManager()),
        ),
        patch("mindroom.tool_system.sandbox_proxy.httpx.Client", _BlockingClient),
        worker_progress_pump_scope(asyncio.get_running_loop(), progress_queue),
    ):
        call_task = asyncio.create_task(
            execute_tool_call(
                read_file,
                {"file_name": "demo.txt"},
                ToolCallContext(run_id="run-1", session_id="session-1", call_id="call-1", user_id=None),
                middleware=(middleware,),
            ),
        )

        try:
            progress_event = await asyncio.wait_for(progress_queue.get(), timeout=5.0)
            assert progress_event.tool_name == "file"
            assert progress_event.function_name == "read_file"
            assert progress_event.progress.phase == "cold_start"
            assert call_task.done() is False
        finally:
            # Join the worker before restoring patches, including on assertion failure.
            release_execute.set()
            outcome = await call_task

    assert not outcome.is_error
    assert outcome.content == "proxied"


def test_worker_routed_tool_error_does_not_record_worker_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Ordinary proxied tool errors should fail the call without tearing down the worker."""
    execution_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
    )
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="off",
    )

    class _ToolErrorClient(_StreamsFromPost):
        def __init__(self, *, timeout: float) -> None:
            del timeout

        def __enter__(self) -> Self:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return

        def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> _FakeResponse:
            del json, headers
            assert url == "http://worker/api/sandbox-runner/execute"
            return _FakeResponse(
                {
                    "ok": False,
                    "error": "Sandbox tool execution failed: FileNotFoundError: missing.txt",
                    "failure_kind": "tool",
                },
            )

    manager = _TrackingWorkerManager()
    tool = get_tool_by_name(
        "file",
        runtime_paths,
        tool_init_overrides={"base_dir": str(tmp_path)},
        runs_in_worker=_runs_in_worker(runtime_paths, "file", ["file"]),
        worker_target=_worker_target(runtime_paths, "shared", "code", execution_identity),
        tools=builtin_tool_registry(),
    )
    entrypoint = blocking_entrypoint(tool, "read_file")

    with (
        patch.object(
            sandbox_proxy_module,
            "lease_primary_worker_manager",
            lambda *_args, **_kwargs: _static_worker_manager_lease(manager),
        ),
        patch("mindroom.tool_system.sandbox_proxy.httpx.Client", _ToolErrorClient),
        pytest.raises(RuntimeError, match=r"FileNotFoundError: missing\.txt"),
    ):
        entrypoint("missing.txt")

    assert manager.touched == [_worker_target(runtime_paths, "shared", "code", execution_identity).worker_key]
    assert manager.failures == []


def test_worker_routed_oauth_connection_result_survives_proxy_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Structured OAuth prompts returned by the runner should stay tool-visible."""
    execution_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
    )
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="off",
    )
    worker_target = _worker_target(runtime_paths, "shared", "code", execution_identity)
    manager = _TrackingWorkerManager()
    expected_result = {
        "error": "Google Drive is not connected for this agent.",
        "oauth_connection_required": True,
        "provider": "google_drive",
        "connect_url": "/api/oauth/google_drive/connect?agent_name=general",
    }

    class _OAuthResultClient(_StreamsFromPost):
        def __init__(self, *, timeout: float) -> None:
            del timeout

        def __enter__(self) -> Self:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return

        def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> _FakeResponse:
            del json, headers
            assert url == "http://worker/api/sandbox-runner/execute"
            return _FakeResponse({"ok": True, "result": expected_result})

    tool = get_tool_by_name(
        "file",
        runtime_paths,
        tool_init_overrides={"base_dir": str(tmp_path)},
        runs_in_worker=_runs_in_worker(runtime_paths, "file", ["file"]),
        worker_target=worker_target,
        tools=builtin_tool_registry(),
    )
    entrypoint = blocking_entrypoint(tool, "read_file")

    with (
        patch.object(
            sandbox_proxy_module,
            "lease_primary_worker_manager",
            lambda *_args, **_kwargs: _static_worker_manager_lease(manager),
        ),
        patch("mindroom.tool_system.sandbox_proxy.httpx.Client", _OAuthResultClient),
    ):
        result = entrypoint("missing.txt")

    assert result == expected_result
    assert manager.touched == [worker_target.worker_key]
    assert manager.failures == []


def test_worker_routed_worker_failure_records_worker_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Worker-level runner failures should still evict broken workers."""
    execution_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
    )
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="off",
    )
    worker_target = _worker_target(runtime_paths, "shared", "code", execution_identity)
    assert worker_target.worker_key is not None
    manager = _TrackingWorkerManager()

    class _WorkerFailureClient(_StreamsFromPost):
        def __init__(self, *, timeout: float) -> None:
            del timeout

        def __enter__(self) -> Self:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return

        def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> _FakeResponse:
            del json, headers
            assert url == "http://worker/api/sandbox-runner/execute"
            return _FakeResponse(
                {
                    "ok": False,
                    "error": "Sandbox subprocess timed out.",
                    "failure_kind": "worker",
                },
            )

    tool = get_tool_by_name(
        "file",
        runtime_paths,
        tool_init_overrides={"base_dir": str(tmp_path)},
        runs_in_worker=_runs_in_worker(runtime_paths, "file", ["file"]),
        worker_target=worker_target,
        tools=builtin_tool_registry(),
    )
    entrypoint = blocking_entrypoint(tool, "read_file")

    with (
        patch.object(
            sandbox_proxy_module,
            "lease_primary_worker_manager",
            lambda *_args, **_kwargs: _static_worker_manager_lease(manager),
        ),
        patch("mindroom.tool_system.sandbox_proxy.httpx.Client", _WorkerFailureClient),
        pytest.raises(RuntimeError, match=r"Sandbox subprocess timed out\."),
    ):
        entrypoint("missing.txt")

    assert manager.touched == []
    assert manager.failures == [(worker_target.worker_key, "Sandbox subprocess timed out.")]


def test_worker_routed_legacy_structured_failure_records_worker_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Structured failures without failure_kind should fail closed as worker failures."""
    execution_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
    )
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="off",
    )
    worker_target = _worker_target(runtime_paths, "shared", "code", execution_identity)
    assert worker_target.worker_key is not None
    manager = _TrackingWorkerManager()

    class _LegacyFailureClient(_StreamsFromPost):
        def __init__(self, *, timeout: float) -> None:
            del timeout

        def __enter__(self) -> Self:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return

        def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> _FakeResponse:
            del json, headers
            assert url == "http://worker/api/sandbox-runner/execute"
            return _FakeResponse(
                {
                    "ok": False,
                    "error": "Sandbox subprocess timed out.",
                },
            )

    tool = get_tool_by_name(
        "file",
        runtime_paths,
        tool_init_overrides={"base_dir": str(tmp_path)},
        runs_in_worker=_runs_in_worker(runtime_paths, "file", ["file"]),
        worker_target=worker_target,
        tools=builtin_tool_registry(),
    )
    entrypoint = blocking_entrypoint(tool, "read_file")

    with (
        patch.object(
            sandbox_proxy_module,
            "lease_primary_worker_manager",
            lambda *_args, **_kwargs: _static_worker_manager_lease(manager),
        ),
        patch("mindroom.tool_system.sandbox_proxy.httpx.Client", _LegacyFailureClient),
        pytest.raises(RuntimeError, match=r"Sandbox subprocess timed out\."),
    ):
        entrypoint("missing.txt")

    assert manager.touched == []
    assert manager.failures == [(worker_target.worker_key, "Sandbox subprocess timed out.")]


@pytest.mark.parametrize(
    "response_payload",
    [["not", "an", "object"], {"ok": True, "result": "done", "unexpected": "field"}],
    ids=["non-object", "unknown-field"],
)
def test_worker_routed_malformed_success_payload_records_worker_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    response_payload: object,
) -> None:
    """Malformed 2xx worker responses should evict the worker before raising."""
    execution_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
    )
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="off",
    )
    worker_target = _worker_target(runtime_paths, "shared", "code", execution_identity)
    assert worker_target.worker_key is not None
    manager = _TrackingWorkerManager()

    class _MalformedSuccessClient(_StreamsFromPost):
        def __init__(self, *, timeout: float) -> None:
            del timeout

        def __enter__(self) -> Self:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return

        def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> _FakeResponse:
            del json, headers
            assert url == "http://worker/api/sandbox-runner/execute"
            return _FakeResponse(response_payload)

    tool = get_tool_by_name(
        "file",
        runtime_paths,
        tool_init_overrides={"base_dir": str(tmp_path)},
        runs_in_worker=_runs_in_worker(runtime_paths, "file", ["file"]),
        worker_target=worker_target,
        tools=builtin_tool_registry(),
    )
    entrypoint = blocking_entrypoint(tool, "read_file")
    error = "Sandbox execute returned a malformed response: 1 invalid field(s)."

    with (
        patch.object(
            sandbox_proxy_module,
            "lease_primary_worker_manager",
            lambda *_args, **_kwargs: _static_worker_manager_lease(manager),
        ),
        patch("mindroom.tool_system.sandbox_proxy.httpx.Client", _MalformedSuccessClient),
        pytest.raises(RuntimeError, match=re.escape(error)),
    ):
        entrypoint("missing.txt")

    assert manager.touched == []
    assert manager.failures == [(worker_target.worker_key, error)]


@pytest.mark.parametrize(
    ("status_code", "payload", "error", "worker_failure"),
    [
        (
            400,
            {"ok": False, "error": "credential_overrides must be supplied via lease_id.", "failure_kind": "tool"},
            "credential_overrides must be supplied via lease_id.",
            False,
        ),
        (
            422,
            {"ok": False, "error": "Input should be a valid dictionary", "failure_kind": "tool"},
            "Input should be a valid dictionary",
            False,
        ),
        (
            404,
            {"ok": False, "error": "Tool 'file' does not expose 'read_file'.", "failure_kind": "tool"},
            "Tool 'file' does not expose 'read_file'.",
            False,
        ),
        (
            503,
            {"ok": False, "error": "Worker is not ready.", "failure_kind": "worker"},
            "Worker is not ready.",
            True,
        ),
        (
            400,
            {"detail": "credential_overrides must be supplied via lease_id."},
            "credential_overrides must be supplied via lease_id.",
            True,
        ),
        (
            422,
            {"detail": [{"msg": "Input should be a valid dictionary", "type": "dict_type"}]},
            "Input should be a valid dictionary",
            True,
        ),
        (401, {"detail": "Unauthorized sandbox runner request"}, "Unauthorized sandbox runner request", True),
        (404, {"detail": "Not Found"}, "Not Found", True),
    ],
    ids=[
        "typed-400",
        "typed-422",
        "typed-404",
        "typed-worker-503",
        "untyped-400",
        "untyped-422",
        "untyped-401",
        "route-missing-404",
    ],
)
def test_worker_routed_http_error_spares_the_worker_only_for_a_typed_tool_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    status_code: int,
    payload: dict[str, object],
    error: str,
    worker_failure: bool,
) -> None:
    """An HTTP error counts against the worker unless its body is the worker's typed account blaming the request."""
    execution_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
    )
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="off",
    )
    worker_target = _worker_target(runtime_paths, "shared", "code", execution_identity)
    assert worker_target.worker_key is not None
    manager = _TrackingWorkerManager()
    tool = get_tool_by_name(
        "file",
        runtime_paths,
        tool_init_overrides={"base_dir": str(tmp_path)},
        runs_in_worker=_runs_in_worker(runtime_paths, "file", ["file"]),
        worker_target=worker_target,
        tools=builtin_tool_registry(),
    )
    entrypoint = blocking_entrypoint(tool, "read_file")

    with (
        patch.object(
            sandbox_proxy_module,
            "lease_primary_worker_manager",
            lambda *_args, **_kwargs: _static_worker_manager_lease(manager),
        ),
        patch(
            "mindroom.tool_system.sandbox_proxy.httpx.Client",
            _http_status_client_class(status_code=status_code, payload=payload),
        ),
        pytest.raises(_WorkerHttpError, match=re.escape(error)),
    ):
        entrypoint("missing.txt")

    if worker_failure:
        assert manager.touched == []
        assert [worker_key for worker_key, _reason in manager.failures] == [worker_target.worker_key]
    else:
        assert manager.touched == [worker_target.worker_key]
        assert manager.failures == []


def test_explicit_worker_tools_can_use_docker_backend_without_proxy_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Worker-routed tools should stay proxy-enabled when the Docker backend provides worker handles directly."""
    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", "docker")
    monkeypatch.setenv("MINDROOM_DOCKER_WORKER_IMAGE", "ghcr.io/mindroom-ai/mindroom:latest")
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url=None,
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="off",
    )

    assert _runs_in_worker(runtime_paths, "shell", ["shell"]) is True
    assert _runs_in_worker(runtime_paths, "shell", None) is False


def test_docker_backend_keeps_unscoped_env_routing_enabled_without_proxy_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unscoped agents should still route through dedicated workers on the Docker backend."""
    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", "docker")
    monkeypatch.setenv("MINDROOM_DOCKER_WORKER_IMAGE", "ghcr.io/mindroom-ai/mindroom:latest")
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url=None,
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="selective",
        proxy_tools={"shell"},
    )

    assert _runs_in_worker(runtime_paths, "shell", None) is True
    assert _runs_in_worker(runtime_paths, "calculator", None) is False


def test_docker_backend_keeps_wrapping_when_required_config_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Docker routing should stay enabled so misconfiguration fails closed at call time."""
    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", "docker")
    monkeypatch.delenv("MINDROOM_DOCKER_WORKER_IMAGE", raising=False)
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url=None,
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode=None,
    )

    assert _runs_in_worker(runtime_paths, "shell", ["shell"]) is True


@pytest.mark.asyncio
async def test_docker_backend_misconfiguration_raises_instead_of_running_locally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Misconfigured Docker worker routing should raise rather than executing in the primary runtime."""
    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", "docker")
    monkeypatch.delenv("MINDROOM_DOCKER_WORKER_IMAGE", raising=False)
    runtime_paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url=None,
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="off",
    )
    runtime_config = load_config(runtime_paths)

    tool = get_tool_by_name(
        "shell",
        runtime_paths,
        runs_in_worker=_runs_in_worker(runtime_paths, "shell", ["shell"]),
        worker_target=_worker_target(runtime_paths, None, "code", None),
        tools=builtin_tool_registry(),
    )
    entrypoint = _offered_entrypoint(tool, "run_shell_command")

    runtime_context = make_test_tool_runtime_context(
        agent_name="code",
        target=MessageTarget.resolve(
            room_id="!room:example.org",
            thread_id=None,
            reply_to_event_id=None,
        ),
        requester_id="@user:example.org",
        client=object(),
        config=runtime_config,
        runtime_paths=runtime_paths,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
    )

    with (
        tool_runtime_context(runtime_context),
        pytest.raises(
            WorkerBackendError,
            match="MINDROOM_DOCKER_WORKER_IMAGE",
        ),
    ):
        await entrypoint("pwd")


class TestWorkerToolsOverride:
    """Tests for placement under an agent's explicit worker_tools list."""

    def test_override_none_defers_to_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """None override should defer to the standard sandbox-proxy env controls."""
        runtime_paths = _configure_proxy_runtime(
            monkeypatch,
            proxy_url="http://sandbox:8765",
            execution_mode="selective",
            proxy_tools={"shell"},
        )

        # None override -> falls through to sandbox-proxy env controls
        assert _runs_in_worker(runtime_paths, "shell", None) is True
        assert _runs_in_worker(runtime_paths, "calculator", None) is False

    def test_override_empty_list_disables_sandboxing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty list override should disable sandboxing even when sandbox env controls enable it."""
        runtime_paths = _configure_proxy_runtime(
            monkeypatch,
            proxy_url="http://sandbox:8765",
            execution_mode="all",
        )

        assert _runs_in_worker(runtime_paths, "shell", []) is False
        assert _runs_in_worker(runtime_paths, "file", []) is False

    def test_override_explicit_list_selects_tools(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Explicit list override should sandbox only the listed tools."""
        runtime_paths = _configure_proxy_runtime(
            monkeypatch,
            proxy_url="http://sandbox:8765",
            execution_mode="off",
        )

        assert _runs_in_worker(runtime_paths, "shell", ["shell", "file"]) is True
        assert _runs_in_worker(runtime_paths, "file", ["shell", "file"]) is True
        assert _runs_in_worker(runtime_paths, "calculator", ["shell", "file"]) is False

    def test_override_still_respects_runner_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Runner mode should always disable proxying, even with override."""
        runtime_paths = _configure_proxy_runtime(
            monkeypatch,
            proxy_url="http://sandbox:8765",
            execution_mode=None,
            runner_mode=True,
        )

        assert _runs_in_worker(runtime_paths, "shell", ["shell"]) is False

    def test_override_without_proxy_url_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No proxy URL should keep worker-routed execution tools wrapped so calls fail closed."""
        monkeypatch.delenv("MINDROOM_WORKER_BACKEND", raising=False)
        runtime_paths = _configure_proxy_runtime(
            monkeypatch,
            proxy_url=None,
            execution_mode=None,
        )

        assert _runs_in_worker(runtime_paths, "shell", ["shell"]) is True

    def test_default_static_runner_without_proxy_keeps_execution_tools_local(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Plain local static-runner installs should keep execution tools usable."""
        monkeypatch.delenv("MINDROOM_WORKER_BACKEND", raising=False)
        runtime_paths = _configure_proxy_runtime(
            monkeypatch,
            proxy_url=None,
            execution_mode=None,
        )

        assert _runs_in_worker(runtime_paths, "shell", None) is False
        assert _runs_in_worker(runtime_paths, "calculator", None) is False

    def test_unset_mode_with_static_proxy_url_preserves_all_tool_proxying(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A configured static proxy should keep the legacy unset-mode all-tools default."""
        monkeypatch.delenv("MINDROOM_WORKER_BACKEND", raising=False)
        runtime_paths = _configure_proxy_runtime(
            monkeypatch,
            proxy_url="http://sandbox-runner:8765",
            execution_mode=None,
        )

        assert sandbox_proxy_module.sandbox_proxy_config(runtime_paths).proxy_tools is None
        assert _runs_in_worker(runtime_paths, "shell", None) is True
        assert _runs_in_worker(runtime_paths, "calculator", None) is True

    def test_explicit_wildcard_without_proxy_url_fails_closed_for_all_tools(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Explicit all-tools routing should not collapse into plain local execution."""
        monkeypatch.delenv("MINDROOM_WORKER_BACKEND", raising=False)
        _configure_proxy_runtime(
            monkeypatch,
            proxy_url=None,
            execution_mode=None,
        )
        monkeypatch.setenv("MINDROOM_SANDBOX_PROXY_TOOLS", "*")
        runtime_paths = _runtime_paths_from_env()

        assert sandbox_proxy_module.sandbox_proxy_config(runtime_paths).proxy_tools is None
        assert _runs_in_worker(runtime_paths, "shell", None) is True
        assert _runs_in_worker(runtime_paths, "calculator", None) is True

    def test_unsafe_local_execution_opt_in_disables_explicit_routing_without_backend(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Explicit unsafe env opt-in should permit host execution after explicit routing."""
        monkeypatch.delenv("MINDROOM_WORKER_BACKEND", raising=False)
        runtime_paths = _configure_proxy_runtime(
            monkeypatch,
            proxy_url=None,
            execution_mode=None,
        )
        monkeypatch.setenv("MINDROOM_UNSAFE_ALLOW_LOCAL_EXECUTION_TOOLS", "true")
        runtime_paths = _runtime_paths_from_env()

        assert _runs_in_worker(runtime_paths, "shell", ["shell"]) is False

    @pytest.mark.asyncio
    async def test_default_static_runner_shell_call_runs_locally(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Plain local static-runner installs should keep shell usable without extra env."""
        monkeypatch.delenv("MINDROOM_WORKER_BACKEND", raising=False)
        runtime_paths = _configure_proxy_runtime(
            monkeypatch,
            proxy_url=None,
            execution_mode=None,
        )

        tool = get_tool_by_name("shell", runtime_paths, worker_target=None, tools=builtin_tool_registry())
        entrypoint = _offered_entrypoint(tool, "run_shell_command")

        assert await entrypoint("printf local-ok") == "local-ok"

    @pytest.mark.parametrize(
        "tool_name",
        [
            "approved_egress",
            "attachments",
            "browserbase",
            "callback_manager",
            "chat_ui",
            "claude_agent",
            "composio",
            "daytona",
            "desktop",
            "duckdb",
            "e2b",
            "external_trigger_manager",
            "github",
            "gmail",
            "google_calendar",
            "google_docs",
            "google_drive",
            "google_sheets",
            "google_tasks",
            "homeassistant",
            "invite_router",
            "mem0",
            "oauth_connections",
            "pandas",
            "reasoning",
            "script",
            "slack",
            "spotify",
            "sql",
            "todo",
            "usage_stats",
            "zep",
        ],
    )
    def test_local_only_tools_never_proxy(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tool_name: str,
    ) -> None:
        """Tools marked local-only should stay in the primary runtime."""
        runtime_paths = _configure_proxy_runtime(
            monkeypatch,
            proxy_url="http://sandbox:8765",
            execution_mode="all",
        )

        assert _runs_in_worker(runtime_paths, tool_name, None) is False
        assert builtin_tool_registry().entries[tool_name].requires_primary_runtime is True
        assert _runs_in_worker(runtime_paths, tool_name, [tool_name]) is False

    @pytest.mark.parametrize("panel", ["computer", "members"])
    def test_get_tool_by_name_keeps_chat_ui_local_when_explicitly_worker_routed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        panel: str,
    ) -> None:
        """A configured worker_tools entry must retain the live primary-runtime context boundary."""

        class _ForbiddenClient:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                msg = "Sandbox proxy should not be used for local-only tools."
                raise AssertionError(msg)

        runtime_paths = _configure_proxy_runtime(
            monkeypatch,
            proxy_url="http://sandbox:8765",
            proxy_token=_TEST_AUTH_TOKEN,
            execution_mode="all",
            credential_policy={},
        )
        monkeypatch.setattr("mindroom.tool_system.sandbox_proxy.httpx.Client", _ForbiddenClient)
        execution_identity = ToolExecutionIdentity(
            channel="matrix",
            agent_name="general",
            requester_id="@alice:example.org",
            room_id="!room:example.org",
            thread_id="$thread",
            resolved_thread_id="$thread",
            session_id="session-1",
        )

        tool = get_tool_by_name(
            "chat_ui",
            runtime_paths,
            runs_in_worker=_runs_in_worker(runtime_paths, "chat_ui", ["chat_ui"]),
            worker_target=_worker_target(runtime_paths, "user_agent", "general", execution_identity),
            tools=builtin_tool_registry(),
        )
        entrypoint = _offered_entrypoint(tool, "open_panel")

        result = json.loads(asyncio.run(entrypoint(panel=panel)))
        assert result["status"] == "error"
        assert "runtime context" in result["message"]

    def test_usage_stats_stays_local(self) -> None:
        """Usage scans must not leave the primary runtime."""
        assert builtin_tool_registry().entries["usage_stats"].requires_primary_runtime is True

    def test_browser_can_still_use_explicit_worker_routing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Host-browser isolation must not be disabled by the optional Matrix desktop target."""
        runtime_paths = _configure_proxy_runtime(
            monkeypatch,
            proxy_url="http://sandbox:8765",
            execution_mode="all",
        )

        assert _runs_in_worker(runtime_paths, "browser", ["browser"]) is True

    def test_get_tool_by_name_keeps_homeassistant_local_even_when_listed(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Home Assistant should execute locally even if it appears in worker_tools."""

        class _ForbiddenClient:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                msg = "Sandbox proxy should not be used for local-only tools."
                raise AssertionError(msg)

        runtime_paths = _configure_proxy_runtime(
            monkeypatch,
            proxy_url="http://sandbox:8765",
            proxy_token=_TEST_AUTH_TOKEN,
            execution_mode="all",
            credential_policy={},
        )
        monkeypatch.setattr("mindroom.tool_system.sandbox_proxy.httpx.Client", _ForbiddenClient)

        fake_credentials = FakeCredentialsManager({})

        tool = get_tool_by_name(
            "homeassistant",
            runtime_paths,
            credentials_manager=fake_credentials,
            runs_in_worker=_runs_in_worker(runtime_paths, "homeassistant", ["homeassistant"]),
            worker_target=_worker_target(runtime_paths, "shared", "general", None),
            tools=builtin_tool_registry(),
        )
        entrypoint = _offered_entrypoint(tool, "list_entities")

        result = asyncio.run(entrypoint())
        assert "Home Assistant is not configured" in result

    def test_get_tool_by_name_passes_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """get_tool_by_name should wrap the toolkit for the proxy when its calls run in a worker."""
        captured: dict[str, Any] = {}

        runtime_paths = _configure_proxy_runtime(
            monkeypatch,
            proxy_url="http://sandbox:8765",
            proxy_token=_TEST_AUTH_TOKEN,
            execution_mode="off",
            credential_policy={},
        )
        monkeypatch.setattr(
            "mindroom.tool_system.sandbox_proxy.httpx.Client",
            _recording_client_class(captured=captured),
        )

        # The agent lists calculator in its worker tools
        tool = get_tool_by_name(
            "calculator",
            runtime_paths,
            runs_in_worker=_runs_in_worker(runtime_paths, "calculator", ["calculator"]),
            worker_target=None,
            tools=builtin_tool_registry(),
        )
        entrypoint = blocking_entrypoint(tool, "add")
        result = entrypoint(1, 2)
        assert result == "sandbox-result"
        assert captured["url"] == "http://sandbox:8765/api/sandbox-runner/execute"

    def test_get_tool_by_name_passes_tool_init_overrides_to_proxy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Proxy execution should send the tool's base_dir as the request's workspace, outside its init overrides."""
        captured: dict[str, Any] = {}

        runtime_paths = _configure_proxy_runtime(
            monkeypatch,
            proxy_url="http://sandbox:8765",
            proxy_token=_TEST_AUTH_TOKEN,
            execution_mode="off",
            credential_policy={},
        )
        monkeypatch.setattr(
            "mindroom.tool_system.sandbox_proxy.httpx.Client",
            _recording_client_class(captured=captured),
        )

        tool = get_tool_by_name(
            "coding",
            runtime_paths,
            tool_init_overrides={"base_dir": "/workspace/demo"},
            runs_in_worker=_runs_in_worker(runtime_paths, "coding", ["coding"]),
            worker_target=None,
            tools=builtin_tool_registry(),
        )
        entrypoint = blocking_entrypoint(tool, "ls")

        result = entrypoint(path=".")

        assert result == "sandbox-result"
        assert captured["url"] == "http://sandbox:8765/api/sandbox-runner/execute"
        assert captured["json"]["base_dir"] == "/workspace/demo"
        assert captured["json"]["tool_init_overrides"] == {}

    def test_proxy_rewrites_storage_root_base_dir_to_shared_relative_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Worker-keyed proxy execution should send canonical agent base_dir paths relative to the storage root."""
        captured: dict[str, Any] = {}
        storage_root = tmp_path / "mindroom_data"

        monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(storage_root))
        runtime_paths = _configure_proxy_runtime(
            monkeypatch,
            proxy_url="http://sandbox:8765",
            proxy_token=_TEST_AUTH_TOKEN,
            execution_mode="off",
            credential_policy={},
        )
        execution_identity = ToolExecutionIdentity(
            channel="matrix",
            agent_name="general",
            requester_id="@alice:example.org",
            room_id="!room:example.org",
            thread_id="$thread",
            resolved_thread_id="$thread",
            session_id="session-1",
        )
        monkeypatch.setattr(
            "mindroom.tool_system.sandbox_proxy.httpx.Client",
            _recording_client_class(captured=captured),
        )
        tool = get_tool_by_name(
            "coding",
            runtime_paths,
            tool_init_overrides={"base_dir": str(storage_root / "agents" / "general" / "workspace")},
            runs_in_worker=_runs_in_worker(runtime_paths, "coding", ["coding"]),
            worker_target=_worker_target(runtime_paths, "shared", "general", execution_identity),
            tools=builtin_tool_registry(),
        )
        entrypoint = blocking_entrypoint(tool, "ls")
        result = entrypoint(path=".")

        assert result == "sandbox-result"
        assert captured["url"].endswith("/api/sandbox-runner/execute")
        assert captured["json"]["worker_key"] is not None
        assert captured["json"]["base_dir"] == "agents/general/workspace"
        assert captured["json"]["tool_init_overrides"] == {}

    def test_proxy_preserves_storage_root_absolute_base_dir_without_worker_key(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Unscoped proxied calls must keep absolute canonical paths unchanged."""
        captured: dict[str, Any] = {}
        storage_root = tmp_path / "mindroom_data"
        base_dir = storage_root / "agents" / "general" / "workspace" / "mind_data"

        monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(storage_root))
        runtime_paths = _configure_proxy_runtime(
            monkeypatch,
            proxy_url="http://sandbox:8765",
            proxy_token=_TEST_AUTH_TOKEN,
            execution_mode="off",
            credential_policy={},
        )
        monkeypatch.setattr(
            "mindroom.tool_system.sandbox_proxy.httpx.Client",
            _recording_client_class(captured=captured),
        )

        tool = get_tool_by_name(
            "coding",
            runtime_paths,
            tool_init_overrides={"base_dir": str(base_dir)},
            runs_in_worker=_runs_in_worker(runtime_paths, "coding", ["coding"]),
            worker_target=None,
            tools=builtin_tool_registry(),
        )
        entrypoint = blocking_entrypoint(tool, "ls")

        result = entrypoint(path=".")

        assert result == "sandbox-result"
        assert captured["url"] == "http://sandbox:8765/api/sandbox-runner/execute"
        assert captured["json"]["worker_key"] is None
        assert captured["json"]["base_dir"] == str(base_dir)

    def test_proxy_preserves_unrelated_absolute_base_dir_for_worker_key(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Worker-keyed requests must not retarget unrelated absolute paths that merely contain `agents`."""
        captured: dict[str, Any] = {}
        unrelated_base_dir = tmp_path / "demo" / "agents" / "general" / "workspace"

        runtime_paths = _configure_proxy_runtime(
            monkeypatch,
            proxy_url="http://sandbox:8765",
            proxy_token=_TEST_AUTH_TOKEN,
            execution_mode="off",
            credential_policy={},
        )
        execution_identity = ToolExecutionIdentity(
            channel="matrix",
            agent_name="general",
            requester_id="@alice:example.org",
            room_id="!room:example.org",
            thread_id="$thread",
            resolved_thread_id="$thread",
            session_id="session-1",
        )
        monkeypatch.setattr(
            "mindroom.tool_system.sandbox_proxy.httpx.Client",
            _recording_client_class(captured=captured),
        )

        tool = get_tool_by_name(
            "coding",
            runtime_paths,
            tool_init_overrides={"base_dir": str(unrelated_base_dir)},
            runs_in_worker=_runs_in_worker(runtime_paths, "coding", ["coding"]),
            worker_target=_worker_target(runtime_paths, "shared", "general", execution_identity),
            tools=builtin_tool_registry(),
        )
        entrypoint = blocking_entrypoint(tool, "ls")
        result = entrypoint(path=".")

        assert result == "sandbox-result"
        assert captured["json"]["worker_key"] is not None
        assert captured["json"]["base_dir"] == str(unrelated_base_dir)


@pytest.mark.asyncio
async def test_inprocess_runner_blocks_cross_runtime_secret_leakage(
    tmp_path: Path,
) -> None:
    """Runner-only env vars must not leak via glob passthrough patterns."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-6-astra\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    config_path.with_name(".env").write_text("", encoding="utf-8")

    # Runner has a secret env var that matches the passthrough pattern.
    runner_process_env = {"WHISPER_API_TOKEN": "runner-only-secret"}
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=config_path.parent / "storage",
        process_env=runner_process_env,
    )
    config = sandbox_runner_module._runtime_config_or_empty(runtime_paths)

    # Client sends a request with WHISPER_* passthrough but only WHISPER_URL
    # in its execution_env.  The runner-only WHISPER_API_TOKEN must NOT leak.
    call = execute_call(
        runtime_paths,
        config,
        tool_name="shell",
        function_name="run_shell_command",
        args=[
            ["bash", "-lc", "printf '%s' \"$WHISPER_URL|$WHISPER_API_TOKEN\""],
        ],
        execution_env={"WHISPER_URL": "https://whisper.example"},
        extra_env_passthrough="WHISPER_*",
    )

    response = await sandbox_runner_module._execute_request_inprocess(call)

    assert response.ok is True
    # WHISPER_URL from execution_env should be visible; WHISPER_API_TOKEN should not.
    assert response.result == "https://whisper.example|"


def test_shell_extra_env_trusts_explicit_patterns_except_runner_control() -> None:
    """Wildcard passthrough keeps matched user env and drops only runner control names."""
    env = {
        "ANTHROPIC_API_KEY": "sk-ant-secret",
        "OPENAI_API_KEY": "sk-oai-secret",
        "GOOGLE_API_KEY": "goog-secret",
        "MY_SERVICE_PASSWORD": "pass-secret",
        "WEBHOOK_SECRET": "hook-secret",
        "CI_JOB_TOKEN": "ci-token",
        "GITEA_TOKEN": "gitea-token",
        "WHISPER_URL": "https://whisper.example",
        "GITEA_HOST": "gitea.local",
        "MINDROOM_API_KEY": "runner-api-key",
        "MINDROOM_LOCAL_CLIENT_SECRET": "runner-client-secret",
        "MINDROOM_SANDBOX_PROXY_TOKEN": "runner-proxy-token",
        "MINDROOM_SANDBOX_STARTUP_MANIFEST_PATH": "/var/lib/mindroom/manifest.json",
        "MINDROOM_SANDBOX_FOO": "runner-sandbox-value",
        "MINDROOM_LOCAL_CLIENT_ID": "local-client-id",
    }
    result = dict(shell_extra_env_values(extra_env_passthrough="*", process_env=env))

    assert result["WHISPER_URL"] == "https://whisper.example"
    assert result["GITEA_HOST"] == "gitea.local"
    assert result["GITEA_TOKEN"] == "gitea-token"  # noqa: S105
    assert result["ANTHROPIC_API_KEY"] == "sk-ant-secret"
    assert result["OPENAI_API_KEY"] == "sk-oai-secret"
    assert result["GOOGLE_API_KEY"] == "goog-secret"
    assert result["MY_SERVICE_PASSWORD"] == "pass-secret"  # noqa: S105
    assert result["WEBHOOK_SECRET"] == "hook-secret"  # noqa: S105
    assert result["CI_JOB_TOKEN"] == "ci-token"  # noqa: S105
    assert result["MINDROOM_LOCAL_CLIENT_ID"] == "local-client-id"

    assert "MINDROOM_API_KEY" not in result
    assert "MINDROOM_LOCAL_CLIENT_SECRET" not in result
    assert "MINDROOM_SANDBOX_PROXY_TOKEN" not in result
    assert "MINDROOM_SANDBOX_STARTUP_MANIFEST_PATH" not in result
    assert "MINDROOM_SANDBOX_FOO" not in result


def test_shell_extra_env_requires_explicit_patterns_for_service_urls() -> None:
    """extra_env_passthrough should not auto-include unrelated service URLs."""
    env = {
        "GITEA_TOKEN": "gitea-token",
        "WHISPER_URL": "https://whisper.example",
    }

    result = dict(shell_extra_env_values(extra_env_passthrough="GITEA_*", process_env=env))

    assert result == {"GITEA_TOKEN": "gitea-token"}
    assert "WHISPER_URL" not in result


@pytest.mark.parametrize("tool_name", ["browser_mcp", "shell"])
def test_worker_client_returns_raw_browser_envelopes(tool_name: str) -> None:
    """The HTTP client leaves feature envelopes untouched for every tool."""
    envelope = encode_media_result(
        ToolValue(content="screen", images=(Image(content=b"png", mime_type="image/png"),)),
    )
    result = execute_worker_proxy_request(
        config=WorkerProxyClientConfig(
            proxy_url="http://worker/execute",
            proxy_token=_TEST_AUTH_TOKEN,
            proxy_timeout_seconds=7.0,
            credential_lease_ttl_seconds=60,
            credential_policy={},
            lease_tool_credentials=False,
        ),
        request=_execute_request(tool_name, "browser_take_screenshot"),
        credentials_manager=None,
        worker_target=None,
        worker_handle=None,
        worker_manager=_TrackingWorkerManager(),
        client_factory=_recording_client_class(responder=lambda _url, _payload: {"ok": True, "result": envelope}),
    )
    assert result == envelope


@pytest.mark.usefixtures("current_static_runner")
@pytest.mark.parametrize("tool_name", ["browser_mcp", "browser", "shell"])
@pytest.mark.parametrize("valid", [True, False])
def test_proxy_composition_decodes_typed_media_for_every_tool(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    valid: bool,
) -> None:
    """Composition restores native images and rejects malformed native envelopes."""
    envelope = {
        "mindroom_tool_result": {
            "version": 1 if valid else 999,
            "kind": "tool_result",
            "audios": [],
            "videos": [],
            "files": [],
            "content": "screen",
            "images": [{"mime_type": "image/png", "data_base64": "cG5n"}],
        },
    }
    paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="selective",
        proxy_tools={tool_name},
        credential_policy={},
    )
    monkeypatch.setattr(sandbox_proxy_module, "execute_worker_proxy_request", lambda **_kwargs: envelope)

    def call() -> object:
        return call_proxy_sync_for_test(
            runtime_paths=paths,
            tool_name=tool_name,
            function_name="browser_take_screenshot",
            args=(),
            kwargs={},
            credentials_manager=None,
        )

    if not valid:
        with pytest.raises(ValueError, match="worker tool result"):
            call()
    else:
        result = call()
        assert isinstance(result, ToolValue)
        assert result.content == "screen"
        assert result.images
        assert result.images[0].content == b"png"


@pytest.mark.usefixtures("current_static_runner")
@pytest.mark.parametrize("tool_name", ["browser_mcp", "shell"])
def test_proxy_composition_finalizes_decoded_media_and_returns_json_as_it_is(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    tool_name: str,
) -> None:
    """A worker's image view is retained as an attachment; a JSON envelope's value comes back as it is."""
    context = _tool_context(tmp_path)
    viewed = ToolValue(content='{"view_status": "ready"}', images=(Image(content=b"png", mime_type="image/png"),))
    ordinary = {"mindroom_tool_result": {"version": 777}}
    responses = iter([encode_media_result(viewed), encode_media_result(ordinary)])
    paths = _configure_proxy_runtime(
        monkeypatch,
        proxy_url="http://sandbox-runner:8765",
        proxy_token=_TEST_AUTH_TOKEN,
        execution_mode="selective",
        proxy_tools={tool_name},
        credential_policy={},
    )
    monkeypatch.setattr(sandbox_proxy_module, "execute_worker_proxy_request", lambda **_kwargs: next(responses))

    def call() -> object:
        return call_proxy_sync_for_test(
            runtime_paths=paths,
            tool_name=tool_name,
            function_name="browser_take_screenshot",
            args=(),
            kwargs={},
            credentials_manager=None,
        )

    with tool_runtime_context(context):
        media = call()
        plain = call()

    assert isinstance(media, ToolValue)
    assert [image.content for image in media.images] == [b"png"]
    assert json.loads(media.content)["attachment_id"].startswith("att_")
    assert plain == ordinary
