"""Room membership and invite lifecycle helpers for one bot runtime."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import nio

from mindroom.authorization import (
    is_sender_allowed_for_agent_invite,
    is_sender_allowed_for_agent_reply_in_room,
)
from mindroom.background_tasks import run_coroutine_until_complete
from mindroom.commands.handler import generate_welcome_message_for_room
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.matrix.client_room_admin import (
    RoomJoinOutcome,
    get_joined_rooms,
    get_room_members,
    join_room,
)
from mindroom.matrix.invited_rooms_store import (
    invited_rooms_path,
    load_invited_rooms,
    save_invited_rooms,
    should_accept_invites,
    should_persist_invited_rooms,
)
from mindroom.matrix.rooms import filter_non_dm_rooms, leave_rooms
from mindroom.matrix.state import matrix_state_for_runtime
from mindroom.message_target import MessageTarget
from mindroom.runtime_protocols import SupportsClientConfigMemberships  # noqa: TC001

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager
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
    runtime: SupportsClientConfigMemberships
    runtime_paths: RuntimePaths
    continuity_store: SyncContinuityStore
    get_logger: Callable[[], structlog.stdlib.BoundLogger]
    get_configured_rooms: Callable[[], Sequence[str]]
    send_response: _SendRoomResponse
    admit_response: Callable[[], AbstractAsyncContextManager[None]]
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
        self._invite_join_locks: dict[str, asyncio.Lock] = {}
        self._welcome_locks: dict[str, asyncio.Lock] = {}
        self._welcomed_room_ids: set[str] = set()
        self._decrypt_notice_fenced_room_ids: set[str] = set()
        self._join_fence_protected_room_ids: set[str] = set()
        self._accepted_rooms_awaiting_joined_setup: set[str] = set()
        self._next_invite_join_generation = 0
        self._active_invite_join_generations: dict[str, set[int]] = {}
        self._invalidated_invite_join_generations: set[int] = set()
        self._applied_continuity_revision = -1

    def _lock_for_room(self, locks: dict[str, asyncio.Lock], room_id: str) -> asyncio.Lock:
        lock = locks.get(room_id)
        if lock is None:
            lock = asyncio.Lock()
            locks[room_id] = lock
        return lock

    def invite_ownership(self, room_id: str) -> AbstractAsyncContextManager[None]:
        """Serialize live invite work with authoritative departure for one room."""
        return self._lock_for_room(self._invite_join_locks, room_id)

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

    @property
    def has_pending_joined_room_setup(self) -> bool:
        """Return whether startup skipped setup for an accepted room."""
        return bool(self._accepted_rooms_awaiting_joined_setup)

    def invalidate_active_invite_joins(self, room_ids: Iterable[str]) -> None:
        """Fail closed joins overlapped by authoritative ownership loss."""
        for room_id in room_ids:
            generations = self._active_invite_join_generations.get(room_id)
            if generations:
                self._invalidated_invite_join_generations.update(generations)

    @asynccontextmanager
    async def _invite_join_ownership(self, room_id: str) -> AsyncIterator[int]:
        """Own one queued invite generation and its serialized room work."""
        self._next_invite_join_generation += 1
        generation = self._next_invite_join_generation
        room_generations = self._active_invite_join_generations.setdefault(room_id, set())
        room_generations.add(generation)
        try:
            async with self.invite_ownership(room_id):
                yield generation
        finally:
            room_generations.discard(generation)
            if not room_generations:
                self._active_invite_join_generations.pop(room_id, None)
            self._invalidated_invite_join_generations.discard(generation)

    def decrypt_notice_is_fenced(self, room_id: str) -> bool:
        """Return whether pre-join decrypt failures in this room stay silent."""
        return room_id in self._decrypt_notice_fenced_room_ids

    @property
    def has_pending_join_decrypt_fences(self) -> bool:
        """Return whether any durable join fence needs sync settlement."""
        return bool(self._decrypt_notice_fenced_room_ids)

    async def observe_trusted_sync_rooms(self, room_ids: Iterable[str]) -> None:
        """Clear join fences for rooms included in one trusted sync response."""
        settled_room_ids = self.join_fence_settlement_rooms(room_ids)
        record = await asyncio.to_thread(
            self.deps.continuity_store.update_join_fences,
            remove=settled_room_ids,
        )
        self.apply_continuity_record(record)

    def join_fence_settlement_rooms(self, room_ids: Iterable[str]) -> tuple[str, ...]:
        """Exclude still-joined rooms whose compensating leave is unconfirmed."""
        return tuple(room_id for room_id in room_ids if room_id not in self._join_fence_protected_room_ids)

    def apply_continuity_record(self, record: SyncContinuityRecord) -> None:
        """Expose join fences from one already-persisted continuity update."""
        if record.revision <= self._applied_continuity_revision:
            return
        self._applied_continuity_revision = record.revision
        protected_room_ids = self._decrypt_notice_fenced_room_ids & self._join_fence_protected_room_ids
        self._decrypt_notice_fenced_room_ids = set(record.pending_join_decrypt_fences) | protected_room_ids

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
        joined_room_ids = set(joined_rooms)
        protected_room_ids = (self._decrypt_notice_fenced_room_ids & joined_room_ids) - await self._owned_room_ids()
        self._join_fence_protected_room_ids.update(protected_room_ids)
        record = await asyncio.to_thread(
            self.deps.continuity_store.update_join_fences,
            retain=joined_room_ids,
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
            await self._clear_join_decrypt_notice_fence(room_id)
        return join_outcome

    async def _clear_join_decrypt_notice_fence(self, room_id: str) -> None:
        """Clear a join fence after the current join work becomes terminal."""
        self.apply_continuity_record(
            await asyncio.to_thread(
                self.deps.continuity_store.update_join_fences,
                remove=(room_id,),
            ),
        )

    async def _ensure_join_decrypt_notice_fence(self, room_id: str) -> None:
        """Retain a join fence after Matrix fails to confirm departure."""
        self._decrypt_notice_fenced_room_ids.add(room_id)
        self.apply_continuity_record(
            await asyncio.to_thread(
                self.deps.continuity_store.update_join_fences,
                add=(room_id,),
            ),
        )

    async def _on_configured_room_joined(self, room_id: str) -> None:
        """Apply common join state before configured-room setup."""
        self._join_fence_protected_room_ids.discard(room_id)
        await self.deps.on_room_joined(room_id)
        await self.deps.on_configured_room_joined(room_id)
        self._accepted_rooms_awaiting_joined_setup.discard(room_id)

    def _invited_rooms_file_path(self) -> Path:
        """Return the durable path for invited room IDs for this entity."""
        return invited_rooms_path(self.deps.runtime_paths.storage_root, self.deps.agent_name)

    def _load_invited_rooms(self) -> set[str]:
        """Load invited rooms persisted for one eligible entity."""
        if not self._should_persist_invited_rooms():
            return set()
        return load_invited_rooms(self._invited_rooms_file_path())

    async def _accepted_rooms_snapshot(self) -> set[str]:
        """Read accepted ownership without mutating the lifecycle's live state."""
        if not self._should_persist_invited_rooms():
            return set(self.invited_rooms)
        durable_rooms = await asyncio.to_thread(load_invited_rooms, self._invited_rooms_file_path())
        return durable_rooms | self.invited_rooms

    def discard_live_invite(self, room_id: str) -> None:
        """Revoke transient invite ownership after an authoritative departure."""
        client = self.deps.runtime.client
        if client is not None:
            client.invited_rooms.pop(room_id, None)

    def forget_invited_room(self, room_id: str) -> None:
        """Stop preserving an ad-hoc room after this bot leaves it."""
        self._accepted_rooms_awaiting_joined_setup.discard(room_id)
        invited_rooms_file_exists = self._invited_rooms_file_path().exists()
        if not self._should_persist_invited_rooms() and not invited_rooms_file_exists:
            self.invited_rooms.discard(room_id)
        elif not self._update_invited_room(room_id, remember=False):
            msg = f"Failed to forget invited room {room_id}"
            raise OSError(msg)
        self._welcomed_room_ids.discard(room_id)

    def _update_invited_room(self, room_id: str, *, remember: bool) -> bool:
        """Merge one update with durable and in-memory state before saving."""
        room_ids = load_invited_rooms(self._invited_rooms_file_path()) | self.invited_rooms
        if remember:
            room_ids.add(room_id)
        else:
            room_ids.discard(room_id)

        saved = save_invited_rooms(self._invited_rooms_file_path(), room_ids)
        if saved:
            self.invited_rooms = room_ids
        elif not remember:
            self.invited_rooms.discard(room_id)
        return saved

    def _remember_invited_room(self, room_id: str) -> None:
        """Persist one accepted invite or fail so the caller can compensate."""
        if self._should_persist_invited_rooms() and not self._update_invited_room(room_id, remember=True):
            msg = f"Failed to persist invited room {room_id}"
            raise OSError(msg)

    async def _send_invite_welcome(self, room_id: str, sender: str) -> None:
        """Best-effort router welcome after invite acceptance."""
        if self.deps.agent_name != ROUTER_AGENT_NAME:
            return
        await self.send_welcome_message_if_empty(room_id, sender)

    async def join_configured_rooms(self) -> None:
        """Join configured rooms and restore setup for accepted memberships."""
        client = self._client()
        configured_rooms = set(self.deps.get_configured_rooms())
        accepted_rooms = await self._accepted_rooms_snapshot()

        for room_id in sorted(configured_rooms | accepted_rooms):
            async with self.invite_ownership(room_id):
                joined_rooms = await get_joined_rooms(client)
                if joined_rooms is not None and room_id in joined_rooms:
                    self._logger().debug("Already joined room", room_id=room_id)
                    await self._on_configured_room_joined(room_id)
                    continue
                if room_id not in configured_rooms:
                    if joined_rooms is None:
                        self._accepted_rooms_awaiting_joined_setup.add(room_id)
                    continue
                if await self._join_room_with_decrypt_notice_fence(client, room_id) is RoomJoinOutcome.JOINED:
                    self._logger().info("Joined room", room_id=room_id)
                    await self._on_configured_room_joined(room_id)
                else:
                    self._logger().warning("Failed to join room", room_id=room_id)

    async def restore_pending_joined_room_setup(self) -> None:
        """Retry setup after Matrix's authoritative joined-room inventory recovers."""
        joined_room_ids = await get_joined_rooms(self._client())
        if joined_room_ids is None:
            return
        pending_room_ids = set(self._accepted_rooms_awaiting_joined_setup)
        self._accepted_rooms_awaiting_joined_setup.difference_update(pending_room_ids - set(joined_room_ids))
        for room_id in sorted(pending_room_ids):
            if room_id not in joined_room_ids:
                continue
            async with self.invite_ownership(room_id):
                if room_id not in self._accepted_rooms_awaiting_joined_setup:
                    continue
                if room_id not in await self._owned_room_ids():
                    self._accepted_rooms_awaiting_joined_setup.discard(room_id)
                    continue
                try:
                    await self._on_configured_room_joined(room_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._logger().exception("accepted_room_setup_retry_failed", room_id=room_id)

    async def leave_unconfigured_rooms(self, room_ids: list[str] | None = None) -> None:
        """Leave any rooms this bot is no longer configured for."""
        rooms_to_leave = room_ids if room_ids is not None else await self._rooms_to_leave()
        await self._leave_rooms_with_ownership(rooms_to_leave, recheck_ownership=True)

    async def leave_non_dm_rooms_for_cleanup(self, room_ids: list[str]) -> None:
        """Leave non-DM rooms during entity removal through lifecycle ownership."""
        client = self._client()
        non_dm_room_ids = await filter_non_dm_rooms(client, room_ids)
        preserved_room_ids = set(room_ids) - set(non_dm_room_ids)
        for room_id in preserved_room_ids:
            self._logger().debug("dm_room_preserved", room_id=room_id)
        await self._leave_rooms_with_ownership(non_dm_room_ids, recheck_ownership=False)

    async def _leave_rooms_with_ownership(self, room_ids: list[str], *, recheck_ownership: bool) -> None:
        """Serialize confirmed local leaves with invite and departure work."""
        cancellation: asyncio.CancelledError | None = None
        cleanup_errors: list[Exception] = []
        for room_id in room_ids:
            try:
                await run_coroutine_until_complete(
                    self._leave_owned_room(room_id, recheck_ownership=recheck_ownership),
                )
            except asyncio.CancelledError as error:
                cancellation = cancellation or error
            except Exception as error:
                cleanup_errors.append(error)

        for error in cleanup_errors[1:]:
            self._logger().error(
                "additional_room_leave_failure",
                error=str(error),
            )
        if cancellation is not None:
            if cancellation.__cause__ is None and cleanup_errors:
                raise cancellation from cleanup_errors[0]
            raise cancellation
        if cleanup_errors:
            raise cleanup_errors[0]

    async def _leave_owned_room(self, room_id: str, *, recheck_ownership: bool) -> None:
        """Acquire room ownership and finish one still-required leave."""
        async with self.invite_ownership(room_id):
            if recheck_ownership and room_id in await self._owned_room_ids():
                return
            await self._leave_room_with_confirmed_cleanup(room_id)

    async def _leave_room_with_confirmed_cleanup(self, room_id: str) -> bool:
        """Run one Matrix leave and confirmed local cleanup as one protected operation."""
        confirmed = False

        async def finish_confirmed_leave(confirmed_room_id: str) -> None:
            nonlocal confirmed
            confirmed = True
            await self._finish_confirmed_leave(confirmed_room_id)

        await leave_rooms(
            self._client(),
            [room_id],
            on_room_left=finish_confirmed_leave,
        )
        return confirmed

    async def leave_orphaned_room(self, room_id: str) -> bool:
        """Leave one startup orphan through the runtime room owner."""
        async with self.invite_ownership(room_id):
            return await self._leave_room_with_confirmed_cleanup(room_id)

    async def _finish_confirmed_leave(self, room_id: str) -> None:
        """Clear local join state only after Matrix confirms departure."""
        cleanup_errors: list[Exception] = []
        try:
            self.forget_invited_room(room_id)
        except Exception as error:
            cleanup_errors.append(error)
        self._join_fence_protected_room_ids.discard(room_id)
        if self.decrypt_notice_is_fenced(room_id):
            try:
                await self._clear_join_decrypt_notice_fence(room_id)
            except Exception as error:
                cleanup_errors.append(error)
        try:
            await self.deps.on_room_left(room_id)
        except Exception as error:
            cleanup_errors.append(error)
        if cleanup_errors:
            for error in cleanup_errors[1:]:
                self._logger().error(
                    "confirmed_leave_additional_cleanup_failure",
                    room_id=room_id,
                    error=str(error),
                )
            raise cleanup_errors[0]

    async def settle_authoritative_departure(self, room_id: str) -> None:
        """Clear transient join state after Matrix authoritatively reports departure."""
        self._join_fence_protected_room_ids.discard(room_id)
        if self.decrypt_notice_is_fenced(room_id):
            await self._clear_join_decrypt_notice_fence(room_id)

    async def _rooms_to_leave(self) -> list[str]:
        """Return joined rooms with no configured or accepted owner."""
        client = self._client()
        joined_rooms = await get_joined_rooms(client)
        if joined_rooms is None:
            return []
        return list(set(joined_rooms) - await self._owned_room_ids())

    async def _owned_room_ids(self) -> set[str]:
        """Return rooms currently owned by configuration or accepted membership."""
        owned_room_ids = set(self.deps.get_configured_rooms())
        if self._should_persist_invited_rooms():
            owned_room_ids.update(await self._accepted_rooms_snapshot())
        if self.deps.agent_name == ROUTER_AGENT_NAME:
            root_space_id = matrix_state_for_runtime(self.deps.runtime_paths).space_room_id
            if root_space_id is not None:
                owned_room_ids.add(root_space_id)

        return owned_room_ids

    async def send_welcome_message_if_empty(
        self,
        room_id: str,
        visible_to_sender_id: str | None = None,
    ) -> bool:
        """Send the router welcome message only when the room has no other history."""
        if visible_to_sender_id is None:
            if room_id in self.invited_rooms and room_id not in self.deps.get_configured_rooms():
                self._logger().debug("Skipping requester-less welcome in an ad-hoc room", room_id=room_id)
                return True
            return await self._send_welcome_message_if_empty_admitted(room_id, None)
        async with self.deps.admit_response():
            return await self._send_welcome_message_if_empty_admitted(room_id, visible_to_sender_id)

    async def _send_welcome_message_if_empty_admitted(
        self,
        room_id: str,
        visible_to_sender_id: str | None,
    ) -> bool:
        """Check room history and deliver a welcome inside the caller's admission slot."""
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
                if visible_to_sender_id is not None and not is_sender_allowed_for_agent_reply_in_room(
                    visible_to_sender_id,
                    self.deps.agent_name,
                    self._config(),
                    room_id,
                    self.deps.runtime_paths,
                    self.deps.runtime.agent_reply_memberships,
                ):
                    self._logger().debug(
                        "invite_welcome_suppressed_by_reply_permissions",
                        user_id=visible_to_sender_id,
                        room_id=room_id,
                    )
                    return True
                return await self._deliver_welcome(room_id, visible_to_sender_id)

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

    async def _deliver_welcome(self, room_id: str, visible_to_sender_id: str | None) -> bool:
        """Generate and deliver one welcome after its caller owns the send boundary."""
        self._logger().info("Room is empty, sending welcome message", room_id=room_id)
        welcome_msg = await generate_welcome_message_for_room(
            self._client(),
            self._room_for_welcome(room_id),
            visible_to_sender_id,
            self._config(),
            self.deps.runtime_paths,
            self.deps.runtime.agent_reply_memberships,
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

    async def reconcile_invites(self) -> None:
        """Re-evaluate only invites currently cached by Matrix."""
        for room in tuple(self._client().invited_rooms.values()):
            if room.inviter is not None:
                await self.handle_invite(room, room.inviter)

    async def _attempt_invite_join(
        self,
        client: nio.AsyncClient,
        room_id: str,
        sender: str,
        expected_invite: nio.MatrixInvitedRoom,
        invite_join_generation: int,
    ) -> bool:
        """Run the one join attempt owned by an exact live invite."""
        if invite_join_generation in self._invalidated_invite_join_generations:
            return False
        try:
            join_outcome = await self._join_room_with_decrypt_notice_fence(client, room_id)
        except (Exception, asyncio.CancelledError):
            self._discard_invite_if_current(room_id, sender, expected_invite)
            raise
        if join_outcome is RoomJoinOutcome.JOINED:
            return True
        self._logger().error("Failed to join room", room_id=room_id)
        self._discard_invite_if_current(room_id, sender, expected_invite)
        await self._clear_join_decrypt_notice_fence(room_id)
        return False

    async def handle_invite(
        self,
        room: nio.MatrixRoom,
        sender: str,
        *,
        send_welcome: bool = True,
    ) -> None:
        """Accept one current Matrix invite without creating recovery state."""
        welcome_after_acceptance = False
        async with self._invite_join_ownership(room.room_id) as invite_join_generation:
            client = self._client()
            current_invite = client.invited_rooms.get(room.room_id)
            if current_invite is None or current_invite.inviter != sender:
                self._logger().debug("ignoring_stale_invite", room_id=room.room_id, sender=sender)
                return
            if not self._should_accept_invite():
                self._logger().info("Ignored invite", room_id=room.room_id, sender=sender)
                return
            if not is_sender_allowed_for_agent_invite(
                sender,
                self.deps.agent_name,
                self._config(),
                self.deps.runtime_paths,
                self.deps.runtime.agent_reply_memberships,
                current_inviter_id=current_invite.inviter,
            ):
                self._logger().debug(
                    "ignoring_invite_from_unauthorized_sender",
                    user_id=sender,
                    room_id=room.room_id,
                )
                return
            self._logger().info("Received invite", room_id=room.room_id, sender=sender)
            if not await self._attempt_invite_join(
                client,
                room.room_id,
                sender,
                current_invite,
                invite_join_generation,
            ):
                return

            self._logger().info("Joined room", room_id=room.room_id)
            try:
                accepted = await self._accept_joined_invite(
                    room.room_id,
                    sender,
                    current_invite,
                    invite_join_generation,
                )
            except asyncio.CancelledError:
                self._discard_invite_if_current(room.room_id, sender, current_invite)
                raise
            except Exception:
                self._logger().exception("invite_acceptance_failed", room_id=room.room_id)
                accepted = False
            if not accepted:
                await self._leave_unaccepted_invite(room.room_id, sender, current_invite)
                return

            self._discard_invite_if_current(room.room_id, sender, current_invite)
            welcome_after_acceptance = send_welcome
        if welcome_after_acceptance:
            try:
                await self._send_invite_welcome(room.room_id, sender)
            except Exception:
                self._logger().exception("invite_welcome_failed", room_id=room.room_id)

    async def _accept_joined_invite(
        self,
        room_id: str,
        sender: str,
        expected_invite: nio.MatrixInvitedRoom,
        invite_join_generation: int,
    ) -> bool:
        """Recheck current policy and persist one confirmed joined invite."""
        if invite_join_generation in self._invalidated_invite_join_generations:
            return False
        await self.deps.on_room_joined(room_id)
        if not self._should_accept_invite():
            return False
        invite_allowed = is_sender_allowed_for_agent_invite(
            sender,
            self.deps.agent_name,
            self._config(),
            self.deps.runtime_paths,
            self.deps.runtime.agent_reply_memberships,
        )
        if not invite_allowed:
            joined_member_ids = await get_room_members(self._client(), room_id)
            invite_allowed = self._should_accept_invite() and is_sender_allowed_for_agent_invite(
                sender,
                self.deps.agent_name,
                self._config(),
                self.deps.runtime_paths,
                self.deps.runtime.agent_reply_memberships,
                joined_member_ids=joined_member_ids,
            )
        current_invite = self._client().invited_rooms.get(room_id)
        if (
            not invite_allowed
            or expected_invite.inviter != sender
            or (
                current_invite is not None
                and (current_invite is not expected_invite or current_invite.inviter != sender)
            )
            or invite_join_generation in self._invalidated_invite_join_generations
        ):
            return False
        self._remember_invited_room(room_id)
        self._join_fence_protected_room_ids.discard(room_id)
        return True

    async def _leave_unaccepted_invite(
        self,
        room_id: str,
        sender: str,
        expected_invite: nio.MatrixInvitedRoom,
    ) -> None:
        """Attempt one compensating leave without creating retry ownership."""
        if room_id in self.deps.get_configured_rooms():
            self._discard_invite_if_current(room_id, sender, expected_invite)
            await self._on_configured_room_joined(room_id)
            return

        self._discard_invite_if_current(room_id, sender, expected_invite)
        self._join_fence_protected_room_ids.add(room_id)
        fence_error: Exception | None = None
        try:
            await self._ensure_join_decrypt_notice_fence(room_id)
        except Exception as error:
            fence_error = error
            self._logger().exception("invite_compensating_leave_fence_failed", room_id=room_id)
        try:
            confirmed = await self._leave_room_with_confirmed_cleanup(room_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._logger().exception("invite_compensating_leave_cleanup_failed", room_id=room_id)
            return
        if not confirmed and fence_error is not None:
            raise fence_error

    def _discard_invite_if_current(
        self,
        room_id: str,
        sender: str,
        expected_invite: nio.MatrixInvitedRoom,
    ) -> None:
        """Consume only the exact live invite that owns this attempt."""
        client = self._client()
        current_invite = client.invited_rooms.get(room_id)
        if current_invite is expected_invite and current_invite.inviter == sender:
            client.invited_rooms.pop(room_id, None)
