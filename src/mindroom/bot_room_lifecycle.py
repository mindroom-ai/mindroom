"""Room membership and invite lifecycle helpers for one bot runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import nio

from mindroom.authorization import is_authorized_sender
from mindroom.commands.handler import generate_welcome_message_for_room
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.matrix.client_room_admin import RoomJoinOutcome, get_joined_rooms, join_room
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
    from mindroom.matrix.sync_continuity import SyncContinuityRecord, SyncContinuityStore
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
    continuity_store: SyncContinuityStore
    get_logger: Callable[[], structlog.stdlib.BoundLogger]
    get_configured_rooms: Callable[[], Sequence[str]]
    send_response: _SendRoomResponse
    on_room_joined: Callable[[str], Awaitable[None]]
    on_configured_room_joined: Callable[[str], Awaitable[None]]
    on_room_left: Callable[[str], Awaitable[None]]


class BotRoomLifecycle:
    """Own room joins, leaves, invite handling, and invited-room persistence."""

    deps: BotRoomLifecycleDeps
    invited_rooms: set[str]

    def __init__(self, deps: BotRoomLifecycleDeps) -> None:
        self.deps = deps
        self.invited_rooms = self._load_invited_rooms()
        self._pending_forgotten_invited_rooms: set[str] = set()
        self._invite_join_locks: dict[str, asyncio.Lock] = {}
        self._welcome_locks: dict[str, asyncio.Lock] = {}
        self._handled_invite_room_ids: set[str] = set()
        self._welcomed_room_ids: set[str] = set()
        self._decrypt_notice_fenced_room_ids: set[str] = set()
        self._applied_continuity_revision = -1

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

    def _should_accept_invite(self) -> bool:
        """Return whether this entity should accept one inbound room invite."""
        return should_accept_invites(self._config(), self.deps.agent_name)

    def _should_persist_invited_rooms(self) -> bool:
        """Return whether this entity persists invited room IDs across restarts."""
        return should_persist_invited_rooms(self._config(), self.deps.agent_name)

    def decrypt_notice_is_fenced(self, room_id: str) -> bool:
        """Return whether pre-join decrypt failures in this room stay silent."""
        return room_id in self._decrypt_notice_fenced_room_ids

    @property
    def has_pending_join_decrypt_fences(self) -> bool:
        """Return whether any durable join fence needs sync settlement."""
        return bool(self._decrypt_notice_fenced_room_ids)

    async def observe_trusted_sync_rooms(self, room_ids: Iterable[str]) -> None:
        """Clear join fences for rooms included in one trusted sync response."""
        record = await asyncio.to_thread(
            self.deps.continuity_store.update_join_fences,
            remove=tuple(room_ids),
        )
        self.apply_continuity_record(record)

    def apply_continuity_record(self, record: SyncContinuityRecord) -> None:
        """Expose join fences from one already-persisted continuity update."""
        if record.revision <= self._applied_continuity_revision:
            return
        self._applied_continuity_revision = record.revision
        self._decrypt_notice_fenced_room_ids = set(record.pending_join_decrypt_fences)

    async def restore_pending_join_decrypt_fences(self) -> None:
        """Validate durable unfinished-join fences before sync can start."""
        self.apply_continuity_record(await asyncio.to_thread(self.deps.continuity_store.load))
        if not self._decrypt_notice_fenced_room_ids:
            return
        joined_rooms = await get_joined_rooms(self._client())
        if joined_rooms is None:
            self._logger().warning(
                "matrix_join_fence_restore_joined_rooms_unavailable",
                pending_join_decrypt_fence_count=len(self._decrypt_notice_fenced_room_ids),
            )
            return
        record = await asyncio.to_thread(
            self.deps.continuity_store.update_join_fences,
            retain=joined_rooms,
        )
        self.apply_continuity_record(record)

    async def _join_room_with_decrypt_notice_fence(
        self,
        client: nio.AsyncClient,
        room_id: str,
    ) -> RoomJoinOutcome:
        """Fence decrypt callbacks before a live join can race its first sync."""
        self.apply_continuity_record(
            await asyncio.to_thread(
                self.deps.continuity_store.update_join_fences,
                add=(room_id,),
            ),
        )
        join_outcome = await join_room(client, room_id)
        if join_outcome is RoomJoinOutcome.TERMINAL_FAILURE:
            self.apply_continuity_record(
                await asyncio.to_thread(
                    self.deps.continuity_store.update_join_fences,
                    remove=(room_id,),
                ),
            )
        return join_outcome

    async def _on_configured_room_joined(self, room_id: str) -> None:
        """Apply common join state before configured-room setup."""
        await self.deps.on_room_joined(room_id)
        await self.deps.on_configured_room_joined(room_id)

    def _invited_rooms_file_path(self) -> Path:
        """Return the durable path for invited room IDs for this entity."""
        return invited_rooms_path(self.deps.runtime_paths.storage_root, self.deps.agent_name)

    def _load_invited_rooms(self) -> set[str]:
        """Load invited rooms persisted for one eligible entity."""
        if not self._should_persist_invited_rooms():
            return set()
        return load_invited_rooms(self._invited_rooms_file_path())

    def forget_invited_room(self, room_id: str) -> None:
        """Stop preserving an ad-hoc room after this bot leaves it."""
        if not self._should_persist_invited_rooms():
            self.invited_rooms.discard(room_id)
        elif not self._update_invited_room(room_id, remember=False):
            msg = f"Failed to forget invited room {room_id}"
            raise OSError(msg)
        self._handled_invite_room_ids.discard(room_id)
        self._welcomed_room_ids.discard(room_id)

    def _update_invited_room(self, room_id: str, *, remember: bool) -> bool:
        """Merge one update with durable and in-memory state before saving."""
        room_ids = load_invited_rooms(self._invited_rooms_file_path()) | self.invited_rooms
        if remember:
            self._pending_forgotten_invited_rooms.discard(room_id)
            room_ids.add(room_id)
        else:
            self._pending_forgotten_invited_rooms.add(room_id)
        room_ids.difference_update(self._pending_forgotten_invited_rooms)

        saved = save_invited_rooms(self._invited_rooms_file_path(), room_ids)
        if saved:
            self._pending_forgotten_invited_rooms.clear()
        self.invited_rooms = room_ids
        return saved

    def _remember_invited_room(self, room_id: str) -> None:
        """Persist one accepted invite or fail so its durable intent can retry."""
        if self._should_persist_invited_rooms() and not self._update_invited_room(room_id, remember=True):
            msg = f"Failed to persist invited room {room_id}"
            raise OSError(msg)

    async def _send_invite_welcome(self, room_id: str, sender: str) -> None:
        """Finish router welcome delivery or leave the invite retryable."""
        if self.deps.agent_name != ROUTER_AGENT_NAME:
            return
        if await self.send_welcome_message_if_empty(room_id, sender):
            return
        msg = f"Failed to complete welcome message for {room_id}"
        raise RuntimeError(msg)

    async def join_configured_rooms(self) -> None:
        """Join all rooms this bot should preserve across restarts."""
        client = self._client()
        joined_rooms = await get_joined_rooms(client)
        current_rooms = set(joined_rooms or ())
        desired_rooms = set(self.deps.get_configured_rooms())
        if self._should_persist_invited_rooms():
            desired_rooms.update(self.invited_rooms)

        for room_id in desired_rooms:
            if room_id in current_rooms:
                self._logger().debug("Already joined room", room_id=room_id)
                await self._on_configured_room_joined(room_id)
                continue

            if await self._join_room_with_decrypt_notice_fence(client, room_id) is RoomJoinOutcome.JOINED:
                current_rooms.add(room_id)
                self._logger().info("Joined room", room_id=room_id)
                await self._on_configured_room_joined(room_id)
            else:
                self._logger().warning("Failed to join room", room_id=room_id)

    async def leave_unconfigured_rooms(self, room_ids: list[str] | None = None) -> None:
        """Leave any rooms this bot is no longer configured for."""
        client = self._client()
        await leave_non_dm_rooms(
            client,
            room_ids if room_ids is not None else await self._rooms_to_leave(),
            on_room_left=self.deps.on_room_left,
        )

    async def _rooms_to_leave(self) -> list[str]:
        """Return joined rooms this bot should now leave before DM filtering."""
        client = self._client()
        joined_rooms = await get_joined_rooms(client)
        if joined_rooms is None:
            return []

        current_rooms = set(joined_rooms)
        configured_rooms = set(self.deps.get_configured_rooms())
        if self._should_persist_invited_rooms():
            configured_rooms.update(self.invited_rooms)
        if self.deps.agent_name == ROUTER_AGENT_NAME:
            root_space_id = matrix_state_for_runtime(self.deps.runtime_paths).space_room_id
            if root_space_id is not None:
                configured_rooms.add(root_space_id)

        return list(current_rooms - configured_rooms)

    async def send_welcome_message_if_empty(
        self,
        room_id: str,
        visible_to_sender_id: str | None = None,
    ) -> bool:
        """Send the router welcome message only when the room has no other history."""
        async with self._lock_for_room(self._welcome_locks, room_id):
            if room_id in self._welcomed_room_ids:
                self._logger().debug("Welcome message already handled", room_id=room_id)
                return True

            client = self._client()
            response = await client.room_messages(
                room_id,
                limit=2,
                message_filter={"types": ["m.room.message"]},
            )
            if not isinstance(response, nio.RoomMessagesResponse):
                self._logger().error("Failed to check room messages", room_id=room_id, error=str(response))
                return False

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
                    return False
                self._welcomed_room_ids.add(room_id)
                self._logger().info("Welcome message sent", room_id=room_id)
                return True

            if len(response.chunk) != 1:
                return True

            message = response.chunk[0]
            if (
                isinstance(message, nio.RoomMessageText)
                and message.sender == self.deps.agent_user.user_id
                and "Welcome to MindRoom" in message.body
            ):
                self._welcomed_room_ids.add(room_id)
                self._logger().debug("Welcome message already sent", room_id=room_id)
            return True

    async def on_invite(self, room: nio.MatrixRoom, event: nio.InviteEvent) -> None:
        """Handle one inbound invite using the configured room membership policy."""
        client = self._client()
        if not self._should_accept_invite():
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
                await self.deps.on_room_joined(room.room_id)
                self._remember_invited_room(room.room_id)
                await self._send_invite_welcome(room.room_id, event.sender)
                return

            self._logger().info("Received invite", room_id=room.room_id, sender=event.sender)
            join_outcome = await self._join_room_with_decrypt_notice_fence(client, room.room_id)
            if join_outcome is not RoomJoinOutcome.JOINED:
                self._logger().error("Failed to join room", room_id=room.room_id)
                if join_outcome is RoomJoinOutcome.TERMINAL_FAILURE:
                    return
                msg = f"Failed to join invited room {room.room_id}"
                raise RuntimeError(msg)

            self._logger().info("Joined room", room_id=room.room_id)
            await self.deps.on_room_joined(room.room_id)
            self._remember_invited_room(room.room_id)
            self._handled_invite_room_ids.add(room.room_id)
            await self._send_invite_welcome(room.room_id, event.sender)
