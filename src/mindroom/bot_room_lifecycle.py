"""Room membership and invite lifecycle helpers for one bot runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import nio

from mindroom.authorization import is_sender_allowed_for_agent_reply_in_room
from mindroom.background_tasks import create_background_task
from mindroom.commands.handler import generate_welcome_message_for_room
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.contacts.store import contact_room_ids
from mindroom.managed_room_notice import managed_room_join_notice
from mindroom.matrix.client_room_admin import get_joined_rooms
from mindroom.matrix.invited_rooms_store import (
    forget_invited_room,
    invited_rooms_path,
    is_inviter_allowed,
    load_pending_room_invites,
    pending_room_invites_path,
    read_invited_rooms,
    remember_invited_room,
    should_accept_invites,
    should_persist_invited_rooms,
    update_pending_room_invites,
)
from mindroom.matrix.rooms import leave_non_dm_rooms
from mindroom.matrix.state import matrix_state_for_runtime
from mindroom.message_target import MessageTarget
from mindroom.runtime_protocols import SupportsClientConfigMemberships  # noqa: TC001
from mindroom.thread_summary.notices import send_welcome_thread_summary

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager
    from pathlib import Path

    import structlog

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.conversation_reads import ConversationReader
    from mindroom.matrix.sync_continuity import SyncContinuityRecord, SyncContinuityStore
    from mindroom.matrix.users import AgentMatrixUser


# An invitation whose join keeps failing stays pending for retry, so keep only
# the newest entries and inviters cannot grow the ledger without bound.
_MAX_PENDING_ROOM_INVITES = 1000


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


class _ChangeRoomMembership(Protocol):
    """Execute one durable local membership action."""

    def __call__(
        self,
        room_id: str,
        target_membership: str,
        *,
        is_authorized: Callable[[], bool] | None = None,
    ) -> Awaitable[bool]:
        """Join or leave through the owned ingestion gateway."""
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
    # Exact durable ownership authorizes rejoining; cleanup exclusions may be broader.
    get_retained_room_ids: Callable[[], set[str]]
    get_cleanup_exclusions: Callable[[], Awaitable[set[str]]]
    send_response: _SendRoomResponse
    change_membership: _ChangeRoomMembership
    admit_response: Callable[[], AbstractAsyncContextManager[None]]
    on_configured_room_joined: Callable[[str], Awaitable[None]]
    # Every successful join, configured or invited, so the bot restores the room's schedules.
    on_room_joined: Callable[[str], Awaitable[None]]
    get_conversation_reader: Callable[[], ConversationReader]


class BotRoomLifecycle:
    """Own room joins, leaves, invite handling, and invited-room persistence."""

    deps: BotRoomLifecycleDeps
    _pending_room_invites: dict[str, str]

    def __init__(self, deps: BotRoomLifecycleDeps) -> None:
        self.deps = deps
        # The file is read wherever invited rooms are needed; reading it here makes an unreadable record fail the
        # bot's construction, as it did when construction loaded the rooms, rather than its first room setup.
        self.invited_room_ids()
        self._pending_room_invites = load_pending_room_invites(self._pending_room_invites_file_path())
        self._invite_join_locks: dict[str, asyncio.Lock] = {}
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

    async def observe_trusted_sync_rooms(self, room_ids: Iterable[str]) -> None:
        """Clear join fences for rooms included in one trusted sync response."""
        record = await asyncio.to_thread(
            self.deps.continuity_store.update_join_fences,
            remove=tuple(room_ids),
        )
        self._apply_continuity_record(record)

    def _apply_continuity_record(self, record: SyncContinuityRecord) -> None:
        """Expose join fences from one already-persisted continuity update."""
        if record.revision <= self._applied_continuity_revision:
            return
        self._applied_continuity_revision = record.revision
        self._decrypt_notice_fenced_room_ids = set(record.pending_join_decrypt_fences)

    async def restore_pending_join_decrypt_fences(self) -> None:
        """Validate durable unfinished-join fences before sync can start."""
        self._apply_continuity_record(await asyncio.to_thread(self.deps.continuity_store.load))
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
        self._apply_continuity_record(record)

    async def _join_room_with_decrypt_notice_fence(
        self,
        room_id: str,
    ) -> bool:
        """Fence decrypt callbacks before a live join can race its first sync."""
        await self._add_join_decrypt_notice_fence(room_id)
        return await self.deps.change_membership(room_id, "join")

    async def _add_join_decrypt_notice_fence(self, room_id: str) -> None:
        """Persist the decrypt-notice fence required before one Matrix join."""
        self._apply_continuity_record(
            await asyncio.to_thread(
                self.deps.continuity_store.update_join_fences,
                add=(room_id,),
            ),
        )

    def _client_has_joined_room(self, room_id: str) -> bool:
        """Return whether the owned client already projects this room as joined."""
        return room_id in self._client().rooms

    async def _clear_join_decrypt_notice_fence(self, room_id: str) -> None:
        """Clear a join fence after the current join work becomes terminal."""
        self._apply_continuity_record(
            await asyncio.to_thread(
                self.deps.continuity_store.update_join_fences,
                remove=(room_id,),
            ),
        )

    def _invited_rooms_file_path(self) -> Path:
        """Return the durable path for invited room IDs for this entity."""
        return invited_rooms_path(self.deps.runtime_paths, self.deps.agent_name)

    def _pending_room_invites_file_path(self) -> Path:
        """Return the durable path for outstanding invites for this entity."""
        return pending_room_invites_path(self.deps.runtime_paths, self.deps.agent_name)

    def invited_room_ids(self) -> frozenset[str]:
        """Return the rooms this entity keeps across restarts because it accepted their invites.

        A personal agent's contact rooms never count, though it may have accepted their invites: it keeps a contact
        room only while the contact is active.
        """
        invited = read_invited_rooms(self.deps.runtime_paths, self._config(), self.deps.agent_name)
        if not invited or self._config().personal_agent_instance(self.deps.agent_name) is None:
            return invited
        return invited - contact_room_ids(self.deps.runtime_paths, self.deps.agent_name, include_removed=True)

    async def _invited_room_ids_after_joined_invites(self) -> frozenset[str]:
        """Return invited rooms once every pending invite this client already joined is persisted.

        Nio removes an invitation when its durable join succeeds, so local persistence finishes before startup computes
        which rooms to join or leave.
        """
        if not self._should_persist_invited_rooms():
            return frozenset()
        for room_id, sender in self._pending_room_invites.items():
            if self._client_has_joined_room(room_id) and is_inviter_allowed(
                self._config(),
                self.deps.runtime_paths,
                self.deps.agent_name,
                sender,
            ):
                await self._remember_invited_room(room_id)
        return await asyncio.to_thread(self.invited_room_ids)

    async def forget_invited_room(self, room_id: str) -> None:
        """Stop preserving an ad-hoc room after this bot leaves it."""
        await self._forget_pending_room_invite(room_id)
        if self._should_persist_invited_rooms():
            await asyncio.to_thread(forget_invited_room, self._invited_rooms_file_path(), room_id)
        self._welcomed_room_ids.discard(room_id)

    async def on_invite(self, room: nio.MatrixRoom, event: nio.InviteEvent) -> None:
        """Act on one invite to this account without journalling it.

        An invite has no Matrix event ID to key durable work on.
        Persist its room and inviter before network work moves to the
        background so policy changes and process restarts can reconcile it.
        """
        if (
            not isinstance(event, nio.InviteMemberEvent)
            or event.state_key != self.deps.agent_user.user_id
            or event.membership != "invite"
        ):
            return
        await self._record_pending_room_invite(room.room_id, event.sender)
        # The bot's runtime owns the handling, so its shutdown drains it.
        create_background_task(self._handle_recorded_invite(room, event.sender), owner=self.deps.runtime)

    async def _record_pending_room_invite(self, room_id: str, sender_id: str) -> None:
        """Persist an outstanding invite before its network work runs."""
        if self._should_persist_invited_rooms():
            await self._record_pending_room_invites({room_id: sender_id})

    async def _record_pending_room_invites(self, invites: Mapping[str, str]) -> None:
        """Merge outstanding invites into fresh durable state off the event loop."""

        def record(pending_invites: dict[str, str]) -> bool:
            changed = False
            for room_id, sender_id in invites.items():
                if pending_invites.get(room_id) != sender_id:
                    # Reinsert so the cap evicts the oldest invitations first.
                    pending_invites.pop(room_id, None)
                    pending_invites[room_id] = sender_id
                    changed = True
            for room_id in tuple(pending_invites)[: max(0, len(pending_invites) - _MAX_PENDING_ROOM_INVITES)]:
                del pending_invites[room_id]
            return changed

        self._pending_room_invites = await asyncio.to_thread(
            update_pending_room_invites,
            self._pending_room_invites_file_path(),
            record,
            "Failed to persist pending room invites",
        )

    async def _forget_pending_room_invite(self, room_id: str, *, expected_sender: str | None = None) -> None:
        """Forget a resolved outstanding invite without losing concurrent state."""

        def forget(pending_invites: dict[str, str]) -> bool:
            if room_id not in pending_invites or (
                expected_sender is not None and pending_invites[room_id] != expected_sender
            ):
                return False
            del pending_invites[room_id]
            return True

        self._pending_room_invites = await asyncio.to_thread(
            update_pending_room_invites,
            self._pending_room_invites_file_path(),
            forget,
            f"Failed to forget pending room invite {room_id}",
        )

    async def _remember_invited_room(self, room_id: str) -> None:
        """Persist one accepted invite or fail so its durable intent can retry."""
        if self._should_persist_invited_rooms():
            await asyncio.to_thread(remember_invited_room, self._invited_rooms_file_path(), room_id)

    async def _send_invite_welcome(self, room_id: str, sender: str) -> None:
        """Finish router welcome delivery or leave the invite retryable."""
        if self.deps.agent_name != ROUTER_AGENT_NAME:
            return
        if await self.send_welcome_message_if_empty(room_id, sender):
            return
        msg = f"Failed to complete welcome message for {room_id}"
        raise RuntimeError(msg)

    async def _send_managed_room_notice(self, room: nio.MatrixRoom, sender: str) -> None:
        """Tell the room once that its configuration does not let this agent answer there."""
        notice = managed_room_join_notice(
            room,
            agent_name=self.deps.agent_name,
            agent_user_id=self.deps.agent_user.user_id,
            inviter=sender,
            config=self._config(),
            runtime_paths=self.deps.runtime_paths,
            membership_index=self.deps.runtime.agent_reply_memberships,
        )
        if notice is None:
            return
        target = MessageTarget.resolve(room_id=room.room_id, thread_id=None, reply_to_event_id=None, room_mode=True)
        async with self.deps.admit_response():
            event_id = await self.deps.send_response(target=target, response_text=notice, skip_mentions=True)
        if event_id is None:
            self._logger().warning("Managed room notice delivery failed", room_id=room.room_id)

    async def join_configured_rooms(self) -> None:
        """Join all rooms this bot should preserve across restarts."""
        invited_rooms = await self._invited_room_ids_after_joined_invites()
        client = self._client()
        joined_rooms = await get_joined_rooms(client)
        current_rooms = set(joined_rooms or ())
        desired_rooms = set(self.deps.get_configured_rooms())
        desired_rooms.update(self.deps.get_retained_room_ids())
        desired_rooms.update(invited_rooms)

        for room_id in desired_rooms:
            if room_id in current_rooms:
                self._logger().debug("Already joined room", room_id=room_id)
                if await self.deps.change_membership(room_id, "join"):
                    await self.deps.on_room_joined(room_id)
                    await self.deps.on_configured_room_joined(room_id)
                else:
                    self._logger().warning(
                        "Failed to reconcile joined room",
                        room_id=room_id,
                    )
                continue

            if await self._join_room_with_decrypt_notice_fence(room_id):
                current_rooms.add(room_id)
                self._logger().info("Joined room", room_id=room_id)
                await self.deps.on_room_joined(room_id)
                await self.deps.on_configured_room_joined(room_id)
            else:
                self._logger().warning("Failed to join room", room_id=room_id)

    async def leave_unconfigured_rooms(self) -> None:
        """Leave any rooms this bot is no longer configured for."""
        client = self._client()
        await leave_non_dm_rooms(
            client,
            await self._rooms_to_leave(),
            leave_room_action=lambda room_id: self.deps.change_membership(
                room_id,
                "leave",
            ),
        )

    async def leave_all_rooms(self, *, timeout_seconds: float) -> None:
        """Leave non-DM rooms before sync stops, bounding durable command waits."""
        client = self._client()
        deadline = asyncio.get_running_loop().time() + timeout_seconds

        async def leave(room_id: str) -> bool:
            try:
                async with asyncio.timeout_at(deadline):
                    return await self.deps.change_membership(room_id, "leave")
            except TimeoutError:
                self._logger().warning("matrix_removal_leave_timeout", room_id=room_id)
                return False

        try:
            joined_rooms = await get_joined_rooms(client)
            if joined_rooms:
                await leave_non_dm_rooms(
                    client,
                    joined_rooms,
                    leave_room_action=leave,
                )
        except Exception:
            self._logger().exception("Error leaving rooms during cleanup")

    async def _rooms_to_leave(self) -> list[str]:
        """Return joined rooms this bot should now leave before DM filtering."""
        client = self._client()
        joined_rooms = await get_joined_rooms(client)
        if joined_rooms is None:
            return []

        current_rooms = set(joined_rooms)
        configured_rooms = set(self.deps.get_configured_rooms())
        configured_rooms.update(await self.deps.get_cleanup_exclusions())
        configured_rooms.update(await self._invited_room_ids_after_joined_invites())
        # A contact room is kept while its contact is active, whatever this agent's invite policy says.
        contact_rooms = await asyncio.to_thread(contact_room_ids, self.deps.runtime_paths, self.deps.agent_name)
        configured_rooms.update(contact_rooms)
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
        if visible_to_sender_id is None:
            if room_id in self.invited_room_ids() and room_id not in self.deps.get_configured_rooms():
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
        try:
            await send_welcome_thread_summary(
                self._client(),
                room_id,
                event_id,
                self.deps.get_conversation_reader(),
            )
        except Exception:
            self._logger().exception("Welcome thread summary send failed", room_id=room_id)
        return True

    async def reconcile_pending_invites(self) -> None:
        """Re-evaluate durable and cached invites after configuration changes.

        Each room is handled on its own, so one whose join keeps failing is
        logged and stays pending without stopping the rest of the pass.
        """
        client = self._client()
        await self._record_pending_room_invites(
            {
                room.room_id: room.inviter
                for room in tuple(client.invited_rooms.values())
                if room.inviter is not None
                and is_inviter_allowed(
                    self._config(),
                    self.deps.runtime_paths,
                    self.deps.agent_name,
                    room.inviter,
                )
            },
        )
        for room_id, sender in tuple(self._pending_room_invites.items()):
            room = client.invited_rooms.get(room_id)
            if room is None:
                room = nio.MatrixInvitedRoom(room_id, self.deps.agent_user.user_id)
                room.inviter = sender
            try:
                await self._handle_recorded_invite(room, sender)
            except Exception:
                self._logger().exception("Pending invite reconciliation failed", room_id=room_id, sender=sender)

    def _allowed_current_inviter(self, room_id: str) -> str | None:
        """Return the current Matrix inviter when the latest policy allows it."""
        current_invite = self._client().invited_rooms.get(room_id)
        if current_invite is None or current_invite.inviter is None:
            return None
        sender = current_invite.inviter
        return (
            sender
            if is_inviter_allowed(
                self._config(),
                self.deps.runtime_paths,
                self.deps.agent_name,
                sender,
            )
            else None
        )

    async def _handle_recorded_invite(self, room: nio.MatrixRoom, sender: str) -> None:
        """Accept one recorded invite when its dedicated invitation policy allows it."""
        async with self._lock_for_room(self._invite_join_locks, room.room_id):
            if not self._should_accept_invite():
                self._logger().info("Ignored invite", room_id=room.room_id, sender=sender)
                return
            joined = self._client_has_joined_room(room.room_id)
            allowed_sender = (
                self._pending_room_invites.get(room.room_id) if joined else self._allowed_current_inviter(room.room_id)
            )
            if (
                joined
                and allowed_sender is not None
                and not is_inviter_allowed(
                    self._config(),
                    self.deps.runtime_paths,
                    self.deps.agent_name,
                    allowed_sender,
                )
            ):
                allowed_sender = None
            if allowed_sender is None:
                self._logger().debug(
                    "ignoring_invite_from_disallowed_sender",
                    user_id=sender,
                    room_id=room.room_id,
                )
                if not joined:
                    # A refused or withdrawn invitation can never be joined, and
                    # keeping its entry would only let inviters grow the ledger.
                    await self._forget_pending_room_invite(room.room_id, expected_sender=sender)
                return
            sender = allowed_sender

            if not joined and not await self._join_current_invitation(room.room_id, sender):
                # The invitation was withdrawn or replaced before the join; a
                # replacement keeps its own entry for its own handler.
                await self._forget_pending_room_invite(room.room_id, expected_sender=sender)
                return

            self._logger().info("Joined room", room_id=room.room_id)
            await self._remember_invited_room(room.room_id)
            await self._send_invite_welcome(room.room_id, sender)
            if not joined:
                await self._send_managed_room_notice(room, sender)
            await self.deps.on_room_joined(room.room_id)
            await self._forget_pending_room_invite(room.room_id, expected_sender=sender)

    async def _join_current_invitation(self, room_id: str, sender: str) -> bool:
        """Authorize a new join at the point Nio takes command ownership."""
        self._logger().info("Received invite", room_id=room_id, sender=sender)
        await self._add_join_decrypt_notice_fence(room_id)

        def invite_is_current() -> bool:
            return self._allowed_current_inviter(room_id) == sender

        if await self.deps.change_membership(room_id, "join", is_authorized=invite_is_current):
            return True
        if not invite_is_current():
            await self._clear_join_decrypt_notice_fence(room_id)
            return False
        # False includes stale position and exhausted HTTP retries;
        # neither proves a terminal rejection of this invitation.
        self._logger().error("Failed to join room", room_id=room_id)
        msg = f"Failed to join invited room {room_id}"
        raise RuntimeError(msg)
