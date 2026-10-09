"""Built-in Google Tasks OAuth provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

import mindroom.oauth.google as google_oauth

if TYPE_CHECKING:
    from mindroom.oauth.providers import OAuthProvider

_GOOGLE_TASKS_OAUTH_SCOPES = (
    *google_oauth.GOOGLE_IDENTITY_SCOPES,
    "https://www.googleapis.com/auth/tasks",
)


def google_tasks_oauth_provider() -> OAuthProvider:
    """Return the built-in Google Tasks provider definition."""
    return google_oauth._google_oauth_provider(
        provider_id="google_tasks",
        display_name="Google Tasks",
        scopes=_GOOGLE_TASKS_OAUTH_SCOPES,
        credential_service="google_tasks_oauth",
        tool_config_service="google_tasks",
        client_config_services=("google_tasks_oauth_client",),
        status_capabilities=("Tasks read/write",),
        include_granted_scopes=False,
    )
