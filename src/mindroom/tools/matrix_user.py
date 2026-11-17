"""Tool configuration for acting in Matrix as a personal agent's owner."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import SetupType, ToolCategory, ToolFileAccess, ToolManagedInitArg, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.matrix_user import MatrixUserTools


@register_tool_with_metadata(
    name="matrix_user",
    file_access=ToolFileAccess.NONE,
    display_name="Matrix as the Owner",
    description=(
        "The owner's own Matrix account: catch up on their rooms and DMs, search, read, and, when they ask for "
        "exactly that, write as them"
    ),
    category=ToolCategory.COMMUNICATION,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="UserRound",
    icon_color="text-blue-500",
    docs_url="https://docs.mindroom.chat/tools/matrix-and-attachments/#matrix_user",
    function_names=("matrix_user_read",),
    # The application-service token lets this toolkit act as anyone on the homeserver, so only the primary reads it.
    requires_primary_runtime=True,
    requires_room_context=False,
    managed_init_args=(
        ToolManagedInitArg.RUNTIME_PATHS,
        ToolManagedInitArg.RUNTIME_CONFIG,
        ToolManagedInitArg.AGENT_NAME,
    ),
)
def matrix_user_tools() -> type[MatrixUserTools]:
    """Return the toolkit that acts in Matrix as a personal agent's owner."""
    from mindroom.custom_tools.matrix_user import MatrixUserTools

    return MatrixUserTools
