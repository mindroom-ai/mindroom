"""Room membership and invite lifecycle helpers for one bot runtime."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from time import monotonic
from typing import TYPE_CHECKING, Protocol

import nio

from mindroom.authorization import is_sender_allowed_for_agent_reply_in_room
from mindroom.background_tasks import create_background_task, run_blocking_until_complete
from mindroom.commands.handler import generate_welcome_message_for_room
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.matrix.client_room_admin import get_joined_rooms
from mindroom.matrix.invited_rooms_store import (
    invited_rooms_path,
    is_inviter_allowed,
    load_invited_rooms,
    load_pending_room_invites,
    pending_room_invites_path,
    save_invited_rooms,
    save_pending_room_invites,
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


# Read-modify-write cycles on one accepted-invite ledger run in worker
# threads, so a large ledger never stalls the event loop, serialized per file.
_PENDING_ROOM_INVITES_LOCKS: dict[Path, threading.Lock] = {}
_PENDING_ROOM_INVITES_LOCKS_GUARD = threading.Lock()
# A pending invite whose handling fails is retried by a timed reconciliation
# pass after delays doubling from this one, and after this many failures it is
# not retried again until the invite is delivered again or the process restarts.
_PENDING_INVITE_RETRY_SECONDS = 30.0
_MAX_PENDING_INVITE_ATTEMPTS = 5
# One reconciliation pass handles at most this many rooms, and a pass that
# stops at the bound schedules the next one this soon.
_MAX_PENDING_INVITE_ROOMS_PER_PASS = 32
_PENDING_INVITE_FOLLOW_UP_SECONDS = 1.0


@dataclass(frozen=True)
class _PendingInviteRetry:
    """Consecutive failed reconciliation passes for one pending invite, held only in memory."""

    failures: int
    retry_in_seconds: float
    retry_at: float

    @property
    def abandoned(self) -> bool:
        return self.failures >= _MAX_PENDING_INVITE_ATTEMPTS


def _after_pending_invite_failure(previous: _PendingInviteRetry | None) -> _PendingInviteRetry:
    failures = 1 if previous is None else previous.failures + 1
    delay = _PENDING_INVITE_RETRY_SECONDS * 2 ** (failures - 1)
    return _PendingInviteRetry(failures, delay, monotonic() + delay)


def _update_pending_room_invites(
    path: Path,
    update: Callable[[dict[str, str]], dict[str, str]],
    failure_message: str,
) -> dict[str, str]:
    """Apply one change to fresh durable pending invites and return the result."""
    with _PENDING_ROOM_INVITES_LOCKS_GUARD:
        lock = _PENDING_ROOM_INVITES_LOCKS.setdefault(path, threading.Lock())
    with lock:
        pending_invites = load_pending_room_invites(path)
        updated = update(dict(pending_invites))
        if updated != pending_invites and not save_pending_room_invites(path, updated):
            raise OSError(failure_message)
        return updated


def _without_pending_invite(room_id: str, expected_sender: str | None) -> Callable[[dict[str, str]], dict[str, str]]:
    def forget(pending_invites: dict[str, str]) -> dict[str, str]:
        if room_id in pending_invites and expected_sender in {None, pending_invites[room_id]}:
            pending_invites.pop(room_id)
        return pending_invites

    return forget


@dataclass
class _QueuedLedgerUpdate:
    """One change waiting for the next accepted-invite ledger write."""

    update: Callable[[dict[str, str]], dict[str, str]]
    failure_message: str
    applied: asyncio.Future[None]


@dataclass
class _RoomLock:
    """A per-room lock that is dropped once nobody holds or waits for it."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


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


