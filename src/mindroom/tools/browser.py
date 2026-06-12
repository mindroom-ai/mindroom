"""OpenClaw-style browser tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.metadata import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolManagedInitArg,
    ToolStatus,
    register_tool_with_metadata,
)

if TYPE_CHECKING:
    from mindroom.custom_tools.browser import BrowserTools


@register_tool_with_metadata(
    name="browser",
    display_name="Browser",
    description=(
        "OpenClaw-style browser control (status/start/stop/profiles/tabs/open/focus/close/"
        "snapshot/screenshot/navigate/console/pdf/upload/dialog/act/help/actions)"
    ),
    category=ToolCategory.RESEARCH,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="FaChrome",
    icon_color="text-orange-500",
    dependencies=["playwright"],
    docs_url="https://github.com/openclaw/openclaw/blob/main/docs/tools/browser.md",
    managed_init_args=(ToolManagedInitArg.RUNTIME_PATHS,),
    config_fields=[
        ConfigField(
            name="output_dir",
            label="Output Directory",
            type="text",
            required=False,
            description=(
                "Optional directory for browser screenshots, PDFs, and downloads. "
                "Defaults to the active storage path's browser/ directory."
            ),
        ),
        ConfigField(
            name="allow_private_networks",
            label="Allow Private Networks",
            type="boolean",
            required=False,
            default=False,
            description=(
                "Allow browser navigation and page subresources to reach trusted private, local, or loopback "
                "network addresses. Cloud metadata and link-local addresses stay blocked."
            ),
        ),
    ],
    function_names=("browser",),
)
def browser_tools() -> type[BrowserTools]:
    """Return Browser tools with OpenClaw-style action routing."""
    from mindroom.custom_tools.browser import BrowserTools

    return BrowserTools
