"""Personal Slack watches: who wants which Slack events in which room, and the `slack_watch` actions on them.

A watch's room must still allow it each time an event is delivered, which `watch_problem` checks.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

import aiohttp
import nio

from mindroom.durable_write import write_json_file_durable
from mindroom.file_locks import advisory_file_lock
from mindroom.oauth.credential_lifecycle import oauth_verified_claim
from mindroom.oauth.providers import OAuthConnectionRequired
from mindroom.requester_identity import (
    equivalent_requester_ids,
    is_access_checked_requester_id,
    resolve_human_requester_alias,
)
from mindroom.slack.api import parse_conversation_target
from mindroom.slack.app import SLACK_APP_NOT_CONFIGURED, load_slack_app

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.credentials.manager import CredentialsManager
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

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
# A turn a Slack watch started carries Slack users' words, not the owner's.
_WATCH_SOURCE_PREFIX = "slack-watch/"
_ACTIONS = ("start", "stop", "status")
_TURN_CHANGE_REFUSAL = "Ask me in a new message to change your Slack watch"
_NEEDS_APP_TOKEN = "The MindRoom Slack app needs an app-level token for watches"  # noqa: S105
_NEEDS_A_FILTER = "Watch mentions, direct messages, or both"
_NEEDS_A_CONVERSATION = "Slack watches work only in a Matrix conversation"
_NEEDS_CONTROL_STATE = "Slack watches need the primary runtime's control state"


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


def _watch_id_for(owner_id: str, agent_name: str, room_id: str) -> str:
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


async def _watch_room_problem(
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
    return await _watch_room_problem(client, watch.room_id, watch.owner_id, config, runtime_paths)


class SlackWatchRefusalError(Exception):
    """A watch action refused; `str()` is the message for the agent."""


@dataclass(frozen=True)
class SlackWatchCaller:
    """The `slack_watch` call a watch action runs for.

    `context` is the call's turn, or None outside one.
    `requester_credentials` returns the requester's usable Slack credentials or raises `OAuthConnectionRequired`;
    `reconnect` returns the prompt to connect Slack again, for a connection whose Slack account is not confirmed.
    """

    context: ToolRuntimeContext | None
    runtime_paths: RuntimePaths
    credentials_manager: CredentialsManager
    requester_credentials: Callable[[], Awaitable[dict[str, Any]]]
    reconnect: Callable[[], Awaitable[OAuthConnectionRequired]]


def watch_hook_source(watch: SlackWatch) -> str:
    """Return the hook source of the turns a watch starts."""
    return f"{_WATCH_SOURCE_PREFIX}{watch.watch_id}"


def started_by_a_watch(context: ToolRuntimeContext | None) -> bool:
    """Return whether a Slack watch started this turn; a call outside any turn was not."""
    return context is not None and (context.hook_source or "").startswith(_WATCH_SOURCE_PREFIX)


def _watching_message(watch: SlackWatch) -> str:
    kinds = (
        kind for kind, wanted in (("mentions", watch.mentions), ("direct messages", watch.direct_messages)) if wanted
    )
    message = f"Watching your Slack {' and '.join(kinds)}; they'll arrive here."
    if watch.channels:
        message += f" Channel messages count only from {', '.join(watch.channels)}."
    return message


async def run_watch_action(
    caller: SlackWatchCaller,
    action: str,
    *,
    mentions: bool,
    direct_messages: bool,
    channels: list[str] | None,
) -> dict[str, object]:
    """Run one watch action for the requester, this agent and this room, touching no one else's watch.

    A refusal raises `SlackWatchRefusalError`, and a missing Slack connection `OAuthConnectionRequired`.
    """
    if action not in _ACTIONS:
        msg = "action must be start, stop, or status"
        raise SlackWatchRefusalError(msg)
    context = caller.context
    if action != "status" and started_by_a_watch(context):
        # Slack text started this turn, so it must not change what is delivered to the owner.
        raise SlackWatchRefusalError(_TURN_CHANGE_REFUSAL)
    if context is None:
        raise SlackWatchRefusalError(_NEEDS_A_CONVERSATION)
    if caller.runtime_paths.control_state_root is None:
        raise SlackWatchRefusalError(_NEEDS_CONTROL_STATE)
    store = SlackWatchStore(caller.runtime_paths.control_state_root)
    # Ingress already resolves bridge aliases; a person's watch must stay theirs under either ID.
    owner_id = resolve_human_requester_alias(context.requester_id, context.current_config, context.runtime_paths)
    if action == "start":
        return await _start_watch(caller, context, store, owner_id, (mentions, direct_messages, channels))
    watch_id = _watch_id_for(owner_id, context.agent_name, context.room_id)
    if action == "stop":
        stopped = await asyncio.to_thread(store.remove, watch_id)
        message = "Stopped watching your Slack in this room" if stopped else "You were not watching Slack in this room"
        return {"message": message}
    watch = await asyncio.to_thread(store.get, watch_id)
    if watch is None:
        return {"watching": False, "message": "You are not watching Slack in this room"}
    return {
        "watching": True,
        "mentions": watch.mentions,
        "direct_messages": watch.direct_messages,
        "channels": list(watch.channels),
        "problem": await _status_problem(caller, watch, context),
    }


async def _start_watch(
    caller: SlackWatchCaller,
    context: ToolRuntimeContext,
    store: SlackWatchStore,
    owner_id: str,
    filters: tuple[bool, bool, list[str] | None],
) -> dict[str, object]:
    """Store the watch for the connected Slack account, once the app, the connection and the room allow it."""
    mentions, direct_messages, channels = filters
    if not (mentions or direct_messages):
        raise SlackWatchRefusalError(_NEEDS_A_FILTER)
    try:
        channel_ids = tuple(dict.fromkeys(parse_conversation_target(channel)[0] for channel in channels or ()))
    except ValueError as exc:
        raise SlackWatchRefusalError(str(exc)) from None
    app = await asyncio.to_thread(load_slack_app, caller.credentials_manager)
    if app is None:
        raise SlackWatchRefusalError(SLACK_APP_NOT_CONFIGURED)
    if app.app_token is None:
        raise SlackWatchRefusalError(_NEEDS_APP_TOKEN)
    credentials = await caller.requester_credentials()
    team_id = oauth_verified_claim(credentials, "team_id")
    slack_user_id = oauth_verified_claim(credentials, "sub")
    if team_id is None or slack_user_id is None:
        raise await caller.reconnect()
    problem = await _watch_room_problem(
        context.client,
        context.room_id,
        owner_id,
        context.current_config,
        context.runtime_paths,
    )
    if problem is not None:
        msg = f"Cannot watch Slack here: {problem.removeprefix('paused: ')}"
        raise SlackWatchRefusalError(msg)
    watch = SlackWatch(
        watch_id=_watch_id_for(owner_id, context.agent_name, context.room_id),
        owner_id=owner_id,
        agent_name=context.agent_name,
        room_id=context.room_id,
        team_id=team_id,
        slack_user_id=slack_user_id,
        mentions=mentions,
        direct_messages=direct_messages,
        channels=channel_ids,
        created_at=int(time.time()),
    )
    await asyncio.to_thread(store.put, watch)
    return {"message": _watching_message(watch)}


async def _status_problem(caller: SlackWatchCaller, watch: SlackWatch, context: ToolRuntimeContext) -> str | None:
    """Read the owner's Slack credentials, then say why the watch is paused."""
    try:
        credentials = await caller.requester_credentials()
    except OAuthConnectionRequired:
        credentials = None
    return await watch_problem(watch, credentials, context.client, context.current_config, context.runtime_paths)
