"""Google Sheets tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import (
    SetupType,
    ToolCategory,
    ToolStatus,
    register_tool_with_metadata,
)

if TYPE_CHECKING:
    from agno.tools.googlesheets import GoogleSheetsTools


@register_tool_with_metadata(
    name="google_sheets",
    display_name="Google Sheets",
    description="Read, create, update, and duplicate Google Sheets spreadsheets",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.SPECIAL,
    icon="FaGoogle",
    icon_color="text-green-600",
    config_fields=None,  # No config fields - uses Google Services OAuth
    dependencies=["google-api-python-client", "google-auth-httplib2", "google-auth-oauthlib"],
    docs_url="https://docs.agno.com/tools/toolkits/others/google_sheets",
)
def google_sheets_tools() -> type[GoogleSheetsTools]:
    """Return Google Sheets tools for spreadsheet integration."""
    from agno.tools.googlesheets import GoogleSheetsTools

    return GoogleSheetsTools
