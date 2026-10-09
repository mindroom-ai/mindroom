"""Tests for moving a thread into another room."""

from __future__ import annotations

from typing import Any

import pytest

from mindroom.constants import (
    ORIGINAL_SENDER_KEY,
    ROUTER_AGENT_NAME,
    SKIP_MENTIONS_KEY,
    STREAM_STATUS_APPROVAL_PENDING,
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_KEY,
    STREAM_STATUS_PENDING,
    STREAM_STATUS_STREAMING,
    TOOL_TRACE_CONTENT_KEY,
)
from mindroom.custom_tools.thread_move import PlannedCopy, plan_thread_copy
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage

HUMAN_ID = "@dominic:example.org"
CODE_ID = "@mindroom_code:example.org"
ABSENT_ID = "@mindroom_research:example.org"
ENTITY_IDS = {CODE_ID: "code", ABSENT_ID: "research", "@mindroom_router:example.org": ROUTER_AGENT_NAME}
TEXT_KEYS = {"msgtype", "body", "format", "formatted_body", TOOL_TRACE_CONTENT_KEY}


def _message(sender: str, content: dict[str, Any], event_id: str = "$event:example.org") -> ResolvedVisibleMessage:
    return ResolvedVisibleMessage.synthetic(
        sender=sender,
        body=str(content.get("body", "")),
        event_id=event_id,
        content=content,
    )


def _plan(
    *messages: ResolvedVisibleMessage,
    display_names: dict[str, str] | None = None,
) -> list[PlannedCopy]:
    return plan_thread_copy(
        messages,
        entity_name_for_sender=ENTITY_IDS.get,
        target_posters=frozenset({"code", ROUTER_AGENT_NAME}),
        display_names=display_names if display_names is not None else {HUMAN_ID: "Dominic"},
    )


def test_plan_posts_entity_messages_as_their_own_account() -> None:
    """An entity joined to the target room re-posts its own message unchanged."""
    [copy] = _plan(_message(CODE_ID, {"msgtype": "m.text", "body": "done"}, "$reply:example.org"))

    assert copy.poster == "code"
    assert copy.source_event_id == "$reply:example.org"
    assert copy.content["body"] == "done"
    assert ORIGINAL_SENDER_KEY not in copy.content


def test_plan_relays_human_and_absent_entity_messages_through_router() -> None:
    """Humans and entities missing from the target room are relayed by the router."""
    human, absent = _plan(
        _message(HUMAN_ID, {"msgtype": "m.text", "body": "hi"}),
        _message(ABSENT_ID, {"msgtype": "m.text", "body": "found it"}),
    )

    assert human.poster == ROUTER_AGENT_NAME
    assert human.content[ORIGINAL_SENDER_KEY] == HUMAN_ID
    assert absent.poster == ROUTER_AGENT_NAME
    assert absent.content[ORIGINAL_SENDER_KEY] == ABSENT_ID


@pytest.mark.parametrize(
    "content",
    [
        {"msgtype": "m.notice", "body": "Thread summary"},
        {"msgtype": "m.text", "body": "...", STREAM_STATUS_KEY: STREAM_STATUS_PENDING},
        {"msgtype": "m.text", "body": "partial", STREAM_STATUS_KEY: STREAM_STATUS_STREAMING},
        {"msgtype": "m.text", "body": "waiting", STREAM_STATUS_KEY: STREAM_STATUS_APPROVAL_PENDING},
    ],
)
def test_plan_skips_notices_and_in_progress_replies(content: dict[str, Any]) -> None:
    """Runtime notices and replies still being written are not copied."""
    assert _plan(_message(CODE_ID, content)) == []


def test_plan_keeps_completed_replies() -> None:
    """A finished streamed reply is copied."""
    [copy] = _plan(
        _message(CODE_ID, {"msgtype": "m.text", "body": "all done", STREAM_STATUS_KEY: STREAM_STATUS_COMPLETED})
    )

    assert copy.content["body"] == "all done"


def test_plan_copies_only_allowlisted_keys() -> None:
    """Run, stream, relation, and attachment metadata never carry over, and every copy is mention-guarded."""
    content = {
        "msgtype": "m.text",
        "body": "@code please check",
        "format": "org.matrix.custom.html",
        "formatted_body": "<p>please check</p>",
        TOOL_TRACE_CONTENT_KEY: {"version": 2, "events": []},
        "m.relates_to": {"rel_type": "m.thread", "event_id": "$root:example.org"},
        "m.mentions": {"user_ids": [CODE_ID]},
        "io.mindroom.ai_run": {"run_id": "run"},
        STREAM_STATUS_KEY: STREAM_STATUS_COMPLETED,
        "com.mindroom.attachment_ids": ["att_1"],
        "io.mindroom.interactive": {"options": []},
    }
    own, relayed = _plan(_message(CODE_ID, content), _message(HUMAN_ID, content))

    for copy in (own, relayed):
        assert copy.content[SKIP_MENTIONS_KEY] is True
        assert copy.content["m.mentions"] == {}
    assert set(own.content) == {*TEXT_KEYS, SKIP_MENTIONS_KEY, "m.mentions"}
    assert set(relayed.content) == {*TEXT_KEYS, SKIP_MENTIONS_KEY, "m.mentions", ORIGINAL_SENDER_KEY}


def test_plan_prefixes_relayed_text() -> None:
    """Relayed text shows its author because clients do not render the relay sender."""
    [copy] = _plan(
        _message(
            HUMAN_ID,
            {"msgtype": "m.text", "body": "hi", "format": "org.matrix.custom.html", "formatted_body": "<p>hi</p>"},
        ),
    )

    assert copy.content["body"] == "Dominic: hi"
    assert copy.content["formatted_body"] == "<strong>Dominic</strong>: <p>hi</p>"


def test_plan_escapes_display_name_in_formatted_prefix() -> None:
    """A display name cannot inject markup into the relayed copy."""
    [copy] = _plan(
        _message(
            HUMAN_ID, {"msgtype": "m.text", "body": "hi", "format": "org.matrix.custom.html", "formatted_body": "hi"}
        ),
        display_names={HUMAN_ID: "<b>Dom</b>"},
    )

    assert copy.content["formatted_body"] == "<strong>&lt;b&gt;Dom&lt;/b&gt;</strong>: hi"


def test_plan_prefixes_relayed_media_caption() -> None:
    """Relayed media keeps its file and shows the author in the caption."""
    plain, captioned = _plan(
        _message(HUMAN_ID, {"msgtype": "m.image", "body": "photo.png", "url": "mxc://example.org/abc"}),
        _message(
            HUMAN_ID,
            {"msgtype": "m.image", "body": "look", "filename": "photo.png", "url": "mxc://example.org/abc"},
        ),
    )

    assert plain.content["filename"] == "photo.png"
    assert plain.content["body"] == "Dominic: photo.png"
    assert plain.content["url"] == "mxc://example.org/abc"
    assert captioned.content["filename"] == "photo.png"
    assert captioned.content["body"] == "Dominic: look"


def test_plan_name_falls_back_to_user_id() -> None:
    """A sender without a display name is named by Matrix user ID."""
    [copy] = _plan(_message(HUMAN_ID, {"msgtype": "m.text", "body": "hi"}), display_names={})

    assert copy.content["body"] == f"{HUMAN_ID}: hi"
