"""Thread summary tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.metadata import SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.thread_summary import ThreadSummaryTools


@register_tool_with_metadata(
    name="thread_summary",
    display_name="Thread Summary",
    description="Set or update Matrix thread summaries with room/thread context defaults",
    category=ToolCategory.COMMUNICATION,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="MessageCircleMore",
    icon_color="text-cyan-500",
    dependencies=["agno"],
    docs_url="https://github.com/mindroom-ai/mindroom",
    function_names=("set_thread_summary",),
)
def register_thread_summary_tools() -> type[ThreadSummaryTools]:
    """Return Matrix thread summary tools."""
    from mindroom.custom_tools.thread_summary import ThreadSummaryTools

    return ThreadSummaryTools
