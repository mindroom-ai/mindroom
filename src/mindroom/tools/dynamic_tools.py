"""Dynamic-tools metadata registration.

Registers the ``dynamic_tools`` control-plane tool for UI visibility.
The actual toolkit searches the agent's deferred toolkits and is instantiated
directly in ``create_agent()``, so it is NOT added to ``TOOL_REGISTRY``.
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
        name="dynamic_tools",
        file_access=ToolFileAccess.NONE,
        display_name="Dynamic Tools",
        description="Search deferred tools and call them",
        category=ToolCategory.DEVELOPMENT,
        status=ToolStatus.AVAILABLE,
        setup_type=SetupType.NONE,
        icon="PackagePlus",
        icon_color="text-sky-500",
        config_fields=[],
        dependencies=[],
        function_names=("tool_search", "call_tool"),
    ),
)
