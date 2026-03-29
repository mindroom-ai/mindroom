"""Matrix API tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.metadata import SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.matrix_api import MatrixApiTools


@register_tool_with_metadata(
    name="matrix_api",
    display_name="Matrix API",
    description="Low-level Matrix event and state operations (send_event, get_state, put_state, redact, get_event)",
    category=ToolCategory.COMMUNICATION,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="MessageSquare",
    icon_color="text-emerald-500",
    dependencies=["agno"],
    docs_url="https://github.com/mindroom-ai/mindroom",
)
def matrix_api_tools() -> type[MatrixApiTools]:
    """Return low-level Matrix API tools."""
    from mindroom.custom_tools.matrix_api import MatrixApiTools

    return MatrixApiTools
