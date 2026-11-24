"""Memory tool metadata registration.

Registers the ``memory`` tool in the metadata registry.
The actual toolkit (``mindroom.custom_tools.memory.MemoryTools``) requires
agent context and is built in ``build_agent_toolkit()``, so it
has no generic factory.
"""

from mindroom.tool_system.declarations import (
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolMetadata,
    ToolStatus,
)
from mindroom.tool_system.registration import register_builtin_tool_metadata

register_builtin_tool_metadata(
    ToolMetadata(
        name="memory",
        file_access=ToolFileAccess.NONE,
        display_name="Agent Memory",
        description="Save, find, and update information the agent remembers",
        docs_url="https://docs.mindroom.chat/memory/#memory",
        category=ToolCategory.PRODUCTIVITY,
        status=ToolStatus.AVAILABLE,
        setup_type=SetupType.NONE,
        icon="Brain",
        icon_color="text-violet-500",
        config_fields=[],
        dependencies=[],
        function_names=(
            "add_memory",
            "delete_memory",
            "get_memory",
            "list_memories",
            "search_memories",
            "update_memory",
        ),
    ),
)
