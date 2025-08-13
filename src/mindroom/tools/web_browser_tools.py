"""Web Browser Tools configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import (
    SetupType,
    ToolCategory,
    ToolStatus,
    register_tool_with_metadata,
)

if TYPE_CHECKING:
    from agno.tools.webbrowser import WebBrowserTools


@register_tool_with_metadata(
    name="web_browser_tools",
    display_name="Web Browser Tools",
    description="Open URLs in web browser tabs or windows",
    category=ToolCategory.DEVELOPMENT,  # From docs URL: /tools/toolkits/others/
    status=ToolStatus.AVAILABLE,  # No configuration required
    setup_type=SetupType.NONE,  # No authentication needed
    icon="FaGlobe",
    icon_color="text-blue-600",  # Web browser blue
    config_fields=[
        # No configuration parameters - WebBrowserTools.__init__() takes no arguments
    ],
    dependencies=[],  # Uses standard library webbrowser module
    docs_url="https://docs.agno.com/tools/toolkits/others/web-browser",
)
def web_browser_tools() -> type[WebBrowserTools]:
    """Return Web Browser Tools for opening URLs."""
    from agno.tools.webbrowser import WebBrowserTools

    return WebBrowserTools
