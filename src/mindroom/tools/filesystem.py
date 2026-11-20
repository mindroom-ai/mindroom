"""Filesystem tool configuration."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolExecutionTarget,
    ToolFileAccess,
    ToolManagedInitArg,
    ToolStatus,
)
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from mindroom.custom_tools.filesystem import FilesystemTools
    from mindroom.tool_system.invocation import ToolCall, ToolNext
    from mindroom.tool_system.tool_function import AdaptedToolkit


@register_tool_with_metadata(
    name="filesystem",
    display_name="Filesystem",
    description="Read, edit, write, and search files",
    docs_url="https://docs.mindroom.chat/tools/execution-and-coding/#filesystem",
    category=ToolCategory.DEVELOPMENT,
    file_access=ToolFileAccess.AGENT,
    managed_init_args=(
        ToolManagedInitArg.FILE_ACCESS,
        ToolManagedInitArg.SHARED_DIRECTORY_ROOTS,
        ToolManagedInitArg.IN_SANDBOX_REQUEST,
    ),
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    default_execution_target=ToolExecutionTarget.WORKER,
    consumes_workspace_paths=True,
    icon="FaFolder",
    icon_color="text-yellow-500",
    config_fields=[
        ConfigField(
            name="base_dir",
            label="Base Directory",
            type="text",
            required=False,
            default=None,
            description="Working directory for file operations. Defaults to current directory.",
            authored_override=False,
        ),
        ConfigField(
            name="enable_delete_file",
            label="Enable Delete File",
            type="boolean",
            required=False,
            default=False,
            description="Offer delete_file, which removes a file or an empty directory.",
        ),
    ],
    dependencies=[],
    function_names=("delete_file", "edit_file", "find_files", "grep", "ls", "read_file", "write_file"),
    workspace_writers=("edit_file", "write_file"),
)
def filesystem_tools() -> type[FilesystemTools]:
    """Return the filesystem tools for LLM agents."""
    from mindroom.custom_tools.filesystem import FilesystemTools

    return FilesystemTools


def with_view_file_hint(
    toolkits: Sequence[AdaptedToolkit],
    views: Callable[[str], bool] | None,
) -> list[AdaptedToolkit]:
    """Return ``toolkits`` with ``read_file`` naming ``view_file`` for an image ``views`` finds, if one offers it."""
    if views is None or not any(
        function.name == "view_file" and function.registered_toolkit == "attachments"
        for toolkit in toolkits
        for function in toolkit.functions
    ):
        return list(toolkits)

    async def name_view_file(call: ToolCall, call_next: ToolNext) -> object:
        # why-lazy: the toolkit module loads only when the tool is built, not when the registry is.
        from mindroom.custom_tools.filesystem import image_read_error

        result = await call_next(call.arguments)
        path = call.arguments.get("path")
        if isinstance(path, str) and result == image_read_error(path) and await asyncio.to_thread(views, path):
            return f"{result} View it with view_file."
        return result

    return [
        replace(
            toolkit,
            functions=tuple(
                replace(function, middleware=(*function.middleware, name_view_file))
                if function.name == "read_file" and function.registered_toolkit == "filesystem"
                else function
                for function in toolkit.functions
            ),
        )
        for toolkit in toolkits
    ]
