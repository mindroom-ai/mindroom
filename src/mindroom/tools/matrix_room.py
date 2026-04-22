"""Native Matrix room introspection tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.metadata import SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.matrix_room import MatrixRoomTools


@register_tool_with_metadata(
    name="matrix_room",
    display_name="Matrix Room",
    description="Inspect Matrix room metadata, members, threads, and state",
    category=ToolCategory.COMMUNICATION,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="LayoutList",
    icon_color="text-blue-500",
    dependencies=["agno"],
    docs_url="https://github.com/mindroom-ai/mindroom",
    function_names=("matrix_room",),
)
def matrix_room_tools() -> type[MatrixRoomTools]:
    """Return native Matrix room introspection tools."""
    from mindroom.custom_tools.matrix_room import MatrixRoomTools

    return MatrixRoomTools
