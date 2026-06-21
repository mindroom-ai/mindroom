"""Tests for the workspace automation management tool."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import pytest

import mindroom.tools  # noqa: F401
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.custom_tools import workspace_automation as workspace_automation_module
from mindroom.custom_tools.workspace_automation import WorkspaceAutomationTools
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.tool_system.metadata import SetupType, ToolCategory, ToolExecutionTarget, ToolStatus
from mindroom.tool_system.registry_state import TOOL_METADATA, TOOL_REGISTRY
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, agent_workspace_root_path
from mindroom.workspace_automations.service import (
    WorkspaceAutomationLoadedStatus,
    WorkspaceAutomationScanResult,
    WorkspaceAutomationService,
    get_active_workspace_automation_service,
    set_active_workspace_automation_service,
)
from mindroom.workspace_automations.targets import WorkspaceAutomationTarget, iter_workspace_automation_targets
from tests.conftest import make_event_cache_mock

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def clear_active_workspace_automation_service() -> Iterator[None]:
    """Keep the process-global active service isolated between tests."""
    set_active_workspace_automation_service(None)
    yield
    set_active_workspace_automation_service(None)


@pytest.fixture
def runtime_paths(tmp_path: Path) -> RuntimePaths:
    """Create isolated runtime paths for tool tests."""
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
        },
    )


def _config(runtime_paths: RuntimePaths) -> Config:
    return Config.validate_with_runtime(
        {
            "memory": {"backend": "none"},
            "agents": {
                "ops": {
                    "display_name": "Ops",
                    "rooms": ["Lobby"],
                    "workspace_automations": {
                        "enabled": True,
                        "allowed_actions": ["agent_message"],
                    },
                },
                "other": {
                    "display_name": "Other",
                    "rooms": ["Other"],
                    "workspace_automations": {
                        "enabled": True,
                        "allowed_actions": ["agent_message"],
                    },
                },
            },
        },
        runtime_paths,
    )


def _tool_context(runtime_paths: RuntimePaths) -> ToolRuntimeContext:
    return ToolRuntimeContext(
        agent_name="ops",
        room_id="!room:localhost",
        thread_id="$thread:localhost",
        resolved_thread_id="$thread:localhost",
        requester_id="@user:localhost",
        client=AsyncMock(),
        config=_config(runtime_paths),
        runtime_paths=runtime_paths,
        conversation_cache=AsyncMock(),
        event_cache=make_event_cache_mock(),
        room=None,
    )


def _private_identity(
    agent_name: str,
    requester_id: str,
    *,
    thread_id: str = "$thread:localhost",
    resolved_thread_id: str = "$thread:localhost",
    session_id: str = "session-1",
) -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name=agent_name,
        requester_id=requester_id,
        room_id="!room:localhost",
        thread_id=thread_id,
        resolved_thread_id=resolved_thread_id,
        session_id=session_id,
        transport_agent_name=agent_name,
    )


def _private_tool_context(
    runtime_paths: RuntimePaths,
    config: Config,
    identity: ToolExecutionIdentity,
) -> ToolRuntimeContext:
    return ToolRuntimeContext(
        agent_name=identity.agent_name,
        room_id=identity.room_id or "!room:localhost",
        thread_id=identity.thread_id,
        resolved_thread_id=identity.resolved_thread_id,
        requester_id=identity.requester_id or "@user:localhost",
        client=AsyncMock(),
        config=config,
        runtime_paths=runtime_paths,
        conversation_cache=AsyncMock(),
        event_cache=make_event_cache_mock(),
        room=None,
        session_id=identity.session_id,
        transport_agent_name=identity.transport_agent_name,
    )


def _write_automations(workspace_root: Path) -> None:
    file_path = workspace_root / ".mindroom" / "automations.yaml"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(
        """
