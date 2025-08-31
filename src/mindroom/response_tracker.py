"""Track which messages have been responded to by agents."""

from __future__ import annotations

import fcntl
import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .constants import TRACKING_DIR
from .logging_config import get_logger

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)


@dataclass
class EventRecord:
    """Record of a responded event with timestamp."""

    event_id: str
    timestamp: float


@dataclass
class ResponseTracker:
    """Track which event IDs have been responded to by an agent."""

    agent_name: str
    base_path: Path = TRACKING_DIR
    _responded_events: dict[str, float] = field(default_factory=dict, init=False)
    _user_to_response_map: dict[str, str] = field(default_factory=dict, init=False)
    _responses_file: Path = field(init=False)

    def __post_init__(self) -> None:
        """Initialize paths and load existing responses."""
        self.base_path.mkdir(parents=True, exist_ok=True)
        self._responses_file = self.base_path / f"{self.agent_name}_responded.json"
        self._responded_events = self._load_responded_events()
        # Perform automatic cleanup on initialization
        self.cleanup_old_events()

    def _load_responded_events(self) -> dict[str, float]:
        """Load the event IDs and timestamps that have been responded to."""
        if not self._responses_file.exists():
            return {}

        with self._responses_file.open() as f:
            data = json.load(f)
            # Load user-to-response mapping if it exists
            self._user_to_response_map = data.get("user_to_response_map", {})
            return data.get("events", {})  # type: ignore[no-any-return]

    def _save_responded_events(self) -> None:
        """Save the responded event IDs with timestamps to disk using file locking."""
        # Use file locking to prevent concurrent access issues
        with self._responses_file.open("w") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                json.dump(
                    {
                        "events": self._responded_events,
                        "user_to_response_map": self._user_to_response_map,
                    },
                    f,
                    indent=2,
                )
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def has_responded(self, event_id: str) -> bool:
        """Check if we've already responded to this event.

        Args:
            event_id: The Matrix event ID

        Returns:
            True if we've already responded to this event

        """
        return event_id in self._responded_events

    def mark_responded(self, event_id: str, response_event_id: str | None = None) -> None:
        """Mark an event as responded to with current timestamp.

        Args:
            event_id: The Matrix event ID we responded to
            response_event_id: The event ID of our response message (optional)

        """
        self._responded_events[event_id] = time.time()
        if response_event_id:
            self._user_to_response_map[event_id] = response_event_id
        self._save_responded_events()
        logger.debug(f"Marked event {event_id} as responded for agent {self.agent_name}")

    def get_response_event_id(self, user_event_id: str) -> str | None:
        """Get the response event ID for a given user message event ID.

        Args:
            user_event_id: The user's message event ID

        Returns:
            The agent's response event ID if it exists, None otherwise

        """
        return self._user_to_response_map.get(user_event_id)

    def remove_response_mapping(self, user_event_id: str) -> None:
        """Remove the response mapping for a user event.

        Args:
            user_event_id: The user's message event ID to remove

        """
        if user_event_id in self._user_to_response_map:
            del self._user_to_response_map[user_event_id]
            self._save_responded_events()
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
        events_to_keep = {
            event_id: timestamp
            for event_id, timestamp in self._responded_events.items()
            if current_time - timestamp < max_age_seconds
        }

        # Clean up user_to_response_map for removed events
        self._user_to_response_map = {
            user_id: response_id
            for user_id, response_id in self._user_to_response_map.items()
            if user_id in events_to_keep
        }

        self._responded_events = events_to_keep

        # Then trim to max_events if still over limit
        if len(self._responded_events) > max_events:
            # Sort by timestamp and keep only the most recent ones
            sorted_events = sorted(self._responded_events.items(), key=lambda x: x[1])
            self._responded_events = dict(sorted_events[-max_events:])

            # Clean up user_to_response_map for trimmed events
            self._user_to_response_map = {
                user_id: response_id
                for user_id, response_id in self._user_to_response_map.items()
                if user_id in self._responded_events
            }

        self._save_responded_events()
        logger.info(f"Cleaned up old events for {self.agent_name}, keeping {len(self._responded_events)} events")

    def get_stats(self) -> dict[str, Any]:
        """Get statistics about tracked responses.

        Returns:
            Dictionary with stats like total count, oldest event age, etc.

        """
        if not self._responded_events:
            return {"total": 0, "oldest_age_hours": 0, "newest_age_hours": 0}

        current_time = time.time()
        timestamps = list(self._responded_events.values())
        oldest = min(timestamps)
        newest = max(timestamps)

        return {
            "total": len(self._responded_events),
            "oldest_age_hours": (current_time - oldest) / 3600,
            "newest_age_hours": (current_time - newest) / 3600,
        }
