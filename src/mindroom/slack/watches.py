"""Personal Slack watches: who wants which Slack events delivered to which room, and the check that a room may receive them."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

import aiohttp
import nio

from mindroom.durable_write import write_json_file_durable
from mindroom.file_locks import advisory_file_lock
from mindroom.oauth.credential_lifecycle import oauth_verified_claim
from mindroom.requester_identity import equivalent_requester_ids, is_access_checked_requester_id

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

# What `status` reports while a watch is not delivering; each names what the owner can fix.
_PAUSED_SLACK_NOT_CONNECTED = "paused: Slack is not connected"
_PAUSED_SLACK_ACCOUNT_CHANGED = "paused: Slack account changed; start the watch again"
_PAUSED_NOT_IN_ROOM = "paused: you are not in this room"
_PAUSED_OTHER_PERSON = "paused: another person is in this room"
_PAUSED_LATER_JOINERS = "paused: people who join this room later could read earlier messages"
# Fail closed: a room whose members or history setting cannot be read may hold someone else.
_PAUSED_ROOM_UNCHECKED = "paused: room members could not be checked"
# Matrix hides earlier messages from a person who joins later only under these, and `shared` is its default.
_PRIVATE_HISTORY_VISIBILITY = frozenset({"invited", "joined"})
_PRESENT_MEMBERSHIPS = frozenset({"join", "invite"})


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

    The owner must be in the room, under their own ID or a bridge alias, and nobody else may be, joined or invited:
    an invited person reads what is delivered after the invite once they join. Only MindRoom's own accounts may share
    the room, and a bridge or other bot counts as a person because it relays text out of Matrix.
    The room must also hide earlier messages from people who join later.
    Anything the homeserver does not answer pauses the watch.
    """
    try:
        state = await client.room_get_state(room_id)
    except (aiohttp.ClientError, TimeoutError, nio.LocalProtocolError):
        return _PAUSED_ROOM_UNCHECKED
    if not isinstance(state, nio.RoomGetStateResponse):
        return _PAUSED_ROOM_UNCHECKED
    memberships = {
        event.get("state_key", ""): event.get("content", {}).get("membership")
        for event in state.events
        if event.get("type") == "m.room.member"
    }
    owner_ids = equivalent_requester_ids(owner_id, config, runtime_paths)
    if not any(memberships.get(user_id) == "join" for user_id in owner_ids):
        return _PAUSED_NOT_IN_ROOM
    present = {user_id for user_id, membership in memberships.items() if membership in _PRESENT_MEMBERSHIPS}
    if any(is_access_checked_requester_id(member, config, runtime_paths) for member in present - owner_ids):
        return _PAUSED_OTHER_PERSON
    visibility = next(
        (
            event.get("content", {}).get("history_visibility")
            for event in state.events
            if event.get("type") == "m.room.history_visibility"
        ),
        None,
    )
    return None if visibility in _PRIVATE_HISTORY_VISIBILITY else _PAUSED_LATER_JOINERS


async def watch_problem(
    watch: SlackWatch,
    credentials: dict[str, Any] | None,
    client: nio.AsyncClient,
    config: Config,
    runtime_paths: RuntimePaths,
) -> str | None:
    """Return why a stored watch is not delivering now, or None.

    `credentials` are the owner's usable Slack credentials, or None when there are none.
    The Slack account they name must still be the one the watch started with.
    """
    if credentials is None:
        return _PAUSED_SLACK_NOT_CONNECTED
    account = (oauth_verified_claim(credentials, "team_id"), oauth_verified_claim(credentials, "sub"))
    if account != (watch.team_id, watch.slack_user_id):
        return _PAUSED_SLACK_ACCOUNT_CHANGED
    return await watch_room_problem(client, watch.room_id, watch.owner_id, config, runtime_paths)
