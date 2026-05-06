"""Dynamic-tools metadata registration.

Registers the ``dynamic_tools`` control-plane tool for UI visibility.
The actual toolkit requires agent and session context and is instantiated
directly in ``create_agent()``, so it is NOT added to ``TOOL_REGISTRY``.
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
        name="dynamic_tools",
        display_name="Dynamic Tools",
        description="Load and unload allowed toolkits for the current session",
        category=ToolCategory.DEVELOPMENT,
        status=ToolStatus.AVAILABLE,
        setup_type=SetupType.NONE,
        icon="PackagePlus",
        icon_color="text-sky-500",
        config_fields=[],
        dependencies=[],
    ),
)
