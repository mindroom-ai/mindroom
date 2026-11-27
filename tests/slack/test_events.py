"""Tests for Slack event verification and classification."""

# ruff: noqa: D103

from __future__ import annotations

import hashlib
import hmac
from dataclasses import replace
from typing import Any

import pytest

from mindroom.config.slack import SlackAnsweringAgent
from mindroom.slack.events import (
    SlackMessageEvent,
    mentioned_user_ids,
    parse_message_event,
    project_thread_key,
    verify_slack_signature,
)

SECRET = "signing-secret"  # noqa: S105
BOT = "U0BOT0001"
OWNER = "U0OWNER01"
AUTHOR = "U0AUTHOR1"
TEAM = "T0123ABCD"
CHANNEL = "C0123ABCD"
OTHER_CHANNEL = "C0456EFGH"
DM = "D0123ABCD"
NOW = 1_700_000_000
# The channels an agent answers, where an app mention also arrives as an `app_mention` event.
ANSWERED = frozenset({CHANNEL})


def _sign(body: bytes, timestamp: int | str = NOW, secret: str = SECRET) -> str:
    base = b"v0:" + str(timestamp).encode() + b":" + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


def _envelope(event_type: str = "message", **inner: Any) -> dict[str, Any]:  # noqa: ANN401
    event: dict[str, Any] = {
        "type": event_type,
        "user": AUTHOR,
        "channel": CHANNEL,
        "ts": "1700000000.000100",
        "text": "hello",
    }
    if event_type == "message":
        event["channel_type"] = "channel"
    event.update(inner)
    event = {key: value for key, value in event.items() if value is not None}
    return {"type": "event_callback", "team_id": TEAM, "event_id": "Ev0123ABCD", "event": event}


def _event(**changes: Any) -> SlackMessageEvent:  # noqa: ANN401
    parsed = parse_message_event(_envelope(), bot_user_id=BOT, answered_channels=ANSWERED)
    assert parsed is not None
    return replace(parsed, **changes)


def _answering(agent: str, *channels: str) -> SlackAnsweringAgent:
    return SlackAnsweringAgent(agent, "!room:example.org", frozenset(channels))


@pytest.mark.parametrize(
    ("timestamp", "signature", "secret", "now", "valid"),
    [
        (str(NOW), _sign(b"{}"), SECRET, NOW, True),
        (str(NOW), _sign(b"{}"), SECRET, NOW + 300, True),
        (str(NOW), _sign(b"{}"), SECRET, NOW - 300, True),
        (str(NOW), _sign(b"{}"), SECRET, NOW + 301, False),
        (str(NOW), _sign(b"{}"), SECRET, NOW - 301, False),
        (str(NOW), _sign(b"{}", secret="other"), SECRET, NOW, False),  # noqa: S106
        (str(NOW), _sign(b"{ }"), SECRET, NOW, False),
        (str(NOW + 1), _sign(b"{}"), SECRET, NOW, False),
        (str(NOW), _sign(b"{}")[3:], SECRET, NOW, False),
        (str(NOW), "", SECRET, NOW, False),
        (str(NOW), "v0=é", SECRET, NOW, False),
        # Each odd timestamp is signed as sent, so only the digit-only rule can reject it.
        *(
            (odd, _sign(b"{}", odd), SECRET, NOW, False)
            for odd in (
                "",
                "abc",
                f"{NOW}.5",
                f" {NOW}",
                f"+{NOW}",
                f"{NOW}_0",
                "\uff11\uff17\uff10\uff10\uff10\uff10\uff10\uff10\uff10\uff10",
                "1" * 5000,
            )
        ),
        (str(NOW), _sign(b"{}", secret=""), "", NOW, False),
    ],
)
def test_signature_good_bad_stale(timestamp: str, signature: str, secret: str, now: int, valid: bool) -> None:
    assert verify_slack_signature(secret, timestamp, b"{}", signature, now=now) is valid


def test_signature_covers_the_raw_body_bytes() -> None:
    body = '{"text": "café"}'.encode()
    assert verify_slack_signature(SECRET, str(NOW), body, _sign(body), now=NOW)
    assert not verify_slack_signature(SECRET, str(NOW), body + b" ", _sign(body), now=NOW)


def test_parse_keeps_a_channel_message() -> None:
    parsed = parse_message_event(_envelope(thread_ts="1700000000.000050"), bot_user_id=BOT, answered_channels=ANSWERED)
    assert parsed == SlackMessageEvent(
        event_id="Ev0123ABCD",
        team_id=TEAM,
        channel_id=CHANNEL,
        channel_type="channel",
        user_id=AUTHOR,
        ts="1700000000.000100",
        thread_ts="1700000000.000050",
        text="hello",
        is_app_mention=False,
    )


def test_parse_app_mention_has_no_channel_type_and_sets_the_flag() -> None:
    parsed = parse_message_event(
        _envelope("app_mention", text=f"<@{BOT}> hi"),
        bot_user_id=BOT,
        answered_channels=ANSWERED,
    )
    assert parsed is not None
    assert (parsed.is_app_mention, parsed.channel_type, parsed.thread_ts) == (True, "channel", None)


