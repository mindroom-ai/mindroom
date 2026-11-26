"""Todo tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import (
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolStatus,
)
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.todo import TodoTools


@register_tool_with_metadata(
    name="todo",
    file_access=ToolFileAccess.NONE,
    display_name="Todo",
    description="Manage a visible per-thread task plan with priorities, dependencies, and reusable templates",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    requires_primary_runtime=True,
    icon="ListTodo",
    icon_color="text-blue-500",
    requires_room_context=True,
    docs_url="https://docs.mindroom.chat/tools/project-management/#todo",
    function_names=("todo",),
)
def todo_tools() -> type[TodoTools]:
    """Return built-in todo tools."""
    from mindroom.custom_tools.todo import TodoTools

    return TodoTools
