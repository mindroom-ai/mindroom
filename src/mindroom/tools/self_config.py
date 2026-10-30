"""Self-config tool metadata registration.

Registers the ``self_config`` tool in the metadata registry for UI display.
The actual toolkit (``mindroom.custom_tools.self_config.SelfConfigTools``)
requires ``agent_name`` at instantiation and is injected directly in
``create_agent()``, so its metadata has no generic factory.
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
        name="self_config",
        file_access=ToolFileAccess.NONE,
        display_name="Self Config",
        description="Allow an agent to read and modify its own configuration",
        docs_url="https://docs.mindroom.chat/tools/agent-orchestration/#self_config",
        category=ToolCategory.DEVELOPMENT,
        status=ToolStatus.AVAILABLE,
        setup_type=SetupType.NONE,
        icon="Settings",
        icon_color="text-indigo-500",
        config_fields=[],
        dependencies=[],
        function_names=("get_own_config", "update_own_config"),
    ),
)