@pytest.mark.parametrize("subtype", ["thread_broadcast", "file_share"])
def test_parse_keeps_allowed_subtypes(subtype: str) -> None:
    assert parse_message_event(_envelope(subtype=subtype), bot_user_id=BOT, answered_channels=ANSWERED) is not None


def test_shared_channel_event_keeps_the_receiving_team() -> None:
    parsed = parse_message_event(_envelope(team="T0OTHER99"), bot_user_id=BOT, answered_channels=ANSWERED)
    assert parsed is not None
    assert parsed.team_id == TEAM


@pytest.mark.parametrize(
    "payload",
    [
        {**_envelope(), "type": "url_verification"},
        {**_envelope(), "type": None},
        {key: value for key, value in _envelope().items() if key != "event"},
        {**_envelope(), "event": "message"},
        _envelope("reaction_added"),
        _envelope(type=["message"]),
        _envelope(subtype=["file_share"]),
        _envelope(subtype={"kind": "file_share"}),
        _envelope(bot_id="B0123ABCD"),
        _envelope(app_id="A0123ABCD"),
        _envelope(user=BOT),
        _envelope(subtype="message_changed"),
        _envelope(subtype="message_deleted"),
        _envelope(subtype="bot_message"),
        _envelope(subtype="channel_join"),
        _envelope(hidden=True),
        _envelope("app_mention", bot_id="B0123ABCD"),
        _envelope("app_mention", user=BOT),
        _envelope("app_mention", subtype="message_changed"),
    ],
    ids=lambda payload: str(payload.get("event", payload)) if isinstance(payload, dict) else str(payload),
)
def test_bot_and_app_messages_never_wake(payload: dict[str, Any]) -> None:
    assert parse_message_event(payload, bot_user_id=BOT, answered_channels=ANSWERED) is None


@pytest.mark.parametrize("text", [f"<@{BOT}> hi", f"hi <@{BOT}|mindroom>", f"a <@{OWNER}> <@{BOT}>"])
def test_message_event_mentioning_app_dropped_where_an_agent_answers(text: str) -> None:
    """The `app_mention` twin carries that message."""
    assert parse_message_event(_envelope(text=text), bot_user_id=BOT, answered_channels=ANSWERED) is None
    assert (
        parse_message_event(_envelope("app_mention", text=text), bot_user_id=BOT, answered_channels=ANSWERED)
        is not None
    )


def test_message_event_mentioning_app_kept_where_no_agent_answers() -> None:
    """Slack sends no `app_mention` from a channel the app is not in, so a watch must still get the message."""
    payload = _envelope(text=f"<@{BOT}> <@{OWNER}> hi", channel=OTHER_CHANNEL)

    parsed = parse_message_event(payload, bot_user_id=BOT, answered_channels=ANSWERED)

    assert parsed is not None
    assert parsed.is_app_mention is False


def test_message_event_mentioning_someone_else_is_kept() -> None:
    assert (
        parse_message_event(_envelope(text=f"<@{OWNER}> hi"), bot_user_id=BOT, answered_channels=ANSWERED) is not None
    )


@pytest.mark.parametrize("missing", ["event_id", "team_id"])
def test_parse_drops_an_envelope_without_ids(missing: str) -> None:
    payload = _envelope()
    del payload[missing]
    assert parse_message_event(payload, bot_user_id=BOT, answered_channels=ANSWERED) is None


@pytest.mark.parametrize("missing", ["user", "channel", "ts", "text", "channel_type"])
def test_parse_drops_an_event_without_fields(missing: str) -> None:
    assert parse_message_event(_envelope(**{missing: None}), bot_user_id=BOT, answered_channels=ANSWERED) is None


def test_parse_drops_an_empty_id() -> None:
    assert parse_message_event(_envelope(channel=""), bot_user_id=BOT, answered_channels=ANSWERED) is None


def test_parse_keeps_an_empty_text_of_a_file_share() -> None:
    parsed = parse_message_event(_envelope(subtype="file_share", text=""), bot_user_id=BOT, answered_channels=ANSWERED)
    assert parsed is not None
    assert parsed.text == ""


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("<@U0123ABCD>", {"U0123ABCD"}),
        ("<@U0123ABCD|alice>", {"U0123ABCD"}),
        ("<@W0123ABCD> and <@U0456EFGH|bob smith>", {"W0123ABCD", "U0456EFGH"}),
        ("<@U0123ABCD> <@U0123ABCD>", {"U0123ABCD"}),
        ("<!here> <#C0123ABCD|general> <@U0123ABCD", set()),
        ("@U0123ABCD U0123ABCD <mailto:a@example.com|a@example.com>", set()),
        ("", set()),
    ],
)
def test_mention_parsing_forms(text: str, expected: set[str]) -> None:
    assert mentioned_user_ids(text) == frozenset(expected)


def test_thread_keys() -> None:
    assert project_thread_key(TEAM, CHANNEL, "1700000000.000050") == f"{TEAM}:{CHANNEL}:1700000000.000050"
