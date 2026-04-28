"""Room membership and invite lifecycle helpers for one bot runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import nio

from mindroom.authorization import is_authorized_sender
from mindroom.commands.handler import _generate_welcome_message
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.matrix.client_room_admin import get_joined_rooms, join_room
from mindroom.matrix.invited_rooms_store import (
    invited_rooms_path,
    load_invited_rooms,
    save_invited_rooms,
    should_persist_invited_rooms,
)
from mindroom.matrix.rooms import leave_non_dm_rooms
from mindroom.matrix.state import matrix_state_for_runtime
from mindroom.runtime_protocols import SupportsClientConfig  # noqa: TC001

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from pathlib import Path

    import structlog

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.users import AgentMatrixUser


@dataclass(frozen=True)
class BotRoomLifecycleDeps:
    """Dependencies required for room membership and invite handling."""

    agent_name: str
    agent_user: AgentMatrixUser
    runtime: SupportsClientConfig
    runtime_paths: RuntimePaths
    get_logger: Callable[[], structlog.stdlib.BoundLogger]
    get_configured_rooms: Callable[[], Sequence[str]]
    send_response: Callable[..., Awaitable[str | None]]
    on_configured_room_joined: Callable[[str], Awaitable[None]]
    on_router_invite_joined: Callable[[str], Awaitable[None]]


class BotRoomLifecycle:
    """Own room joins, leaves, invite handling, and invited-room persistence."""

    deps: BotRoomLifecycleDeps
    invited_rooms: set[str]

    def __init__(self, deps: BotRoomLifecycleDeps) -> None:
        self.deps = deps
        self.invited_rooms = self.load_invited_rooms()

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

    def should_accept_invite(self) -> bool:
        """Return whether this entity should accept one inbound room invite."""
        if self.deps.agent_name == ROUTER_AGENT_NAME:
            return True
        if self.deps.agent_name in self._config().teams:
            return True
        agent_config = self._config().agents.get(self.deps.agent_name)
        if agent_config is None:
            return False
        return agent_config.accept_invites

    def should_persist_invited_rooms(self) -> bool:
        """Return whether this entity persists invited room IDs across restarts."""
        return should_persist_invited_rooms(self._config(), self.deps.agent_name)

    def invited_rooms_file_path(self) -> Path:
        """Return the durable path for invited room IDs for this entity."""
        return invited_rooms_path(self.deps.runtime_paths.storage_root, self.deps.agent_name)

    def load_invited_rooms(self) -> set[str]:
        """Load invited rooms persisted for one eligible named agent."""
        if not self.should_persist_invited_rooms():
            return set()
        return load_invited_rooms(self.invited_rooms_file_path())

    def save_invited_rooms(self) -> None:
        """Persist invited room IDs for one eligible named agent."""
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

    async def leave_unconfigured_rooms(self) -> None:
        """Leave any rooms this bot is no longer configured for."""
        client = self._client()
        joined_rooms = await get_joined_rooms(client)
        if joined_rooms is None:
            return

        current_rooms = set(joined_rooms)
        configured_rooms = set(self.deps.get_configured_rooms())
        if self.should_persist_invited_rooms():
            configured_rooms.update(self.invited_rooms)
        if self.deps.agent_name == ROUTER_AGENT_NAME:
            root_space_id = matrix_state_for_runtime(self.deps.runtime_paths).space_room_id
            if root_space_id is not None:
                configured_rooms.add(root_space_id)

        await leave_non_dm_rooms(client, list(current_rooms - configured_rooms))

    async def send_welcome_message_if_empty(self, room_id: str) -> None:
        """Send the router welcome message only when the room has no other history."""
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
            welcome_msg = _generate_welcome_message(room_id, self._config(), self.deps.runtime_paths)
            await self.deps.send_response(
                room_id=room_id,
                reply_to_event_id=None,
                response_text=welcome_msg,
                thread_id=None,
                skip_mentions=True,
            )
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
            self._logger().debug("Welcome message already sent", room_id=room_id)

    async def on_invite(self, room: nio.MatrixRoom, event: nio.InviteEvent) -> None:
        """Handle one inbound invite using the configured room membership policy."""
        client = self._client()
        if not self.should_accept_invite():
            self._logger().info("Ignored invite", room_id=room.room_id, sender=event.sender)
            return

        room_alias = room.canonical_alias
        if not isinstance(room_alias, str):
            room_alias = None
        if not is_authorized_sender(
            event.sender,
            self._config(),
            room.room_id,
            self.deps.runtime_paths,
            room_alias=room_alias,
        ):
            self._logger().debug(
                "ignoring_invite_from_unauthorized_sender",
                user_id=event.sender,
                room_id=room.room_id,
            )
            return

        self._logger().info("Received invite", room_id=room.room_id, sender=event.sender)
        if not await join_room(client, room.room_id):
            self._logger().error("Failed to join room", room_id=room.room_id)
            return

        self._logger().info("Joined room", room_id=room.room_id)
        if self.should_persist_invited_rooms() and room.room_id not in self.invited_rooms:
            self.invited_rooms.add(room.room_id)
            self.save_invited_rooms()
        if self.deps.agent_name == ROUTER_AGENT_NAME:
            await self.deps.on_router_invite_joined(room.room_id)
