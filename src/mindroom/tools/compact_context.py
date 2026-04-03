"""Compact-context tool metadata registration.

Registers the ``compact_context`` tool in the metadata registry for UI display.
The actual toolkit (``mindroom.custom_tools.compact_context.CompactContextTools``)
requires agent context and is instantiated directly in ``create_agent()``, so it
is NOT added to ``TOOL_REGISTRY`` (no generic factory).
"""

from mindroom.tool_system.metadata import (
    SetupType,
    ToolCategory,
    ToolMetadata,
    ToolStatus,
    register_builtin_tool_metadata,
)

register_builtin_tool_metadata(
    ToolMetadata(
        name="compact_context",
        display_name="Context Compaction",
        description="Schedule context compaction for the next reply in the current conversation scope",
        category=ToolCategory.PRODUCTIVITY,
        status=ToolStatus.AVAILABLE,
        setup_type=SetupType.NONE,
        icon="Minimize2",
        icon_color="text-amber-500",
        config_fields=[],
        dependencies=[],
    ),
)
