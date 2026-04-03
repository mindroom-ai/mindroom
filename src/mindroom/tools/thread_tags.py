"""Thread tags tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.metadata import SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.thread_tags import ThreadTagsTools


@register_tool_with_metadata(
    name="thread_tags",
    display_name="Thread Tags",
    description="Tag, untag, and inspect Matrix threads using shared room-state markers",
    category=ToolCategory.COMMUNICATION,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="Tags",
    icon_color="text-emerald-500",
    dependencies=["agno"],
    docs_url="https://github.com/mindroom-ai/mindroom",
)
def thread_tags_tools() -> type[ThreadTagsTools]:
    """Return Matrix thread tagging tools."""
    from mindroom.custom_tools.thread_tags import ThreadTagsTools

    return ThreadTagsTools
