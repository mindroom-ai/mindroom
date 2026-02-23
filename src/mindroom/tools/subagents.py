"""Sub-agents toolkit configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.subagents import SubAgentsTools


@register_tool_with_metadata(
    name="subagents",
    display_name="Sub-Agents",
    description="Spawn, steer, and inspect Matrix session and sub-agent runs across agents",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="Workflow",
    icon_color="text-teal-500",
    dependencies=["agno"],
    docs_url="https://github.com/mindroom-ai/mindroom",
)
def subagents_tools() -> type[SubAgentsTools]:
    """Return sub-agents tools."""
    from mindroom.custom_tools.subagents import SubAgentsTools

    return SubAgentsTools
