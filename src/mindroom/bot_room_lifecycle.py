"""Room membership and invite lifecycle helpers for one bot runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import nio

from mindroom.access_policy import resolve_responder_access
from mindroom.authorization import (
    is_sender_allowed_by_authoritative_current_room_members,
    is_sender_allowed_for_agent_invite,
    is_sender_allowed_for_agent_reply_in_room,
)
from mindroom.commands.handler import generate_welcome_message_for_room
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.matrix.client_room_admin import RoomJoinOutcome, get_joined_rooms, join_room, leave_room
from mindroom.matrix.invited_rooms_store import (
    PendingRoomInvite,
    PendingRoomInvitePhase,
    RoomInviteState,
    invited_rooms_path,
    load_room_invite_states,
    save_room_invite_states,
    should_accept_invites,
    should_persist_invited_rooms,
)
from mindroom.matrix.rooms import leave_non_dm_rooms
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


@dataclass(frozen=True, slots=True)
class _CurrentRoomInvite:
    """One live invite observation that later sync state can invalidate."""

    revision: int
    inviter_id: str


class BotRoomLifecycle:
    """Own room joins, leaves, invite handling, and invited-room persistence."""

    deps: BotRoomLifecycleDeps
    invited_rooms: set[str]
    _pending_room_invites: dict[str, PendingRoomInvite]

    def __init__(self, deps: BotRoomLifecycleDeps) -> None:
        self.deps = deps
        self._set_room_invite_state_cache(self._load_room_invite_states())
        self._invite_join_locks: dict[str, asyncio.Lock] = {}
        self._invite_departure_events: dict[str, asyncio.Event] = {}
        self._current_room_invites: dict[str, _CurrentRoomInvite] = {}
        self._current_room_invite_revision = 0
        self._welcome_locks: dict[str, asyncio.Lock] = {}
        self._welcomed_room_ids: set[str] = set()
        self._decrypt_notice_fenced_room_ids: set[str] = set()
        self._applied_continuity_revision = -1

    def _lock_for_room(self, locks: dict[str, asyncio.Lock], room_id: str) -> asyncio.Lock:
        lock = locks.get(room_id)
        if lock is None:
            lock = asyncio.Lock()
            locks[room_id] = lock
        return lock

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

    def invalidate_current_invite_evidence(self) -> None:
        """Discard invite evidence tied to the previous Matrix sync generation."""
        self._current_room_invites.clear()
        client = self.deps.runtime.client
        if client is not None:
            client.invited_rooms.clear()

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

    async def _on_configured_room_joined(self, room_id: str) -> None:
        """Apply common join state before configured-room setup."""
        await self.deps.on_room_joined(room_id)
        await self.deps.on_configured_room_joined(room_id)

    def _invited_rooms_file_path(self) -> Path:
        """Return the durable path for this entity's room-invite ledger."""
        return invited_rooms_path(self.deps.runtime_paths.storage_root, self.deps.agent_name)

    def _load_room_invite_states(self) -> dict[str, RoomInviteState]:
        """Load the complete invited-room ledger for one eligible entity."""
        if not self._should_persist_invited_rooms():
            return {}
        return load_room_invite_states(self._invited_rooms_file_path())

    def _set_room_invite_state_cache(self, states: dict[str, RoomInviteState]) -> None:
        """Refresh derived in-memory views from one complete durable snapshot."""
        self.invited_rooms = {room_id for room_id, state in states.items() if state.accepted}
        self._pending_room_invites = {
            room_id: state.pending for room_id, state in states.items() if state.pending is not None
        }

    def _save_room_invite_states(
        self,
        states: dict[str, RoomInviteState],
        *,
        failure_message: str,
    ) -> None:
        """Persist and publish one complete ledger replacement."""
        if self._should_persist_invited_rooms() and not save_room_invite_states(
            self._invited_rooms_file_path(),
            states,
        ):
            raise OSError(failure_message)
        self._set_room_invite_state_cache(states)

    def _reload_room_invite_states(self) -> dict[str, RoomInviteState]:
        """Reload and publish the latest durable ledger snapshot."""
        states = self._load_room_invite_states()
        self._set_room_invite_state_cache(states)
        return states

    async def _refresh_invited_rooms(self) -> None:
        """Refresh rooms written by other runtime components."""
        if not self._should_persist_invited_rooms():
            return
        states = await asyncio.to_thread(load_room_invite_states, self._invited_rooms_file_path())
        self._set_room_invite_state_cache(states)

    def begin_invited_room_departure(self, room_id: str) -> None:
        """Durably remove old invite state before departure fencing can suspend."""
        if room_id in self._invite_departure_events:
            return
        self._invite_departure_events[room_id] = asyncio.Event()
        self._current_room_invites.pop(room_id, None)
        client = self.deps.runtime.client
        if client is not None:
            client.invited_rooms.pop(room_id, None)
        states = self._load_room_invite_states()
        if states.pop(room_id, None) is not None:
            self._save_room_invite_states(
                states,
                failure_message=f"Failed to forget invited room {room_id}",
            )
        else:
            self._set_room_invite_state_cache(states)
        self._welcomed_room_ids.discard(room_id)

    async def forget_invited_room_after_departure(self, room_id: str) -> None:
        """Finish departure cleanup without deleting a subsequently observed invite."""
        if room_id not in self._invite_departure_events:
            self.begin_invited_room_departure(room_id)
        departure_event = self._invite_departure_events[room_id]
        try:
            async with self._lock_for_room(self._invite_join_locks, room_id):
                current_invite = self._current_room_invites.get(room_id)
                states = self._load_room_invite_states()
                previous_states = states.copy()
                state = states.get(room_id)
                if current_invite is None:
                    states.pop(room_id, None)
                elif state is not None and state.accepted:
                    states[room_id] = RoomInviteState(pending=state.pending)
                if states != previous_states:
                    self._save_room_invite_states(
                        states,
                        failure_message=f"Failed to forget invited room {room_id}",
                    )
                else:
                    self._set_room_invite_state_cache(states)
                self._welcomed_room_ids.discard(room_id)
                if current_invite is None:
                    await self._clear_join_decrypt_notice_fence(room_id)
        finally:
            if self._invite_departure_events.get(room_id) is departure_event:
                self._invite_departure_events.pop(room_id)
            departure_event.set()

    def record_current_room_invite(self, room_id: str, sender_id: str) -> _CurrentRoomInvite:
        """Record durable and live evidence for the room's current inviter."""
        self._record_pending_room_invite(room_id, sender_id)
        self._current_room_invite_revision += 1
        current_invite = _CurrentRoomInvite(
            revision=self._current_room_invite_revision,
            inviter_id=sender_id,
        )
        self._current_room_invites[room_id] = current_invite
        return current_invite

    def authorize_current_room_invite(
        self,
        room_id: str,
        sender_id: str,
        expected_invite: _CurrentRoomInvite,
    ) -> bool:
        """Commit one current authorization before its sync response can certify."""
        if self._current_invite_authorization(room_id, sender_id, expected_invite) is not True:
            return False
        return self._set_pending_room_invite_phase(
            room_id,
            sender_id,
            PendingRoomInvitePhase.AUTHORIZED,
        )

    def _record_pending_room_invite(self, room_id: str, sender_id: str) -> None:
        """Persist an outstanding invite before its network work runs."""
        if not self._should_persist_invited_rooms():
            return
        states = self._load_room_invite_states()
        states[room_id] = RoomInviteState(
            pending=PendingRoomInvite(
                inviter_id=sender_id,
                phase=PendingRoomInvitePhase.OBSERVED,
            ),
        )
        self._save_room_invite_states(
            states,
            failure_message=f"Failed to persist pending room invite {room_id}",
        )

    def _set_pending_room_invite_phase(
        self,
        room_id: str,
        sender_id: str,
        phase: PendingRoomInvitePhase,
    ) -> bool:
        """Persist one phase only while the same inviter still owns the record."""
        states = self._load_room_invite_states()
        state = states.get(room_id)
        pending_invite = state.pending if state is not None else None
        if pending_invite is None or pending_invite.inviter_id != sender_id:
            self._set_room_invite_state_cache(states)
            return False
        if pending_invite.phase is PendingRoomInvitePhase.LEAVING:
            self._set_room_invite_state_cache(states)
            return phase is PendingRoomInvitePhase.LEAVING
        if pending_invite.phase is phase:
            self._set_room_invite_state_cache(states)
            return True
        assert state is not None
        states[room_id] = RoomInviteState(
            accepted=state.accepted,
            pending=PendingRoomInvite(inviter_id=sender_id, phase=phase),
        )
        self._save_room_invite_states(
            states,
            failure_message=f"Failed to persist pending room invite phase for {room_id}",
        )
        return True

    def _pending_room_invite_matches(
        self,
        room_id: str,
        sender_id: str,
        phase: PendingRoomInvitePhase,
    ) -> bool:
        """Return whether durable state still describes the expected transaction."""
        states = self._reload_room_invite_states()
        state = states.get(room_id)
        return state is not None and state.pending == PendingRoomInvite(
            inviter_id=sender_id,
            phase=phase,
        )

    def _forget_pending_room_invite(self, room_id: str, *, expected_sender_id: str | None = None) -> None:
        """Forget a resolved outstanding invite without losing concurrent state."""
        states = self._load_room_invite_states()
        state = states.get(room_id)
        pending_invite = state.pending if state is not None else None
        if expected_sender_id is not None and (
            pending_invite is None or pending_invite.inviter_id != expected_sender_id
        ):
            self._set_room_invite_state_cache(states)
            return
        if pending_invite is None:
            self._set_room_invite_state_cache(states)
            return
        assert state is not None
        if state.accepted:
            states[room_id] = RoomInviteState(accepted=True)
        else:
            states.pop(room_id)
        self._save_room_invite_states(
            states,
            failure_message=f"Failed to forget pending room invite {room_id}",
        )

    def _complete_recorded_room_invite(
        self,
        room_id: str,
        sender: str,
        expected_invite: _CurrentRoomInvite | None,
    ) -> None:
        """Forget one resolved invite without deleting a newer replacement."""
        current_invite = self._current_room_invites.get(room_id)
        if current_invite != expected_invite:
            return
        self._forget_pending_room_invite(room_id, expected_sender_id=sender)
        if current_invite is not None:
            self._current_room_invites.pop(room_id, None)

    def _forget_recorded_room_invite(
        self,
        room_id: str,
        sender: str,
        expected_invite: _CurrentRoomInvite | None,
    ) -> bool:
        """Remove one whole terminal transaction without touching a replacement."""
        current_invite = self._current_room_invites.get(room_id)
        if current_invite != expected_invite:
            return False
        states = self._load_room_invite_states()
        state = states.get(room_id)
        if state is None or state.pending is None or state.pending.inviter_id != sender:
            self._set_room_invite_state_cache(states)
            return False
        states.pop(room_id)
        self._save_room_invite_states(
            states,
            failure_message=f"Failed to forget terminal invited room {room_id}",
        )
        if current_invite is not None:
            self._current_room_invites.pop(room_id, None)
        return True

    def _mark_terminal_invite_leaving(
        self,
        room_id: str,
        sender: str,
        expected_invite: _CurrentRoomInvite | None,
    ) -> bool:
        """Atomically revoke accepted state while retaining explicit leave work."""
        if self._current_room_invites.get(room_id) != expected_invite:
            return False
        states = self._load_room_invite_states()
        state = states.get(room_id)
        if state is None or state.pending is None or state.pending.inviter_id != sender:
            self._set_room_invite_state_cache(states)
            return False
        leaving_invite = PendingRoomInvite(
            inviter_id=sender,
            phase=PendingRoomInvitePhase.LEAVING,
        )
        if state == RoomInviteState(pending=leaving_invite):
            self._set_room_invite_state_cache(states)
            return True
        states[room_id] = RoomInviteState(pending=leaving_invite)
        self._save_room_invite_states(
            states,
            failure_message=f"Failed to persist terminal invited room departure {room_id}",
        )
        client = self.deps.runtime.client
        if client is not None:
            client.invited_rooms.pop(room_id, None)
        return True

    def _remember_invited_room(self, room_id: str, sender: str) -> bool:
        """Persist acceptance while retaining the exact pending transaction."""
        if not self._should_persist_invited_rooms():
            return True
        states = self._load_room_invite_states()
        state = states.get(room_id)
        if state is None or state.pending != PendingRoomInvite(
            inviter_id=sender,
            phase=PendingRoomInvitePhase.AUTHORIZED,
        ):
            self._set_room_invite_state_cache(states)
            return False
        states[room_id] = RoomInviteState(accepted=True, pending=state.pending)
        self._save_room_invite_states(
            states,
            failure_message=f"Failed to persist invited room {room_id}",
        )
        return True

    async def _send_invite_welcome(
        self,
        room_id: str,
        sender: str,
    ) -> None:
        """Finish a post-join-authorized router welcome or leave it retryable."""
        if self.deps.agent_name != ROUTER_AGENT_NAME:
            return
        if await self.send_welcome_message_if_empty(
            room_id,
            sender,
            sender_is_authorized=True,
        ):
            return
        msg = f"Failed to complete welcome message for {room_id}"
        raise RuntimeError(msg)

    async def join_configured_rooms(self, *, include_persisted_invited_rooms: bool = True) -> None:
        """Join all rooms this bot should preserve across restarts."""
        desired_rooms = set(self.deps.get_configured_rooms())
        if include_persisted_invited_rooms and self._should_persist_invited_rooms():
            desired_rooms.update(self.invited_rooms - self._pending_room_invites.keys())
        await self._join_rooms(desired_rooms)

    async def rejoin_persisted_invited_rooms(self) -> None:
        """Rejoin accepted ad-hoc rooms only after live sync has exposed fresh invites."""
        if not self._should_persist_invited_rooms():
            return
        desired_rooms = self.invited_rooms - self._pending_room_invites.keys() - set(self.deps.get_configured_rooms())
        if not desired_rooms:
            return
        await self._join_rooms(desired_rooms)

    async def _join_rooms(self, desired_rooms: set[str]) -> None:
        """Join one already-selected set of desired rooms."""
        client = self._client()
        joined_rooms = await get_joined_rooms(client)
        current_rooms = set(joined_rooms or ())

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
            await self._refresh_invited_rooms()
            configured_rooms.update(self.invited_rooms)
            configured_rooms.update(self._pending_room_invites)
        if self.deps.agent_name == ROUTER_AGENT_NAME:
            root_space_id = matrix_state_for_runtime(self.deps.runtime_paths).space_room_id
            if root_space_id is not None:
                configured_rooms.add(root_space_id)

        return list(current_rooms - configured_rooms)

    async def send_welcome_message_if_empty(
        self,
        room_id: str,
        visible_to_sender_id: str | None = None,
        *,
        sender_is_authorized: bool = False,
    ) -> bool:
        """Send the router welcome message only when the room has no other history."""
        if visible_to_sender_id is None:
            if room_id in self.invited_rooms and room_id not in self.deps.get_configured_rooms():
                self._logger().debug("Skipping requester-less welcome in an ad-hoc room", room_id=room_id)
                return True
            return await self._send_welcome_message_if_empty_admitted(room_id, None, False)
        async with self.deps.admit_response():
            return await self._send_welcome_message_if_empty_admitted(
                room_id,
                visible_to_sender_id,
                sender_is_authorized,
            )

    async def _send_welcome_message_if_empty_admitted(
        self,
        room_id: str,
        visible_to_sender_id: str | None,
        sender_is_authorized: bool,
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
                sender_allowed = (
                    visible_to_sender_id is None
                    or sender_is_authorized
                    or is_sender_allowed_for_agent_reply_in_room(
                        visible_to_sender_id,
                        self.deps.agent_name,
                        self._config(),
                        room_id,
                        self.deps.runtime_paths,
                        self.deps.runtime.agent_reply_memberships,
                    )
                )
                if not sender_allowed:
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

    async def handle_recorded_invite(
        self,
        room: nio.MatrixRoom,
        sender: str,
        expected_invite: _CurrentRoomInvite,
    ) -> None:
        """Handle one durable invite against lifecycle-owned current evidence."""
        await self._handle_invite(room, sender, expected_invite)

    async def reconcile_pending_invites(self) -> None:
        """Serialize recovery of every durable invite transaction."""
        self._reload_room_invite_states()
        for room_id in tuple(self._pending_room_invites):
            await self._reconcile_pending_invite(room_id)

    async def _reconcile_pending_invite(self, room_id: str) -> None:
        """Reconcile one room from authoritative state while holding its join lock."""
        departure_event = self._invite_departure_events.get(room_id)
        if departure_event is not None:
            await departure_event.wait()

        async with self._lock_for_room(self._invite_join_locks, room_id):
            if room_id in self._invite_departure_events:
                return
            states = self._reload_room_invite_states()
            state = states.get(room_id)
            pending_invite = state.pending if state is not None else None
            if pending_invite is None:
                return
            observed_current_invite = self._current_room_invites.get(room_id)

            joined_rooms = await get_joined_rooms(self._client())
            states = self._reload_room_invite_states()
            state = states.get(room_id)
            if (
                state is None
                or state.pending != pending_invite
                or self._current_room_invites.get(room_id) != observed_current_invite
                or room_id in self._invite_departure_events
            ):
                return
            if joined_rooms is None:
                return
            current_invite = observed_current_invite
            if current_invite is not None and current_invite.inviter_id != pending_invite.inviter_id:
                current_invite = None
            await self._reconcile_pending_invite_under_lock(
                room_id,
                pending_invite,
                current_invite,
                joined=room_id in joined_rooms,
            )

    async def _reconcile_pending_invite_under_lock(
        self,
        room_id: str,
        pending_invite: PendingRoomInvite,
        current_invite: _CurrentRoomInvite | None,
        *,
        joined: bool,
    ) -> None:
        """Apply one already-validated authoritative membership result."""
        sender = pending_invite.inviter_id
        if pending_invite.phase is PendingRoomInvitePhase.LEAVING:
            await self._reconcile_leaving_invite(room_id, sender, current_invite, joined=joined)
            return
        if pending_invite.phase is PendingRoomInvitePhase.OBSERVED:
            await self._reconcile_observed_invite(room_id, sender, current_invite, joined=joined)
            return
        await self._reconcile_authorized_invite(room_id, sender, current_invite, joined=joined)

    async def _reconcile_observed_invite(
        self,
        room_id: str,
        sender: str,
        current_invite: _CurrentRoomInvite | None,
        *,
        joined: bool,
    ) -> None:
        """Handle evidence that was recorded but never authorized."""
        if joined:
            if room_id in self.deps.get_configured_rooms():
                self._forget_recorded_room_invite(room_id, sender, current_invite)
                if room_id not in self._pending_room_invites:
                    await self._clear_join_decrypt_notice_fence(room_id)
                return
            await self._finish_terminal_invite(
                room_id,
                sender,
                current_invite,
                leave_joined_room=True,
            )
            return
        if current_invite is not None:
            room = self._invited_room_for_retry(room_id)
            await self._handle_invite_under_lock(self._client(), room, sender, current_invite)
            return
        if self._ordinary_invite_authorization(room_id, sender) is not True:
            return
        if not self._set_pending_room_invite_phase(
            room_id,
            sender,
            PendingRoomInvitePhase.AUTHORIZED,
        ):
            return
        await self._retry_authorized_invite_under_lock(room_id, sender)

    async def _reconcile_authorized_invite(
        self,
        room_id: str,
        sender: str,
        current_invite: _CurrentRoomInvite | None,
        *,
        joined: bool,
    ) -> None:
        """Finish or retry a transaction authorized before its join attempt."""
        if joined:
            await self._finish_joined_invite_under_lock(room_id, sender, current_invite)
            return
        if current_invite is not None:
            current_authorization = self._current_invite_authorization(room_id, sender, current_invite)
            if current_authorization is None:
                return
            if current_authorization:
                room = self._invited_room_for_retry(room_id)
                await self._handle_invite_under_lock(self._client(), room, sender, current_invite)
            else:
                await self._finish_terminal_invite(room_id, sender, current_invite)
            return
        invite_can_retry = self._authorized_invite_can_retry(room_id, sender)
        if invite_can_retry is None:
            return
        if invite_can_retry:
            await self._retry_authorized_invite_under_lock(room_id, sender)
        else:
            await self._finish_terminal_invite(room_id, sender, None)

    async def _reconcile_leaving_invite(
        self,
        room_id: str,
        sender: str,
        current_invite: _CurrentRoomInvite | None,
        *,
        joined: bool,
    ) -> None:
        """Retry or complete one terminally rejected joined-room departure."""
        if joined:
            await self._leave_terminal_invite_under_lock(room_id, sender, current_invite)
            return
        await self._finish_left_terminal_invite_under_lock(room_id, sender, current_invite)

    async def _finish_left_terminal_invite_under_lock(
        self,
        room_id: str,
        sender: str,
        current_invite: _CurrentRoomInvite | None,
    ) -> None:
        """Fence one completed Matrix leave before clearing its durable work."""
        await self.deps.on_room_left(room_id)
        self._forget_recorded_room_invite(room_id, sender, current_invite)
        if room_id not in self._pending_room_invites:
            await self._clear_join_decrypt_notice_fence(room_id)

    def _invited_room_for_retry(self, room_id: str) -> nio.MatrixRoom:
        """Return cached invite context or the minimal room needed for a retry."""
        room = self._client().invited_rooms.get(room_id)
        if room is not None:
            return room
        return nio.MatrixInvitedRoom(room_id, self.deps.agent_user.user_id)

    def _ordinary_invite_authorization(self, room_id: str, sender: str) -> bool | None:
        """Evaluate normal policy, deferring while a relevant grant room is unready."""
        if not self._should_accept_invite():
            return False
        if is_sender_allowed_for_agent_reply_in_room(
            sender,
            self.deps.agent_name,
            self._config(),
            room_id,
            self.deps.runtime_paths,
            self.deps.runtime.agent_reply_memberships,
        ):
            return True
        access = resolve_responder_access(self._config(), self.deps.agent_name)
        if access.members_of_rooms and not self.deps.runtime.agent_reply_memberships.has_authoritative_memberships(
            access.members_of_rooms,
            self._config(),
        ):
            return None
        return False

    def _authorized_invite_can_retry(self, room_id: str, sender: str) -> bool | None:
        """Return whether current policy still permits a committed join retry."""
        ordinary_authorization = self._ordinary_invite_authorization(room_id, sender)
        if ordinary_authorization is True:
            return True
        access = resolve_responder_access(self._config(), self.deps.agent_name)
        if self._should_accept_invite() and access.current_room_members:
            return True
        return ordinary_authorization

    async def _retry_authorized_invite_under_lock(self, room_id: str, sender: str) -> None:
        """Retry one already-authorized transaction without inventing live evidence."""
        join_outcome = await self._join_room_with_decrypt_notice_fence(self._client(), room_id)
        if join_outcome is not RoomJoinOutcome.JOINED:
            await self._handle_failed_invite_join(room_id, sender, None, join_outcome)
            return
        await self._finish_joined_invite_under_lock(room_id, sender, None)

    async def _finish_joined_invite_under_lock(
        self,
        room_id: str,
        sender: str,
        current_invite: _CurrentRoomInvite | None,
    ) -> None:
        """Finish one authorized transaction after Matrix confirms membership."""
        if not self._pending_room_invite_matches(
            room_id,
            sender,
            PendingRoomInvitePhase.AUTHORIZED,
        ):
            return
        await self.deps.on_room_joined(room_id)
        if not self._joined_invite_transaction_is_current(room_id, sender, current_invite):
            return

        invite_is_authorized = await self._post_join_inviter_authorization(room_id, sender, current_invite)
        if invite_is_authorized is None or not self._joined_invite_transaction_is_current(
            room_id,
            sender,
            current_invite,
        ):
            return
        if not invite_is_authorized:
            self._logger().info(
                "recovered_invite_no_longer_authorized",
                room_id=room_id,
                sender=sender,
            )
            await self._finish_terminal_invite(
                room_id,
                sender,
                current_invite,
                leave_joined_room=True,
            )
            return
        if not self._remember_invited_room(room_id, sender):
            return
        await self._send_invite_welcome(room_id, sender)
        self._complete_recorded_room_invite(room_id, sender, current_invite)

    def _joined_invite_transaction_is_current(
        self,
        room_id: str,
        sender: str,
        current_invite: _CurrentRoomInvite | None,
    ) -> bool:
        """Return whether post-join work still owns the same transaction."""
        return (
            room_id not in self._invite_departure_events
            and (current_invite is None or self._current_room_invites.get(room_id) == current_invite)
            and self._pending_room_invite_matches(
                room_id,
                sender,
                PendingRoomInvitePhase.AUTHORIZED,
            )
        )

    async def _post_join_inviter_authorization(
        self,
        room_id: str,
        sender: str,
        current_invite: _CurrentRoomInvite | None,
    ) -> bool | None:
        """Verify one inviter for accepted membership and its lifecycle welcome."""
        if not self._should_accept_invite():
            return False
        ordinary_authorization = self._ordinary_invite_authorization(room_id, sender)
        if ordinary_authorization is True or (
            current_invite is not None and self._current_invite_authorization(room_id, sender, current_invite) is True
        ):
            return True
        access = resolve_responder_access(self._config(), self.deps.agent_name)
        if not access.current_room_members:
            return ordinary_authorization
        joined_members_response = await self._client().joined_members(room_id)
        if not isinstance(joined_members_response, nio.JoinedMembersResponse):
            self._logger().warning(
                "recovered_invite_membership_query_failed",
                room_id=room_id,
                sender=sender,
                error=str(joined_members_response),
            )
            return None
        if is_sender_allowed_by_authoritative_current_room_members(
            sender,
            self.deps.agent_name,
            self._config(),
            (member.user_id for member in joined_members_response.members),
        ):
            return True
        return ordinary_authorization

    async def _handle_invite(
        self,
        room: nio.MatrixRoom,
        sender: str,
        expected_invite: _CurrentRoomInvite | None,
    ) -> None:
        """Accept one invite when its inviter currently passes responder access."""
        client = self._client()
        if not self._should_accept_invite():
            self._logger().info("Ignored invite", room_id=room.room_id, sender=sender)
            return

        departure_event = self._invite_departure_events.get(room.room_id)
        if departure_event is not None:
            await departure_event.wait()

        async with self._lock_for_room(self._invite_join_locks, room.room_id):
            if room.room_id in self._invite_departure_events:
                self._logger().debug(
                    "invite_deferred_during_departure",
                    room_id=room.room_id,
                    sender=sender,
                )
                return
            await self._handle_invite_under_lock(client, room, sender, expected_invite)

    async def _handle_invite_under_lock(
        self,
        client: nio.AsyncClient,
        room: nio.MatrixRoom,
        sender: str,
        expected_invite: _CurrentRoomInvite | None,
    ) -> None:
        """Run invite authorization and join work while owning the room lock."""
        if not self._current_invite_is_authorized(room.room_id, sender, expected_invite):
            self._logger().debug(
                "ignoring_invite_from_unauthorized_sender",
                user_id=sender,
                room_id=room.room_id,
            )
            return

        self._logger().info("Received invite", room_id=room.room_id, sender=sender)
        if not self._set_pending_room_invite_phase(
            room.room_id,
            sender,
            PendingRoomInvitePhase.AUTHORIZED,
        ):
            return
        join_outcome = await self._join_room_with_decrypt_notice_fence(client, room.room_id)
        if join_outcome is not RoomJoinOutcome.JOINED:
            await self._handle_failed_invite_join(room.room_id, sender, expected_invite, join_outcome)
            return

        self._logger().info("Joined room", room_id=room.room_id)
        await self._finish_joined_invite_under_lock(room.room_id, sender, expected_invite)

    async def _handle_failed_invite_join(
        self,
        room_id: str,
        sender: str,
        current_invite: _CurrentRoomInvite | None,
        join_outcome: RoomJoinOutcome,
    ) -> None:
        """Apply retry or cleanup semantics for one unsuccessful invite join."""
        self._logger().error("Failed to join room", room_id=room_id)
        invite_is_current = self._current_room_invites.get(room_id) == current_invite
        if join_outcome is RoomJoinOutcome.ACCESS_DENIED and (current_invite is None or not invite_is_current):
            await self._finish_terminal_invite(room_id, sender, current_invite)
            return
        if join_outcome is RoomJoinOutcome.TERMINAL_FAILURE:
            await self._finish_terminal_invite(room_id, sender, current_invite)
            return
        msg = f"Failed to join invited room {room_id}"
        raise RuntimeError(msg)

    async def _finish_terminal_invite(
        self,
        room_id: str,
        sender: str,
        current_invite: _CurrentRoomInvite | None,
        *,
        leave_joined_room: bool = False,
    ) -> None:
        """Durably clear one terminal transaction and then release its fence."""
        if leave_joined_room:
            if not self._mark_terminal_invite_leaving(room_id, sender, current_invite):
                return
            await self._leave_terminal_invite_under_lock(room_id, sender, current_invite)
            return
        self._forget_recorded_room_invite(room_id, sender, current_invite)
        if room_id not in self._pending_room_invites:
            await self._clear_join_decrypt_notice_fence(room_id)

    async def _leave_terminal_invite_under_lock(
        self,
        room_id: str,
        sender: str,
        current_invite: _CurrentRoomInvite | None,
    ) -> None:
        """Explicitly leave one rejected joined room and durably settle its work."""
        if not self._pending_room_invite_matches(
            room_id,
            sender,
            PendingRoomInvitePhase.LEAVING,
        ):
            return
        if not await leave_room(self._client(), room_id):
            msg = f"Failed to leave terminally rejected invited room {room_id}"
            raise RuntimeError(msg)
        await self._finish_left_terminal_invite_under_lock(room_id, sender, current_invite)

    def _current_invite_is_authorized(
        self,
        room_id: str,
        sender: str,
        expected_invite: _CurrentRoomInvite | None,
    ) -> bool:
        """Revalidate one exact live invite and its current responder access."""
        return self._current_invite_authorization(room_id, sender, expected_invite) is True

    def _current_invite_authorization(
        self,
        room_id: str,
        sender: str,
        expected_invite: _CurrentRoomInvite | None,
    ) -> bool | None:
        """Evaluate one exact live invite without treating unready grants as denial."""
        current_invite = self._current_room_invites.get(room_id)
        if current_invite != expected_invite:
            return False
        if current_invite is not None and current_invite.inviter_id != sender:
            return False
        if is_sender_allowed_for_agent_invite(
            sender,
            self.deps.agent_name,
            self._config(),
            room_id,
            self.deps.runtime_paths,
            self.deps.runtime.agent_reply_memberships,
            current_inviter_id=(current_invite.inviter_id if current_invite is not None else None),
        ):
            return True
        return self._ordinary_invite_authorization(room_id, sender)
