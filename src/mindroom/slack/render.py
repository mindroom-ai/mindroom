"""Plain dictionaries for agents from Slack Web API answers: messages, people, and conversations.

Slack's answers are untrusted JSON, so every reader tolerates a missing or mistyped field.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mindroom.slack.api import parse_slack_target, slack_permalink

if TYPE_CHECKING:
    from collections.abc import Mapping


def json_objects(payload: Mapping[str, Any], field: str) -> list[dict[str, Any]]:
    """Return the objects in one list field of a Slack answer, skipping anything else."""
    value = payload.get(field)
    return [entry for entry in value if isinstance(entry, dict)] if isinstance(value, list) else []


def json_object(payload: Mapping[str, Any], field: str) -> dict[str, Any]:
    """Return one object field of a Slack answer, or an empty one."""
    value = payload.get(field)
    return value if isinstance(value, dict) else {}


def next_cursor(body: Mapping[str, Any]) -> str | None:
    """Return the cursor of a listing's next page, or None on its last page."""
    return json_object(body, "response_metadata").get("next_cursor") or None


def slack_display_name(user: Mapping[str, Any]) -> str:
    """Return the name Slack shows for a person: their display name, else their full name, else their handle or ID."""
    profile = json_object(user, "profile")
    names = (profile.get("display_name"), profile.get("real_name"), user.get("real_name"), user.get("name"))
    return next((name for name in (*names, user.get("id")) if isinstance(name, str) and name), "")


def render_person(user: Mapping[str, Any]) -> dict[str, object]:
    """Return one Slack person as the people search shows them."""
    return {
        "id": user.get("id"),
        "user_name": slack_display_name(user),
        "real_name": user.get("real_name"),
        "email": json_object(user, "profile").get("email"),
        "is_bot": user.get("is_bot", False),
        "deleted": user.get("deleted", False),
    }


def render_conversation(channel: Mapping[str, Any]) -> dict[str, object]:
    """Return one Slack conversation; a direct message names the other person as `user_id`."""
    return {
        "id": channel.get("id"),
        "name": channel.get("name"),
        "is_private": channel.get("is_private", False),
        "is_im": channel.get("is_im", False),
        "is_mpim": channel.get("is_mpim", False),
        "user_id": channel.get("user"),
    }


def render_message(
    channel_id: str,
    message: Mapping[str, Any],
    workspace_url: str,
    names: Mapping[str, str | None],
) -> dict[str, object]:
    """Return one Slack message with its author's name from `names`, its link, and its files' IDs, names, and sizes."""
    ts = message.get("ts")
    thread_ts = message.get("thread_ts")
    user_id = message.get("user") or None
    bot_name = json_object(message, "bot_profile").get("name")
    permalink = None
    if isinstance(ts, str):
        thread = thread_ts if isinstance(thread_ts, str) else None
        permalink = slack_permalink(workspace_url, channel_id, ts, thread_ts=thread)
    return {
        "channel_id": channel_id,
        "ts": ts,
        "thread_ts": thread_ts,
        "user_id": user_id,
        "user_name": names.get(user_id) if isinstance(user_id, str) else message.get("username") or bot_name,
        "text": message.get("text") or "",
        "permalink": permalink,
        "files": [
            {"id": file.get("id"), "name": file.get("name"), "size": file.get("size")}
            for file in json_objects(message, "files")
        ],
        "reply_count": message.get("reply_count", 0),
    }


def match_thread_ts(match: Mapping[str, Any]) -> str | None:
    """Return the thread a search match sits in; Slack names it only in the match's permalink."""
    permalink = match.get("permalink")
    try:
        return parse_slack_target(permalink).thread_ts if isinstance(permalink, str) else None
    except ValueError:
        return None
