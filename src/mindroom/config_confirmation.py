"""Configuration change confirmation system using Matrix reactions with persistence."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import nio

from .logging_config import get_logger

if TYPE_CHECKING:
    from .bot import AgentBot

logger = get_logger(__name__)

# Event type for pending config changes in Matrix state
PENDING_CONFIG_EVENT_TYPE = "com.mindroom.pending.config"

# Maximum age for pending confirmations (24 hours)
MAX_PENDING_AGE_HOURS = 24


@dataclass
class PendingConfigChange:
    """Represents a pending configuration change awaiting confirmation."""

    room_id: str
    thread_id: str | None
    config_path: str
    old_value: Any
    new_value: Any
    requester: str  # User who requested the change
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def is_expired(self) -> bool:
        """Check if this pending change has expired."""
        age = datetime.now(UTC) - self.created_at
        return age.total_seconds() > MAX_PENDING_AGE_HOURS * 3600

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for Matrix state storage."""
        return {
            "room_id": self.room_id,
            "thread_id": self.thread_id,
            "config_path": self.config_path,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "requester": self.requester,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PendingConfigChange:
        """Create from dictionary retrieved from Matrix state."""
        # Parse the ISO format datetime
        created_at = datetime.fromisoformat(data["created_at"])

        return cls(
            room_id=data["room_id"],
            thread_id=data.get("thread_id"),
            config_path=data["config_path"],
            old_value=data["old_value"],
            new_value=data["new_value"],
            requester=data["requester"],
            created_at=created_at,
        )


# Track pending configuration changes by event_id
_pending_changes: dict[str, PendingConfigChange] = {}


def register_pending_change(
    event_id: str,
    room_id: str,
    thread_id: str | None,
    config_path: str,
    old_value: Any,  # noqa: ANN401
    new_value: Any,  # noqa: ANN401
    requester: str,
) -> None:
    """Register a pending configuration change for confirmation.

    Args:
        event_id: The event ID of the confirmation message
        room_id: The room ID
        thread_id: Thread ID if in a thread
        config_path: The configuration path being changed
        old_value: The current value
        new_value: The proposed new value
        requester: User ID who requested the change

    """
    _pending_changes[event_id] = PendingConfigChange(
        room_id=room_id,
        thread_id=thread_id,
        config_path=config_path,
        old_value=old_value,
        new_value=new_value,
        requester=requester,
    )
    logger.info(
        "Registered pending config change",
        event_id=event_id,
        path=config_path,
        requester=requester,
    )


def get_pending_change(event_id: str) -> PendingConfigChange | None:
    """Get a pending configuration change by event ID.

    Args:
        event_id: The event ID of the confirmation message

    Returns:
        The pending change or None if not found

    """
    return _pending_changes.get(event_id)


def remove_pending_change(event_id: str) -> PendingConfigChange | None:
    """Remove and return a pending configuration change.

    Args:
        event_id: The event ID of the confirmation message

    Returns:
        The removed pending change or None if not found

    """
    return _pending_changes.pop(event_id, None)


async def store_pending_change_in_matrix(
    client: nio.AsyncClient,
    event_id: str,
    pending_change: PendingConfigChange,
) -> None:
    """Store pending config change in Matrix room state for persistence.

    Args:
        client: The Matrix client
        event_id: The event ID of the confirmation message
        pending_change: The pending configuration change

    """
    try:
        response = await client.room_put_state(
            room_id=pending_change.room_id,
            event_type=PENDING_CONFIG_EVENT_TYPE,
            content=pending_change.to_dict(),
            state_key=event_id,
        )

        if isinstance(response, nio.RoomPutStateResponse):
            logger.info(
                "Stored pending config change in Matrix state",
                event_id=event_id,
                room_id=pending_change.room_id,
                config_path=pending_change.config_path,
            )
        else:
            logger.error(
                "Failed to store pending config change in Matrix state",
                event_id=event_id,
                error=str(response),
            )
    except Exception:
        logger.exception("Error storing pending config change in Matrix state")


async def remove_pending_change_from_matrix(
    client: nio.AsyncClient,
    room_id: str,
    event_id: str,
) -> None:
    """Remove pending config change from Matrix room state.

    Args:
        client: The Matrix client
        room_id: The room ID
        event_id: The event ID of the confirmation message

    """
    try:
        # To remove a state event, set it with empty content
        response = await client.room_put_state(
            room_id=room_id,
            event_type=PENDING_CONFIG_EVENT_TYPE,
            content={},
            state_key=event_id,
        )

        if isinstance(response, nio.RoomPutStateResponse):
            logger.info(
                "Removed pending config change from Matrix state",
                event_id=event_id,
                room_id=room_id,
            )
        else:
            logger.error(
                "Failed to remove pending config change from Matrix state",
                event_id=event_id,
                error=str(response),
            )
    except Exception:
        logger.exception("Error removing pending config change from Matrix state")


async def restore_pending_changes(client: nio.AsyncClient, room_id: str) -> int:
    """Restore pending config changes from Matrix state after bot restart.

    Args:
        client: The Matrix client
        room_id: The room ID to restore from

    Returns:
        Number of pending changes restored

    """
    try:
        response = await client.room_get_state(room_id)
        if not isinstance(response, nio.RoomGetStateResponse):
            logger.warning(
                "Failed to get room state for pending config changes",
                room_id=room_id,
                error=str(response),
            )
            return 0

        restored_count = 0
        expired_count = 0

        for event in response.events:
            if event.get("type") != PENDING_CONFIG_EVENT_TYPE:
                continue

            state_key = event.get("state_key")
            content = event.get("content", {})

            # Skip empty content (deleted state events)
            if not content:
                continue

            try:
                pending_change = PendingConfigChange.from_dict(content)

                # Check if expired
                if pending_change.is_expired():
                    logger.info(
                        "Skipping expired pending config change",
                        event_id=state_key,
                        created_at=pending_change.created_at,
                    )
                    # Remove from Matrix state
                    await remove_pending_change_from_matrix(client, room_id, state_key)
                    expired_count += 1
                else:
                    # Restore to memory
                    _pending_changes[state_key] = pending_change
                    restored_count += 1
                    logger.info(
                        "Restored pending config change",
                        event_id=state_key,
                        config_path=pending_change.config_path,
                        requester=pending_change.requester,
                    )
            except Exception:
                logger.exception(
                    "Error restoring pending config change",
                    event_id=state_key,
                )

        if restored_count > 0 or expired_count > 0:
            logger.info(
                "Completed restoration of pending config changes",
                room_id=room_id,
                restored=restored_count,
                expired=expired_count,
            )

        return restored_count  # noqa: TRY300

    except Exception:
        logger.exception("Error restoring pending config changes from Matrix state")
        return 0


def cleanup() -> None:
    """Clean up when shutting down."""
    _pending_changes.clear()


async def add_confirmation_reactions(client: nio.AsyncClient, room_id: str, event_id: str) -> None:
    """Add confirmation reaction buttons to a config change message.

    Args:
        client: The Matrix client
        room_id: The room ID
        event_id: The event ID of the message to add reactions to

    """
    # Add ✅ reaction
    confirm_response = await client.room_send(
        room_id=room_id,
        message_type="m.reaction",
        content={
            "m.relates_to": {
                "rel_type": "m.annotation",
                "event_id": event_id,
                "key": "✅",
            },
        },
    )
    if not isinstance(confirm_response, nio.RoomSendResponse):
        logger.warning("Failed to add confirm reaction", error=str(confirm_response))

    # Add ❌ reaction
    cancel_response = await client.room_send(
        room_id=room_id,
        message_type="m.reaction",
        content={
            "m.relates_to": {
                "rel_type": "m.annotation",
                "event_id": event_id,
                "key": "❌",
            },
        },
    )
    if not isinstance(cancel_response, nio.RoomSendResponse):
        logger.warning("Failed to add cancel reaction", error=str(cancel_response))


async def handle_confirmation_reaction(
    bot: AgentBot,
    room: nio.MatrixRoom,
    event: nio.ReactionEvent,
    pending_change: PendingConfigChange,
) -> None:
    """Handle reactions to config confirmation messages.

    Args:
        bot: The agent bot instance
        room: The room the reaction occurred in
        event: The reaction event
        pending_change: The pending configuration change

    """
    # Only process reactions from the requester
    if event.sender != pending_change.requester:
        logger.debug(
            "Ignoring config reaction from non-requester",
            sender=event.sender,
            requester=pending_change.requester,
        )
        return

    # Don't process our own reactions
    assert bot.client is not None
    if event.sender == bot.client.user_id:
        return

    reaction_key = event.key

    # Only handle ✅ and ❌ reactions
    if reaction_key not in ["✅", "❌"]:
        return

    # Remove the pending change from memory and Matrix state
    remove_pending_change(event.reacts_to)
    await remove_pending_change_from_matrix(
        bot.client,
        pending_change.room_id,
        event.reacts_to,
    )

    if reaction_key == "✅":
        # User confirmed - apply the change
        from .config_commands import apply_config_change  # noqa: PLC0415

        response_text = await apply_config_change(
            pending_change.config_path,
            pending_change.new_value,
        )

        logger.info(
            "Config change confirmed",
            path=pending_change.config_path,
            requester=event.sender,
        )
    else:
        # User cancelled
        response_text = "❌ Configuration change cancelled."
        logger.info(
            "Config change cancelled",
            path=pending_change.config_path,
            requester=event.sender,
        )

    # Send the response
    await bot._send_response(
        room.room_id,
        event.reacts_to,  # Reply to the confirmation message
        response_text,
        pending_change.thread_id,
        skip_mentions=True,
    )
