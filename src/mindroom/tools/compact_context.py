"""Compact-context tool metadata registration.

Registers the ``compact_context`` tool in the metadata registry.
The actual toolkit (``mindroom.custom_tools.compact_context.CompactContextTools``)
requires agent context and is built in ``build_agent_toolkit()``, so it
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
        name="compact_context",
        file_access=ToolFileAccess.NONE,
        display_name="Context Compaction",
        description="Request context compaction before the next reply in this conversation scope",
        docs_url="https://docs.mindroom.chat/configuration/history/#manual-compaction",
        category=ToolCategory.PRODUCTIVITY,
        status=ToolStatus.AVAILABLE,
        setup_type=SetupType.NONE,
        icon="Minimize2",
        icon_color="text-amber-500",
        config_fields=[],
        dependencies=[],
        function_names=("compact_context",),
    ),
)
