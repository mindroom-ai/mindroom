"""Thread resolution tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import SetupType, ToolCategory, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.thread_resolution import ThreadResolutionTools


@register_tool_with_metadata(
    name="thread_resolution",
    display_name="Thread Resolution",
    description="Explicitly resolve or reopen the active Matrix thread",
    category=ToolCategory.COMMUNICATION,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="CircleCheckBig",
    icon_color="text-emerald-500",
    dependencies=["agno"],
    docs_url="https://github.com/mindroom-ai/mindroom",
    function_names=("reopen_thread", "resolve_thread"),
    requires_room_context=True,
)
def thread_resolution_tools() -> type[ThreadResolutionTools]:
    """Return explicit Matrix thread lifecycle tools."""
    from mindroom.custom_tools.thread_resolution import ThreadResolutionTools

    return ThreadResolutionTools
