"""Tests for workspace automation policy configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING, get_args

import pytest
from pydantic import ValidationError

from mindroom.config.main import Config
from mindroom.config.models import WorkspaceAutomationActionName, WorkspaceAutomationPolicyConfig
from mindroom.constants import RuntimePaths, resolve_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def runtime_paths(tmp_path: Path) -> RuntimePaths:
    """Create isolated runtime paths for config validation."""
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
        },
    )


def test_workspace_automation_action_type_is_public() -> None:
    """Workspace automation action names should be importable from config models."""
    assert get_args(WorkspaceAutomationActionName) == ("agent_message", "matrix_message", "hook")


def test_workspace_automation_policy_model_defaults_are_concrete() -> None:
    """The effective policy model should have non-null defaults at construction."""
    policy = WorkspaceAutomationPolicyConfig()

    assert policy.enabled is False
    assert policy.min_interval_seconds == 60
    assert policy.max_timeout_seconds == 30
    assert policy.max_output_bytes == 65536
    assert policy.allowed_actions == []


def test_workspace_automation_policy_defaults_disabled(runtime_paths: RuntimePaths) -> None:
    """Agents should inherit the disabled workspace automation defaults."""
    config = Config.validate_with_runtime({"agents": {"ops": {"display_name": "Ops"}}}, runtime_paths)

    policy = config.get_agent_workspace_automation_policy("ops")

    assert policy.enabled is False
    assert policy.min_interval_seconds == 60
    assert policy.max_timeout_seconds == 30
    assert policy.max_output_bytes == 65536
    assert policy.allowed_actions == []


def test_workspace_automation_policy_agent_can_enable_actions(runtime_paths: RuntimePaths) -> None:
    """Per-agent policy should allow enabling explicit visible actions."""
    config = Config.validate_with_runtime(
        {
            "agents": {
                "ops": {
                    "display_name": "Ops",
                    "workspace_automations": {
                        "enabled": True,
                        "allowed_actions": ["agent_message"],
                    },
                },
            },
        },
        runtime_paths,
    )

    policy = config.get_agent_workspace_automation_policy("ops")

    assert policy.enabled is True
    assert policy.min_interval_seconds == 60
    assert policy.max_timeout_seconds == 30
    assert policy.max_output_bytes == 65536
    assert policy.allowed_actions == ["agent_message"]


def test_workspace_automation_policy_merges_agent_fields_over_defaults(runtime_paths: RuntimePaths) -> None:
    """Agent workspace automation policy should override defaults field by field."""
    config = Config.validate_with_runtime(
        {
            "defaults": {
                "workspace_automations": {
                    "enabled": True,
                    "min_interval_seconds": 120,
                    "max_timeout_seconds": 20,
                    "max_output_bytes": 4096,
                    "allowed_actions": ["agent_message", "hook"],
                },
            },
            "agents": {
                "ops": {
                    "display_name": "Ops",
                    "workspace_automations": {
                        "max_timeout_seconds": 5,
                    },
                },
            },
        },
        runtime_paths,
    )

    policy = config.get_agent_workspace_automation_policy("ops")

    assert policy.enabled is True
    assert policy.min_interval_seconds == 120
    assert policy.max_timeout_seconds == 5
    assert policy.max_output_bytes == 4096
    assert policy.allowed_actions == ["agent_message", "hook"]


def test_workspace_automation_empty_agent_policy_preserves_defaults(runtime_paths: RuntimePaths) -> None:
    """An empty per-agent override should not wipe explicitly authored defaults."""
    config = Config.validate_with_runtime(
        {
            "defaults": {
                "workspace_automations": {
                    "enabled": True,
                    "min_interval_seconds": 180,
                    "max_timeout_seconds": 15,
                    "max_output_bytes": 8192,
                    "allowed_actions": ["hook"],
                },
            },
            "agents": {
                "ops": {
                    "display_name": "Ops",
                    "workspace_automations": {},
                },
            },
        },
        runtime_paths,
    )

    policy = config.get_agent_workspace_automation_policy("ops")

    assert policy.enabled is True
    assert policy.min_interval_seconds == 180
    assert policy.max_timeout_seconds == 15
    assert policy.max_output_bytes == 8192
    assert policy.allowed_actions == ["hook"]


def test_workspace_automation_policy_rejects_invalid_action_names(runtime_paths: RuntimePaths) -> None:
    """Policy allowed_actions should reject unsupported names including none."""
    with pytest.raises(ValidationError, match="none"):
        Config.validate_with_runtime(
            {
                "agents": {
                    "ops": {
                        "display_name": "Ops",
                        "workspace_automations": {
                            "allowed_actions": ["none"],
                        },
                    },
                },
            },
            runtime_paths,
        )


def test_workspace_automation_policy_rejects_duplicate_actions(runtime_paths: RuntimePaths) -> None:
    """Policy allowed_actions should reject duplicates in encounter order."""
    with pytest.raises(ValidationError, match="Duplicate workspace automation allowed actions are not allowed: hook"):
        Config.validate_with_runtime(
            {
                "defaults": {
                    "workspace_automations": {
                        "allowed_actions": ["agent_message", "hook", "hook"],
                    },
                },
            },
            runtime_paths,
        )


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("min_interval_seconds", 59),
        ("max_timeout_seconds", 0),
        ("max_output_bytes", 1023),
    ],
)
def test_workspace_automation_policy_rejects_minimum_bound_violations(
    runtime_paths: RuntimePaths,
    field_name: str,
    value: int,
) -> None:
    """Workspace automation numeric policy limits should enforce lower bounds."""
    with pytest.raises(ValidationError, match=field_name):
        Config.validate_with_runtime(
            {
                "defaults": {
                    "workspace_automations": {
                        field_name: value,
                    },
                },
            },
            runtime_paths,
        )
