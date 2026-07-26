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
    terminal_delivery_coordinator = MagicMock()

    async def redact(*, room_id: str, event_id: str) -> None:
        ordering.append(f"terminal:{room_id}:{event_id}")

    terminal_delivery_coordinator.redact = AsyncMock(side_effect=redact)
    conversation_cache = MagicMock()

    async def apply_redaction(room_id: str, _event: nio.RedactionEvent) -> None:
        ordering.append(f"cache:{room_id}")

    conversation_cache.apply_redaction = AsyncMock(side_effect=apply_redaction)
    cleanup = RedactedTurnCleanup(
        RedactedTurnCleanupDeps(
            conversation_cache=conversation_cache,
            terminal_delivery_coordinator=terminal_delivery_coordinator,
        ),
    )
    room = nio.MatrixRoom(room_id=ROOM_ID, own_user_id="@agent:example.org")
    event = _redaction_event()

    await cleanup.handle(room, event)

    assert ordering == [f"terminal:{ROOM_ID}:{EVENT_ID}", f"cache:{ROOM_ID}"]
    terminal_delivery_coordinator.redact.assert_awaited_once_with(room_id=ROOM_ID, event_id=EVENT_ID)
    conversation_cache.apply_redaction.assert_awaited_once_with(ROOM_ID, event)


@pytest.mark.asyncio
async def test_failed_tombstone_still_applies_redaction_to_advisory_cache() -> None:
    """The in-process cache should reflect Matrix truth even when persistence fails closed."""
    terminal_delivery_coordinator = MagicMock()
    terminal_delivery_coordinator.redact = AsyncMock(side_effect=RuntimeError("persist failed"))
    conversation_cache = MagicMock()
    conversation_cache.apply_redaction = AsyncMock()
    cleanup = RedactedTurnCleanup(
        RedactedTurnCleanupDeps(
            conversation_cache=conversation_cache,
            terminal_delivery_coordinator=terminal_delivery_coordinator,
        ),
    )
    room = nio.MatrixRoom(room_id=ROOM_ID, own_user_id="@agent:example.org")
    event = _redaction_event()

    with pytest.raises(RuntimeError, match="persist failed"):
        await cleanup.handle(room, event)

    conversation_cache.apply_redaction.assert_awaited_once_with(ROOM_ID, event)
