"""Matrix API tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.metadata import SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.matrix_api import MatrixApiTools


@register_tool_with_metadata(
    name="matrix_api",
    display_name="Matrix API",
    description="Low-level Matrix event, state, and room search operations (send_event, get_state, put_state, redact, get_event, search)",
    category=ToolCategory.COMMUNICATION,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="MessageSquare",
    icon_color="text-emerald-500",
    dependencies=["agno"],
    docs_url="https://github.com/mindroom-ai/mindroom",
    helper_text=(
        "Search uses action='search' with required `search_term`. "
        "`room_id` defaults to the current room. "
        "Optional `keys` defaults to ['content.body'], `order_by` is `rank` or `recent`, "
        "`limit` must be 1-50, and `next_batch`, `filter`, and `event_context` are passed through "
        "to Matrix room-event search."
    ),
)
def matrix_api_tools() -> type[MatrixApiTools]:
    """Return low-level Matrix API tools."""
    from mindroom.custom_tools.matrix_api import MatrixApiTools

    return MatrixApiTools
