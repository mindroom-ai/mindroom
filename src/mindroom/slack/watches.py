"""Personal Slack watches: who wants which Slack events delivered to which room, and the check that a room may receive them."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from mindroom.durable_write import write_json_file_durable
from mindroom.file_locks import advisory_file_lock
from mindroom.matrix.client_room_admin import get_room_members
from mindroom.requester_identity import equivalent_requester_ids, is_human_requester_id

if TYPE_CHECKING:
    from pathlib import Path

    import nio

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

# What `status` reports while a watch is not delivering; each names what the owner can fix.
_PAUSED_NOT_IN_ROOM = "paused: you are not in this room"
_PAUSED_OTHER_PERSON = "paused: another person is in this room"
# Fail closed: a room whose members cannot be read may hold someone else.
_PAUSED_ROOM_UNCHECKED = "paused: room members could not be checked"
PAUSED_SLACK_NOT_CONNECTED = "paused: Slack is not connected"
PAUSED_SLACK_ACCOUNT_CHANGED = "paused: Slack account changed; start the watch again"


@dataclass(frozen=True)
class SlackWatch:
    """One person's request to receive their Slack mentions and direct messages in one room through one agent.

    `team_id` and `slack_user_id` are the Slack account the owner had connected when the watch started.
    `channels` limits channel messages to those channels; empty means every channel the owner can see.
    """

    watch_id: str
    owner_id: str
    agent_name: str
    room_id: str
    team_id: str
    slack_user_id: str
    mentions: bool
    direct_messages: bool
    channels: tuple[str, ...]
    created_at: int


def watch_id_for(owner_id: str, agent_name: str, room_id: str) -> str:
    """Return the ID of the one watch an owner can have with an agent in a room."""
    return hashlib.sha256(f"{owner_id}|{agent_name}|{room_id}".encode()).hexdigest()[:16]


def _decode(fields: dict[str, Any]) -> SlackWatch:
    decoded = dict(fields)
    # JSON has no tuples.
    decoded["channels"] = tuple(decoded["channels"])
    return SlackWatch(**decoded)


class SlackWatchStore:
    """The watches in `<control state>/slack/watches.json`, replaced whole under an advisory file lock."""

    def __init__(self, control_state_root: Path) -> None:
        self._path = control_state_root / "slack" / "watches.json"
        self._lock_path = self._path.with_name("watches.json.lock")

    def all(self) -> tuple[SlackWatch, ...]:
        """Return every watch."""
        with advisory_file_lock(self._lock_path, exclusive=False):
            return tuple(self._read().values())

    def get(self, watch_id: str) -> SlackWatch | None:
        """Return one watch, or None when there is none with this ID."""
        with advisory_file_lock(self._lock_path, exclusive=False):
            return self._read().get(watch_id)

    def put(self, watch: SlackWatch) -> None:
        """Store a watch, replacing the one with the same ID."""
        with advisory_file_lock(self._lock_path):
            watches = self._read()
            watches[watch.watch_id] = watch
            self._write(watches)

    def remove(self, watch_id: str) -> bool:
        """Delete a watch and return whether it existed."""
        with advisory_file_lock(self._lock_path):
            watches = self._read()
            if watches.pop(watch_id, None) is None:
                return False
            self._write(watches)
            return True

    def _read(self) -> dict[str, SlackWatch]:
        if not self._path.exists():
            return {}
        stored = json.loads(self._path.read_text(encoding="utf-8"))
        return {watch_id: _decode(fields) for watch_id, fields in stored["watches"].items()}

    def _write(self, watches: dict[str, SlackWatch]) -> None:
        stored = {"watches": {watch_id: asdict(watch) for watch_id, watch in watches.items()}}
        write_json_file_durable(self._path, stored, indent=2)


async def watch_room_problem(
    client: nio.AsyncClient,
    room_id: str,
    owner_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> str | None:
    """Return why a room may not receive its owner's Slack messages, or None when it may.

    The owner must be joined, under their own ID or a bridge alias, and no other person may be: agents and MindRoom's
    own accounts may share the room, but a colleague's direct message must never reach someone else.
    """
    members = await get_room_members(client, room_id)
    if members is None:
        return _PAUSED_ROOM_UNCHECKED
    owner_ids = equivalent_requester_ids(owner_id, config, runtime_paths)
    if members.isdisjoint(owner_ids):
        return _PAUSED_NOT_IN_ROOM
    if any(is_human_requester_id(member, config, runtime_paths) for member in members - owner_ids):
        return _PAUSED_OTHER_PERSON
    return None
