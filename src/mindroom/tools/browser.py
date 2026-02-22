"""OpenClaw-style browser tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.browser import BrowserTools


@register_tool_with_metadata(
    name="browser",
    display_name="Browser",
    description=(
        "OpenClaw-style browser control (status/start/stop/profiles/tabs/open/focus/close/"
        "snapshot/screenshot/navigate/console/pdf/upload/dialog/act)"
    ),
    category=ToolCategory.RESEARCH,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="FaChrome",
    icon_color="text-orange-500",
    dependencies=["playwright"],
    docs_url="https://github.com/openclaw/openclaw/blob/main/docs/tools/browser.md",
)
def browser_tools() -> type[BrowserTools]:
    """Return Browser tools with OpenClaw-style action routing."""
    from mindroom.custom_tools.browser import BrowserTools

    return BrowserTools
