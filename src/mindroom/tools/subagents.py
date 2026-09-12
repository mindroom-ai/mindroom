"""Sub-agents toolkit configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import SetupType, ToolCategory, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.subagents import SubAgentsTools


@register_tool_with_metadata(
    name="subagents",
    display_name="Sub-Agents",
    description="Delegate tasks to sub-agents and follow up on their work",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    requires_room_context=True,
    icon="Workflow",
    icon_color="text-teal-500",
    dependencies=["agno"],
    docs_url="https://github.com/mindroom-ai/mindroom",
    function_names=("agents_list", "list_sessions", "sessions_send", "sessions_spawn"),
)
def subagents_tools() -> type[SubAgentsTools]:
    """Return sub-agents tools."""
    from mindroom.custom_tools.subagents import SubAgentsTools

    return SubAgentsTools
