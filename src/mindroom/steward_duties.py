"""The invisible work a room's steward does for the room: its MindRoom room state and its member hooks.

Every bot builds the duties, and each duty asks whether its bot is the room's steward at the moment it runs.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass, field
from functools import partial
from random import uniform
from typing import TYPE_CHECKING, Any

import nio

from mindroom.background_tasks import create_background_task
from mindroom.bot_runtime_view import RunPhase
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.entity_resolution import prepared_entity_names_by_user_id
from mindroom.hooks import (
    EVENT_ROOM_MEMBER_JOINED,
    EVENT_ROOM_MEMBER_LEFT,
    RoomMemberJoinedContext,
    RoomMemberLeftContext,
    emit,
)
from mindroom.matrix.event_types import MINDROOM_ACCOUNTS_EVENT_TYPE
from mindroom.matrix.room_accounts import reconcile_room_accounts, reconcile_room_router_binding
from mindroom.matrix.room_cleanup import cleanup_all_orphaned_bots
from mindroom.matrix.room_member_joins import (
    RoomMemberJoin,
    RoomMemberLeave,
    emit_room_member_join_at_least_once,
    room_member_left_from_event,
)
from mindroom.matrix.room_thread_mode import reconcile_room_thread_mode
from mindroom.room_settings import on_thread_mode_change

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine

    import structlog

    from mindroom.bot import AgentBot
    from mindroom.bot_room_lifecycle import BotRoomLifecycle
    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.event_journal import PrincipalStore
    from mindroom.hooks import HookContextSupport
    from mindroom.personal_room_lifecycle import PersonalRoomLifecycle
    from mindroom.runtime_protocols import OrchestratorRuntime


def moderator_rights_message(feature: str) -> str:
    """Return what an agent answers when a person's room refuses the state write a feature needs."""
    return f"I need moderator rights in this room to {feature}."


def setting_write_failure_message(taken: bool | None, feature: str) -> str:
    """Return what a command answers when the room did not take a setting: None is a refusal for lack of power."""
    if taken is None:
        return moderator_rights_message(feature)
    return "❌ The room did not take the change. Run the command again to retry."


async def runs_room_hooks(
    orchestrator: OrchestratorRuntime,
    agent_name: str,
    room_id: str,
    event_name: str,
    event_id: str | None,
) -> bool:
    """Return whether the bot of that name runs one emission's room-scoped hooks.

    A Matrix event has one claim, which its first writer keeps on each retry and after a handover: a membership
    event's goes to the steward of that moment, and a message's or reaction's to the first bot that emits it, which
    admitted its sender. Any other emission runs them on the room's steward.
    """
    steward = orchestrator.room_steward(room_id)
    if event_id is None:
        return steward is not None and steward.agent_name == agent_name
    elects = event_name in (EVENT_ROOM_MEMBER_JOINED, EVENT_ROOM_MEMBER_LEFT)
    owner = steward.agent_name if steward is not None and elects else agent_name
    purpose = f"hook:{event_name}"
    return await orchestrator.event_journal_store().claim_room_event(room_id, event_id, purpose, owner) == agent_name


def steward_may_write_state(room_steward: Callable[[str], AgentBot | None], room_id: str, event_type: str) -> bool:
    """Return whether the room's steward may write one state event type, by its own view of the room's power levels."""
    steward = room_steward(room_id)
    client = None if steward is None else steward.client
    room = None if client is None else client.rooms.get(room_id)
    return client is not None and room is not None and room.power_levels.can_user_send_state(client.user_id, event_type)


async def room_state_response_as_steward(
    room_steward: Callable[[str], AgentBot | None],
    room_id: str,
    event_type: str,
    content: dict[str, Any],
    *,
    state_key: str | None = "",
) -> nio.RoomPutStateResponse | nio.RoomPutStateError | None:
    """Write one state event through the room steward's client and return the homeserver's answer, None without one.

    A ``state_key`` of None is the steward's own Matrix ID, the one such key Matrix lets it write.
    """
    steward = room_steward(room_id)
    if steward is None or steward.client is None:
        return None
    key = steward.client.user_id if state_key is None else state_key
    return await steward.client.room_put_state(room_id, event_type, content, state_key=key)


