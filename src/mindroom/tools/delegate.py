"""Delegate tool metadata registration.

Registers the ``delegate`` tool in the metadata registry for UI display.
The actual toolkit (``mindroom.custom_tools.delegate.DelegateTools``) requires
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

TOOL_METADATA["delegate"] = ToolMetadata(
    name="delegate",
    display_name="Agent Delegation",
    description="Delegate tasks to other configured agents",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="Users",
    icon_color="text-blue-500",
    config_fields=[],
    dependencies=[],
)
