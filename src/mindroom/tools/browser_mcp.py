"""Configuration for the optional native worker browser provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import (
    ConfigField,
    ToolCategory,
    ToolExecutionTarget,
    ToolFileAccess,
    ToolManagedInitArg,
)
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.worker_computer.mcp_catalog import browser_mcp_catalog

if TYPE_CHECKING:
    from mindroom.custom_tools.browser_mcp import BrowserMCPTools


@register_tool_with_metadata(
    name="browser_mcp",
    file_access=ToolFileAccess.NONE,
    display_name="Browser MCP",
    description="Native Playwright browser tools in an isolated worker Computer",
    category=ToolCategory.RESEARCH,
    default_execution_target=ToolExecutionTarget.WORKER,
    consumes_workspace_paths=True,
    icon="FaChrome",
    dependencies=[],
    managed_init_args=(ToolManagedInitArg.RUNTIME_PATHS,),
    config_fields=[
        ConfigField(
            name="allow_private_networks",
            label="Allow Private Networks",
            type="boolean",
            required=False,
            default=False,
            description="Allow trusted private networks. Metadata and link-local addresses remain blocked.",
        ),
    ],
    function_names=tuple(browser_mcp_catalog()),
)
def browser_mcp_tools() -> type[BrowserMCPTools]:
    """Return native browser functions without importing MCP at registry startup."""
    from mindroom.custom_tools.browser_mcp import BrowserMCPTools

    return BrowserMCPTools
