"""Direct tests for durable source-redaction callback ordering."""

from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.redacted_turn_cleanup import RedactedTurnCleanup, RedactedTurnCleanupDeps

ROOM_ID = "!room:example.org"
EVENT_ID = "$source:example.org"


def _redaction_event() -> nio.RedactionEvent:
    event = MagicMock(spec=nio.RedactionEvent)
    event.redacts = EVENT_ID
    return event


@pytest.mark.asyncio
async def test_redaction_tombstones_before_updating_advisory_cache() -> None:
    """Sync certification must not outrun the durable source tombstone."""
    ordering: list[str] = []
    turn_store = MagicMock()
    turn_store.mark_source_redacted.side_effect = lambda event_id: ordering.append(f"tombstone:{event_id}")
    conversation_cache = MagicMock()

    async def apply_redaction(room_id: str, _event: nio.RedactionEvent) -> None:
        ordering.append(f"cache:{room_id}")

    conversation_cache.apply_redaction = AsyncMock(side_effect=apply_redaction)
    cleanup = RedactedTurnCleanup(
        RedactedTurnCleanupDeps(
            conversation_cache=conversation_cache,
            turn_store=turn_store,
        ),
    )
    room = nio.MatrixRoom(room_id=ROOM_ID, own_user_id="@agent:example.org")
    event = _redaction_event()

    await cleanup.handle(room, event)

    assert ordering == [f"tombstone:{EVENT_ID}", f"cache:{ROOM_ID}"]
    turn_store.mark_source_redacted.assert_called_once_with(EVENT_ID)
    conversation_cache.apply_redaction.assert_awaited_once_with(ROOM_ID, event)


@pytest.mark.asyncio
async def test_failed_tombstone_does_not_apply_redaction_to_cache() -> None:
    """A failed durable barrier must leave the source available for sync replay."""
    turn_store = MagicMock()
    turn_store.mark_source_redacted.side_effect = RuntimeError("persist failed")
    conversation_cache = MagicMock()
    conversation_cache.apply_redaction = AsyncMock()
    cleanup = RedactedTurnCleanup(
        RedactedTurnCleanupDeps(
            conversation_cache=conversation_cache,
            turn_store=turn_store,
        ),
    )
    room = nio.MatrixRoom(room_id=ROOM_ID, own_user_id="@agent:example.org")

    with pytest.raises(RuntimeError, match="persist failed"):
        await cleanup.handle(room, _redaction_event())

    conversation_cache.apply_redaction.assert_not_awaited()