class BotRoomLifecycle:
    """Own room joins, leaves, invite handling, and invited-room persistence."""

    deps: BotRoomLifecycleDeps
    invited_rooms: set[str]
    _pending_room_invites: dict[str, str]

    def __init__(self, deps: BotRoomLifecycleDeps) -> None:
        self.deps = deps
        self.invited_rooms = self._load_invited_rooms()
        self._pending_room_invites = load_pending_room_invites(self._pending_room_invites_file_path())
        self._pending_forgotten_invited_rooms: set[str] = set()
        self._invite_join_locks: dict[str, _RoomLock] = {}
        self._welcome_locks: dict[str, asyncio.Lock] = {}
        self._welcomed_room_ids: set[str] = set()
        self._decrypt_notice_fenced_room_ids: set[str] = set()
        self._applied_continuity_revision = -1
        self._pending_invite_retries: dict[str, _PendingInviteRetry] = {}
        # Inviters of joins that reported failure; kept only in memory in case
        # the join went through after all, since the ledger holds accepted
        # joins that are in flight or joined.
        self._unconfirmed_join_inviters: dict[str, str] = {}
        self._reconcile_lock = asyncio.Lock()
        self._reconcile_requested = 0
        self._reconcile_completed = 0
        self._pending_invite_retry_timer: asyncio.TimerHandle | None = None
        self._pending_invite_retry_due = 0.0
        self._pending_invite_retries_closed = False
        self._ledger_lock = asyncio.Lock()
        self._queued_ledger_updates: list[_QueuedLedgerUpdate] = []

    def _lock_for_room(self, locks: dict[str, asyncio.Lock], room_id: str) -> asyncio.Lock:
        lock = locks.get(room_id)
        if lock is None:
            lock = asyncio.Lock()
            locks[room_id] = lock
        return lock

    @asynccontextmanager
    async def _invite_join_lock(self, room_id: str) -> AsyncIterator[None]:
        """Serialize one room's invite handling, keeping the lock only while it is in use."""
        room_lock = self._invite_join_locks.setdefault(room_id, _RoomLock())
        room_lock.users += 1
        try:
            async with room_lock.lock:
                yield
        finally:
            room_lock.users -= 1
            if room_lock.users == 0 and self._invite_join_locks.get(room_id) is room_lock:
                del self._invite_join_locks[room_id]

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
        return invited_rooms_path(self.deps.runtime_paths.storage_root, self.deps.agent_name)

    def _pending_room_invites_file_path(self) -> Path:
        """Return the durable path for outstanding invites for this entity."""
        return pending_room_invites_path(self.deps.runtime_paths.storage_root, self.deps.agent_name)

    def _load_invited_rooms(self) -> set[str]:
        """Load invited rooms persisted for one eligible entity."""
        if not self._should_persist_invited_rooms():
            return set()
        return load_invited_rooms(self._invited_rooms_file_path())

    async def _refresh_invited_rooms(self) -> None:
        """Merge durable rooms written by other runtime components into memory."""
        if not self._should_persist_invited_rooms():
            return
        durable_rooms = await asyncio.to_thread(load_invited_rooms, self._invited_rooms_file_path())
        room_ids = durable_rooms | self.invited_rooms
        room_ids.difference_update(self._pending_forgotten_invited_rooms)
        self.invited_rooms = room_ids
        # Nio removes an invitation when its durable join succeeds. Finish
        # local persistence before startup computes which rooms to leave.
        for room_id, sender in self._pending_room_invites.items():
            if self._client_has_joined_room(room_id) and is_inviter_allowed(
                self._config(),
                self.deps.runtime_paths,
                self.deps.agent_name,
                sender,
            ):
                self._remember_invited_room(room_id)

    async def forget_invited_room(self, room_id: str) -> None:
        """Stop preserving an ad-hoc room after this bot leaves it."""
        await self._forget_pending_room_invite(room_id)
        self._pending_invite_retries.pop(room_id, None)
        self._unconfirmed_join_inviters.pop(room_id, None)
        if not self._should_persist_invited_rooms():
            self.invited_rooms.discard(room_id)
        elif not self._update_invited_room(room_id, remember=False):
            msg = f"Failed to forget invited room {room_id}"
            raise OSError(msg)
        self._welcomed_room_ids.discard(room_id)

    async def _apply_pending_room_invites_update(
        self,
        update: Callable[[dict[str, str]], dict[str, str]],
        failure_message: str,
    ) -> None:
        """Rewrite the durable ledger off the event loop and adopt the result.

        One write runs at a time on one worker thread, and every change that
        queued behind it is applied together in the next read-modify-write.
        """
        queued = _QueuedLedgerUpdate(update, failure_message, asyncio.get_running_loop().create_future())
        self._queued_ledger_updates.append(queued)
        try:
            async with self._ledger_lock:
                if not queued.applied.done():
                    await self._write_queued_ledger_updates()
        except asyncio.CancelledError:
            if queued in self._queued_ledger_updates:
                self._queued_ledger_updates.remove(queued)
            raise
        await queued.applied

    async def _write_queued_ledger_updates(self) -> None:
        batch, self._queued_ledger_updates = self._queued_ledger_updates, []

        def apply_batch(pending_invites: dict[str, str]) -> dict[str, str]:
            for queued in batch:
                pending_invites = queued.update(pending_invites)
            return pending_invites

        try:
            self._pending_room_invites = await run_blocking_until_complete(
                _update_pending_room_invites,
                self._pending_room_invites_file_path(),
                apply_batch,
                "Failed to save accepted room invites",
            )
        except Exception as error:
            for queued in batch:
                if isinstance(error, OSError):
                    failure = OSError(queued.failure_message)
                    failure.__cause__ = error
                    queued.applied.set_exception(failure)
                else:
                    queued.applied.set_exception(error)
            return
        except asyncio.CancelledError:
            # The write finished before cancellation propagated, but nobody was
            # told; hand the batch to the next writer, which reapplies these
            # idempotent changes on fresh state and answers every waiter.
            self._queued_ledger_updates[:0] = batch
            raise
        for queued in batch:
            queued.applied.set_result(None)

    async def _record_accepted_invite(self, room_id: str, sender_id: str) -> None:
        """Keep the accepted inviter durably before requesting the join.

        Nio's durable store restores every current invite, but it forgets the
        inviter once the join succeeds, and finishing the join (remembering the
        room and the router's welcome) still needs it after a restart.
        """
        await self._apply_pending_room_invites_update(
            lambda pending_invites: {**pending_invites, room_id: sender_id},
            f"Failed to persist accepted room invite {room_id}",
        )

    async def _forget_pending_room_invite(self, room_id: str, *, expected_sender: str | None = None) -> None:
        """Forget a resolved outstanding invite without losing concurrent state."""
        await self._apply_pending_room_invites_update(
            _without_pending_invite(room_id, expected_sender),
            f"Failed to forget pending room invite {room_id}",
        )

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
        await self._refresh_invited_rooms()
        client = self._client()
        joined_rooms = await get_joined_rooms(client)
        current_rooms = set(joined_rooms or ())
        desired_rooms = set(self.deps.get_configured_rooms())
        desired_rooms.update(self.deps.get_retained_room_ids())
        if self._should_persist_invited_rooms():
            desired_rooms.update(self.invited_rooms)

        for room_id in desired_rooms:
            if room_id in current_rooms:
                self._logger().debug("Already joined room", room_id=room_id)
                if await self.deps.change_membership(room_id, "join"):
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
                await self.deps.on_configured_room_joined(room_id)
            else:
                self._logger().warning("Failed to join room", room_id=room_id)

    async def leave_unconfigured_rooms(self, room_ids: list[str] | None = None) -> None:
        """Leave any rooms this bot is no longer configured for."""
        client = self._client()
        await leave_non_dm_rooms(
            client,
            room_ids if room_ids is not None else await self._rooms_to_leave(),
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
        if self._should_persist_invited_rooms():
            await self._refresh_invited_rooms()
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

    async def handle_invite(self, room: nio.MatrixRoom, sender: str) -> None:
        """Handle one freshly delivered invite, restarting retries that had given up on its room.

        A failure schedules the room's first timed retry before it propagates.
        """
        self._pending_invite_retries.pop(room.room_id, None)
        try:
            await self._handle_invite(room, sender)
        except Exception as error:
            retry = self._pending_invite_failed(room.room_id, sender, error)
            if not retry.abandoned:
                self._arm_pending_invite_retry(retry.retry_at)
            raise

    def cancel_pending_invite_retry(self) -> None:
        """Stop timed retries at shutdown, including ones a finishing pass or handler would arm.

        The next sync loop resumes them; nio and the ledger keep every invite.
        """
        self._pending_invite_retries_closed = True
        timer = self._pending_invite_retry_timer
        self._pending_invite_retry_timer = None
        if timer is not None:
            timer.cancel()

    def resume_pending_invite_retries(self) -> None:
        """Allow timed retries again once the sync loop runs, re-arming any still owed."""
        self._pending_invite_retries_closed = False
        self._schedule_pending_invite_retry()

    def _schedule_pending_invite_retry(self, *, follow_up: bool = False) -> None:
        """Arm the timer for the earliest retry due, or for a pass that stopped at its bound."""
        due_times = [retry.retry_at for retry in self._pending_invite_retries.values() if not retry.abandoned]
        if follow_up:
            due_times.append(monotonic() + _PENDING_INVITE_FOLLOW_UP_SECONDS)
        if due_times:
            self._arm_pending_invite_retry(min(due_times))

    def _arm_pending_invite_retry(self, due: float) -> None:
        """Keep one timer armed for the earliest due time seen."""
        if self._pending_invite_retries_closed:
            return
        timer = self._pending_invite_retry_timer
        if timer is not None:
            if self._pending_invite_retry_due <= due:
                return
            timer.cancel()
        self._pending_invite_retry_due = due
        self._pending_invite_retry_timer = asyncio.get_running_loop().call_later(
            max(0.0, due - monotonic()),
            self._pending_invite_retry_fired,
        )

    def _pending_invite_retry_fired(self) -> None:
        self._pending_invite_retry_timer = None
        if self._pending_invite_retries_closed:
            return
        create_background_task(
            self.reconcile_pending_invites(),
            name=f"pending_invite_retry_{self.deps.agent_name}",
            owner=self.deps.runtime,
        )

    async def reconcile_pending_invites(self) -> None:
        """Re-evaluate current invites and unfinished accepted joins, one pass at a time.

        A request made while a pass runs waits for the next pass, and requests
        that pile up meanwhile share it.
        """
        self._reconcile_requested += 1
        requested = self._reconcile_requested
        async with self._reconcile_lock:
            if self._reconcile_completed >= requested:
                return
            covered = self._reconcile_requested
            await self._reconcile_pending_invites_once()
            self._reconcile_completed = covered

    async def _reconcile_pending_invites_once(self) -> None:
        """Handle nio's current invites and the accepted joins still owed local completion.

        Nio's durable store is the authority on current invites, so accepted
        entries whose room is neither invited nor joined any more are dropped,
        along with their retry state. Each room is handled on its own, at most
        a bounded number per pass, and one that fails is retried by a timed
        pass after doubling delays until reconciliation gives up on it for the
        life of this process.
        """
        client = self._client()
        config = self._config()

        def allowed(sender: str | None) -> bool:
            return sender is not None and is_inviter_allowed(
                config,
                self.deps.runtime_paths,
                self.deps.agent_name,
                sender,
            )

        refused_room_ids = {
            room_id for room_id, room in tuple(client.invited_rooms.items()) if not allowed(room.inviter)
        }
        current_room_ids = set(client.invited_rooms) | set(client.rooms)
        await self._apply_pending_room_invites_update(
            lambda pending_invites: {
                room_id: sender
                for room_id, sender in pending_invites.items()
                if room_id in current_room_ids and room_id not in refused_room_ids
            },
            "Failed to drop resolved accepted room invites",
        )
        for room_id in set(self._unconfirmed_join_inviters) - current_room_ids:
            del self._unconfirmed_join_inviters[room_id]
        joined_inviters = {
            **{
                room_id: sender
                for room_id, sender in self._unconfirmed_join_inviters.items()
                if room_id in client.rooms
            },
            **{room_id: sender for room_id, sender in self._pending_room_invites.items() if room_id in client.rooms},
        }
        owed_room_ids = {
            *(room_id for room_id in client.invited_rooms if room_id not in refused_room_ids),
            *(room_id for room_id, sender in joined_inviters.items() if allowed(sender)),
        }
        for room_id in set(self._pending_invite_retries) - owed_room_ids:
            del self._pending_invite_retries[room_id]
        try:
            await self._handle_owed_invites(client, sorted(owed_room_ids))
        finally:
            self._schedule_pending_invite_retry()

    async def _handle_owed_invites(self, client: nio.AsyncClient, room_ids: Sequence[str]) -> None:
        """Handle each owed room that is due, at most a bounded number, re-reading nio at every step.

        Nio keeps projecting sync while this awaits, so an invite can be
        retracted or turn into a join between rooms; such a room is skipped.
        """
        handled = 0
        for room_id in room_ids:
            retry = self._pending_invite_retries.get(room_id)
            if retry is not None and (retry.abandoned or monotonic() < retry.retry_at):
                continue
            invited = client.invited_rooms.get(room_id)
            room = invited if invited is not None else client.rooms.get(room_id)
            sender = (
                invited.inviter
                if invited is not None
                else self._pending_room_invites.get(room_id, self._unconfirmed_join_inviters.get(room_id))
            )
            if room is None or sender is None:
                continue
            if handled == _MAX_PENDING_INVITE_ROOMS_PER_PASS:
                self._schedule_pending_invite_retry(follow_up=True)
                return
            handled += 1
            try:
                await self._handle_invite(room, sender)
            except Exception as error:
                self._pending_invite_failed(room_id, sender, error)

    def _pending_invite_failed(self, room_id: str, sender: str, error: Exception) -> _PendingInviteRetry:
        """Record when to retry one pending invite, or stop retrying it in this process.

        Giving up changes no durable state: nio keeps the invite, and an
        accepted join keeps its inviter, so a restart retries both.
        """
        retry = _after_pending_invite_failure(self._pending_invite_retries.get(room_id))
        self._pending_invite_retries[room_id] = retry
        if not retry.abandoned:
            self._logger().warning(
                "Pending invite reconciliation failed",
                room_id=room_id,
                sender=sender,
                attempt=retry.failures,
                retry_in_seconds=retry.retry_in_seconds,
                error_type=type(error).__name__,
                error=str(error),
            )
            return retry
        self._logger().warning(
            "Giving up on a pending invite until it is delivered again or the process restarts",
            room_id=room_id,
            sender=sender,
            attempts=retry.failures,
            error_type=type(error).__name__,
            error=str(error),
        )
        return retry

    def _current_inviter(self, room_id: str) -> str | None:
        """Return who Matrix currently says invited this bot, if the invite is still current."""
        current_invite = self._client().invited_rooms.get(room_id)
        return None if current_invite is None else current_invite.inviter

    def _allowed_current_inviter(self, room_id: str) -> str | None:
        """Return the current Matrix inviter when the latest policy allows it."""
        sender = self._current_inviter(room_id)
        if sender is None:
            return None
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

    async def _handle_invite(
        self,
        room: nio.MatrixRoom,
        sender: str,
    ) -> None:
        """Accept one invite when its dedicated invitation policy allows it."""
        async with self._invite_join_lock(room.room_id):
            if not self._should_accept_invite():
                self._logger().info("Ignored invite", room_id=room.room_id, sender=sender)
                return
            joined = self._client_has_joined_room(room.room_id)
            allowed_sender = (
                self._pending_room_invites.get(room.room_id, self._unconfirmed_join_inviters.get(room.room_id))
                if joined
                else self._allowed_current_inviter(room.room_id)
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
                if not joined and room.room_id in self._pending_room_invites:
                    # An accepted join whose invite is gone or now refused owes
                    # no completion. A joined room keeps its inviter so a later
                    # policy change can still finish it.
                    await self._forget_pending_room_invite(room.room_id)
                return
            sender = allowed_sender

            if not joined and not await self._join_current_invitation(room.room_id, sender):
                return

            self._logger().info("Joined room", room_id=room.room_id)
            self._remember_invited_room(room.room_id)
            await self._send_invite_welcome(room.room_id, sender)
            await self._forget_pending_room_invite(room.room_id, expected_sender=sender)
            self._unconfirmed_join_inviters.pop(room.room_id, None)
            self._pending_invite_retries.pop(room.room_id, None)

    async def _join_current_invitation(self, room_id: str, sender: str) -> bool:
        """Authorize a new join at the point Nio takes command ownership."""
        self._logger().info("Received invite", room_id=room_id, sender=sender)
        await self._add_join_decrypt_notice_fence(room_id)
        await self._record_accepted_invite(room_id, sender)

        def invite_is_current() -> bool:
            return self._allowed_current_inviter(room_id) == sender

        if await self.deps.change_membership(room_id, "join", is_authorized=invite_is_current):
            return True
        still_current = invite_is_current()
        await self._forget_pending_room_invite(room_id, expected_sender=sender)
        if not still_current:
            if self._client_has_joined_room(room_id):
                # The join landed although the command reported failure; the
                # next pass finishes it with this inviter.
                self._unconfirmed_join_inviters[room_id] = sender
                self._arm_pending_invite_retry(monotonic() + _PENDING_INVITE_FOLLOW_UP_SECONDS)
                return False
            self._unconfirmed_join_inviters.pop(room_id, None)
            await self._clear_join_decrypt_notice_fence(room_id)
            return False
        # False includes stale position and exhausted HTTP retries; neither
        # proves a terminal rejection, and the join may still have landed.
        # The ledger keeps only joins in flight or joined, so the inviter of
        # this failed one stays in memory while nio still holds the invite.
        self._unconfirmed_join_inviters[room_id] = sender
        self._logger().error("Failed to join room", room_id=room_id)
        msg = f"Failed to join invited room {room_id}"
        raise RuntimeError(msg)
