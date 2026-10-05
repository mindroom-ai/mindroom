"""Call origin parsing and brief building."""

from __future__ import annotations

from mindroom.matrix_rtc.call_origin import (
    AGENT_CALL_STATE_EVENT_TYPE,
    CallBriefMessage,
    CallOrigin,
    CallOriginContext,
    build_call_brief,
    parse_call_origin,
)
from mindroom.token_budget import approximate_o200k_tokens

CALLER = "@alice:example.org"
AGENT = "@mindroom_helper:example.org"


def _agent_call_event(*, sender: str = CALLER, **content_overrides: object) -> dict:
    content: dict[str, object] = {
        "version": 1,
        "agent_user_id": AGENT,
        "creator_user_id": CALLER,
        "ephemeral": True,
        "origin": {"room_id": "!origin:example.org", "thread_id": "$root"},
    }
    content.update(content_overrides)
    return {"type": AGENT_CALL_STATE_EVENT_TYPE, "state_key": "", "sender": sender, "content": content}


def test_parse_call_origin_accepts_caller_stamped_origin() -> None:
    """The caller's own stamped thread origin is returned."""
    origin = parse_call_origin([_agent_call_event()], requester_id=CALLER, agent_user_id=AGENT)
    assert origin == CallOrigin(room_id="!origin:example.org", thread_id="$root")


def test_parse_call_origin_accepts_room_level_origin() -> None:
    """A room-level origin has no thread id."""
    event = _agent_call_event(origin={"room_id": "!origin:example.org", "thread_id": None})
    assert parse_call_origin([event], requester_id=CALLER, agent_user_id=AGENT) == CallOrigin(
        room_id="!origin:example.org",
        thread_id=None,
    )


def test_parse_call_origin_rejects_untrusted_or_malformed_state() -> None:
    """Foreign senders, mismatched ids, and malformed origins yield no origin."""
    cases = [
        [],
        [_agent_call_event(sender="@mallory:example.org")],
        [_agent_call_event(creator_user_id="@mallory:example.org")],
        [_agent_call_event(agent_user_id="@mindroom_other:example.org")],
        [_agent_call_event(version=2)],
        [_agent_call_event(origin=None)],
        [_agent_call_event(origin={"room_id": "", "thread_id": None})],
        [_agent_call_event(origin={"room_id": "!origin:example.org", "thread_id": 5})],
        [{**_agent_call_event(), "state_key": "other"}],
    ]
    for events in cases:
        assert parse_call_origin(events, requester_id=CALLER, agent_user_id=AGENT) is None, events


def _context(messages: list[tuple[str, str]], *, title: str | None = "Trip planning") -> CallOriginContext:
    return CallOriginContext(
        origin=CallOrigin(room_id="!origin:example.org", thread_id="$root"),
        room_name="Lobby",
        thread_title=title,
        messages=tuple(CallBriefMessage(label=label, body=body) for label, body in messages),
    )


def test_build_call_brief_includes_header_and_messages_in_order() -> None:
    """The brief names the room and thread and lists messages oldest first."""
    brief = build_call_brief(
        _context([("Alice", "Book the train"), ("You", "Booked for 9am")]),
        token_budget=6_000,
    )
    assert "Lobby" in brief
    assert "Trip planning" in brief
    assert brief.index("Alice: Book the train") < brief.index("You: Booked for 9am")
    assert "omitted" not in brief


def test_build_call_brief_keeps_newest_messages_within_budget() -> None:
    """Oldest messages are dropped, with a marker, once the budget is full."""
    messages = [("Alice", f"message number {index} " + "word " * 40) for index in range(200)]
    brief = build_call_brief(_context(messages), token_budget=1_000)
    assert approximate_o200k_tokens(brief) <= 1_000
    assert "message number 199" in brief
    assert "message number 0 " not in brief
    assert "earlier messages omitted]" in brief


def test_build_call_brief_caps_long_message_bodies() -> None:
    """One huge message cannot consume the whole brief."""
    brief = build_call_brief(_context([("Alice", "x" * 10_000)]), token_budget=6_000)
    assert "x" * 1_999 + "…" in brief
    assert "x" * 2_001 not in brief


def test_build_call_brief_returns_empty_string_when_header_does_not_fit() -> None:
    """A budget smaller than the header yields no brief."""
    assert build_call_brief(_context([("Alice", "hi")]), token_budget=10) == ""


def test_build_call_brief_stays_within_budget_for_many_tiny_messages() -> None:
    """Newline joiners count toward the budget, and the newest message survives."""
    messages = [("Alice", f"hi {index}") for index in range(2_000)]
    for budget in (300, 1_000, 6_000):
        brief = build_call_brief(_context(messages), token_budget=budget)
        assert approximate_o200k_tokens(brief) <= budget, budget
        assert brief.endswith("- Alice: hi 1999"), budget
        assert "earlier messages omitted]" in brief, budget


def test_build_call_brief_never_exceeds_budget_just_above_header_size() -> None:
    """The omission marker and oversized messages are dropped rather than overflowing."""
    header_only = build_call_brief(_context([]), token_budget=6_000)
    header_tokens = approximate_o200k_tokens(header_only)
    assert header_tokens > 0
    for slack in range(6):
        budget = header_tokens + slack
        brief = build_call_brief(_context([("Alice", "word " * 400)]), token_budget=budget)
        assert approximate_o200k_tokens(brief) <= budget, slack
        assert brief.startswith(header_only), slack
        assert "word word" not in brief, slack
