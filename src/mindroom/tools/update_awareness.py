"""MindRoom update-awareness tool registration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import SetupType, ToolCategory, ToolManagedInitArg, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.update_awareness import UpdateAwarenessTools


@register_tool_with_metadata(
    name="update_awareness",
    display_name="MindRoom Update Awareness",
    description="Add daily-cached installed and latest MindRoom release information to the agent system prompt",
    category=ToolCategory.INFORMATION,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="RefreshCw",
    icon_color="text-cyan-500",
    dependencies=["agno"],
    docs_url="https://docs.mindroom.chat/tools/#mindroom-update-awareness",
    managed_init_args=(ToolManagedInitArg.RUNTIME_PATHS,),
    function_names=("get_mindroom_update_status",),
)
def update_awareness_tools() -> type[UpdateAwarenessTools]:
    """Return the MindRoom update-awareness toolkit."""
    from mindroom.custom_tools.update_awareness import UpdateAwarenessTools

    return UpdateAwarenessTools
