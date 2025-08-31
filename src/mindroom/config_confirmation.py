"""Configuration change confirmation system using Matrix reactions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class PendingConfigChange:
    """Represents a pending configuration change awaiting confirmation."""

    room_id: str
    thread_id: str | None
    config_path: str
    old_value: Any
    new_value: Any
    config_dict: dict[str, Any]  # The complete modified config dict
    requester: str  # User who requested the change


# Track pending configuration changes by event_id
_pending_changes: dict[str, PendingConfigChange] = {}


def register_pending_change(
    event_id: str,
    room_id: str,
    thread_id: str | None,
    config_path: str,
    old_value: Any,
    new_value: Any,
    config_dict: dict[str, Any],
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
        config_dict: The complete modified configuration dictionary
        requester: User ID who requested the change

    """
    _pending_changes[event_id] = PendingConfigChange(
        room_id=room_id,
        thread_id=thread_id,
        config_path=config_path,
        old_value=old_value,
        new_value=new_value,
        config_dict=config_dict,
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


def cleanup() -> None:
    """Clean up when shutting down."""
    _pending_changes.clear()
