"""Tests for the newer-unresponded dispatch replay guard."""

from __future__ import annotations

from typing import Any

import pytest

from mindroom.dispatch_handoff import PreparedTextEvent
from mindroom.dispatch_replay_guard import (
    has_newer_unresponded_cached_thread_event,
    has_newer_unresponded_in_thread,
)
from mindroom.logging_config import get_logger
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage

_REQUESTER = "@user:example"


def _history_message(event_id: str, timestamp: int) -> ResolvedVisibleMessage:
    return ResolvedVisibleMessage(
        sender=_REQUESTER,
        body=f"message {event_id}",
        timestamp=timestamp,
        event_id=event_id,
        content={"body": f"message {event_id}"},
        thread_id="$root",
        latest_event_id=event_id,
    )


def _guard(
    event: PreparedTextEvent,
    thread_history: list[ResolvedVisibleMessage],
    *,
    current_turn_event_ids: tuple[str, ...] = (),
) -> bool:
    return has_newer_unresponded_in_thread(
        event,
        _REQUESTER,
        thread_history,
        may_be_superseded_by_newer_requester_turn=True,
        current_turn_event_ids=current_turn_event_ids,
        requester_user_id_for_event=lambda sender, _source: sender,
        is_visible_router_voice_echo=lambda _sender, _content: False,
        sender_is_trusted_for_ingress_metadata=lambda _sender: True,
        is_handled=lambda _event_id: False,
        logger=get_logger(__name__),
    )


def _event(event_id: str, timestamp: int) -> PreparedTextEvent:
    source: dict[str, Any] = {"event_id": event_id, "origin_server_ts": timestamp}
    return PreparedTextEvent(
        sender=_REQUESTER,
        event_id=event_id,
        body=f"message {event_id}",
        source=source,
        server_timestamp=timestamp,
    )


def test_guard_skips_for_newer_unhandled_message_outside_current_turn() -> None:
    """A genuinely newer unresponded requester message still supersedes."""
    event = _event("$current", 1_000)
    history = [_history_message("$newer", 2_000)]

    assert _guard(event, history) is True


def test_guard_never_treats_own_coalesced_sources_as_superseding() -> None:
    """A sibling source of the same turn must not cancel its own dispatch.

    Editing an unresponded coalesced sibling bumps its visible history
    timestamp above the anchor's, which previously skipped the whole
    combined turn and durably starved every batched source.
    """
    event = _event("$current", 1_000)
    history = [_history_message("$edited-sibling", 2_000)]

    assert (
        _guard(
            event,
            history,
            current_turn_event_ids=("$edited-sibling", "$other-sibling", "$current"),
        )
        is False
    )


def _cached_event_source(event_id: str, timestamp: int) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "sender": _REQUESTER,
        "origin_server_ts": timestamp,
        "type": "m.room.message",
        "content": {
            "msgtype": "m.text",
            "body": f"message {event_id}",
            "m.relates_to": {"rel_type": "m.thread", "event_id": "$root"},
        },
    }


async def _cached_guard(
    event: PreparedTextEvent,
    recent_events: list[dict[str, Any]],
    *,
    current_turn_event_ids: tuple[str, ...] = (),
) -> bool:
    async def get_recent_room_events(_room_id: str, *, event_type: str, since_ts_ms: int) -> list[dict[str, Any]]:
        assert event_type == "m.room.message"
        assert since_ts_ms == event.server_timestamp
        return recent_events

    return await has_newer_unresponded_cached_thread_event(
        room_id="!room:example",
        event=event,
        requester_user_id=_REQUESTER,
        thread_id="$root",
        may_be_superseded_by_newer_requester_turn=True,
        current_turn_event_ids=current_turn_event_ids,
        get_recent_room_events=get_recent_room_events,
        get_thread_id_for_event=None,
        requester_user_id_for_event=lambda sender, _source: sender,
        is_visible_router_voice_echo=lambda _sender, _content: False,
        sender_is_trusted_for_ingress_metadata=lambda _sender: True,
        is_handled=lambda _event_id: False,
        logger=get_logger(__name__),
    )


@pytest.mark.asyncio
async def test_cached_guard_skips_for_newer_cached_event_outside_current_turn() -> None:
    """A genuinely newer unresponded cached requester event still supersedes."""
    event = _event("$current", 1_000)
    recent_events = [_cached_event_source("$newer", 2_000)]

    assert await _cached_guard(event, recent_events) is True


@pytest.mark.asyncio
async def test_cached_guard_never_treats_own_coalesced_sources_as_superseding() -> None:
    """The degraded cached-path guard must exclude the current turn's own sources.

    This mirrors the full-history exclusion above: after a cold restart or
    degraded sync, replay flows through the cached-event guard, and a
    coalesced sibling of the current turn must not supersede its own turn.
    """
    event = _event("$current", 1_000)
    recent_events = [_cached_event_source("$edited-sibling", 2_000)]

    assert (
        await _cached_guard(
            event,
            recent_events,
            current_turn_event_ids=("$edited-sibling", "$other-sibling", "$current"),
        )
        is False
    )
