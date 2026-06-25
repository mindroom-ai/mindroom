"""Todo tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.metadata import (
    SetupType,
    ToolCategory,
    ToolStatus,
    register_tool_with_metadata,
)

if TYPE_CHECKING:
    from mindroom.custom_tools.todo import TodoTools


@register_tool_with_metadata(
    name="todo",
    display_name="Todo",
    description="Create and manage per-thread work plans with dependencies",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="ListTodo",
    icon_color="text-blue-500",
    requires_room_context=True,
    docs_url="https://github.com/mindroom-ai/mindroom",
    function_names=(
        "add_todo",
        "apply_template",
        "complete_todo",
        "list_templates",
        "list_todos",
        "plan",
        "update_todo",
    ),
)
def todo_tools() -> type[TodoTools]:
    """Return built-in todo tools."""
    from mindroom.custom_tools.todo import TodoTools

    return TodoTools
