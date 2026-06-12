"""Room membership and invite lifecycle helpers for one bot runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import nio

from mindroom.authorization import is_authorized_sender
from mindroom.commands.handler import generate_welcome_message_for_room
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.matrix.client_room_admin import get_joined_rooms, join_room
from mindroom.matrix.invited_rooms_store import (
    invited_rooms_path,
    load_invited_rooms,
    save_invited_rooms,
    should_accept_invites,
    should_persist_invited_rooms,
)
from mindroom.matrix.rooms import leave_non_dm_rooms
from mindroom.matrix.state import matrix_state_for_runtime
from mindroom.message_target import MessageTarget
from mindroom.runtime_protocols import SupportsClientConfig  # noqa: TC001

if TYPE_CHECKING:
    from pathlib import Path

    import structlog

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.users import AgentMatrixUser


class _SendRoomResponse(Protocol):
    """Send one room-lifecycle message to an explicit target."""

    def __call__(
        self,
        *,
        target: MessageTarget,
        response_text: str,
        skip_mentions: bool = False,
    ) -> Awaitable[str | None]:
        """Send text to the explicit Matrix target."""
        ...


@dataclass(frozen=True)
class BotRoomLifecycleDeps:
    """Dependencies required for room membership and invite handling."""

    agent_name: str
    agent_user: AgentMatrixUser
    runtime: SupportsClientConfig
    runtime_paths: RuntimePaths
    get_logger: Callable[[], structlog.stdlib.BoundLogger]
    get_configured_rooms: Callable[[], Sequence[str]]
    send_response: _SendRoomResponse
    on_configured_room_joined: Callable[[str], Awaitable[None]]


class BotRoomLifecycle:
    """Own room joins, leaves, invite handling, and invited-room persistence."""

    deps: BotRoomLifecycleDeps
    invited_rooms: set[str]

    def __init__(self, deps: BotRoomLifecycleDeps) -> None:
        self.deps = deps
        self.invited_rooms = self.load_invited_rooms()
        self._invite_join_locks: dict[str, asyncio.Lock] = {}
        self._welcome_locks: dict[str, asyncio.Lock] = {}
        self._handled_invite_room_ids: set[str] = set()
        self._welcomed_room_ids: set[str] = set()

    def _lock_for_room(self, locks: dict[str, asyncio.Lock], room_id: str) -> asyncio.Lock:
        lock = locks.get(room_id)
        if lock is None:
            lock = asyncio.Lock()
            locks[room_id] = lock
        return lock

    def _client_has_joined_room(self, room_id: str) -> bool:
        rooms = self._client().rooms
        if not isinstance(rooms, Mapping):
            return False
        return any(joined_room_id == room_id for joined_room_id in rooms)

    def _client(self) -> nio.AsyncClient:
        client = self.deps.runtime.client
        if client is None:
            msg = "Matrix client is not ready for room lifecycle work"
            raise RuntimeError(msg)
        return client

    def _config(self) -> Config:
        return self.deps.runtime.config

    def _logger(self) -> structlog.stdlib.BoundLogger:
        return self.deps.get_logger()

    def _room_for_welcome(self, room_id: str) -> nio.MatrixRoom:
        rooms = self._client().rooms
        if isinstance(rooms, Mapping):
            cached_room = rooms.get(room_id)
            if isinstance(cached_room, nio.MatrixRoom):
                return cached_room
        return nio.MatrixRoom(room_id=room_id, own_user_id=self.deps.agent_user.user_id)

    def should_accept_invite(self) -> bool:
        """Return whether this entity should accept one inbound room invite."""
        return should_accept_invites(self._config(), self.deps.agent_name)

    def should_persist_invited_rooms(self) -> bool:
        """Return whether this entity persists invited room IDs across restarts."""
        return should_persist_invited_rooms(self._config(), self.deps.agent_name)

    def invited_rooms_file_path(self) -> Path:
        """Return the durable path for invited room IDs for this entity."""
        return invited_rooms_path(self.deps.runtime_paths.storage_root, self.deps.agent_name)

    def load_invited_rooms(self) -> set[str]:
        """Load invited rooms persisted for one eligible entity."""
        if not self.should_persist_invited_rooms():
            return set()
        return load_invited_rooms(self.invited_rooms_file_path())

    def save_invited_rooms(self) -> None:
        """Persist invited room IDs for one eligible entity."""
        if not self.should_persist_invited_rooms():
            return
        save_invited_rooms(self.invited_rooms_file_path(), self.invited_rooms)

    async def join_configured_rooms(self) -> None:
        """Join all rooms this bot should preserve across restarts."""
        client = self._client()
        joined_rooms = await get_joined_rooms(client)
        current_rooms = set(joined_rooms or [])
        current_rooms.update(client.rooms)
        desired_rooms = set(self.deps.get_configured_rooms())
        if self.should_persist_invited_rooms():
            desired_rooms.update(self.invited_rooms)

        for room_id in desired_rooms:
            if room_id in current_rooms:
                self._logger().debug("Already joined room", room_id=room_id)
                await self.deps.on_configured_room_joined(room_id)
                continue

            if await join_room(client, room_id):
                current_rooms.add(room_id)
                self._logger().info("Joined room", room_id=room_id)
                await self.deps.on_configured_room_joined(room_id)
            else:
                self._logger().warning("Failed to join room", room_id=room_id)

    async def leave_unconfigured_rooms(self, room_ids: list[str] | None = None) -> None:
        """Leave any rooms this bot is no longer configured for."""
        client = self._client()
        await leave_non_dm_rooms(
            client,
            room_ids if room_ids is not None else await self.rooms_to_leave(),
        )

    async def rooms_to_leave(self) -> list[str]:
        """Return joined rooms this bot should now leave before DM filtering."""
        client = self._client()
        joined_rooms = await get_joined_rooms(client)
        if joined_rooms is None:
            return []

        current_rooms = set(joined_rooms)
        configured_rooms = set(self.deps.get_configured_rooms())
        if self.should_persist_invited_rooms():
            configured_rooms.update(self.invited_rooms)
        if self.deps.agent_name == ROUTER_AGENT_NAME:
            root_space_id = matrix_state_for_runtime(self.deps.runtime_paths).space_room_id
            if root_space_id is not None:
                configured_rooms.add(root_space_id)

        return list(current_rooms - configured_rooms)

    async def send_welcome_message_if_empty(self, room_id: str, visible_to_sender_id: str | None = None) -> None:
        """Send the router welcome message only when the room has no other history."""
        async with self._lock_for_room(self._welcome_locks, room_id):
            if room_id in self._welcomed_room_ids:
                self._logger().debug("Welcome message already handled", room_id=room_id)
                return

            client = self._client()
            response = await client.room_messages(
                room_id,
                limit=2,
                message_filter={"types": ["m.room.message"]},
            )
            if not isinstance(response, nio.RoomMessagesResponse):
                self._logger().error("Failed to check room messages", room_id=room_id, error=str(response))
                return

            if not response.chunk:
                self._logger().info("Room is empty, sending welcome message", room_id=room_id)
                welcome_msg = await generate_welcome_message_for_room(
                    client,
                    self._room_for_welcome(room_id),
                    visible_to_sender_id,
                    self._config(),
                    self.deps.runtime_paths,
                )
                target = MessageTarget.resolve(
                    room_id=room_id,
                    thread_id=None,
                    reply_to_event_id=None,
                    room_mode=True,
                )
                event_id = await self.deps.send_response(
                    target=target,
                    response_text=welcome_msg,
                    skip_mentions=True,
                )
                if event_id is None:
                    self._logger().warning("Welcome message delivery failed", room_id=room_id)
                    return
                self._welcomed_room_ids.add(room_id)
                self._logger().info("Welcome message sent", room_id=room_id)
                return

            if len(response.chunk) != 1:
                return

            message = response.chunk[0]
            if (
                isinstance(message, nio.RoomMessageText)
                and message.sender == self.deps.agent_user.user_id
                and "Welcome to MindRoom" in message.body
            ):
                self._welcomed_room_ids.add(room_id)
                self._logger().debug("Welcome message already sent", room_id=room_id)
            return

    async def on_invite(self, room: nio.MatrixRoom, event: nio.InviteEvent) -> None:
        """Handle one inbound invite using the configured room membership policy."""
        client = self._client()
        if not self.should_accept_invite():
            self._logger().info("Ignored invite", room_id=room.room_id, sender=event.sender)
            return

        if not is_authorized_sender(
            event.sender,
            self._config(),
            room.room_id,
            self.deps.runtime_paths,
        ):
            self._logger().debug(
                "ignoring_invite_from_unauthorized_sender",
                user_id=event.sender,
                room_id=room.room_id,
            )
            return

        async with self._lock_for_room(self._invite_join_locks, room.room_id):
            if room.room_id in self._handled_invite_room_ids or self._client_has_joined_room(room.room_id):
                self._logger().debug("Invite already handled", room_id=room.room_id, sender=event.sender)
                if self.deps.agent_name == ROUTER_AGENT_NAME:
                    await self.send_welcome_message_if_empty(room.room_id, event.sender)
                return

            self._logger().info("Received invite", room_id=room.room_id, sender=event.sender)
            if not await join_room(client, room.room_id):
                self._logger().error("Failed to join room", room_id=room.room_id)
                return

            self._handled_invite_room_ids.add(room.room_id)
            self._logger().info("Joined room", room_id=room.room_id)
            if self.should_persist_invited_rooms() and room.room_id not in self.invited_rooms:
                self.invited_rooms.add(room.room_id)
                self.save_invited_rooms()
            if self.deps.agent_name == ROUTER_AGENT_NAME:
                await self.send_welcome_message_if_empty(room.room_id, event.sender)
