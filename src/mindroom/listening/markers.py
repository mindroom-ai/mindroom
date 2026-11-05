"""Where each listening agent has heard each room up to, kept across restarts."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

from mindroom.durable_write import write_json_file_durable
from mindroom.event_journal import ConversationCursor
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Collection
    from pathlib import Path

logger = get_logger(__name__)

# Each agent's markers by room ID.
type _Markers = dict[str, dict[str, ConversationCursor]]


@dataclass
class ListeningMarkers:
    """Each agent's position in each room it listens in: it has heard every message up to it, and none after.

    One JSON file holds them, read once and written durably on every change, so a restart resumes where the last turn
    stopped.
    """

    path: Path
    _markers: _Markers | None = field(default=None, init=False)

    def get(self, agent: str, room_id: str) -> ConversationCursor | None:
        """Return where ``agent`` has heard ``room_id`` up to, or None when it has not listened there yet."""
        return self._load().get(agent, {}).get(room_id)

    def set(self, agent: str, room_id: str, cursor: ConversationCursor) -> None:
        """Record that ``agent`` has heard ``room_id`` up to ``cursor``."""
        self._load().setdefault(agent, {})[room_id] = cursor
        self._write()

    def retain(self, listened: Collection[tuple[str, str]]) -> None:
        """Forget every marker but those of the ``(agent, room_id)`` pairs still listened to.

        Listening turned off and on again then starts at the room's newest message, so it never hears what was said
        while it was off.
        """
        markers = self._load()
        kept = {
            agent: {room_id: cursor for room_id, cursor in rooms.items() if (agent, room_id) in listened}
            for agent, rooms in markers.items()
        }
        kept = {agent: rooms for agent, rooms in kept.items() if rooms}
        if kept != markers:
            self._markers = kept
            self._write()

    def _load(self) -> _Markers:
        if self._markers is None:
            self._markers = self._read()
        return self._markers

    def _read(self) -> _Markers:
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except ValueError:
            # Starting over never hears a room's earlier messages; it skips what came since the lost markers.
            logger.warning("Listening markers were unreadable; each room is heard from its newest message on")
            return {}
        return {
            agent: {room_id: ConversationCursor(**cursor) for room_id, cursor in rooms.items()}
            for agent, rooms in stored.items()
        }

    def _write(self) -> None:
        markers = self._load()
        write_json_file_durable(
            self.path,
            {agent: {room_id: asdict(cursor) for room_id, cursor in rooms.items()} for agent, rooms in markers.items()},
        )
