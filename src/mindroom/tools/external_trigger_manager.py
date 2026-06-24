"""External trigger manager tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.metadata import SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.external_trigger_manager import ExternalTriggerManagerTools


@register_tool_with_metadata(
    name="external_trigger_manager",
    display_name="External Trigger Manager",
    description="Create and manage approval-gated external trigger endpoints",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="Webhook",
    icon_color="text-emerald-500",
    dependencies=["agno"],
    docs_url="https://github.com/mindroom-ai/mindroom",
    function_names=(
        "create_trigger",
        "list_triggers",
        "disable_trigger",
        "delete_trigger",
        "rotate_trigger_key",
    ),
)
def external_trigger_manager_tools() -> type[ExternalTriggerManagerTools]:
    """Return external trigger manager tools."""
    from mindroom.custom_tools.external_trigger_manager import ExternalTriggerManagerTools

    return ExternalTriggerManagerTools
