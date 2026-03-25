"""Thread resolution tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.metadata import SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.thread_resolution import ThreadResolutionTools


@register_tool_with_metadata(
    name="thread_resolution",
    display_name="Thread Resolution",
    description="Resolve or reopen Matrix threads using shared room-state markers",
    category=ToolCategory.COMMUNICATION,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="CheckCheck",
    icon_color="text-emerald-500",
    dependencies=["agno"],
    docs_url="https://github.com/mindroom-ai/mindroom",
)
def thread_resolution_tools() -> type[ThreadResolutionTools]:
    """Return Matrix thread resolution tools."""
    from mindroom.custom_tools.thread_resolution import ThreadResolutionTools

    return ThreadResolutionTools
