"""Tests for the newer-unresponded dispatch replay guard."""

from __future__ import annotations

from typing import Any

from mindroom.dispatch_handoff import PreparedTextEvent
from mindroom.dispatch_replay_guard import has_newer_unresponded_in_thread
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
