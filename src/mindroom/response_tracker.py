"""Track which messages have been responded to by agents."""

import json
from pathlib import Path

from loguru import logger


class ResponseTracker:
    """Track which event IDs have been responded to by an agent."""

    def __init__(self, agent_name: str, base_path: Path | str = "tmp"):
        """Initialize response tracker for an agent.

        Args:
            agent_name: Name of the agent
            base_path: Base directory for storing response tracking data (default: "tmp")
        """
        self.agent_name = agent_name
        self.store_path = Path(base_path) / "response_tracking" / agent_name
        self.store_path.mkdir(parents=True, exist_ok=True)
        self.responses_file = self.store_path / "responded_events.json"
        self.responded_events: set[str] = self._load_responded_events()

    def _load_responded_events(self) -> set[str]:
        """Load the set of event IDs that have been responded to."""
        if self.responses_file.exists():
            try:
                with open(self.responses_file) as f:
                    data = json.load(f)
                    return set(data.get("event_ids", []))
            except Exception as e:
                logger.error(f"Failed to load response tracking for {self.agent_name}: {e}")
        return set()

    def _save_responded_events(self) -> None:
        """Save the set of responded event IDs to disk."""
        try:
            with open(self.responses_file, "w") as f:
                json.dump({"event_ids": list(self.responded_events)}, f)
        except Exception as e:
            logger.error(f"Failed to save response tracking for {self.agent_name}: {e}")

    def has_responded(self, event_id: str) -> bool:
        """Check if we've already responded to this event.

        Args:
            event_id: The Matrix event ID

        Returns:
            True if we've already responded to this event
        """
        return event_id in self.responded_events

    def mark_responded(self, event_id: str) -> None:
        """Mark an event as responded to.

        Args:
            event_id: The Matrix event ID we responded to
        """
        self.responded_events.add(event_id)
        self._save_responded_events()
        logger.debug(f"Marked event {event_id} as responded for agent {self.agent_name}")

    def cleanup_old_events(self, max_events: int = 10000) -> None:
        """Remove old events if we're tracking too many.

        Args:
            max_events: Maximum number of events to track
        """
        if len(self.responded_events) > max_events:
            # Convert to list, sort by event ID (which includes timestamp)
            # and keep only the most recent ones
            sorted_events = sorted(self.responded_events)
            self.responded_events = set(sorted_events[-max_events:])
            self._save_responded_events()
            logger.info(f"Cleaned up old events for {self.agent_name}, keeping {max_events} most recent")
