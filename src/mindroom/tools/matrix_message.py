"""Native Matrix messaging tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import (
    SetupType,
    ToolCategory,
    ToolManagedInitArg,
    ToolStatus,
)
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.matrix_message import MatrixMessageTools


@register_tool_with_metadata(
    name="matrix_message",
    display_name="Matrix Message",
    description="Read, send, edit, and react to messages in Matrix rooms and threads",
    category=ToolCategory.COMMUNICATION,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    requires_room_context=True,
    icon="MessageSquare",
    icon_color="text-green-500",
    dependencies=["agno"],
    docs_url="https://github.com/mindroom-ai/mindroom",
    function_names=("matrix_message",),
    managed_init_args=(ToolManagedInitArg.TOOL_OUTPUT_WORKSPACE_ROOT,),
)
def matrix_message_tools() -> type[MatrixMessageTools]:
    """Return native Matrix messaging tools."""
    from mindroom.custom_tools.matrix_message import MatrixMessageTools

    return MatrixMessageTools