async def put_room_state_as_steward(
    room_steward: Callable[[str], AgentBot | None],
    room_id: str,
    event_type: str,
    content: dict[str, Any],
    *,
    state_key: str | None = "",
) -> bool | None:
    """Write one state event like `room_state_response_as_steward` and return whether it was written.

    None means refused: the room has no steward, or it lacks the power, which only moderator rights can fix.
    """
    response = await room_state_response_as_steward(room_steward, room_id, event_type, content, state_key=state_key)
    if response is None or (isinstance(response, nio.RoomPutStateError) and response.status_code == "M_FORBIDDEN"):
        return None
    return isinstance(response, nio.RoomPutStateResponse)


def republish_thread_mode_as_steward(room_steward: Callable[[str], AgentBot | None], room_id: str) -> None:
    """Have the room's steward republish the thread mode clients see, after a read found its setting changed."""
    if (steward := room_steward(room_id)) is not None:
        steward.steward_duties._thread_mode_setting_changed(room_id)


async def reconcile_held_room_states(runtime: OrchestratorRuntime) -> None:
    """Reconcile the thread mode, MindRoom accounts and binding of every room a bot holds, once, through its steward."""
    bots = runtime.agent_bots.values()
    room_ids = {room_id for bot in bots if bot.client is not None for room_id in bot.client.rooms}
    stewards = ((runtime.room_steward(room_id), room_id) for room_id in room_ids)
    await asyncio.gather(
        *(bot.steward_duties._publish_room_state(room_id) for bot, room_id in stewards if bot is not None),
    )


@dataclass(frozen=True)
class StewardDutiesDeps:
    """The bot's own collaborators its steward duties act through."""

    runtime: BotRuntimeView
    logger: structlog.stdlib.BoundLogger
    hook_context: HookContextSupport
    journal: PrincipalStore
    room_lifecycle: BotRoomLifecycle
    personal_rooms: PersonalRoomLifecycle


