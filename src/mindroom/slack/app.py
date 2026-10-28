"""The Slack app's stored credentials and the bot identity Slack reports for them."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.slack.api import SlackApiError, slack_call

if TYPE_CHECKING:
    import httpx

    from mindroom.credentials.manager import CredentialsManager

SLACK_APP_SERVICE = "slack_app"


@dataclass(frozen=True)
class SlackAppCredentials:
    """The workspace-wide values of the MindRoom Slack app."""

    bot_token: str
    signing_secret: str
    app_token: str | None


@dataclass(frozen=True)
class SlackBotIdentity:
    """Who the bot token belongs to; `workspace_url` keeps Slack's trailing slash."""

    team_id: str
    bot_user_id: str
    workspace_url: str


# Keyed by a digest of the bot token, so the cache never holds the token itself.
_IDENTITIES: dict[str, SlackBotIdentity] = {}


def _stored_text(credentials: dict[str, object], key: str) -> str | None:
    value = credentials.get(key)
    return (value.strip() or None) if isinstance(value, str) else None


def load_slack_app(credentials_manager: CredentialsManager) -> SlackAppCredentials | None:
    """Return the Slack app from the shared credential store, or None until it has a bot token and signing secret."""
    credentials = credentials_manager.shared_manager().load_credentials(SLACK_APP_SERVICE) or {}
    bot_token = _stored_text(credentials, "bot_token")
    signing_secret = _stored_text(credentials, "signing_secret")
    if bot_token is None or signing_secret is None:
        return None
    return SlackAppCredentials(bot_token, signing_secret, _stored_text(credentials, "app_token"))


def _identity_key(credentials: SlackAppCredentials) -> str:
    return hashlib.sha256(credentials.bot_token.encode()).hexdigest()


def cached_slack_bot_identity(credentials: SlackAppCredentials) -> SlackBotIdentity | None:
    """Return the identity `slack_bot_identity` already looked up for this bot token."""
    return _IDENTITIES.get(_identity_key(credentials))


async def slack_bot_identity(client: httpx.AsyncClient, credentials: SlackAppCredentials) -> SlackBotIdentity:
    """Return the bot's identity, asking Slack once per bot token; a failed lookup is not cached."""
    key = _identity_key(credentials)
    if (identity := _IDENTITIES.get(key)) is None:
        body = await slack_call(client, "auth.test", credentials.bot_token)
        if not all(isinstance(body.get(key), str) and body[key] for key in ("team_id", "user_id", "url")):
            error = "invalid_response"
            raise SlackApiError(error)
        identity = SlackBotIdentity(team_id=body["team_id"], bot_user_id=body["user_id"], workspace_url=body["url"])
        _IDENTITIES[key] = identity
    return identity
