"""Direct tests for durable source-redaction cleanup policy."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.handled_turns import TurnRecord
from mindroom.history.types import HistoryScope
from mindroom.message_target import MessageTarget
from mindroom.redacted_turn_cleanup import RedactedTurnCleanup, RedactedTurnCleanupDeps

if TYPE_CHECKING:
    from collections.abc import Callable

ROOM_ID = "!room:example.org"
EVENT_ID = "$source:example.org"
REQUESTER_ID = "@requester:example.org"


def _redaction_event() -> nio.RedactionEvent:
    event = MagicMock(spec=nio.RedactionEvent)
    event.redacts = EVENT_ID
    event.sender = "@moderator:example.org"
    return event


def _cleanup() -> tuple[RedactedTurnCleanup, RedactedTurnCleanupDeps]:
    deps = RedactedTurnCleanupDeps(
        conversation_cache=MagicMock(),
        resolver=MagicMock(),
        ingress=MagicMock(),
        response_runner=MagicMock(),
        turn_store=MagicMock(),
    )

    async def run_mutation(*, target: MessageTarget, mutation: Callable[[], bool]) -> bool:
        del target
        return mutation()

    deps.conversation_cache.apply_redaction = AsyncMock(return_value=True)
    deps.conversation_cache.get_event = AsyncMock()
    deps.response_runner.run_serialized_state_mutation = AsyncMock(side_effect=run_mutation)
    return RedactedTurnCleanup(deps), deps


@pytest.mark.asyncio
async def test_handled_redaction_tombstones_before_cache_and_cleans_recorded_scope() -> None:
    """Known turn context should avoid source lookup and serialize exact cleanup."""
    cleanup, deps = _cleanup()
    room = nio.MatrixRoom(room_id=ROOM_ID, own_user_id="@agent:example.org")
    target = MessageTarget.resolve(ROOM_ID, "$thread:example.org", EVENT_ID)
    turn_record = TurnRecord.create(
        [EVENT_ID],
        redacted_source_event_ids=[EVENT_ID],
        requester_id=REQUESTER_ID,
        response_owner="agent",
        history_scope=HistoryScope(kind="agent", scope_id="agent"),
        conversation_target=target,
    )
    ordering: list[str] = []
    deps.turn_store.mark_source_redacted.side_effect = lambda *_args, **_kwargs: (
        ordering.append("tombstone") or turn_record
    )

    async def apply_redaction(*_args: object) -> bool:
        ordering.append("cache")
        return True

    deps.conversation_cache.apply_redaction.side_effect = apply_redaction
    deps.turn_store.forget_redacted_turn.return_value = True

    await cleanup.handle(room, _redaction_event())

    assert ordering == ["tombstone", "cache"]
    deps.turn_store.mark_source_redacted.assert_called_once_with(EVENT_ID, room_id=ROOM_ID)
    deps.conversation_cache.get_event.assert_not_awaited()
    deps.turn_store.forget_redacted_turn.assert_called_once_with(
        room=room,
        redacted_event_id=EVENT_ID,
        requester_user_id=REQUESTER_ID,
        target_hint=target,
        cache_sanitized=True,
    )


@pytest.mark.asyncio
async def test_missing_ledger_context_uses_source_requester_not_moderator() -> None:
    """Source lookup should enrich the tombstone with the original private-state identity."""
    cleanup, deps = _cleanup()
    room = nio.MatrixRoom(room_id=ROOM_ID, own_user_id="@agent:example.org")
    target = MessageTarget.resolve(ROOM_ID, "$thread:example.org", EVENT_ID)
    tombstone = TurnRecord.create([EVENT_ID], redacted_source_event_ids=[EVENT_ID], completed=False)
    enriched = TurnRecord.create(
        [EVENT_ID],
        redacted_source_event_ids=[EVENT_ID],
        completed=False,
        requester_id=REQUESTER_ID,
        conversation_target=target,
    )
    ordering: list[str] = []

    def mark_source_redacted(*_args: object, **kwargs: object) -> TurnRecord:
        if kwargs.get("requester_user_id") is not None:
            ordering.append("enriched-tombstone")
            return enriched
        ordering.append("tombstone")
        return tombstone

    async def apply_redaction(*_args: object) -> bool:
        ordering.append("cache")
        return True

    deps.turn_store.mark_source_redacted.side_effect = mark_source_redacted
    deps.conversation_cache.apply_redaction.side_effect = apply_redaction
    source_event = MagicMock(spec=nio.RoomMessageText)
    source_event.sender = REQUESTER_ID
    source_event.source = {
        "event_id": EVENT_ID,
        "sender": REQUESTER_ID,
        "content": {
            "body": "secret",
            "msgtype": "m.text",
            "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread:example.org"},
        },
    }
    response = MagicMock(spec=nio.RoomGetEventResponse)
    response.event = source_event
    deps.conversation_cache.get_event.return_value = response
    deps.resolver.build_message_target.return_value = target
    deps.ingress.requester_user_id.return_value = REQUESTER_ID

    await cleanup.handle(room, _redaction_event())

    assert ordering == ["tombstone", "enriched-tombstone", "cache"]
    deps.turn_store.mark_source_redacted.assert_any_call(
        EVENT_ID,
        room_id=ROOM_ID,
        requester_user_id=REQUESTER_ID,
        target_hint=target,
    )
    deps.turn_store.forget_redacted_turn.assert_called_once_with(
        room=room,
        redacted_event_id=EVENT_ID,
        requester_user_id=REQUESTER_ID,
        target_hint=target,
        cache_sanitized=True,
    )


@pytest.mark.asyncio
async def test_missing_context_resolves_transitive_plain_reply_thread() -> None:
    """Recovery must classify plain replies through the canonical thread resolver."""
    cleanup, deps = _cleanup()
    room = nio.MatrixRoom(room_id=ROOM_ID, own_user_id="@agent:example.org")
    target = MessageTarget.resolve(ROOM_ID, "$thread:example.org", EVENT_ID)
    tombstone = TurnRecord.create([EVENT_ID], redacted_source_event_ids=[EVENT_ID], completed=False)
    enriched = TurnRecord.create(
        [EVENT_ID],
        redacted_source_event_ids=[EVENT_ID],
        completed=False,
        requester_id=REQUESTER_ID,
        conversation_target=target,
    )
    deps.turn_store.mark_source_redacted.side_effect = [tombstone, enriched]
    source_event = MagicMock(spec=nio.RoomMessageText)
    source_event.sender = REQUESTER_ID
    source_event.source = {
        "event_id": EVENT_ID,
        "sender": REQUESTER_ID,
        "content": {
            "body": "secret",
            "msgtype": "m.text",
            "m.relates_to": {"m.in_reply_to": {"event_id": "$thread-child:example.org"}},
        },
    }
    response = MagicMock(spec=nio.RoomGetEventResponse)
    response.event = source_event
    deps.conversation_cache.get_event.return_value = response
    deps.resolver.resolve_related_event_thread_id_dispatch_snapshot_best_effort = AsyncMock(
        return_value="$thread:example.org",
    )
    deps.resolver.build_message_target.return_value = target
    deps.ingress.requester_user_id.return_value = REQUESTER_ID

    await cleanup.handle(room, _redaction_event())

    deps.resolver.resolve_related_event_thread_id_dispatch_snapshot_best_effort.assert_awaited_once_with(
        ROOM_ID,
        "$thread-child:example.org",
        caller_label="redacted_turn_cleanup",
    )
    deps.resolver.build_message_target.assert_called_once_with(
        room_id=ROOM_ID,
        thread_id="$thread:example.org",
        reply_to_event_id=EVENT_ID,
        event_source=source_event.source,
    )
    deps.turn_store.forget_redacted_turn.assert_called_once_with(
        room=room,
        redacted_event_id=EVENT_ID,
        requester_user_id=REQUESTER_ID,
        target_hint=target,
        cache_sanitized=True,
    )


@pytest.mark.asyncio
async def test_failed_cache_redaction_keeps_durable_cleanup_intent() -> None:
    """Session cleanup may run, but failed cache sanitization must remain retryable."""
    cleanup, deps = _cleanup()
    room = nio.MatrixRoom(room_id=ROOM_ID, own_user_id="@agent:example.org")
    target = MessageTarget.resolve(ROOM_ID, "$thread:example.org", EVENT_ID)
    turn_record = TurnRecord.create(
        [EVENT_ID],
        redacted_source_event_ids=[EVENT_ID],
        pending_redaction_cleanup_event_ids=[EVENT_ID],
        pending_redaction_room_id=ROOM_ID,
        requester_id=REQUESTER_ID,
        response_owner="agent",
        history_scope=HistoryScope(kind="agent", scope_id="agent"),
        conversation_target=target,
    )
    deps.turn_store.mark_source_redacted.return_value = turn_record
    deps.conversation_cache.apply_redaction.return_value = False

    await cleanup.handle(room, _redaction_event())

    deps.turn_store.forget_redacted_turn.assert_called_once_with(
        room=room,
        redacted_event_id=EVENT_ID,
        requester_user_id=REQUESTER_ID,
        target_hint=target,
        cache_sanitized=False,
    )


@pytest.mark.asyncio
async def test_unresolved_redaction_still_records_tombstone_before_cache_mutation() -> None:
    """A missing source lookup may block storage cleanup but must stop later dispatch."""
    cleanup, deps = _cleanup()
    room = nio.MatrixRoom(room_id=ROOM_ID, own_user_id="@agent:example.org")
    tombstone = TurnRecord.create([EVENT_ID], redacted_source_event_ids=[EVENT_ID], completed=False)
    deps.turn_store.mark_source_redacted.return_value = tombstone
    deps.conversation_cache.get_event.return_value = MagicMock(spec=nio.RoomGetEventError)

    await cleanup.handle(room, _redaction_event())

    assert deps.turn_store.mark_source_redacted.call_count == 2
    deps.conversation_cache.apply_redaction.assert_awaited_once()
    deps.response_runner.run_serialized_state_mutation.assert_not_awaited()


@pytest.mark.asyncio
async def test_startup_resumes_contextless_cleanup_before_forgetting_intent() -> None:
    """A crash after the locator write must recover context from the unsanitized durable cache."""
    cleanup, deps = _cleanup()
    target = MessageTarget.resolve(ROOM_ID, "$thread:example.org", EVENT_ID)
    tombstone = TurnRecord.create(
        [EVENT_ID],
        redacted_source_event_ids=[EVENT_ID],
        pending_redaction_cleanup_event_ids=[EVENT_ID],
        pending_redaction_room_id=ROOM_ID,
        completed=False,
    )
    enriched = TurnRecord.create(
        [EVENT_ID],
        redacted_source_event_ids=[EVENT_ID],
        pending_redaction_cleanup_event_ids=[EVENT_ID],
        pending_redaction_room_id=ROOM_ID,
        requester_id=REQUESTER_ID,
        conversation_target=target,
        completed=False,
    )
    deps.turn_store.pending_redaction_cleanups.return_value = ((EVENT_ID, ROOM_ID),)
    deps.turn_store.get_turn_record.return_value = tombstone
    deps.turn_store.mark_source_redacted.return_value = enriched
    source_event = MagicMock(spec=nio.RoomMessageText)
    source_event.sender = REQUESTER_ID
    source_event.source = {
        "event_id": EVENT_ID,
        "sender": REQUESTER_ID,
        "content": {
            "body": "secret",
            "msgtype": "m.text",
            "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread:example.org"},
        },
    }
    response = MagicMock(spec=nio.RoomGetEventResponse)
    response.event = source_event
    deps.conversation_cache.get_event.return_value = response
    deps.resolver.build_message_target.return_value = target
    deps.ingress.requester_user_id.return_value = REQUESTER_ID

    await cleanup.resume_pending()

    deps.turn_store.mark_source_redacted.assert_called_once_with(
        EVENT_ID,
        room_id=ROOM_ID,
        requester_user_id=REQUESTER_ID,
        target_hint=target,
    )
    deps.conversation_cache.apply_redaction.assert_awaited_once()
    applied_room_id, applied_event = deps.conversation_cache.apply_redaction.await_args.args
    assert applied_room_id == ROOM_ID
    assert isinstance(applied_event, nio.RedactionEvent)
    assert applied_event.redacts == EVENT_ID
    deps.turn_store.forget_redacted_turn.assert_called_once()
