"""Background script control tool registration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolExecutionTarget,
    ToolStatus,
)
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.script import ScriptTools


@register_tool_with_metadata(
    name="script",
    display_name="Background Scripts",
    description=(
        "Run trusted arbitrary Python code with scoped worker filesystem and environment access plus "
        "deployment-policy network access"
    ),
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    default_execution_target=ToolExecutionTarget.PRIMARY,
    icon="FileCode2",
    icon_color="text-cyan-500",
    config_fields=[
        ConfigField(
            name="allowed_tools",
            label="Allowed Tools",
            type="string[]",
            required=False,
            default=[],
            description=(
                "Optional toolkit names allowed through the governed SDK only; this does not restrict Python, OS, "
                "filesystem, environment, or network access."
            ),
        ),
        ConfigField(
            name="max_concurrent_runs",
            label="Maximum Concurrent Runs",
            type="number",
            required=False,
            default=3,
            validation={"min": 1},
        ),
        ConfigField(
            name="max_tool_calls_per_minute",
            label="Maximum Tool Calls Per Minute",
            type="number",
            required=False,
            default=30,
            validation={"min": 1},
        ),
        ConfigField(
            name="max_runtime_hours",
            label="Maximum Runtime Hours",
            type="number",
            required=False,
            default=24,
            validation={"min": 0.01},
        ),
    ],
    dependencies=["agno"],
    requires_room_context=True,
    function_names=("run_script", "status_script", "cancel_script", "list_scripts"),
    supports_toolkit_filters=False,
)
def script_tools() -> type[ScriptTools]:
    """Return primary-owned background script controls."""
    from mindroom.custom_tools.script import ScriptTools

    return ScriptTools