version: 1
automations:
  urgent_email_poll:
    schedule: "* * * * *"
    check:
      type: shell
      command: "true"
      timeout_seconds: 1
    trigger:
      exit_code: 42
    action:
      type: agent_message
      room: "Lobby"
      message: "Urgent email condition matched."
  too_slow:
    schedule: "* * * * *"
    check:
      type: shell
      command: "true"
      timeout_seconds: 999
    action:
      type: none
""",
        encoding="utf-8",
    )


def _write_other_automations(workspace_root: Path) -> None:
    file_path = workspace_root / ".mindroom" / "automations.yaml"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(
        """
version: 1
automations:
  other_only:
    schedule: "* * * * *"
    check:
      type: shell
      command: "true"
      timeout_seconds: 1
    action:
      type: none
""",
        encoding="utf-8",
    )


class _FakeWorkspaceAutomationService:
    def __init__(
        self,
        targets: tuple[WorkspaceAutomationTarget, ...] = (),
        automations: tuple[WorkspaceAutomationLoadedStatus, ...] | None = None,
    ) -> None:
        self.scan_now_call_count = 0
        self.targets_by_agent = {target.agent_name: target for target in targets}
        self._automations = automations or (
            WorkspaceAutomationLoadedStatus(
                agent_name="ops",
                automation_id="urgent_email_poll",
                workspace_root="/workspace/ops",
                schedule="* * * * *",
                last_status="action_succeeded",
                last_run_at="2026-06-16T12:00:00+00:00",
                last_exit_code=42,
                last_error=None,
                last_event_id="$event:localhost",
            ),
            WorkspaceAutomationLoadedStatus(
                agent_name="other",
                automation_id="other_poll",
                workspace_root="/workspace/other",
                schedule="* * * * *",
                last_status="action_succeeded",
                last_run_at="2026-06-16T12:00:00+00:00",
                last_exit_code=0,
                last_error=None,
                last_event_id="$other:localhost",
            ),
        )

    @property
    def is_started(self) -> bool:
        return True

    def list_loaded(
        self,
        target_filter: Callable[[WorkspaceAutomationTarget], bool] | None = None,
    ) -> tuple[WorkspaceAutomationLoadedStatus, ...]:
        if target_filter is None:
            return self._automations
        return tuple(
            status
            for status in self._automations
            if status.agent_name in self.targets_by_agent and target_filter(self.targets_by_agent[status.agent_name])
        )

    async def scan_now(
        self,
        target_filter: Callable[[WorkspaceAutomationTarget], bool] | None = None,
    ) -> WorkspaceAutomationScanResult:
        self.scan_now_call_count += 1
        return WorkspaceAutomationScanResult(loaded_count=len(self.list_loaded(target_filter)), error_count=0)


def _targets(runtime_paths: RuntimePaths) -> tuple[WorkspaceAutomationTarget, ...]:
    return tuple(iter_workspace_automation_targets(_config(runtime_paths), runtime_paths))


def _targeted_service(runtime_paths: RuntimePaths) -> _FakeWorkspaceAutomationService:
    return _FakeWorkspaceAutomationService(_targets(runtime_paths))


@pytest.mark.asyncio
async def test_validate_automations_requires_tool_runtime_context() -> None:
    """Validation should fail clearly when no live tool runtime context exists."""
    payload = json.loads(await WorkspaceAutomationTools().validate_automations())

    assert payload["status"] == "error"
    assert payload["tool"] == "workspace_automation"
    assert payload["code"] == "unavailable"
    assert "tool runtime context" in payload["message"]


@pytest.mark.asyncio
async def test_validate_automations_scans_context_without_active_service(
    monkeypatch: pytest.MonkeyPatch,
    runtime_paths: RuntimePaths,
) -> None:
    """Validation should load configured targets from context without using the live service."""

    def fail_if_accessor_is_used() -> WorkspaceAutomationService | None:
        msg = "validate_automations must not use the active service accessor"
        raise AssertionError(msg)

    monkeypatch.setattr(
        workspace_automation_module,
        "get_active_workspace_automation_service",
        fail_if_accessor_is_used,
    )
    workspace_root = agent_workspace_root_path(runtime_paths.storage_root, "ops")
    other_workspace_root = agent_workspace_root_path(runtime_paths.storage_root, "other")
    _write_automations(workspace_root)
    _write_automations(other_workspace_root)

    with tool_runtime_context(_tool_context(runtime_paths)):
        payload = json.loads(await WorkspaceAutomationTools().validate_automations())

    assert payload["status"] == "ok"
    assert payload["loaded_count"] == 1
    assert payload["error_count"] == 1
    assert payload["automations"][0]["agent_name"] == "ops"
    assert payload["automations"][0]["automation_id"] == "urgent_email_poll"
    assert payload["automations"][0]["schedule"] == "* * * * *"
    assert payload["errors"][0]["automation_id"] == "too_slow"
    assert "timeout_seconds" in payload["errors"][0]["message"]
    assert {automation["agent_name"] for automation in payload["automations"]} == {"ops"}
    assert {error["automation_id"] for error in payload["errors"]} == {"too_slow"}


@pytest.mark.asyncio
async def test_validate_automations_scopes_private_targets_to_current_requester(
    runtime_paths: RuntimePaths,
) -> None:
    """Private automation validation should not expose sibling requester workspaces."""
    config = Config.validate_with_runtime(
        {
            "memory": {"backend": "none"},
            "agents": {
                "mind": {
                    "display_name": "Mind",
                    "rooms": ["Lobby"],
                    "private": {"per": "user"},
                    "workspace_automations": {
                        "enabled": True,
                        "allowed_actions": ["agent_message"],
                    },
                },
            },
        },
        runtime_paths,
    )
    alice_identity = _private_identity("mind", "@alice:localhost")
    bob_identity = _private_identity("mind", "@bob:localhost")
    alice_runtime = resolve_agent_runtime("mind", config, runtime_paths, alice_identity, create=True)
    bob_runtime = resolve_agent_runtime("mind", config, runtime_paths, bob_identity, create=True)
    assert alice_runtime.workspace is not None
    assert bob_runtime.workspace is not None
    _write_automations(alice_runtime.workspace.root)
    _write_other_automations(bob_runtime.workspace.root)

    with tool_runtime_context(_private_tool_context(runtime_paths, config, alice_identity)):
        payload = json.loads(await WorkspaceAutomationTools().validate_automations())

    assert payload["status"] == "ok"
    assert [automation["automation_id"] for automation in payload["automations"]] == ["urgent_email_poll"]
    assert [error["automation_id"] for error in payload["errors"]] == ["too_slow"]


@pytest.mark.asyncio
async def test_list_automations_returns_unavailable_when_active_service_is_missing(
    runtime_paths: RuntimePaths,
) -> None:
    """Listing should return a structured unavailable payload instead of raising."""
    assert get_active_workspace_automation_service() is None

    with tool_runtime_context(_tool_context(runtime_paths)):
        payload = json.loads(await WorkspaceAutomationTools().list_automations())

    assert payload["status"] == "error"
    assert payload["tool"] == "workspace_automation"
    assert payload["code"] == "unavailable"
    assert "Workspace automation service is unavailable" in payload["message"]


@pytest.mark.asyncio
async def test_list_automations_returns_context_scoped_statuses_from_active_service(
    runtime_paths: RuntimePaths,
) -> None:
    """Listing should expose service status snapshots as JSON payloads."""
    service = _targeted_service(runtime_paths)
    set_active_workspace_automation_service(cast("WorkspaceAutomationService", service))

    with tool_runtime_context(_tool_context(runtime_paths)):
        payload = json.loads(await WorkspaceAutomationTools().list_automations())

    assert payload["status"] == "ok"
    assert payload["automations"] == [
        {
            "agent_name": "ops",
            "automation_id": "urgent_email_poll",
            "last_error": None,
            "last_event_id": "$event:localhost",
            "last_exit_code": 42,
            "last_run_at": "2026-06-16T12:00:00+00:00",
            "last_status": "action_succeeded",
            "schedule": "* * * * *",
            "workspace_root": "/workspace/ops",
        },
    ]


@pytest.mark.asyncio
async def test_reload_automations_scans_and_returns_context_scoped_statuses(
    runtime_paths: RuntimePaths,
) -> None:
    """Reloading should run a service scan and include fresh counts and loaded statuses."""
    service = _targeted_service(runtime_paths)
    set_active_workspace_automation_service(cast("WorkspaceAutomationService", service))

    with tool_runtime_context(_tool_context(runtime_paths)):
        payload = json.loads(await WorkspaceAutomationTools().reload_automations())

    assert service.scan_now_call_count == 1
    assert payload["status"] == "ok"
    assert payload["loaded_count"] == 1
    assert payload["error_count"] == 0
    assert payload["errors"] == []
    assert payload["automations"][0]["automation_id"] == "urgent_email_poll"


@pytest.mark.asyncio
async def test_private_list_automations_matches_same_requester_across_sessions(
    runtime_paths: RuntimePaths,
) -> None:
    """Private automation status should follow the requester worker, not one Matrix session."""
    config = Config.validate_with_runtime(
        {
            "memory": {"backend": "none"},
            "agents": {
                "mind": {
                    "display_name": "Mind",
                    "rooms": ["Lobby"],
                    "private": {"per": "user"},
                    "workspace_automations": {"enabled": True},
                },
            },
        },
        runtime_paths,
    )
    original_identity = _private_identity("mind", "@alice:localhost")
    later_identity = _private_identity(
        "mind",
        "@alice:localhost",
        thread_id="$later:localhost",
        resolved_thread_id="$later:localhost",
        session_id="session-2",
    )
    runtime = resolve_agent_runtime("mind", config, runtime_paths, original_identity, create=True)
    assert runtime.workspace is not None
    (target,) = iter_workspace_automation_targets(config, runtime_paths)
    service = _FakeWorkspaceAutomationService(
        targets=(target,),
        automations=(
            WorkspaceAutomationLoadedStatus(
                agent_name="mind",
                automation_id="urgent_email_poll",
                workspace_root=str(runtime.workspace.root),
                schedule="* * * * *",
                last_status="action_succeeded",
                last_run_at="2026-06-16T12:00:00+00:00",
                last_exit_code=0,
                last_error=None,
                last_event_id="$event:localhost",
            ),
        ),
    )
    set_active_workspace_automation_service(cast("WorkspaceAutomationService", service))

    with tool_runtime_context(_private_tool_context(runtime_paths, config, later_identity)):
        payload = json.loads(await WorkspaceAutomationTools().list_automations())

    assert payload["status"] == "ok"
    assert [automation["automation_id"] for automation in payload["automations"]] == ["urgent_email_poll"]


def test_workspace_automation_tool_metadata_is_registered() -> None:
    """The workspace automation tool should be registered with primary execution metadata."""
    assert "workspace_automation" in TOOL_METADATA
    assert "workspace_automation" in TOOL_REGISTRY

    metadata = TOOL_METADATA["workspace_automation"]
    assert metadata.category in {ToolCategory.PRODUCTIVITY, ToolCategory.DEVELOPMENT}
    assert metadata.status == ToolStatus.AVAILABLE
    assert metadata.setup_type == SetupType.NONE
    assert metadata.default_execution_target == ToolExecutionTarget.PRIMARY
    assert metadata.function_names == (
        "list_automations",
        "reload_automations",
        "validate_automations",
    )
    assert TOOL_REGISTRY["workspace_automation"]() is WorkspaceAutomationTools
