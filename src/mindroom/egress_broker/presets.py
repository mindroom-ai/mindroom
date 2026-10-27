"""Built-in egress service presets.

Each entry is a partial `EgressService` payload keyed by preset name.
`EgressService` merges the selected entry into the authored service before field validation,
so the validated config always carries the expanded rules and downstream code needs no preset awareness.
This module is pure data and must not import from `mindroom.config`.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["EGRESS_PRESETS"]

_BROKERED = "mindroom-brokered"
_GOOGLE_API_HOST = "www.googleapis.com"


def _bearer(host: str, path_prefix: str = "/") -> dict[str, object]:
    return {"host": host, "path_prefix": path_prefix, "auth": {"type": "bearer"}}


# These APIs read with GET and HEAD, so a connected account allowed read-only is safe there. GitHub is left out: git
# clone and fetch read with POST to `.../git-upload-pack`, which a path prefix cannot name.
_READ_ONLY: dict[str, object] = {"post_paths": []}


EGRESS_PRESETS: Mapping[str, dict[str, object]] = MappingProxyType(
    {
        "github": {
            "display_name": "GitHub",
            "description": "GitHub API, gh CLI, and git over HTTPS",
            "oauth_provider": "github",
            "rules": [
                _bearer("api.github.com"),
                _bearer("uploads.github.com"),
                {"host": "github.com", "auth": {"type": "basic", "username": "x-access-token"}},
            ],
            "placeholder_env": {"GH_TOKEN": _BROKERED, "GITHUB_TOKEN": _BROKERED},
        },
        "google_drive": {
            "display_name": "Google Drive",
            "description": "Google Drive API",
            "oauth_provider": "google_drive",
            "rules": [
                _bearer(_GOOGLE_API_HOST, "/drive/"),
                _bearer(_GOOGLE_API_HOST, "/upload/drive/"),
            ],
            "read_only": _READ_ONLY,
        },
        "google_gmail": {
            "display_name": "Gmail",
            "description": "Gmail API",
            "oauth_provider": "google_gmail",
            "rules": [
                _bearer("gmail.googleapis.com"),
                _bearer(_GOOGLE_API_HOST, "/gmail/"),
            ],
            "read_only": _READ_ONLY,
        },
        "google_calendar": {
            "display_name": "Google Calendar",
            "description": "Google Calendar API",
            "oauth_provider": "google_calendar",
            "rules": [_bearer(_GOOGLE_API_HOST, "/calendar/")],
            # Free/busy queries read with POST.
            "read_only": {"post_paths": ["/calendar/v3/freeBusy"]},
        },
        "google_sheets": {
            "display_name": "Google Sheets",
            "description": "Google Sheets API",
            "oauth_provider": "google_sheets",
            "rules": [_bearer("sheets.googleapis.com")],
            "read_only": _READ_ONLY,
        },
        "google_docs": {
            "display_name": "Google Docs",
            "description": "Google Docs API",
            "oauth_provider": "google_docs",
            "rules": [_bearer("docs.googleapis.com")],
            "read_only": _READ_ONLY,
        },
        "google_tasks": {
            "display_name": "Google Tasks",
            "description": "Google Tasks API",
            "oauth_provider": "google_tasks",
            "rules": [
                _bearer("tasks.googleapis.com"),
                _bearer(_GOOGLE_API_HOST, "/tasks/"),
            ],
            "read_only": _READ_ONLY,
        },
        "atlassian": {
            "display_name": "Atlassian",
            "description": "Atlassian Cloud APIs (Jira and Confluence)",
            "oauth_provider": "atlassian",
            "rules": [_bearer("api.atlassian.com")],
            "read_only": _READ_ONLY,
        },
        "openai": {
            "display_name": "OpenAI",
            "description": "OpenAI API",
            "rules": [_bearer("api.openai.com")],
            "placeholder_env": {"OPENAI_API_KEY": _BROKERED},
        },
        "anthropic": {
            "display_name": "Anthropic",
            "description": "Anthropic API",
            "rules": [
                {
                    "host": "api.anthropic.com",
                    "auth": {"type": "header", "name": "x-api-key", "template": "{secret}"},
                },
            ],
            "placeholder_env": {"ANTHROPIC_API_KEY": _BROKERED},
        },
    },
)
