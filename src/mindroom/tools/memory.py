"""Memory tool configuration.

This registers the ``memory`` tool in the metadata/UI registry.
The actual toolkit class lives in ``mindroom.custom_tools.memory``
and is instantiated with agent context in ``create_agent()``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.memory import MemoryTools


@register_tool_with_metadata(
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
def memory_tools() -> type[MemoryTools]:
    """Return the MemoryTools class (requires agent context at instantiation)."""
    from mindroom.custom_tools.memory import MemoryTools

    return MemoryTools
