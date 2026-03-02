"""Self-config tool metadata registration.

Registers the ``self_config`` tool in the metadata registry for UI display.
The actual toolkit (``mindroom.custom_tools.self_config.SelfConfigTools``)
requires ``agent_name`` at instantiation and is injected directly in
``create_agent()``, so it is NOT added to ``TOOL_REGISTRY``.
"""

from mindroom.tool_system.metadata import (
    TOOL_METADATA,
    SetupType,
    ToolCategory,
    ToolMetadata,
    ToolStatus,
)

TOOL_METADATA["self_config"] = ToolMetadata(
    name="self_config",
    display_name="Self Config",
    description="Allow an agent to read and modify its own configuration",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="Settings",
    icon_color="text-indigo-500",
    config_fields=[],
    dependencies=[],
)
