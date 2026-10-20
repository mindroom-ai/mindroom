"""Microsoft To Do tool registration."""

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
    from mindroom.custom_tools.microsoft_todo import MicrosoftTodoTools


@register_tool_with_metadata(
    name="microsoft_todo",
    display_name="Microsoft To Do",
    description="List, create, update, complete, and delete tasks in the connected user's Microsoft To Do",
    category=ToolCategory.PRODUCTIVITY,
    file_access=ToolFileAccess.NONE,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.OAUTH,
    requires_primary_runtime=True,
    auth_provider="microsoft_todo",
    icon="ListTodo",
    icon_color="text-blue-600",
    config_fields=[
        ConfigField(
            name="write",
            label="Allow Write Operations",
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
    docs_url="https://docs.mindroom.chat/tools/microsoft-365/",
    helper_text=(
        "Each user connects their own Microsoft account. "
        "Store the Microsoft Entra app's client ID and secret in the microsoft_oauth_client credential service."
    ),
    function_names=(
        "microsoft_todo_list_lists",
        "microsoft_todo_list_tasks",
        "microsoft_todo_create_task",
        "microsoft_todo_update_task",
        "microsoft_todo_delete_task",
    ),
)
def microsoft_todo_tools() -> type[MicrosoftTodoTools]:
    """Return Microsoft To Do tools that act as the requester."""
    from mindroom.custom_tools.microsoft_todo import MicrosoftTodoTools

    return MicrosoftTodoTools
