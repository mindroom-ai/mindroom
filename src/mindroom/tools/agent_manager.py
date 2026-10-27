"""Agent manager tool metadata; the toolkit takes ``agent_name`` and what it may do, so it is injected, not in ``TOOL_REGISTRY``."""

from mindroom.tool_system.declarations import ToolCategory, ToolFileAccess, ToolMetadata
from mindroom.tool_system.registration import register_builtin_tool_metadata

register_builtin_tool_metadata(
    ToolMetadata(
        name="agent_manager",
        file_access=ToolFileAccess.NONE,
        display_name="Agent Manager",
        description="Create agents for the people asking, and let owners change or delete the agents generated for them",
        docs_url="https://docs.mindroom.chat/tools/agent-orchestration/#agent_manager",
        category=ToolCategory.DEVELOPMENT,
        requires_primary_runtime=True,
        icon="Settings",
        icon_color="text-teal-500",
        config_fields=[],
        dependencies=[],
        function_names=("create_agent", "agent_settings", "update_agent", "delete_agent"),
    ),
)
