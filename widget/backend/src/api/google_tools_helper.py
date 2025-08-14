"""Helper utilities for Google tools management."""

from typing import Any


def is_google_managed_tool(tool_name: str) -> bool:
    """Check if a tool is managed through Google Services OAuth.

    A tool is considered Google-managed if it uses Google OAuth for authentication,
    regardless of whether it has additional config fields for operational parameters.
    """
    # List of tools that use Google Services OAuth for authentication
    # These tools may have additional config_fields for operational parameters
    google_oauth_tools = {"google_calendar", "google_sheets", "gmail"}

    return tool_name in google_oauth_tools


def get_google_tool_scopes(tool_name: str) -> list[str]:
    """Get required OAuth scopes for a Google tool."""
    scope_map = {
        "google_calendar": [
            "https://www.googleapis.com/auth/calendar",
            "https://www.googleapis.com/auth/calendar.readonly",
        ],
        "google_sheets": [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/spreadsheets.readonly",
        ],
        "gmail": [
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.compose",
            "https://www.googleapis.com/auth/gmail.send",
        ],
    }

    return scope_map.get(tool_name, [])


def check_google_tool_configured(tool_name: str, google_creds: dict[str, Any]) -> bool:
    """Check if a Google tool has the required OAuth scopes configured."""
    if not google_creds or "token" not in google_creds:
        return False

    configured_scopes = google_creds.get("scopes", [])
    required_scopes = get_google_tool_scopes(tool_name)

    if not required_scopes:
        return False

    # Check if any of the required scopes are present
    return any(scope in configured_scopes for scope in required_scopes)
