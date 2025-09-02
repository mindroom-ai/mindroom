"""Track which messages have been responded to by agents."""

from __future__ import annotations

import fcntl
import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypedDict

from .constants import TRACKING_DIR
from .logging_config import get_logger

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)


class ResponseRecord(TypedDict):
    """Record of a response to a user message."""

    timestamp: float
    response_id: str | None


@dataclass
class ResponseTracker:
    """Track which event IDs have been responded to by an agent."""

    agent_name: str
    base_path: Path = TRACKING_DIR
    _responses: dict[str, ResponseRecord] = field(default_factory=dict, init=False)
    _responses_file: Path = field(init=False)

    def __post_init__(self) -> None:
        """Initialize paths and load existing responses."""
        self.base_path.mkdir(parents=True, exist_ok=True)
        self._responses_file = self.base_path / f"{self.agent_name}_responded.json"
        self._load_responses()
        # Perform automatic cleanup on initialization
        self.cleanup_old_events()

    def _load_responses(self) -> None:
        """Load the responses from disk."""
        if not self._responses_file.exists():
            self._responses = {}
            return

        with self._responses_file.open() as f:
            data = json.load(f)
            self._responses = data

    def _save_responses(self) -> None:
        """Save the responses to disk using file locking."""
        with self._responses_file.open("w") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                json.dump(self._responses, f, indent=2)
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def has_responded(self, event_id: str) -> bool:
        """Check if we've already responded to this event.

        Args:
            event_id: The Matrix event ID

        Returns:
            True if we've already responded to this event

        """
        return event_id in self._responses

    def mark_responded(self, event_id: str, response_event_id: str | None = None) -> None:
        """Mark an event as responded to with current timestamp.

        Args:
            event_id: The Matrix event ID we responded to
            response_event_id: The event ID of our response message (optional)

        """
        self._responses[event_id] = {
            "timestamp": time.time(),
            "response_id": response_event_id,
        }
        self._save_responses()
        logger.debug(f"Marked event {event_id} as responded for agent {self.agent_name}")

    def get_response_event_id(self, user_event_id: str) -> str | None:
        """Get the response event ID for a given user message event ID.

        Args:
            user_event_id: The user's message event ID

        Returns:
            The agent's response event ID if it exists, None otherwise

        """
        record = self._responses.get(user_event_id)
        return record["response_id"] if record else None

    def remove_response_mapping(self, user_event_id: str) -> None:
        """Remove the response mapping for a user event.

        Args:
            user_event_id: The user's message event ID to remove

        """
        if user_event_id in self._responses:
            # Keep the timestamp but clear the response_id
            self._responses[user_event_id]["response_id"] = None
            self._save_responses()
            logger.debug(f"Removed response mapping for event {user_event_id}")

    def cleanup_old_events(self, max_events: int = 10000, max_age_days: int = 30) -> None:
        """Remove old events based on count and age.

        Args:
            max_events: Maximum number of events to track
            max_age_days: Maximum age of events in days

        """
        current_time = time.time()
        max_age_seconds = max_age_days * 24 * 60 * 60

        # First remove events older than max_age_days
        self._responses = {
            event_id: record
            for event_id, record in self._responses.items()
            if current_time - record["timestamp"] < max_age_seconds
        }

        # Then trim to max_events if still over limit
        if len(self._responses) > max_events:
            # Sort by timestamp and keep only the most recent ones
            sorted_events = sorted(self._responses.items(), key=lambda x: x[1]["timestamp"])
            self._responses = dict(sorted_events[-max_events:])

        self._save_responses()
        logger.info(f"Cleaned up old events for {self.agent_name}, keeping {len(self._responses)} events")

    def get_stats(self) -> dict[str, Any]:
        """Get statistics about tracked responses.

        Returns:
            Dictionary with stats like total count, oldest event age, etc.

        """
        if not self._responses:
            return {"total": 0, "oldest_age_hours": 0, "newest_age_hours": 0}

        current_time = time.time()
        timestamps = [record["timestamp"] for record in self._responses.values()]
        oldest = min(timestamps)
        newest = max(timestamps)

        return {
            "total": len(self._responses),
            "oldest_age_hours": (current_time - oldest) / 3600,
            "newest_age_hours": (current_time - newest) / 3600,
        }
