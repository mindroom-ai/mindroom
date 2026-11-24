"""The `slack` OAuth provider: each requester's own Slack user token, issued by the MindRoom Slack app."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import httpx

from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.oauth.providers import (
    OAuthClaimValidationError,
    OAuthProvider,
    OAuthProviderError,
    OAuthRuntimeEndpoints,
    OAuthTokenResult,
    OAuthUserFacingError,
)
from mindroom.slack.api import SLACK_UNREACHABLE, SlackApiError, slack_http_client
from mindroom.slack.app import (
    SLACK_APP_NOT_CONFIGURED,
    SlackAppCredentials,
    cached_slack_bot_identity,
    load_slack_app,
    slack_bot_identity,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.constants import RuntimePaths
    from mindroom.oauth.providers import OAuthClientConfig

__all__ = ["SLACK_USER_SCOPES", "slack_oauth_provider"]

SLACK_USER_SCOPES = (
    "search:read",
    "channels:history",
    "channels:read",
    "groups:history",
    "groups:read",
    "im:history",
    "im:read",
    "mpim:history",
    "mpim:read",
    "users:read",
    "users:read.email",
    "chat:write",
    "im:write",
    "reactions:write",
    "files:read",
    "files:write",
)


def _load_slack_app(runtime_paths: RuntimePaths) -> SlackAppCredentials:
    """Read the app from the runtime's own credential store; a requester-scoped store would be the wrong one."""
    app = load_slack_app(get_runtime_credentials_manager(runtime_paths))
    if app is None:
        raise OAuthUserFacingError(SLACK_APP_NOT_CONFIGURED)
    return app


async def _slack_runtime_endpoints(provider: OAuthProvider, runtime_paths: RuntimePaths) -> OAuthRuntimeEndpoints:
    """Return Slack's fixed endpoints, once `auth.test` accepts the app's bot token whenever an OAuth client is saved.

    The lookup caches the bot's workspace, which the token parser compares every connecting account against.
    Running it here puts it before both the authorization URL and the code exchange.
    Without a client nothing can connect, so status reports the missing client instead of checking the app.
    """
    endpoints = OAuthRuntimeEndpoints(
        authorization_url=provider.authorization_url,
        token_url=provider.token_url,
        token_endpoint_auth_method=provider.token_endpoint_auth_method,
    )
    if await asyncio.to_thread(provider.client_config_resolution, runtime_paths) is None:
        return endpoints
    app = await asyncio.to_thread(_load_slack_app, runtime_paths)
    try:
        async with slack_http_client() as client:
            await slack_bot_identity(client, app)
    except SlackApiError as exc:
        raise OAuthUserFacingError(SLACK_UNREACHABLE if exc.transient else SLACK_APP_NOT_CONFIGURED) from exc
    except httpx.HTTPError as exc:
        raise OAuthUserFacingError(SLACK_UNREACHABLE) from exc
    return endpoints


def _object(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def _text(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def _parse_slack_token(
    provider: OAuthProvider,
    payload: Mapping[str, Any],
    client_config: OAuthClientConfig,
    runtime_paths: RuntimePaths,
) -> OAuthTokenResult:
    """Read the `oauth.v2.access` code-exchange answer; Slack's user tokens neither expire nor refresh here."""
    del provider, client_config
    authed_user = _object(payload, "authed_user")
    token = _text(authed_user, "access_token")
    user_id = _text(authed_user, "id")
    if token is None or user_id is None:
        msg = "Slack returned no user token"
        raise OAuthProviderError(msg)
    identity = cached_slack_bot_identity(_load_slack_app(runtime_paths))
    if identity is None:
        raise OAuthProviderError(SLACK_APP_NOT_CONFIGURED)
    team_id = _text(_object(payload, "team"), "id")
    if team_id != identity.team_id:
        # A claim validation error is the one the OAuth callback shows the person as it is.
        msg = "This Slack account belongs to another workspace"
        raise OAuthClaimValidationError(msg)
    scope = _text(authed_user, "scope") or ""
    return OAuthTokenResult(
        token_data={"token": token, "scopes": [part.strip() for part in scope.split(",") if part.strip()]},
        claims={"sub": user_id, "team_id": team_id},
        claims_verified=True,
    )


def slack_oauth_provider() -> OAuthProvider:
    """Return the built-in Slack user OAuth provider definition.

    Slack's `scope` parameter means bot scopes, so the app requests everything as `user_scope` and sends no `scope`.
    No `account_claims_reader` (it swallows errors) and no `claim_validator` (it runs on every credential read):
    the token parser alone rejects accounts of another workspace.
    """
    return OAuthProvider(
        id="slack",
        display_name="Slack",
        authorization_url="https://slack.com/oauth/v2/authorize",
        token_url="https://slack.com/api/oauth.v2.access",  # noqa: S106
        scopes=(),
        allow_empty_scopes=True,
        extra_auth_params={"user_scope": ",".join(SLACK_USER_SCOPES)},
        credential_service="slack_oauth",
        tool_config_service="slack",
        client_config_services=("slack_oauth_client",),
        default_redirect_path="/api/oauth/slack/callback",
        requester_scoped_credentials=True,
        token_parser=_parse_slack_token,
        runtime_bootstrapper=_slack_runtime_endpoints,
    )
