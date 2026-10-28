"""Slack event verification and classification: who an event is for, decided from the event alone."""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.config.slack import SlackAnsweringAgent
    from mindroom.slack.watches import SlackWatch

_SIGNATURE_WINDOW_SECONDS = 300
_TIMESTAMP = re.compile(r"[0-9]{1,15}")
_MESSAGE_EVENT_TYPES = frozenset({"message", "app_mention"})
_ALLOWED_SUBTYPES = frozenset({None, "thread_broadcast", "file_share"})
_DIRECT_CHANNEL_TYPES = frozenset({"im", "mpim"})
# `<@U0123ABCD>` and `<@U0123ABCD|name>`; Slack user IDs start with U, or W on Enterprise Grid.
_USER_MENTION = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")


def verify_slack_signature(signing_secret: str, timestamp: str, body: bytes, signature: str, *, now: int) -> bool:
    """Check Slack's `X-Slack-Signature` for the raw request body and reject a timestamp more than 300 s from `now`."""
    if not signing_secret or not _TIMESTAMP.fullmatch(timestamp):
        return False
    if abs(now - int(timestamp)) > _SIGNATURE_WINDOW_SECONDS:
        return False
    base = b"v0:" + timestamp.encode() + b":" + body
    expected = "v0=" + hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), signature.encode())


@dataclass(frozen=True)
class SlackMessageEvent:
    """One Slack message or app mention worth classifying.

    `team_id` is the workspace that received the event.
    `author_team_id` is the author's own workspace when the event names a different one, as in a shared channel.
    """

    event_id: str
    team_id: str
    channel_id: str
    channel_type: str
    user_id: str
    ts: str
    thread_ts: str | None
    text: str
    is_app_mention: bool
    author_team_id: str | None = None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _is_new_message_by_a_person(event: Mapping[str, Any], *, bot_user_id: str) -> bool:
    return (
        event.get("type") in _MESSAGE_EVENT_TYPES
        and event.get("subtype") in _ALLOWED_SUBTYPES
        and "bot_id" not in event
        and "app_id" not in event
        and not event.get("hidden")
        and event.get("user") != bot_user_id
    )


def parse_message_event(payload: Mapping[str, Any], *, bot_user_id: str) -> SlackMessageEvent | None:
    """Return the message in an Events API envelope, or None when it must never wake anyone.

    That is anything but a new message or app mention by a person other than the app,
    and a `message` event that mentions the app, because its paired `app_mention` event carries that message.
    """
    event = payload.get("event")
    if payload.get("type") != "event_callback" or not isinstance(event, dict):
        return None
    if not _is_new_message_by_a_person(event, bot_user_id=bot_user_id):
        return None
    is_app_mention = event["type"] == "app_mention"
    text = event.get("text")
    if not isinstance(text, str) or (not is_app_mention and bot_user_id in mentioned_user_ids(text)):
        return None
    event_id = _text(payload.get("event_id"))
    team_id = _text(payload.get("team_id"))
    channel_id = _text(event.get("channel"))
    # An app mention event carries no channel type.
    channel_type = "channel" if is_app_mention else _text(event.get("channel_type"))
    user_id = _text(event.get("user"))
    ts = _text(event.get("ts"))
    thread_ts = event.get("thread_ts")
    if not (event_id and team_id and channel_id and channel_type and user_id and ts):
        return None
    if thread_ts is not None and not _text(thread_ts):
        return None
    author_team = _text(event.get("team"))
    return SlackMessageEvent(
        event_id=event_id,
        team_id=team_id,
        channel_id=channel_id,
        channel_type=channel_type,
        user_id=user_id,
        ts=ts,
        thread_ts=thread_ts,
        text=text,
        is_app_mention=is_app_mention,
        author_team_id=author_team if author_team != team_id else None,
    )


def mentioned_user_ids(text: str) -> frozenset[str]:
    """Return the Slack user IDs mentioned in message text."""
    return frozenset(_USER_MENTION.findall(text))


def _is_direct(event: SlackMessageEvent) -> bool:
    return event.channel_type in _DIRECT_CHANNEL_TYPES


def project_thread_key(event: SlackMessageEvent) -> str:
    """Return the key of the Slack thread an event belongs to, for a project agent."""
    return f"{event.team_id}:{event.channel_id}:{event.thread_ts or event.ts}"


def watch_thread_key(watch_id: str, event: SlackMessageEvent) -> str:
    """Return the key of the Slack thread an event belongs to for one watch; a direct message conversation is one thread."""
    if _is_direct(event):
        return f"{watch_id}:{event.channel_id}"
    return f"{watch_id}:{event.channel_id}:{event.thread_ts or event.ts}"


def project_wake_agent(
    event: SlackMessageEvent,
    answering: tuple[SlackAnsweringAgent, ...],
    *,
    bound: bool,
) -> SlackAnsweringAgent | None:
    """Return the agent that answers an event in its channel: an app mention, or a reply in a bound thread."""
    if not (event.is_app_mention or bound):
        return None
    return next((agent for agent in answering if event.channel_id in agent.channels), None)


def _watch_wants(watch: SlackWatch, event: SlackMessageEvent, *, bound: bool) -> bool:
    """Apply everything about a watch and an event that needs no lookup.

    A direct message counts when the watch takes direct messages, and a mention of the owner when it takes mentions.
    A reply in a thread where the owner was mentioned always counts, but never outside the watch's channels.
    The watch's channels limit channel messages only.
    """
    if watch.team_id != event.team_id or watch.slack_user_id == event.user_id:
        return False
    mentioned = watch.mentions and watch.slack_user_id in mentioned_user_ids(event.text)
    if _is_direct(event):
        return watch.direct_messages or mentioned
    if watch.channels and event.channel_id not in watch.channels:
        return False
    return bound or mentioned


def watch_candidates(
    event: SlackMessageEvent,
    watches: tuple[SlackWatch, ...],
    *,
    bound_watch_ids: frozenset[str],
) -> tuple[SlackWatch, ...]:
    """Return the watches an event may be for, without asking Slack who can see it.

    Only a candidate is worth an authorizations lookup, and `watch_accepts` makes the final decision.
    """
    return tuple(watch for watch in watches if _watch_wants(watch, event, bound=watch.watch_id in bound_watch_ids))


def watch_accepts(watch: SlackWatch, event: SlackMessageEvent, *, bound: bool, visible_to: frozenset[str]) -> bool:
    """Return whether an event is delivered to a watch, given the Slack users who can see it."""
    return watch.slack_user_id in visible_to and _watch_wants(watch, event, bound=bound)
