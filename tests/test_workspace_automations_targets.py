"""Tests for resolving workspace automation targets."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.runtime_resolution import resolve_agent_runtime as resolve_runtime
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, agent_workspace_root_path
from mindroom.workspace_automations import targets
from mindroom.workspace_automations.targets import iter_workspace_automation_targets, resolve_action_room
from mindroom.workspace_instances import WorkspaceInstanceRecord

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def runtime_paths(tmp_path: Path) -> RuntimePaths:
    """Create isolated runtime paths for target resolution."""
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={
            "CUSTOMER_ID": "tenant-123",
            "ACCOUNT_ID": "account-456",
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
        },
    )


def _config(runtime_paths: RuntimePaths, agents: dict[str, dict[str, object]]) -> Config:
    return Config.validate_with_runtime(
        {
            "memory": {"backend": "none"},
            "agents": agents,
        },
        runtime_paths,
    )


def _identity(
    agent_name: str,
    requester_id: str = "@alice:example.org",
    *,
    session_id: str = "session-1",
) -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name=agent_name,
        requester_id=requester_id,
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id=session_id,
        tenant_id="tenant-123",
        account_id="account-456",
        transport_agent_name=f"mindroom_{agent_name}",
    )


def _workspace_instance_record(
    *,
    agent_name: str,
    workspace_root: Path,
    execution_identity: ToolExecutionIdentity | None,
    is_private: bool = True,
) -> WorkspaceInstanceRecord:
    return WorkspaceInstanceRecord(
        agent_name=agent_name,
        workspace_root=workspace_root,
        state_root=workspace_root.parent,
        execution_scope="user",
        execution_identity=execution_identity,
        worker_key=f"worker-key-{agent_name}",
        is_private=is_private,
    )


def test_shared_enabled_agents_are_returned_with_resolved_runtime_workspace_policy_and_rooms(
    runtime_paths: RuntimePaths,
) -> None:
    """Enabled shared agents should become complete automation targets."""
    config = _config(
        runtime_paths,
        {
            "ops": {
                "display_name": "Ops",
                "rooms": ["Lobby", "Ops"],
                "workspace_automations": {
                    "enabled": True,
                    "allowed_actions": ["agent_message"],
                },
            },
        },
    )

    result = iter_workspace_automation_targets(config, runtime_paths)

    expected_root = agent_workspace_root_path(runtime_paths.storage_root, "ops")
    assert len(result) == 1
    target = result[0]
    assert target.agent_name == "ops"
    assert target.agent_configured_rooms == ("Lobby", "Ops")
    assert target.policy.enabled is True
    assert target.policy.allowed_actions == ["agent_message"]
    assert target.agent_runtime.agent_name == "ops"
    assert target.agent_runtime.workspace is not None
    assert target.agent_runtime.workspace.root == expected_root
    assert target.workspace_root == expected_root
    assert expected_root.is_dir()


def test_disabled_agents_are_skipped(runtime_paths: RuntimePaths) -> None:
    """Policy-disabled agents should not become automation targets."""
    config = _config(
        runtime_paths,
        {
            "ops": {
                "display_name": "Ops",
                "workspace_automations": {"enabled": True},
            },
            "quiet": {
                "display_name": "Quiet",
                "workspace_automations": {"enabled": False},
            },
        },
    )

    result = iter_workspace_automation_targets(config, runtime_paths)

    assert [target.agent_name for target in result] == ["ops"]


def test_agents_with_no_workspace_after_resolution_are_skipped(
    monkeypatch: pytest.MonkeyPatch,
    runtime_paths: RuntimePaths,
) -> None:
    """Targets should require a resolved usable workspace root."""
    config = _config(
        runtime_paths,
        {
            "ops": {
                "display_name": "Ops",
                "workspace_automations": {"enabled": True},
            },
        },
    )
    runtime_without_workspace = replace(
        resolve_runtime("ops", config, runtime_paths, execution_identity=None, create=True),
        workspace=None,
        tool_base_dir=None,
        file_memory_root=None,
    )

    def resolve_without_workspace(
        agent_name: str,
        _config: Config,
        _runtime_paths: RuntimePaths,
        execution_identity: object | None,
        *,
        create: bool = False,
    ) -> object:
        assert agent_name == "ops"
        assert execution_identity is None
        assert create is True
        return runtime_without_workspace

    monkeypatch.setattr(targets, "resolve_agent_runtime", resolve_without_workspace)

    result = iter_workspace_automation_targets(config, runtime_paths)

    assert result == []


def test_explicit_room_target_returns_that_room() -> None:
    """Explicit authored action rooms should be preserved for later Matrix resolution."""
    assert resolve_action_room(action_room="!ops:example.org", agent_configured_rooms=["Lobby"]) == "!ops:example.org"


def test_single_configured_room_fallback_returns_that_room() -> None:
    """Omitted action rooms should inherit a single configured room."""
    assert resolve_action_room(action_room=None, agent_configured_rooms=["Lobby"]) == "Lobby"


def test_multi_room_ambiguity_returns_none() -> None:
    """Omitted action rooms should not guess between multiple configured rooms."""
    assert resolve_action_room(action_room=None, agent_configured_rooms=["Lobby", "Ops"]) is None


def test_no_room_or_missing_room_ambiguity_returns_none() -> None:
    """Omitted action rooms should not invent a room when none are configured."""
    assert resolve_action_room(action_room=None, agent_configured_rooms=[]) is None


def test_existing_private_workspace_instance_with_enabled_policy_becomes_target(
    runtime_paths: RuntimePaths,
) -> None:
    """Already-materialized private workspace instances should become automation targets."""
    config = _config(
        runtime_paths,
        {
            "mind": {
                "display_name": "Mind",
                "private": {"per": "user"},
                "rooms": ["Private"],
                "workspace_automations": {
                    "enabled": True,
                    "allowed_actions": ["agent_message"],
                },
            },
        },
    )
    identity = _identity("mind")
    private_runtime = resolve_runtime("mind", config, runtime_paths, execution_identity=identity, create=True)
    assert private_runtime.workspace is not None

    result = iter_workspace_automation_targets(config, runtime_paths)

    assert len(result) == 1
    target = result[0]
    assert target.agent_name == "mind"
    assert target.agent_configured_rooms == ("Private",)
    assert target.policy.enabled is True
    assert target.policy.allowed_actions == ["agent_message"]
    assert target.agent_runtime.is_private is True
    assert target.agent_runtime.execution_identity == identity
    assert target.agent_runtime.worker_key == private_runtime.worker_key
    assert target.agent_runtime.workspace is not None
    assert target.agent_runtime.workspace.root == private_runtime.workspace.root
    assert target.workspace_root == private_runtime.workspace.root


def test_private_agents_without_registered_workspace_instance_are_not_proactively_created(
    runtime_paths: RuntimePaths,
) -> None:
    """Private automation discovery should not materialize every possible requester workspace."""
    config = _config(
        runtime_paths,
        {
            "mind": {
                "display_name": "Mind",
                "private": {"per": "user"},
                "workspace_automations": {"enabled": True},
            },
        },
    )

    result = iter_workspace_automation_targets(config, runtime_paths)

    assert result == []
    assert not (runtime_paths.storage_root / "private_instances").exists()


def test_stale_private_registry_records_are_skipped(
    monkeypatch: pytest.MonkeyPatch,
    runtime_paths: RuntimePaths,
) -> None:
    """Private registry discovery should ignore records that no longer describe usable targets."""
    config = _config(
        runtime_paths,
        {
            "no_workspace": {
                "display_name": "No Workspace",
                "private": {"per": "user"},
                "workspace_automations": {"enabled": True},
            },
            "disabled": {
                "display_name": "Disabled",
                "private": {"per": "user"},
                "workspace_automations": {"enabled": False},
            },
            "shared_record": {
                "display_name": "Shared Record",
                "worker_scope": "user",
                "workspace_automations": {"enabled": True},
            },
            "no_identity": {
                "display_name": "No Identity",
                "private": {"per": "user"},
                "workspace_automations": {"enabled": True},
            },
        },
    )
    no_workspace_runtime = resolve_runtime(
        "no_workspace",
        config,
        runtime_paths,
        execution_identity=_identity("no_workspace"),
        create=False,
    )
    assert no_workspace_runtime.workspace is not None
    disabled_root = runtime_paths.storage_root / "stale" / "disabled"
    disabled_root.mkdir(parents=True)
    shared_record_root = runtime_paths.storage_root / "stale" / "shared_record"
    shared_record_root.mkdir(parents=True)
    no_identity_root = runtime_paths.storage_root / "stale" / "no_identity"
    no_identity_root.mkdir(parents=True)
    missing_agent_root = runtime_paths.storage_root / "stale" / "missing_agent"
    missing_agent_root.mkdir(parents=True)
    stale_records = [
        _workspace_instance_record(
            agent_name="missing_agent",
            workspace_root=missing_agent_root,
            execution_identity=_identity("missing_agent"),
        ),
        _workspace_instance_record(
            agent_name="no_workspace",
            workspace_root=no_workspace_runtime.workspace.root,
            execution_identity=_identity("no_workspace"),
        ),
        _workspace_instance_record(
            agent_name="disabled",
            workspace_root=disabled_root,
            execution_identity=_identity("disabled"),
        ),
        _workspace_instance_record(
            agent_name="shared_record",
            workspace_root=shared_record_root,
            execution_identity=_identity("shared_record"),
            is_private=False,
        ),
        _workspace_instance_record(
            agent_name="no_identity",
            workspace_root=no_identity_root,
            execution_identity=None,
        ),
    ]
    monkeypatch.setattr(targets, "load_workspace_instance_records", lambda _runtime_paths: stale_records, raising=False)

    result = iter_workspace_automation_targets(config, runtime_paths)

    assert result == []


def test_identity_bearing_shared_registry_records_are_ignored_by_private_discovery(
    monkeypatch: pytest.MonkeyPatch,
    runtime_paths: RuntimePaths,
) -> None:
    """Shared registry records should not become requester-owned private automation targets."""
    config = _config(
        runtime_paths,
        {
            "ops": {
                "display_name": "Ops",
                "worker_scope": "user",
                "workspace_automations": {"enabled": True},
            },
        },
    )
    workspace_root = agent_workspace_root_path(runtime_paths.storage_root, "ops")
    workspace_root.mkdir(parents=True)
    identity_bearing_shared_record = _workspace_instance_record(
        agent_name="ops",
        workspace_root=workspace_root,
        execution_identity=_identity("ops"),
        is_private=False,
    )
    monkeypatch.setattr(
        targets,
        "load_workspace_instance_records",
        lambda _runtime_paths: [identity_bearing_shared_record],
        raising=False,
    )

    result = iter_workspace_automation_targets(config, runtime_paths)

    assert result == []


@pytest.mark.parametrize("worker_scope", ["user", "user_agent"])
def test_requester_scoped_agents_are_skipped_with_a_clear_reason(
    caplog: pytest.LogCaptureFixture,
    runtime_paths: RuntimePaths,
    worker_scope: str,
) -> None:
    """Requester-scoped shared agents cannot run unattended automations safely."""
    config = _config(
        runtime_paths,
        {
            "ops": {
                "display_name": "Ops",
                "worker_scope": worker_scope,
                "workspace_automations": {"enabled": True},
            },
        },
    )
    caplog.set_level("INFO", logger="mindroom.workspace_automations.targets")

    result = iter_workspace_automation_targets(config, runtime_paths)

    assert result == []
    assert "Skipping workspace automation target for agent 'ops'" in caplog.text
    assert f"worker_scope={worker_scope}" in caplog.text
    assert "requester-scoped workspace automations require a live requester identity" in caplog.text


def test_default_requester_scoped_agents_are_skipped_with_a_clear_reason(
    caplog: pytest.LogCaptureFixture,
    runtime_paths: RuntimePaths,
) -> None:
    """Inherited requester-scoped worker policies should also make automations ineligible."""
    config = Config.validate_with_runtime(
        {
            "memory": {"backend": "none"},
            "defaults": {
                "worker_scope": "user",
                "workspace_automations": {"enabled": True},
            },
            "agents": {
                "ops": {
                    "display_name": "Ops",
                },
            },
        },
        runtime_paths,
    )
    caplog.set_level("INFO", logger="mindroom.workspace_automations.targets")

    result = iter_workspace_automation_targets(config, runtime_paths)

    assert result == []
    assert "Skipping workspace automation target for agent 'ops'" in caplog.text
    assert "worker_scope=user" in caplog.text
    assert "requester-scoped workspace automations require a live requester identity" in caplog.text


def test_disabled_private_agents_do_not_log_private_unsupported_reason(
    caplog: pytest.LogCaptureFixture,
    runtime_paths: RuntimePaths,
) -> None:
    """Disabled private agents should be skipped as disabled without unsupported-private noise."""
    config = _config(
        runtime_paths,
        {
            "mind": {
                "display_name": "Mind",
                "private": {"per": "user"},
                "workspace_automations": {"enabled": False},
            },
        },
    )
    caplog.set_level("INFO", logger="mindroom.workspace_automations.targets")

    result = iter_workspace_automation_targets(config, runtime_paths)

    assert result == []
    assert "private workspace automations are not supported yet" not in caplog.text
