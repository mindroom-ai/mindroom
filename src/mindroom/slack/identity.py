"""Map a Slack author to a MindRoom (Matrix) account by the email on their Slack profile, and look up Slack channels."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.matrix.identity import matrix_user_id_from_email
from mindroom.requester_identity import is_human_requester_id
from mindroom.slack.api import SlackApiError, slack_call
from mindroom.slack.render import json_object, slack_display_name

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    import httpx

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

_CACHE_TTL_SECONDS = 600
_CACHE_MAX_ENTRIES = 4096
# Slack answers these for someone we may never map; they are final, not outages.
_UNKNOWN_USER_ERRORS = frozenset({"user_not_found", "user_not_visible"})
# Accounts that are not full members of our workspace never map to a MindRoom user.
_NOT_A_MEMBER_FLAGS = ("deleted", "is_bot", "is_app_user", "is_restricted", "is_ultra_restricted")
# Oldest first: an entry is re-inserted when it is looked up again after it expired.
_PROFILES: dict[tuple[str, str], tuple[float, _Profile]] = {}
_CHANNELS: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
NOT_PROJECT_CHANNEL = "Not one of this agent's Slack channels"


@dataclass(frozen=True)
class SlackAuthor:
    """A Slack person and the MindRoom account they map to, if any."""

    slack_user_id: str
    display_name: str
    matrix_id: str | None


@dataclass(frozen=True)
class _Profile:
    """What Slack says about a person, whatever MindRoom's configuration is.

    `email` is set only for a confirmed full member of our team, so a config change never needs a new lookup.
    """

    display_name: str
    email: str | None


def _profile(user: Mapping[str, Any], team_id: str) -> _Profile:
    email = json_object(user, "profile").get("email")
    is_member = (
        user.get("team_id") == team_id
        and not any(user.get(flag) for flag in _NOT_A_MEMBER_FLAGS)
        and user.get("is_email_confirmed", True)
    )
    return _Profile(slack_display_name(user), email if is_member and isinstance(email, str) else None)


async def _fetch_profile(client: httpx.AsyncClient, bot_token: str, team_id: str, slack_user_id: str) -> _Profile:
    try:
        body = await slack_call(client, "users.info", bot_token, user=slack_user_id)
    except SlackApiError as exc:
        if exc.error in _UNKNOWN_USER_ERRORS:
            return _Profile(slack_user_id, None)
        raise
    user = json_object(body, "user")
    if not user:
        error = "invalid_response"
        raise SlackApiError(error)
    return _profile(user, team_id)


async def slack_channel_info(client: httpx.AsyncClient, token: str, channel_id: str) -> dict[str, Any]:
    """Return what `conversations.info` shows a token of one conversation, or nothing for one it cannot see."""
    try:
        body = await slack_call(client, "conversations.info", token, channel=channel_id)
    except SlackApiError as exc:
        if exc.error == "channel_not_found":
            return {}
        raise
    return json_object(body, "channel")


def project_channel_refusal(channel_id: str, info: Mapping[str, Any]) -> str | None:
    """Return why a project agent may not use a listed conversation, or None for a channel the app is a member of.

    `info` is what `slack_channel_info` shows the bot token.
    Only `is_member` proves membership: the bot can read a public channel's info without being in it.
    """
    if info.get("is_im") or info.get("is_mpim"):
        return NOT_PROJECT_CHANNEL
    if info.get("is_member") is not True:
        return f"Invite the MindRoom app to <#{channel_id}>"
    return None


async def _cached[T](
    cache: dict[tuple[str, str], tuple[float, T]],
    key: tuple[str, str],
    now: Callable[[], float],
    fetch: Callable[[], Awaitable[T]],
    *,
    max_age: float = _CACHE_TTL_SECONDS,
) -> T:
    """Return a cached lookup that is under `max_age` seconds old, else fetch it; a failed fetch caches nothing."""
    looked_up_at = now()
    cached = cache.get(key)
    if cached is not None and looked_up_at - cached[0] < max_age:
        return cached[1]
    value = await fetch()
    cache.pop(key, None)
    while len(cache) >= _CACHE_MAX_ENTRIES:
        del cache[next(iter(cache))]
    cache[key] = (looked_up_at, value)
    return value


def _matrix_id(email: str | None, config: Config, runtime_paths: RuntimePaths) -> str | None:
    """Return the Matrix ID an email in the configured domain maps to, unless it names an account that is not a person."""
    template = config.slack.matrix_user_id_template
    if template is None or email is None:
        return None
    try:
        matrix_id = matrix_user_id_from_email(email, template, config.slack.email_domain)
    except ValueError:
        return None
    return matrix_id if is_human_requester_id(matrix_id, config, runtime_paths) else None


async def slack_author(
    client: httpx.AsyncClient,
    bot_token: str,
    team_id: str,
    slack_user_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    now: Callable[[], float] = time.monotonic,
) -> SlackAuthor:
    """Look up a Slack person with `users.info`, cached for ten minutes per team and user.

    `team_id` must be the bot token's own team (`SlackBotIdentity.team_id`), never the event author's team,
    because a person from another team is refused by comparing their team with it.
    `matrix_id` is None for anyone who must not drive an agent, and follows `config` as it is now.
    The display name is always set; someone Slack does not know or show is named by their ID.
    Any other Slack error raises `SlackApiError` and is not cached.
    """
    profile = await _cached(
        _PROFILES,
        (team_id, slack_user_id),
        now,
        lambda: _fetch_profile(client, bot_token, team_id, slack_user_id),
    )
    return SlackAuthor(slack_user_id, profile.display_name, _matrix_id(profile.email, config, runtime_paths))


async def slack_channel(
    client: httpx.AsyncClient,
    bot_token: str,
    team_id: str,
    channel_id: str,
    *,
    max_age: float = _CACHE_TTL_SECONDS,
    now: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Return what `slack_channel_info` shows the bot of a conversation, reusing a lookup under `max_age` seconds old.

    A name for display may be ten minutes old, the default; whether the app may act in a channel is decided only with
    `max_age=0`, a fresh lookup, since the app may have left it meanwhile.
    `team_id` must be the bot token's own team, as for `slack_author`.
    Any Slack error raises `SlackApiError` and is not cached.
    """
    return await _cached(
        _CHANNELS,
        (team_id, channel_id),
        now,
        lambda: slack_channel_info(client, bot_token, channel_id),
        max_age=max_age,
    )
