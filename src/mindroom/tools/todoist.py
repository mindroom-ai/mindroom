"""Todoist tool configuration."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger
from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.todoist import TodoistTools


logger = get_logger(__name__)


@register_tool_with_metadata(
    name="todoist",
    file_access=ToolFileAccess.NONE,
    display_name="Todoist",
    description="Task management with Todoist - create, update, delete, and organize tasks and projects",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="SiTodoist",
    icon_color="text-red-500",
    config_fields=[
        ConfigField(
            name="api_token",
            label="API Token",
            type="password",
            required=False,
            default=None,
        ),
    ],
    dependencies=["todoist-api-python"],
    docs_url="https://docs.agno.com/tools/toolkits/others/todoist",
    function_names=(
        "close_task",
        "create_task",
        "delete_task",
        "get_active_tasks",
        "get_projects",
        "get_task",
        "update_task",
    ),
)
def todoist_tools() -> type[TodoistTools]:
    """Return Todoist tools for task management."""
    from agno.tools.todoist import TodoistTools

    # AGNO_COMPAT: Todoist project discovery treats SDK result pages as projects.
    # Reason: The pinned SDK yields lists, and projects contain date values that
    # require the SDK serializer instead of direct __dict__ JSON encoding.
    # Upstream issue: Tracking gap; this adapter repairs the pinned page contract.
    # Upstream PR: No verified fix identified.
    # Remove when: Agno flattens SDK project pages with JSON-safe serialization.
    # Coverage: tests/test_todoist_tools.py.
    class MindRoomTodoistTools(TodoistTools):
        def get_projects(self) -> str:
            """Get all projects."""
            try:
                return json.dumps([project.to_dict() for page in self.api.get_projects() for project in page])
            except Exception as error:
                logger.exception("Failed to get projects")
                return json.dumps({"error": str(error)})

    return MindRoomTodoistTools
