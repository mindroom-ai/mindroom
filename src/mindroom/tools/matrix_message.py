"""Native Matrix messaging tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.matrix_message import MatrixMessageTools


@register_tool_with_metadata(
    name="matrix_message",
    display_name="Matrix Message",
    description="Send, reply, react, and read Matrix messages with room/thread context defaults",
    category=ToolCategory.COMMUNICATION,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="MessageSquare",
    icon_color="text-green-500",
    dependencies=["agno"],
    docs_url="https://github.com/mindroom-ai/mindroom",
)
def matrix_message_tools() -> type[MatrixMessageTools]:
    """Return native Matrix messaging tools."""
    from mindroom.custom_tools.matrix_message import MatrixMessageTools

    return MatrixMessageTools
