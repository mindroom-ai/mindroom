"""Tests for executing workspace automation checks."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from mindroom.workspace_automations.executor import run_shell_check
from mindroom.workspace_automations.models import (
    LoadedWorkspaceAutomation,
    WorkspaceAutomationAction,
    WorkspaceAutomationCheck,
)
from mindroom.workspace_automations.targets import WorkspaceAutomationTarget

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def runtime_paths(tmp_path: Path) -> RuntimePaths:
    """Create isolated runtime paths for executor tests."""
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={
            "ACCOUNT_ID": "account-456",
            "CUSTOMER_ID": "tenant-123",
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
        },
    )


@pytest.fixture
def config(runtime_paths: RuntimePaths) -> Config:
    """Create a shared scoped agent with workspace automations enabled."""
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
    """Resolve the automation target used by executor tests."""
    return _resolve_target(config, runtime_paths, execution_identity=None)


def _resolve_target(
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    execution_identity: ToolExecutionIdentity | None,
) -> WorkspaceAutomationTarget:
    agent_runtime = resolve_agent_runtime(
        "ops",
        config,
        runtime_paths,
        execution_identity=execution_identity,
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
        agent_name=target.agent_name,
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


def _private_config(runtime_paths: RuntimePaths) -> Config:
    return Config.validate_with_runtime(
        {
            "memory": {"backend": "none"},
            "defaults": {"worker_tools": ["shell"]},
            "agents": {
                "ops": {
                    "display_name": "Ops",
                    "private": {"per": "user", "root": "ops_data"},
                    "workspace_automations": {
                        "enabled": True,
                        "max_output_bytes": 4096,
                    },
                },
            },
        },
        runtime_paths,
    )


def _private_execution_identity(*, session_id: str = "private-turn-session") -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name="ops",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id=session_id,
        tenant_id="tenant-123",
        account_id="account-456",
        transport_agent_name="mindroom_ops",
    )


def _successful_shell_toolkit() -> object:
    async def run_shell_command_structured(
        _args: str,
        *,
        tail: int,
        timeout: int,  # noqa: ASYNC109
        max_output_bytes: int,
    ) -> dict[str, object]:
        return {
            "ok": True,
            "exit_code": 0,
            "stdout": f"tail={tail} timeout={timeout} max={max_output_bytes}",
            "stderr": "",
            "raw_output": "ok",
            "timed_out": False,
            "error": None,
        }

    return SimpleNamespace(
        async_functions={
            "run_shell_command_structured": SimpleNamespace(entrypoint=run_shell_command_structured),
        },
    )


@pytest.mark.asyncio
async def test_shell_check_runs_through_worker_routed_shell_toolkit(  # noqa: PLR0915
    monkeypatch: pytest.MonkeyPatch,
    config: Config,
    runtime_paths: RuntimePaths,
    target: WorkspaceAutomationTarget,
) -> None:
    """Shell automation checks should route through the configured shell toolkit."""
    automation = _automation(target)
    structured_calls: list[tuple[str, int, int, int]] = []
    runtime_override_calls: list[tuple[str, str]] = []

    def get_agent_tool_runtime_overrides(
        self: Config,
        agent_name: str,
        tool_name: str,
    ) -> dict[str, object]:
        assert self is config
        runtime_override_calls.append((agent_name, tool_name))
        return {"extra_env_passthrough": "GITEA_TOKEN"}

    def resolve_runtime_worker_tools(
        agent_name: str,
        actual_config: Config,
        actual_runtime_paths: RuntimePaths,
        runtime_tool_names: list[str],
    ) -> list[str]:
        assert agent_name == "ops"
        assert actual_config is config
        assert actual_runtime_paths is runtime_paths
        assert runtime_tool_names == ["shell"]
        return ["shell"]

    async def run_shell_command_structured(
        args: str,
        *,
        tail: int,
        timeout: int,  # noqa: ASYNC109
        max_output_bytes: int,
    ) -> dict[str, object]:
        structured_calls.append((args, tail, timeout, max_output_bytes))
        return {
            "ok": False,
            "exit_code": 42,
            "stdout": "matched urgent mail",
            "stderr": "",
            "raw_output": "matched urgent mail",
            "timed_out": False,
            "error": None,
        }

    def build_agent_toolkit(
        tool_name: str,
        *,
        agent_name: str,
        config: Config,
        runtime_paths: RuntimePaths,
        worker_tools: list[str],
        runtime_overrides: dict[str, object] | None,
        agent_runtime: object | None = None,
        tool_config_overrides: dict[str, object] | None = None,
        execution_identity: object | None,
        session_id: str | None = None,
        delegation_depth: int = 0,
        refresh_scheduler: object | None = None,
    ) -> object:
        assert tool_name == "shell"
        assert agent_name == "ops"
        assert config is config_fixture
        assert runtime_paths is runtime_paths_fixture
        assert worker_tools == ["shell"]
        assert runtime_overrides == {"extra_env_passthrough": "GITEA_TOKEN"}
        assert agent_runtime is target.agent_runtime
        assert target.agent_runtime.execution_scope == "shared"
        assert target.agent_runtime.tool_base_dir == target.workspace_root
        assert tool_config_overrides is None
        assert execution_identity is not None
        assert execution_identity.channel == "matrix"
        assert execution_identity.agent_name == "ops"
        assert execution_identity.transport_agent_name == "ops"
        assert execution_identity.requester_id is None
        assert execution_identity.room_id is None
        assert execution_identity.thread_id is None
        assert execution_identity.resolved_thread_id is None
        assert execution_identity.session_id == "workspace-automation:ops:urgent_email_poll"
        assert execution_identity.tenant_id == "tenant-123"
        assert execution_identity.account_id == "account-456"
        assert session_id is None
        assert delegation_depth == 0
        assert refresh_scheduler is None
        return SimpleNamespace(
            async_functions={
                "run_shell_command_structured": SimpleNamespace(entrypoint=run_shell_command_structured),
            },
        )

    config_fixture = config
    runtime_paths_fixture = runtime_paths
    monkeypatch.setattr(Config, "get_agent_tool_runtime_overrides", get_agent_tool_runtime_overrides)
    monkeypatch.setattr(
        "mindroom.workspace_automations.executor.resolve_runtime_worker_tools",
        resolve_runtime_worker_tools,
    )
    monkeypatch.setattr("mindroom.workspace_automations.executor.build_agent_toolkit", build_agent_toolkit)

    result = await run_shell_check(
        config=config,
        runtime_paths=runtime_paths,
        target=target,
        automation=automation,
    )

    assert structured_calls == [("./scripts/check_urgent_email.sh", 37, 12, 4096)]
    assert runtime_override_calls == [("ops", "shell")]
    assert result.automation_id == "urgent_email_poll"
    assert result.ok is False
    assert result.exit_code == 42
    assert result.stdout == "matched urgent mail"
    assert result.stderr == ""
    assert result.raw_output == "matched urgent mail"
    assert result.timed_out is False
    assert result.error is None


@pytest.mark.asyncio
async def test_shell_check_honors_shell_tool_config_overrides(runtime_paths: RuntimePaths) -> None:
    """Automation shell checks should fail closed when shell execution is disabled."""
    config = Config.validate_with_runtime(
        {
            "memory": {"backend": "none"},
            "defaults": {"worker_tools": ["shell"]},
            "agents": {
                "ops": {
                    "display_name": "Ops",
                    "tools": [{"shell": {"enable_run_shell_command": False}}],
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
    target = _resolve_target(config, runtime_paths, execution_identity=None)

    result = await run_shell_check(
        config=config,
        runtime_paths=runtime_paths,
        target=target,
        automation=_automation(target),
    )

    assert result.ok is False
    assert result.error == "Shell toolkit did not expose structured execution."


@pytest.mark.asyncio
async def test_shell_check_uses_private_target_persisted_execution_identity(
    monkeypatch: pytest.MonkeyPatch,
    runtime_paths: RuntimePaths,
) -> None:
    """Private automation checks should use the target runtime identity unchanged."""
    private_config = _private_config(runtime_paths)
    private_identity = _private_execution_identity()
    private_target = _resolve_target(private_config, runtime_paths, execution_identity=private_identity)
    automation = _automation(private_target)
    captured_identities: list[object | None] = []

    def build_agent_toolkit(
        _tool_name: str,
        *,
        execution_identity: object | None,
        **_kwargs: object,
    ) -> object:
        captured_identities.append(execution_identity)
        return _successful_shell_toolkit()

    def build_tool_execution_identity(**_kwargs: object) -> ToolExecutionIdentity:
        msg = "private identity should not be synthesized"
        raise RuntimeError(msg)

    monkeypatch.setattr(
        "mindroom.workspace_automations.executor.resolve_runtime_worker_tools",
        lambda *_args, **_kwargs: ["shell"],
    )
    monkeypatch.setattr(
        "mindroom.workspace_automations.executor.build_tool_execution_identity",
        build_tool_execution_identity,
    )
    monkeypatch.setattr("mindroom.workspace_automations.executor.build_agent_toolkit", build_agent_toolkit)

    result = await run_shell_check(
        config=private_config,
        runtime_paths=runtime_paths,
        target=private_target,
        automation=automation,
    )

    assert result.ok is True
    assert private_target.agent_runtime.execution_identity is private_identity
    assert captured_identities == [private_identity]
    assert captured_identities[0] is private_identity


@pytest.mark.asyncio
async def test_shell_check_synthesizes_automation_identity_for_shared_targets(
    monkeypatch: pytest.MonkeyPatch,
    config: Config,
    runtime_paths: RuntimePaths,
    target: WorkspaceAutomationTarget,
) -> None:
    """Shared automation checks should keep using the deterministic automation identity."""
    automation = _automation(target)
    captured_identities: list[ToolExecutionIdentity | None] = []

    def build_agent_toolkit(
        _tool_name: str,
        *,
        execution_identity: ToolExecutionIdentity | None,
        **_kwargs: object,
    ) -> object:
        captured_identities.append(execution_identity)
        return _successful_shell_toolkit()

    monkeypatch.setattr(
        "mindroom.workspace_automations.executor.resolve_runtime_worker_tools",
        lambda *_args, **_kwargs: ["shell"],
    )
    monkeypatch.setattr("mindroom.workspace_automations.executor.build_agent_toolkit", build_agent_toolkit)

    result = await run_shell_check(
        config=config,
        runtime_paths=runtime_paths,
        target=target,
        automation=automation,
    )

    assert result.ok is True
    assert len(captured_identities) == 1
    execution_identity = captured_identities[0]
    assert execution_identity is not None
    assert execution_identity.channel == "matrix"
    assert execution_identity.agent_name == "ops"
    assert execution_identity.transport_agent_name == "ops"
    assert execution_identity.requester_id is None
    assert execution_identity.room_id is None
    assert execution_identity.thread_id is None
    assert execution_identity.resolved_thread_id is None
    assert execution_identity.session_id == "workspace-automation:ops:urgent_email_poll"
    assert execution_identity.tenant_id == "tenant-123"
    assert execution_identity.account_id == "account-456"


@pytest.mark.asyncio
async def test_shell_check_does_not_overwrite_private_target_session_id(
    monkeypatch: pytest.MonkeyPatch,
    runtime_paths: RuntimePaths,
) -> None:
    """Private target identities should keep their persisted requester session id."""
    private_config = _private_config(runtime_paths)
    private_identity = _private_execution_identity(session_id="matrix-private-session")
    private_target = _resolve_target(private_config, runtime_paths, execution_identity=private_identity)
    automation = _automation(private_target)
    captured_session_ids: list[str | None] = []

    def build_agent_toolkit(
        _tool_name: str,
        *,
        execution_identity: ToolExecutionIdentity | None,
        **_kwargs: object,
    ) -> object:
        assert execution_identity is not None
        captured_session_ids.append(execution_identity.session_id)
        return _successful_shell_toolkit()

    monkeypatch.setattr(
        "mindroom.workspace_automations.executor.resolve_runtime_worker_tools",
        lambda *_args, **_kwargs: ["shell"],
    )
    monkeypatch.setattr("mindroom.workspace_automations.executor.build_agent_toolkit", build_agent_toolkit)

    result = await run_shell_check(
        config=private_config,
        runtime_paths=runtime_paths,
        target=private_target,
        automation=automation,
    )

    assert result.ok is True
    assert private_identity.session_id == "matrix-private-session"
    assert captured_session_ids == ["matrix-private-session"]


@pytest.mark.asyncio
async def test_shell_check_returns_failed_result_when_toolkit_construction_raises(
    monkeypatch: pytest.MonkeyPatch,
    config: Config,
    runtime_paths: RuntimePaths,
    target: WorkspaceAutomationTarget,
) -> None:
    """Toolkit construction errors should not escape the automation service loop."""
    automation = _automation(target)

    def raise_from_build_agent_toolkit(*_args: object, **_kwargs: object) -> object:
        message = "worker unavailable"
        raise RuntimeError(message)

    monkeypatch.setattr(
        "mindroom.workspace_automations.executor.resolve_runtime_worker_tools",
        lambda *_args, **_kwargs: ["shell"],
    )
    monkeypatch.setattr(
        "mindroom.workspace_automations.executor.build_agent_toolkit",
        raise_from_build_agent_toolkit,
    )

    result = await run_shell_check(
        config=config,
        runtime_paths=runtime_paths,
        target=target,
        automation=automation,
    )

    assert result.automation_id == "urgent_email_poll"
    assert result.ok is False
    assert result.exit_code is None
    assert result.stdout == ""
    assert result.stderr == ""
    assert result.raw_output == ""
    assert result.timed_out is False
    assert result.error is not None
    assert "worker unavailable" in result.error


@pytest.mark.asyncio
async def test_shell_check_returns_failed_result_when_structured_command_raises(
    monkeypatch: pytest.MonkeyPatch,
    config: Config,
    runtime_paths: RuntimePaths,
    target: WorkspaceAutomationTarget,
) -> None:
    """Structured shell execution errors should become failed check results."""
    automation = _automation(target)

    async def run_shell_command_structured(*_args: object, **_kwargs: object) -> dict[str, object]:
        message = "sandbox call failed"
        raise RuntimeError(message)

    monkeypatch.setattr(
        "mindroom.workspace_automations.executor.resolve_runtime_worker_tools",
        lambda *_args, **_kwargs: ["shell"],
    )
    monkeypatch.setattr(
        "mindroom.workspace_automations.executor.build_agent_toolkit",
        lambda *_args, **_kwargs: SimpleNamespace(
            async_functions={
                "run_shell_command_structured": SimpleNamespace(entrypoint=run_shell_command_structured),
            },
        ),
    )

    result = await run_shell_check(
        config=config,
        runtime_paths=runtime_paths,
        target=target,
        automation=automation,
    )

    assert result.automation_id == "urgent_email_poll"
    assert result.ok is False
    assert result.exit_code is None
    assert result.stdout == ""
    assert result.stderr == ""
    assert result.raw_output == ""
    assert result.timed_out is False
    assert result.error is not None
    assert "sandbox call failed" in result.error
