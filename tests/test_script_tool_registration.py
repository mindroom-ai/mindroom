"""Registration and routing tests for background script controls."""

import mindroom.tools  # noqa: F401
from mindroom.tool_system.declarations import ToolExecutionTarget
from mindroom.tool_system.registry_state import TOOL_METADATA
from mindroom.tool_system.worker_routing import tool_stays_local


def test_script_tool_metadata_declares_primary_room_controls_and_limits() -> None:
    """The catalog describes the primary-only room tool and its authored limits."""
    metadata = TOOL_METADATA["script"]

    assert metadata.default_execution_target is ToolExecutionTarget.PRIMARY
    assert metadata.requires_room_context is True
    assert metadata.function_names == (
        "run_script",
        "status_script",
        "cancel_script",
        "list_scripts",
    )
    assert {field.name for field in metadata.config_fields or ()} == {
        "allowed_tools",
        "max_concurrent_runs",
        "max_runtime_hours",
        "max_tool_calls_per_minute",
    }


def test_script_control_tool_always_stays_in_primary_runtime() -> None:
    """Only the launched process routes to a worker; its control plane remains primary-owned."""
    assert tool_stays_local("script") is True
