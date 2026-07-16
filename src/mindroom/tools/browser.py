"""OpenClaw-style browser tool configuration."""

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
        ConfigField(
            name="default_target",
            label="Default Browser Target",
            type="select",
            required=False,
            default="host",
            options=[
                {"label": "MindRoom host profile", "value": "host"},
                {"label": "Matrix desktop profile", "value": "desktop"},
            ],
            description=(
                "Use host for MindRoom's managed Playwright profile or desktop for the Playwright extension "
                "installed in the user's existing local browser profile."
            ),
        ),
        ConfigField(
            name="device_user_id",
            label="Desktop Matrix User ID",
            type="text",
            required=False,
            description="Required for target=desktop; dedicated Matrix account used by the local desktop bridge.",
        ),
        ConfigField(
            name="device_id",
            label="Desktop Matrix Device ID",
            type="text",
            required=False,
            description="Required for target=desktop; exact device ID printed by 'mindroom desktop login'.",
        ),
        ConfigField(
            name="device_ed25519",
            label="Desktop Device Fingerprint",
            type="text",
            required=False,
            description="Required for target=desktop; exact Ed25519 fingerprint of the local bridge device.",
        ),
        ConfigField(
            name="timeout_seconds",
            label="Desktop Browser Timeout Seconds",
            type="number",
            required=False,
            default=90,
            description="Matrix and local Playwright MCP timeout from 1 to 120 seconds.",
        ),
    ],
    function_names=("browser",),
)
def browser_tools() -> type[BrowserTools]:
    """Return Browser tools with OpenClaw-style action routing."""
    from mindroom.custom_tools.browser import BrowserTools

    return BrowserTools