@dataclass
class StewardDuties:
    """Publish the room state of the rooms this bot stewards and emit their room-membership hooks.

    The owner account's administration, orphaned-account cleanup and the welcome, also lives here for now.
    """

    deps: StewardDutiesDeps
    # Serializes at-least-once room-member-joined emission across the bot's callbacks.
    _member_join_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    # Rooms whose state is due since the last sync frame; a local join's room has no members until that frame.
    _joined_room_ids: set[str] = field(default_factory=set, init=False, repr=False)
    # The marked-room publishes in flight, which run in the background so no sync frame waits for a room state read.
    _room_state_publishes: set[asyncio.Task[None]] = field(default_factory=set, init=False, repr=False)
    # Warn once per room until reconciliation succeeds.
    _room_state_warned: set[str] = field(default_factory=set, init=False, repr=False)
    # One backoff loop per failed room.
    _room_state_retries: dict[str, asyncio.Task[None]] = field(default_factory=dict, init=False, repr=False)
    # Retire a sleeping timer on recovery, but do not interrupt an attempt already in progress.
    _room_state_retrying: set[str] = field(default_factory=set, init=False, repr=False)
    # Pending operations, including fresh failures that race an in-flight retry.
    _room_state_reruns: dict[str, set[str]] = field(default_factory=dict, init=False, repr=False)
    # Each room's running republish of its thread mode after a change to the setting.
    _thread_mode_republishes: dict[str, asyncio.Task[None]] = field(default_factory=dict, init=False, repr=False)

    @property
    def _is_owner(self) -> bool:
        """Whether this bot is the owner account, which administers the configured rooms."""
        # Owner-only administration (design decision 8): this tests an entity's role, not who is in a room.
        return self.deps.hook_context.agent_name == ROUTER_AGENT_NAME

    def is_steward(self, room_id: str) -> bool:
        """Return whether this bot is the room's steward now."""
        steward = self.deps.runtime.orchestrator.room_steward(room_id)
        return steward is not None and steward.agent_name == self.deps.hook_context.agent_name

    async def started(self, client: nio.AsyncClient) -> None:
        """Have reads that find a room's thread mode changed tell its steward, and clean up as the owner account.

        The owner account removes managed accounts the config no longer has from the rooms it can see.
        """
        runtime = self.deps.runtime
        republish = partial(republish_thread_mode_as_steward, runtime.orchestrator.room_steward)
        on_thread_mode_change(runtime.runtime_paths, republish)
        if not self._is_owner:
            return
        try:
            await cleanup_all_orphaned_bots(client, self.deps.runtime.config, self.deps.runtime.runtime_paths)
        except Exception as e:
            self.deps.logger.warning("orphaned_bot_cleanup_failed", error=str(e))

    def stopping(self) -> None:
        """Cancel the room state publishes and retries, which a stopping bot discards, without waiting.

        The next steward redoes them, and waiting for them would spend the stop budget its replies need to finish.
        """
        for task in (*self._room_state_publishes, *self._room_state_retries.values()):
            task.cancel()

    async def room_joined(self, room_id: str) -> None:
        """Welcome an empty room the owner account joined."""
        if self._is_owner:
            await self.deps.room_lifecycle.send_welcome_message_if_empty(room_id)

    def mark_joined_for_room_state(self, room_id: str) -> None:
        """Publish the state of a room this bot joined or left by any path, once a sync frame carries its members."""
        self._joined_room_ids.add(room_id)

    def _thread_mode_setting_changed(self, room_id: str) -> None:
        """Republish the thread mode of a room this bot stewards, alone, after its `thread_mode` setting changed.

        Every bot that sees the change in sync tells the steward, whose own sync may not show it. The republish reads
        the setting itself and writes only a mode that differs. It runs in the background, so no sync frame or turn
        waits for it, and one republish serves every change told while it runs: it ends only once the mode it
        published is the setting the runtime holds.
        """
        running = self._thread_mode_republishes.get(room_id)
        # A republish that ended, or was cancelled before it ran, serves no later change.
        if running is None or running.done():
            republish = self._publish_in_background(self._publish_room_state(room_id, {"mode"}), "steward_thread_mode")
            self._thread_mode_republishes[room_id] = republish
            republish.add_done_callback(lambda done: self._forget_thread_mode_republish(room_id, done))

    def _forget_thread_mode_republish(self, room_id: str, done: asyncio.Task[None]) -> None:
        if self._thread_mode_republishes.get(room_id) is done:
            del self._thread_mode_republishes[room_id]

    async def power_levels_event(self, room: nio.MatrixRoom, _event: nio.PowerLevelsEvent) -> None:
        """Publish a room's state after its power levels changed, which can change its steward."""
        self._joined_room_ids.add(room.room_id)

    def publish_joined_room_state(self, *, every_room: bool = False) -> None:
        """Reconcile the thread mode and MindRoom accounts of the marked rooms, each through its steward.

        ``every_room`` first marks each room this bot stewards: a sync that starts can bring a power change as state
        alone, which reaches no callback.
        """
        client = self.deps.runtime.client
        if every_room and client is not None:
            self._joined_room_ids.update(room_id for room_id in client.rooms if self.is_steward(room_id))
        room_ids, self._joined_room_ids = self._joined_room_ids, set()
        if room_ids:
            self._publish_in_background(self._publish_rooms(room_ids), "steward_joined_room_state")

    def _publish_in_background(self, publish: Coroutine[Any, Any, None], name: str) -> asyncio.Task[None]:
        task = create_background_task(publish, name=name, owner=self.deps.runtime)
        self._room_state_publishes.add(task)
        task.add_done_callback(self._room_state_publishes.discard)
        return task

    async def _publish_rooms(self, room_ids: set[str]) -> None:
        await asyncio.gather(*(self._publish_room_state(room_id) for room_id in room_ids))

    async def _publish_room_state(self, room_id: str, only: set[str] | None = None) -> None:
        """Reconcile one held room, or ``only`` some of its state, and let transient failures retry without another trigger."""
        queued = self._room_state_reruns.get(room_id)
        failed = await self._reconcile_room_state(room_id, only)
        # Supersede only the older work, which a publish of the whole state redid; concurrent failures replace the set.
        if only is None and queued is not None and self._room_state_reruns.get(room_id) is queued:
            del self._room_state_reruns[room_id]
        if not failed:
            if room_id not in self._room_state_reruns and room_id not in self._room_state_retrying:
                self._room_state_warned.discard(room_id)
                if (timer := self._room_state_retries.pop(room_id, None)) is not None:
                    timer.cancel()
            return
        if self.deps.runtime.phase is not RunPhase.ACCEPTING:
            return
        self._room_state_reruns[room_id] = self._room_state_reruns.get(room_id, set()) | failed
        pending = self._room_state_retries.get(room_id)
        if pending is None or pending.done():
            retry = create_background_task(
                self._retry_room_state(room_id),
                "steward_room_state_retry",
                owner=self.deps.runtime,
            )
            self._room_state_retries[room_id] = retry
            retry.add_done_callback(lambda done: self._retire_room_state_retry(room_id, done))

    def _retire_room_state_retry(self, room_id: str, task: asyncio.Task[None]) -> None:
        if self._room_state_retries.get(room_id) is task:
            del self._room_state_retries[room_id]
            self._room_state_reruns.pop(room_id, None)

    async def _retry_room_state(self, room_id: str) -> None:
        """Retry failed operations with capped backoff, including failures that arrive mid-attempt."""
        runtime = self.deps.runtime
        delay = 1
        while runtime.phase is RunPhase.ACCEPTING and room_id in self._room_state_reruns:
            await asyncio.sleep(uniform(delay * 0.5, min(delay * 1.5, 60)))  # noqa: S311 - retry timing, not security
            if runtime.phase is not RunPhase.ACCEPTING:
                return
            if not (needed := self._room_state_reruns.pop(room_id, None)):
                break
            self._room_state_retrying.add(room_id)
            try:
                failed = await self._reconcile_room_state(room_id, needed, delay)
            finally:
                self._room_state_retrying.discard(room_id)
            if failed:
                self._room_state_reruns[room_id] = self._room_state_reruns.get(room_id, set()) | failed
            delay = min(delay * 2, 60)
        self._room_state_warned.discard(room_id)

    def _may_publish_room_state(self, room_id: str) -> bool:
        """Recheck the election and hand work over to a different steward's next sync frame."""
        steward = self.deps.runtime.orchestrator.room_steward(room_id)
        if steward is not None and steward.agent_name != self.deps.hook_context.agent_name:
            steward.steward_duties.mark_joined_for_room_state(room_id)
            return False
        return steward is not None

    async def _reconcile_room_state(self, room_id: str, needed: set[str] | None = None, delay: int = 0) -> set[str]:
        """Return failed operations; run retries in order so they do not compete for nio's crypto lock."""
        runtime = self.deps.runtime

        def failure_level(timed_out: bool) -> int:
            if room_id in self._room_state_warned:
                return logging.DEBUG
            if timed_out and delay < 60:
                return logging.INFO
            self._room_state_warned.add(room_id)
            return logging.WARNING

        def room_checks() -> dict[str, Callable[..., Awaitable[bool | None]]]:
            client = runtime.client
            if not self._may_publish_room_state(room_id) or client is None or room_id not in client.rooms:
                return {}

            steward_id = client.user_id

            def get_alternate_clients() -> dict[str, nio.AsyncClient]:
                return {
                    other.user_id: other
                    for bot in runtime.orchestrator.agent_bots.values()
                    if bot.running
                    and bot.accepting_work
                    and (other := bot.client) is not None
                    and other.user_id != steward_id
                    and (room := other.rooms.get(room_id)) is not None
                    and room.power_levels.can_user_send_state(other.user_id, MINDROOM_ACCOUNTS_EVENT_TYPE)
                }

            args = (client, runtime.config, runtime.runtime_paths, room_id)
            may_claim = partial(self._may_publish_room_state, room_id)
            return {
                "accounts": partial(reconcile_room_accounts, *args, get_alternate_clients=get_alternate_clients),
                "mode": partial(reconcile_room_thread_mode, *args),
                "binding": partial(reconcile_room_router_binding, client, room_id, may_claim=may_claim),
            }

        if needed is not None:
            return {
                check
                for check in ("mode", "accounts", "binding")
                if check in needed
                and runtime.phase is RunPhase.ACCEPTING
                and (run := room_checks().get(check)) is not None
                and await run(failure_level=failure_level) is False
            }
        checks = room_checks()
        results = await asyncio.gather(*(check(failure_level=failure_level) for check in checks.values()))
        return {check for check, result in zip(checks, results, strict=True) if result is False}

    async def member_event(self, room: nio.MatrixRoom, event: nio.RoomMemberEvent) -> None:
        """Refresh responder presentation, the room's MindRoom accounts and the welcome, and emit human membership hooks."""
        registry = self.deps.hook_context.registry
        runtime = self.deps.runtime
        if event.membership != event.prev_membership and event.state_key in prepared_entity_names_by_user_id(
            runtime.config,
            runtime.runtime_paths,
        ):
            await self._publish_room_state(room.room_id)
            if self._is_owner:
                # An edit can wait on a slow homeserver, so it holds none of the room's later events.
                create_background_task(
                    self.deps.room_lifecycle.refresh_welcome(room.room_id),
                    name="owner_welcome_refresh",
                    owner=self.deps.runtime,
                )
        if registry.has_hooks(EVENT_ROOM_MEMBER_LEFT):
            leave = room_member_left_from_event(room, event, config=runtime.config, runtime_paths=runtime.runtime_paths)
            if leave is not None:
                await self._emit_member_left(leave)
                return
        if not (registry.has_hooks(EVENT_ROOM_MEMBER_JOINED) or self.deps.personal_rooms.onboards_from(room.room_id)):
            return
        await emit_room_member_join_at_least_once(
            room,
            event,
            config=runtime.config,
            runtime_paths=runtime.runtime_paths,
            store=self.deps.journal,
            lock=self._member_join_lock,
            emit=self._emit_member_joined,
        )

    async def _emit_member_joined(self, join: RoomMemberJoin) -> None:
        """Emit room:member_joined for one live human Matrix room join, where this bot holds the event's claim."""
        orchestrator, name = self.deps.runtime.orchestrator, self.deps.hook_context.agent_name
        if not await runs_room_hooks(orchestrator, name, join.room_id, EVENT_ROOM_MEMBER_JOINED, join.event_id):
            return
        await self.deps.personal_rooms.baseline_join(join)
        if self.deps.hook_context.registry.has_hooks(EVENT_ROOM_MEMBER_JOINED):
            await self._emit_member_change(EVENT_ROOM_MEMBER_JOINED, RoomMemberJoinedContext, join)

    async def _emit_member_left(self, leave: RoomMemberLeave) -> None:
        """Emit room:member_left for one live human Matrix room self-leave, where this bot holds the event's claim."""
        orchestrator, name = self.deps.runtime.orchestrator, self.deps.hook_context.agent_name
        if await runs_room_hooks(orchestrator, name, leave.room_id, EVENT_ROOM_MEMBER_LEFT, leave.event_id):
            await self._emit_member_change(EVENT_ROOM_MEMBER_LEFT, RoomMemberLeftContext, leave)

    async def _emit_member_change(
        self,
        event_name: str,
        context_type: type[RoomMemberJoinedContext | RoomMemberLeftContext],
        change: RoomMemberJoin | RoomMemberLeave,
    ) -> None:
        """Emit one membership hook whose context carries the change's fields."""
        hook_context = self.deps.hook_context
        context = context_type(
            **hook_context.base_kwargs(event_name, change.event_id),
            agent_name=hook_context.agent_name,
            **asdict(change),
        )
        await emit(hook_context.registry, event_name, context)
