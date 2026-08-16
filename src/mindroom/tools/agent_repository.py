"""Constrained autonomous repository tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolManagedInitArg,
    ToolStatus,
)
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.agent_repository import AgentRepositoryTools


@register_tool_with_metadata(
    name="agent_repository",
    display_name="Agent Repository",
    description="Ensure the one private GitHub repository bound to this agent worker",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.SPECIAL,
    icon="GitBranch",
    icon_color="text-slate-700",
    dependencies=["agno", "httpx"],
    consumes_workspace_paths=True,
    supports_output_redirection=False,
    config_fields=[
        ConfigField(
            name="organization",
            label="Organization",
            required=True,
            authored_override=False,
            description="Trusted global policy; agents cannot override it.",
        ),
        ConfigField(
            name="prefix",
            label="Repository Prefix",
            required=True,
            authored_override=False,
            description="Trusted global policy; agents cannot override it.",
        ),
    ],
    function_names=("ensure_my_repository",),
    managed_init_args=(
        ToolManagedInitArg.RUNTIME_PATHS,
        ToolManagedInitArg.WORKER_TARGET,
        ToolManagedInitArg.TOOL_OUTPUT_WORKSPACE_ROOT,
    ),
    supports_toolkit_filters=False,
)
def agent_repository_tools() -> type[AgentRepositoryTools]:
    """Return the constrained agent repository toolkit."""
    from mindroom.custom_tools.agent_repository import AgentRepositoryTools

    return AgentRepositoryTools
