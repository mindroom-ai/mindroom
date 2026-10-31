"""Egress credentials tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import SetupType, ToolCategory, ToolFileAccess, ToolManagedInitArg, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.egress_credentials import EgressCredentialsTools


@register_tool_with_metadata(
    name="egress_credentials",
    file_access=ToolFileAccess.NONE,
    requires_primary_runtime=True,
    display_name="Egress Credentials",
    description=(
        "Opt this agent's worker-routed shell and python calls into Agent Keys and see which keys and accounts it can use"
    ),
    docs_url="https://docs.mindroom.chat/deployment/egress-broker/#agent-tool",
    category=ToolCategory.INTEGRATIONS,
    icon="KeyRound",
    icon_color="text-amber-600",
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    managed_init_args=(
        ToolManagedInitArg.RUNTIME_PATHS,
        ToolManagedInitArg.WORKER_TARGET,
    ),
    config_fields=[],
    function_names=("list_egress_credentials",),
)
def egress_credentials_tools() -> type[EgressCredentialsTools]:
    """Return the egress broker credential status tool."""
    from mindroom.custom_tools.egress_credentials import EgressCredentialsTools

    return EgressCredentialsTools
