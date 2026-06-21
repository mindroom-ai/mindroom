"""Regression tests for workspace automation worker routing."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import mindroom.tool_system.sandbox_proxy as sandbox_proxy_module
import mindroom.tools  # noqa: F401
from mindroom.agents import build_agent_toolkit, resolve_runtime_worker_tools
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.hooks import HookRegistry
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, build_tool_execution_identity, resolve_worker_key
from mindroom.workers.models import WorkerHandle, WorkerSpec
from mindroom.workspace_automations.executor import run_shell_check
from mindroom.workspace_automations.models import (
    LoadedWorkspaceAutomation,
    WorkspaceAutomationAction,
    WorkspaceAutomationCheck,
)
from mindroom.workspace_automations.targets import WorkspaceAutomationTarget

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

_TEST_AUTH_TOKEN = "test-token"  # noqa: S105
_WORKSPACE_AUTOMATION_RESOURCE_SCAN_ROOTS = (
    Path("src/mindroom/workspace_automations"),
    Path("src/mindroom/orchestrator.py"),
    Path("src/mindroom/custom_tools/workspace_automation.py"),
    Path("src/mindroom/tools/workspace_automation.py"),
    Path("src/mindroom/workers/backends"),
    Path("cluster/k8s"),
    Path("cluster/terraform/terraform-k8s/manifests"),
)
_WORKSPACE_AUTOMATION_RESOURCE_SCAN_SUFFIXES = {
    ".j2",
    ".py",
    ".tf",
    ".tpl",
    ".yaml",
    ".yml",
}
_WORKSPACE_AUTOMATION_SCOPE_MARKERS = (
    "workspace automation",
    "workspace automations",
    "workspace-automation",
    "workspace-automations",
    "workspace_automation",
    "workspace_automations",
    "workspaceautomation",
    "workspaceautomations",
)
_FORBIDDEN_WORKSPACE_AUTOMATION_KUBERNETES_MARKERS = (
    "apiVersion: batch/v1",
    "batch/v1",
    "CronJob",
    "cronjob",
    "kind: Job",
    "kind: CronJob",
    "kind: Pod",
    "kind: Deployment",
    "kind: StatefulSet",
    "kind: DaemonSet",
    "kind: HorizontalPodAutoscaler",
    "replicas:",
    "keep-alive",
    "keepalive",
    "keep_alive",
    "always-on",
    "always_on",
)


class _RecordingKubernetesWorkerManager:
    """Small fake for the worker manager boundary used by sandbox proxy routing."""

    def __init__(self) -> None:
        self.ensure_specs: list[WorkerSpec] = []
        self.touched_worker_keys: list[str] = []
        self.failures: list[tuple[str, str]] = []
        self.replicas_by_worker_key: dict[str, int] = {}

    def ensure_worker(
        self,
        spec: WorkerSpec,
        *,
        now: float | None = None,
        progress_sink: object = None,
    ) -> WorkerHandle:
        del now, progress_sink
        self.ensure_specs.append(spec)
        self.replicas_by_worker_key[spec.worker_key] = 1
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
        del now
        self.touched_worker_keys.append(worker_key)
        return None

    def record_failure(self, worker_key: str, failure_reason: str, *, now: float | None = None) -> object:
        del now
        self.failures.append((worker_key, failure_reason))
        return None

    def cleanup_idle_workers(self, *, now: float | None = None) -> list[WorkerHandle]:
        del now
        idle_handles: list[WorkerHandle] = []
        for worker_key, replicas in list(self.replicas_by_worker_key.items()):
            if replicas == 0:
                continue
            self.replicas_by_worker_key[worker_key] = 0
            idle_handles.append(
                WorkerHandle(
                    worker_id="worker-1",
                    worker_key=worker_key,
                    endpoint=None,
                    auth_token=None,
                    status="idle",
                    backend_name="kubernetes",
                    last_used_at=0.0,
                    created_at=0.0,
                ),
            )
        return idle_handles


class _WorkerManagerLease:
    def __init__(self, manager: _RecordingKubernetesWorkerManager) -> None:
        self._manager = manager

    def __enter__(self) -> _RecordingKubernetesWorkerManager:
        return self._manager

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


def _find_forbidden_workspace_automation_kubernetes_resources(
    scan_roots: Iterable[Path] = _WORKSPACE_AUTOMATION_RESOURCE_SCAN_ROOTS,
) -> list[str]:
    """Return workspace-automation-scoped Kubernetes resources that would bypass worker routing."""
    matches: list[str] = []
    for source_path in _iter_workspace_automation_resource_scan_files(scan_roots):
        source_text = source_path.read_text(encoding="utf-8")
        if not _is_workspace_automation_scoped_source(source_path, source_text):
            continue
        source_text_lower = source_text.lower()
        matches.extend(
            f"{source_path}:{marker}"
            for marker in _FORBIDDEN_WORKSPACE_AUTOMATION_KUBERNETES_MARKERS
            if marker.lower() in source_text_lower
        )
    return sorted(matches)


def _iter_workspace_automation_resource_scan_files(scan_roots: Iterable[Path]) -> Iterator[Path]:
    for root in scan_roots:
        if root.is_file():
            if _should_scan_workspace_automation_resource_file(root):
                yield root
            continue
        if not root.exists():
            continue
        for source_path in root.rglob("*"):
            if source_path.is_file() and _should_scan_workspace_automation_resource_file(source_path):
                yield source_path


def _should_scan_workspace_automation_resource_file(source_path: Path) -> bool:
    return source_path.name in {"Chart.yaml", "values.yaml"} or (
        source_path.suffix in _WORKSPACE_AUTOMATION_RESOURCE_SCAN_SUFFIXES
    )


def _is_workspace_automation_scoped_source(source_path: Path, source_text: str) -> bool:
    source_identity = f"{source_path.as_posix()}\n{source_text}".lower()
    return any(marker in source_identity for marker in _WORKSPACE_AUTOMATION_SCOPE_MARKERS)


@pytest.fixture
def runtime_paths(tmp_path: Path) -> RuntimePaths:
    """Create runtime paths with tenant/account identity available to worker routing."""
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={
            "ACCOUNT_ID": "account-456",
            "CUSTOMER_ID": "tenant-123",
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
            "MINDROOM_WORKER_BACKEND": "kubernetes",
        },
    )


@pytest.fixture
def config(runtime_paths: RuntimePaths) -> Config:
    """Create a shared-scope shell agent with workspace automations enabled."""
    return Config.validate_with_runtime(
        {
            "memory": {"backend": "none"},
            "defaults": {"worker_tools": ["shell"]},
            "agents": {
                "ops": {
                    "display_name": "Ops",
                    "worker_scope": "shared",
                    "workspace_automations": {
                        "enabled": True,
                        "max_output_bytes": 4096,
                    },
                },
            },
        },
        runtime_paths,
    )


@pytest.fixture
def target(config: Config, runtime_paths: RuntimePaths) -> WorkspaceAutomationTarget:
    """Resolve the shared agent runtime used by worker routing tests."""
    agent_runtime = resolve_agent_runtime(
        "ops",
        config,
        runtime_paths,
        execution_identity=None,
        create=True,
    )
    assert agent_runtime.workspace is not None
    return WorkspaceAutomationTarget(
        agent_name="ops",
        agent_configured_rooms=("Lobby",),
        policy=config.get_agent_workspace_automation_policy("ops"),
        agent_runtime=agent_runtime,
        workspace_root=agent_runtime.workspace.root,
    )


def _automation(target: WorkspaceAutomationTarget) -> LoadedWorkspaceAutomation:
    return LoadedWorkspaceAutomation(
        agent_name="ops",
        automation_id="urgent_email_poll",
        workspace_root=target.workspace_root,
        file_path=target.workspace_root / ".mindroom" / "automations.yaml",
        schedule="*/1 * * * *",
        check=WorkspaceAutomationCheck(
            type="shell",
            command="./scripts/check_urgent_email.sh",
            timeout_seconds=12,
            tail=37,
        ),
        trigger=None,
        action=WorkspaceAutomationAction(type="none"),
    )


def _automation_execution_identity(runtime_paths: RuntimePaths) -> ToolExecutionIdentity:
    return build_tool_execution_identity(
        channel="matrix",
        agent_name="ops",
        transport_agent_name="ops",
        runtime_paths=runtime_paths,
        requester_id=None,
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id="workspace-automation:ops:urgent_email_poll",
    )


def _expected_worker_key(runtime_paths: RuntimePaths) -> str:
    worker_key = resolve_worker_key(
        "shared",
        _automation_execution_identity(runtime_paths),
        agent_name="ops",
    )
    assert worker_key is not None
    return worker_key


def _patch_worker_proxy(
    monkeypatch: pytest.MonkeyPatch,
    manager: _RecordingKubernetesWorkerManager,
    captured_payloads: list[dict[str, object]],
) -> None:
    monkeypatch.setattr(
        sandbox_proxy_module,
        "primary_worker_backend_available",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        sandbox_proxy_module,
        "lease_primary_worker_manager",
        lambda *_args, **_kwargs: _WorkerManagerLease(manager),
    )

    def execute_worker_proxy_request(
        *,
        payload: dict[str, object],
        worker_handle: WorkerHandle | None,
        worker_manager: object,
        tool_name: str,
        function_name: str,
        **_kwargs: object,
    ) -> object:
        assert worker_manager is manager
        assert worker_handle is not None
        assert tool_name == "shell"
        captured_payloads.append(payload)
        if function_name == "run_shell_command_structured":
            return {
                "ok": True,
                "exit_code": 0,
                "stdout": "automation-worker-output",
                "stderr": "",
                "raw_output": "automation-worker-output",
                "timed_out": False,
                "error": None,
            }
        return "normal-shell-output"

    monkeypatch.setattr(
        sandbox_proxy_module,
        "execute_worker_proxy_request",
        execute_worker_proxy_request,
    )


def _payload_execution_identity(payload: dict[str, object]) -> dict[str, object]:
    execution_identity = payload["execution_identity"]
    assert isinstance(execution_identity, dict)
    return execution_identity


async def _run_normal_shell_tool(
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    target: WorkspaceAutomationTarget,
) -> object:
    toolkit = build_agent_toolkit(
        "shell",
        agent_name="ops",
        config=config,
        runtime_paths=runtime_paths,
        worker_tools=resolve_runtime_worker_tools("ops", config, runtime_paths, ["shell"]),
        runtime_overrides=config.get_agent_tool_runtime_overrides("ops", "shell"),
        agent_runtime=target.agent_runtime,
        tool_config_overrides=None,
        execution_identity=_automation_execution_identity(runtime_paths),
    )
    assert toolkit is not None
    function = toolkit.async_functions["run_shell_command"].entrypoint
    assert function is not None
    return await function("echo normal", tail=5, timeout=7)


@pytest.mark.asyncio
async def test_automation_shell_check_routes_to_same_worker_target_as_normal_shell_tool(
    monkeypatch: pytest.MonkeyPatch,
    config: Config,
    runtime_paths: RuntimePaths,
    target: WorkspaceAutomationTarget,
) -> None:
    """Automation shell checks should reuse the normal shell-tool worker route."""
    manager = _RecordingKubernetesWorkerManager()
    captured_payloads: list[dict[str, object]] = []
    _patch_worker_proxy(monkeypatch, manager, captured_payloads)

    automation_result = await run_shell_check(
        config=config,
        runtime_paths=runtime_paths,
        hook_registry=HookRegistry.empty(),
        target=target,
        automation=_automation(target),
    )
    normal_result = await _run_normal_shell_tool(
        config=config,
        runtime_paths=runtime_paths,
        target=target,
    )

    expected_worker_key = _expected_worker_key(runtime_paths)
    assert automation_result.ok is True
    assert normal_result == "normal-shell-output"
    assert [spec.worker_key for spec in manager.ensure_specs] == [expected_worker_key, expected_worker_key]
    assert [spec.private_agent_names for spec in manager.ensure_specs] == [None, None]
    assert [payload["worker_scope"] for payload in captured_payloads] == ["shared", "shared"]
    assert [payload["routing_agent_name"] for payload in captured_payloads] == ["ops", "ops"]
    assert [payload["worker_key"] for payload in captured_payloads] == [expected_worker_key, expected_worker_key]

    automation_payload = captured_payloads[0]
    normal_payload = captured_payloads[1]
    assert automation_payload["function_name"] == "run_shell_command_structured"
    assert automation_payload["args"] == ["./scripts/check_urgent_email.sh"]
    assert automation_payload["kwargs"] == {
        "tail": 37,
        "timeout": 12,
        "max_output_bytes": 4096,
    }
    assert normal_payload["function_name"] == "run_shell_command"
    assert normal_payload["args"] == ["echo normal"]
    assert normal_payload["kwargs"] == {"tail": 5, "timeout": 7}

    execution_identity = _payload_execution_identity(automation_payload)
    assert execution_identity["tenant_id"] == "tenant-123"
    assert execution_identity["account_id"] == "account-456"
    assert execution_identity["session_id"] == "workspace-automation:ops:urgent_email_poll"
    assert execution_identity["requester_id"] is None
    assert execution_identity["room_id"] is None


@pytest.mark.asyncio
async def test_due_automation_run_ensures_worker_again_after_kubernetes_idle_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    config: Config,
    runtime_paths: RuntimePaths,
    target: WorkspaceAutomationTarget,
) -> None:
    """A later automation run must re-ensure the worker after Kubernetes cleanup."""
    manager = _RecordingKubernetesWorkerManager()
    captured_payloads: list[dict[str, object]] = []
    _patch_worker_proxy(monkeypatch, manager, captured_payloads)
    automation = _automation(target)
    expected_worker_key = _expected_worker_key(runtime_paths)

    first_result = await run_shell_check(
        config=config,
        runtime_paths=runtime_paths,
        hook_registry=HookRegistry.empty(),
        target=target,
        automation=automation,
    )
    cleaned = manager.cleanup_idle_workers(now=80.0)
    second_result = await run_shell_check(
        config=config,
        runtime_paths=runtime_paths,
        hook_registry=HookRegistry.empty(),
        target=target,
        automation=automation,
    )

    assert first_result.ok is True
    assert second_result.ok is True
    assert [handle.worker_key for handle in cleaned] == [expected_worker_key]
    assert cleaned[0].status == "idle"
    assert [spec.worker_key for spec in manager.ensure_specs] == [expected_worker_key, expected_worker_key]
    assert manager.replicas_by_worker_key[expected_worker_key] == 1
    assert [payload["worker_key"] for payload in captured_payloads] == [expected_worker_key, expected_worker_key]


def test_workspace_automations_do_not_define_kubernetes_cronjobs() -> None:
    """Workspace automation scheduling should stay in the service/executor path."""
    assert _find_forbidden_workspace_automation_kubernetes_resources() == []


def test_kubernetes_resource_scan_catches_workspace_automation_cronjobs_and_keepalive(
    tmp_path: Path,
) -> None:
    """The negative scan should catch automation-specific manifests outside Python roots."""
    manifest_path = tmp_path / "cluster" / "k8s" / "runtime" / "templates" / "workspace-automation-cronjob.yaml"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        """
apiVersion: batch/v1
kind: CronJob
metadata:
  name: workspace-automation-shell-check
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: workspace-automation-keepalive
  labels:
    app.kubernetes.io/component: workspace-automation-keepalive
spec:
  replicas: 1
""",
        encoding="utf-8",
    )

    matches = _find_forbidden_workspace_automation_kubernetes_resources((tmp_path,))

    assert any("batch/v1" in match for match in matches)
    assert any("CronJob" in match for match in matches)
    assert any("kind: Deployment" in match for match in matches)
    assert any("keepalive" in match for match in matches)


def test_kubernetes_resource_scan_ignores_unrelated_cronjobs(tmp_path: Path) -> None:
    """Existing unrelated Kubernetes resources should not make the guard brittle."""
    manifest_path = tmp_path / "cluster" / "k8s" / "runtime" / "templates" / "agent-vault-bootstrap.yaml"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        """
apiVersion: batch/v1
kind: CronJob
metadata:
  name: agent-vault-bootstrap
""",
        encoding="utf-8",
    )

    assert _find_forbidden_workspace_automation_kubernetes_resources((tmp_path,)) == []
