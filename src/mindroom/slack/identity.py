"""Map a Slack author to a MindRoom (Matrix) account by the email on their Slack profile."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.matrix.identity import matrix_user_id_from_email
from mindroom.slack.api import SlackApiError, slack_call
from mindroom.slack.render import json_object, slack_display_name

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    import httpx

    from mindroom.config.slack import SlackConfig

_CACHE_TTL_SECONDS = 600
_CACHE_MAX_ENTRIES = 4096
# Oldest first: an entry is re-inserted when it is looked up again after it expired.
_AUTHORS: dict[tuple[str, str], tuple[float, SlackAuthor]] = {}
# Accounts that are not full members of our workspace never map to a MindRoom user.
_NOT_A_MEMBER_FLAGS = ("deleted", "is_bot", "is_app_user", "is_restricted", "is_ultra_restricted")


@dataclass(frozen=True)
class SlackAuthor:
    """A Slack person and the MindRoom account they map to, if any."""

    slack_user_id: str
    display_name: str
    matrix_id: str | None


def _matrix_id(user: Mapping[str, Any], team_id: str, slack_config: SlackConfig) -> str | None:
    """Return the Matrix ID of a full member of our workspace whose email is in the configured domain."""
    template = slack_config.matrix_user_id_template
    email = json_object(user, "profile").get("email")
    if (
        template is None
        or user.get("team_id") != team_id
        or any(user.get(flag) for flag in _NOT_A_MEMBER_FLAGS)
        or not isinstance(email, str)
    ):
        return None
    try:
        return matrix_user_id_from_email(email, template, slack_config.email_domain)
    except ValueError:
        return None


async def slack_author(
    client: httpx.AsyncClient,
    bot_token: str,
    team_id: str,
    slack_user_id: str,
    slack_config: SlackConfig,
    *,
    now: Callable[[], float] = time.monotonic,
) -> SlackAuthor:
    """Look up a Slack person with `users.info`, cached for ten minutes per team and user.

    `matrix_id` is None for anyone who must not drive an agent; the display name is always set.
    A Slack error raises `SlackApiError` and is not cached.
    """
    key = (team_id, slack_user_id)
    looked_up_at = now()
    cached = _AUTHORS.get(key)
    if cached is not None and looked_up_at - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]
    body = await slack_call(client, "users.info", bot_token, user=slack_user_id)
    user = json_object(body, "user")
    if not user:
        error = "invalid_response"
        raise SlackApiError(error)
    author = SlackAuthor(slack_user_id, slack_display_name(user), _matrix_id(user, team_id, slack_config))
    _AUTHORS.pop(key, None)
    while len(_AUTHORS) >= _CACHE_MAX_ENTRIES:
        del _AUTHORS[next(iter(_AUTHORS))]
    _AUTHORS[key] = (looked_up_at, author)
    return author
