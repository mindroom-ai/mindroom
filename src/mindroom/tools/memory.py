"""Memory tool metadata registration.

Registers the ``memory`` tool in the metadata registry for UI display.
The actual toolkit (``mindroom.custom_tools.memory.MemoryTools``) requires
agent context and is instantiated directly in ``create_agent()``, so it
is NOT added to ``TOOL_REGISTRY`` (no generic factory).
"""

from mindroom.tools_metadata import (
    TOOL_METADATA,
    SetupType,
    ToolCategory,
    ToolMetadata,
    ToolStatus,
)

TOOL_METADATA["memory"] = ToolMetadata(
    name="memory",
    display_name="Agent Memory",
    description="Explicitly store and search agent memories on demand",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="Brain",
    icon_color="text-violet-500",
    config_fields=[],
    dependencies=[],
)
