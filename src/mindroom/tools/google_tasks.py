"""Google Tasks tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolManagedInitArg,
    ToolStatus,
)
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.google_tasks import GoogleTasksTools


@register_tool_with_metadata(
    name="google_tasks",
    file_access=ToolFileAccess.NONE,
    display_name="Google Tasks",
    description="List, create, update, complete, and delete tasks in the connected user's Google Tasks",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.OAUTH,
    requires_primary_runtime=True,
    auth_provider="google_tasks",
    icon="SiGoogletasks",
    icon_color="text-blue-600",
    config_fields=[
        ConfigField(
            name="read_tasks",
            label="Read Tasks",
            type="boolean",
            required=False,
            default=True,
            description="Allow listing task lists and tasks.",
        ),
        ConfigField(
            name="manage_tasks",
            label="Manage Tasks",
            type="boolean",
            required=False,
            default=True,
            description="Allow creating, updating, completing, and deleting tasks.",
        ),
    ],
    managed_init_args=(
        ToolManagedInitArg.RUNTIME_PATHS,
        ToolManagedInitArg.CREDENTIALS_MANAGER,
        ToolManagedInitArg.WORKER_TARGET,
        ToolManagedInitArg.RUNTIME_CONFIG,
    ),
    dependencies=[
        "google-api-python-client",
        "google-auth",
        "google-auth-httplib2",
        "google-auth-oauthlib",
    ],
    docs_url="https://developers.google.com/workspace/tasks/reference/rest",
    function_names=(
        "google_tasks_create_task",
        "google_tasks_delete_task",
        "google_tasks_list_task_lists",
        "google_tasks_list_tasks",
        "google_tasks_update_task",
    ),
)
def google_tasks_tools() -> type[GoogleTasksTools]:
    """Return Google Tasks tools for task list and task management."""
    from mindroom.custom_tools.google_tasks import GoogleTasksTools

    return GoogleTasksTools
