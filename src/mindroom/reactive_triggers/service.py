"""ReactiveTriggerService runtime: live tag observer, room message poller, and async delivery drainer."""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Literal

import nio

from mindroom.authorization import ReplyMembershipPendingError, is_sender_allowed_for_agent_reply_in_room
from mindroom.background_tasks import SupervisedLoop
from mindroom.event_journal import ConversationCursor
from mindroom.logging_config import get_logger
from mindroom.matrix.agent_authored import carries_agent_authored_marker
from mindroom.matrix.conversation_reads import projected_visible_messages
from mindroom.matrix.room_reconciliation import RoomStateSnapshot, read_room_state
from mindroom.matrix.transport_progress import is_unfinished_reply
from mindroom.reactive_triggers.dispatch import (
    DispatchResult,
    check_reactive_source_access,
    dispatch_reactive_trigger,
)
from mindroom.reactive_triggers.matching import is_reactive_trigger_event_content, matches_room_message
from mindroom.reactive_triggers.models import (
    FiringRecord,
    ReactiveTriggerRecord,
    RoomCursor,
    RoomMessageSourceSpec,
    ThreadTagSourceSpec,
    TriggerEvent,
)
from mindroom.reactive_triggers.store import ReactiveTriggerStore, ReactiveTriggerStoreError
from mindroom.reactive_triggers.triage import evaluate_triage
from mindroom.requester_identity import equivalent_requester_ids, is_human_requester_id
from mindroom.thread_tags import (
    THREAD_TAGS_EVENT_TYPE,
    parse_thread_tag_record,
    parse_thread_tag_state_key,
)

if TYPE_CHECKING:
    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.bot import AgentBot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage

logger = get_logger(__name__)

_POLL_INTERVAL_SECONDS = 5.0
_PRUNE_INTERVAL_SECONDS = 3600.0
_RESTORE_RETRY_DELAY_SECONDS = 30.0
_READ_TIMEOUT_SECONDS = 60.0
_UNFINISHED_WAIT_SECONDS = 300.0
_REFETCH_WAIT_SECONDS = 600.0
_ROOM_MEMBER_CACHE_TTL_SECONDS = 30.0
_MAX_MESSAGES_PER_POLL = 50
_MAX_FIRINGS_PER_DRAIN = 50
_STORE_PERSIST_ERRORS = (ReactiveTriggerStoreError, sqlite3.Error)

_MessageDecision = Literal["allow", "skip", "hold"]


def _reactive_enabled(config: Config) -> bool:
    """Return whether reactive triggers are currently enabled in policy."""
    return config.external_trigger_policy.enabled and config.external_trigger_policy.reactive_enabled


def _is_rule_expired(rule: ReactiveTriggerRecord, now: float) -> bool:
    """Return whether a rule has an expires_at timestamp at or before now."""
    return rule.expires_at is not None and now >= rule.expires_at


def _joined_members_from_snapshot(room_state: RoomStateSnapshot) -> frozenset[str]:
    """Extract currently joined Matrix user IDs from an authoritative room state snapshot."""
    return frozenset(
        state_key
        for (event_type, state_key), content in room_state.events.items()
        if event_type == "m.room.member" and content.get("membership") == "join"
    )


def _joined_members_from_raw_events(events: Sequence[object]) -> frozenset[str]:
    """Extract currently joined Matrix user IDs from raw RoomGetStateResponse state events."""
    joined: set[str] = set()
    for raw_event in events:
        if not isinstance(raw_event, Mapping):
            continue
        if raw_event.get("type") != "m.room.member":
            continue
        state_key = raw_event.get("state_key")
        content = raw_event.get("content")
        if isinstance(state_key, str) and isinstance(content, Mapping) and content.get("membership") == "join":
            joined.add(state_key)
    return frozenset(joined)


def _owner_joined_in_members(
    joined_members: frozenset[str],
    owner_user_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> bool:
    """Return whether the rule owner (or an equivalent human alias) is in the joined member set."""
    return any(user_id in joined_members for user_id in equivalent_requester_ids(owner_user_id, config, runtime_paths))


def _extract_thread_tag_observation(
    source: Mapping[str, object],
) -> tuple[str, str, bool, str | None, str | None] | None:
    """Parse a raw Matrix thread tag state event into (thread_root_id, tag, tag_present, event_id, actor)."""
    if source.get("type") != THREAD_TAGS_EVENT_TYPE:
        return None
    parsed = parse_thread_tag_state_key(source.get("state_key"))
    if parsed is None:
        return None
    thread_root_id, tag = parsed
    tag_present = parse_thread_tag_record(tag, source.get("content")) is not None
    raw_event_id = source.get("event_id")
    state_event_id = raw_event_id if isinstance(raw_event_id, str) and raw_event_id else None
    raw_sender = source.get("sender")
    actor = raw_sender if isinstance(raw_sender, str) and raw_sender else None
    return thread_root_id, tag, tag_present, state_event_id, actor


def _snapshot_thread_tags(
    events: Sequence[object],
) -> dict[tuple[str, str], tuple[bool, str | None, str | None]]:
    """Index raw room state events by (thread_root_id, tag) -> (present, state_event_id, actor)."""
    snapshot_tags: dict[tuple[str, str], tuple[bool, str | None, str | None]] = {}
    for raw_event in events:
        if not isinstance(raw_event, Mapping):
            continue
        observed = _extract_thread_tag_observation(raw_event)
        if observed is None:
            continue
        t_root, t_tag, present, ev_id, sender = observed
        snapshot_tags[(t_root, t_tag)] = (present, ev_id, sender)
    return snapshot_tags


class ReactiveTriggerService:
    """Runtime service coordinating reactive trigger observers, room polling, triage, and dispatch."""

    name = "reactive_triggers"

    def __init__(
        self,
        *,
        runtime_paths: RuntimePaths,
        config_provider: Callable[[], Config],
        bots: Callable[[], Mapping[str, AgentBot]],
        memberships: AgentReplyMembershipIndex,
        store: ReactiveTriggerStore | None = None,
    ) -> None:
        """Initialize the reactive trigger service and its supervised delivery loop."""
        self.runtime_paths = runtime_paths
        self.config_provider = config_provider
        self.bots = bots
        self.memberships = memberships
        if store is not None:
            self._store: ReactiveTriggerStore | None = store
        elif runtime_paths.control_state_root is not None:
            self._store = ReactiveTriggerStore(runtime_paths)
        else:
            self._store = None

        self._poll_interval_seconds = _POLL_INTERVAL_SECONDS
        self._wake_event = asyncio.Event()
        self._running_loop: asyncio.AbstractEventLoop | None = None
        self._loop = SupervisedLoop(self.name, self._run)

        self._restored_rooms: set[str] = set()
        self._restore_locks: dict[str, asyncio.Lock] = {}
        self._restore_retries: dict[str, asyncio.Task[None]] = {}
        self._reconcile_task: asyncio.Task[None] | None = None
        self._live_during_restore: dict[str, set[tuple[str, str]]] = {}

        self._unfinished_waits: dict[str, tuple[str, float]] = {}
        self._refetch_waits: dict[str, tuple[str, float]] = {}
        self._room_member_cache: dict[str, tuple[frozenset[str], float]] = {}

        self._in_flight_firing_ids: set[int] = set()
        self._in_flight_trigger_ids: set[str] = set()

    @property
    def store(self) -> ReactiveTriggerStore | None:
        """Return the backing reactive trigger store when running in primary control state."""
        return self._store

    async def sync(self, config: Config) -> None:
        """Start or stop the background loop to match the active reactive trigger policy."""
        if self._store is not None and _reactive_enabled(config):
            self.start()
            self._schedule_thread_tag_reconcile()
            self.notify_work()
        else:
            await self.stop()

    def start(self) -> None:
        """Start the supervised background loop unless it is already running."""
        if self._store is None:
            return
        self._loop.start()

    async def stop(self) -> None:
        """Stop the background loop, cancel reconcile/restore tasks, and reset transient wait/restore caches."""
        reconcile_task = self._reconcile_task
        self._reconcile_task = None
        if reconcile_task is not None:
            reconcile_task.cancel()
        retries = list(self._restore_retries.values())
        self._restore_retries.clear()
        for retry in retries:
            retry.cancel()
        tasks_to_await = [*retries, *([reconcile_task] if reconcile_task is not None else [])]
        if tasks_to_await:
            await asyncio.gather(*tasks_to_await, return_exceptions=True)
        await self._loop.stop()
        self._restored_rooms.clear()
        self._refetch_waits.clear()
        self._unfinished_waits.clear()
        self._room_member_cache.clear()

    def notify_work(self) -> None:
        """Wake the delivery loop immediately when new reactive trigger work is enqueued."""
        running_loop = self._running_loop
        if running_loop is not None and not running_loop.is_closed():
            try:
                current_loop = asyncio.get_running_loop()
            except RuntimeError:
                current_loop = None
            if current_loop is running_loop:
                self._wake_event.set()
            else:
                running_loop.call_soon_threadsafe(self._wake_event.set)
            return
        self._wake_event.set()

    def _schedule_thread_tag_reconcile(self) -> None:
        """Schedule background thread_tag reconciliation without blocking config hot-reload."""
        if self._reconcile_task is not None and not self._reconcile_task.done():
            return
        task = asyncio.create_task(self._reconcile_unrestored_thread_tag_rooms())
        self._reconcile_task = task

        def _clear_task(done_task: asyncio.Task[None]) -> None:
            if self._reconcile_task is done_task:
                self._reconcile_task = None

        task.add_done_callback(_clear_task)

    async def _reconcile_unrestored_thread_tag_rooms(self) -> None:
        """Reconcile joined rooms with enabled thread_tag rules that are not yet marked restored."""
        if self._store is None:
            return
        try:
            now = time.time()
            rules = await asyncio.to_thread(
                self._store.list_triggers,
                include_disabled=False,
                source_type="thread_tag",
            )
            room_ids = sorted(
                {
                    rule.source.room_id
                    for rule in rules
                    if isinstance(rule.source, ThreadTagSourceSpec)
                    and not _is_rule_expired(rule, now)
                    and rule.source.room_id not in self._restored_rooms
                },
            )
            for room_id in room_ids:
                bot = self._pick_bot_for_room(room_id)
                if bot is not None:
                    await self.restore_room(bot, room_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Reactive trigger thread_tag sync reconciliation failed")

    async def _run(self) -> None:
        """Poll room messages on a fixed cadence, drain due firings immediately on wakeup, and prune hourly."""
        self._running_loop = asyncio.get_running_loop()
        next_poll_at = 0.0
        next_prune_at = time.monotonic() + _PRUNE_INTERVAL_SECONDS
        try:
            while True:
                now_mono = time.monotonic()
                if now_mono >= next_poll_at:
                    try:
                        await self.poll_room_messages_once()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("Reactive trigger room message poll failed")
                    next_poll_at = time.monotonic() + self._poll_interval_seconds

                self._wake_event.clear()
                try:
                    await self.drain_due_firings_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Reactive trigger firing drain failed")

                now_mono = time.monotonic()
                if now_mono >= next_prune_at:
                    try:
                        await self._prune_old_firings()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("Reactive trigger firing prune failed")
                    next_prune_at = time.monotonic() + _PRUNE_INTERVAL_SECONDS

                wait_seconds = max(0.0, min(self._poll_interval_seconds, next_poll_at - time.monotonic()))
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(wait_seconds):
                        await self._wake_event.wait()
        finally:
            self._running_loop = None

    async def _prune_old_firings(self) -> int:
        """Prune finished firing ledger rows older than retention."""
        if self._store is None:
            return 0
        return await asyncio.to_thread(self._store.prune_old_firings)

    def observe_state_event(self, room_id: str, source: Mapping[str, object]) -> None:
        """Observe one live raw Matrix state event, invalidating member cache or updating thread_tag rules."""
        if source.get("type") == "m.room.member":
            self._room_member_cache.pop(room_id, None)
            return
        if self._store is None:
            return
        config = self.config_provider()
        if not _reactive_enabled(config):
            return
        observed = _extract_thread_tag_observation(source)
        if observed is None:
            return
        thread_root_id, tag, tag_present, state_event_id, actor = observed
        live_keys = self._live_during_restore.get(room_id)
        if live_keys is not None:
            live_keys.add((thread_root_id, tag))
        self._handle_thread_tag_observation(
            room_id=room_id,
            thread_root_id=thread_root_id,
            tag=tag,
            tag_present=tag_present,
            state_event_id=state_event_id,
            actor=actor,
            config=config,
        )

    def _known_room_members_sync(self, room_id: str) -> frozenset[str] | None:
        """Return currently known joined members for room_id from fresh cache or populated room sync state."""
        now_mono = time.monotonic()
        cached = self._room_member_cache.get(room_id)
        if cached is not None and now_mono - cached[1] < _ROOM_MEMBER_CACHE_TTL_SECONDS:
            return cached[0]
        for _bot, client in self._joined_bots(room_id):
            room = client.rooms.get(room_id)
            users = getattr(room, "users", None)
            if isinstance(users, Mapping) and users:
                return frozenset(str(uid) for uid in users)
        return None

    def _handle_thread_tag_observation(
        self,
        *,
        room_id: str,
        thread_root_id: str,
        tag: str,
        tag_present: bool,
        state_event_id: str | None,
        actor: str | None,
        config: Config,
    ) -> None:
        """Reconcile one thread tag state observation against all matching thread_tag rules (enabled & disabled)."""
        if self._store is None:
            return
        now = time.time()
        known_members = self._known_room_members_sync(room_id)
        rules = self._store.list_triggers(
            include_disabled=True,
            source_type="thread_tag",
            source_room_id=room_id,
        )
        for rule in rules:
            if not isinstance(rule.source, ThreadTagSourceSpec):
                continue
            if rule.source.thread_id != thread_root_id or rule.source.tag.lower() != tag:
                continue
            allow_enqueue = rule.enabled and not _is_rule_expired(rule, now)
            if allow_enqueue and not is_human_requester_id(rule.owner_user_id, config, self.runtime_paths):
                allow_enqueue = False
            if (
                allow_enqueue
                and known_members is not None
                and not _owner_joined_in_members(known_members, rule.owner_user_id, config, self.runtime_paths)
            ):
                allow_enqueue = False
            self._handle_thread_tag_observation_for_rule(
                rule=rule,
                room_id=room_id,
                thread_root_id=thread_root_id,
                tag=tag,
                tag_present=tag_present,
                state_event_id=state_event_id,
                actor=actor,
                now=now,
                allow_enqueue=allow_enqueue,
            )

    def _handle_thread_tag_observation_for_rule(
        self,
        *,
        rule: ReactiveTriggerRecord,
        room_id: str,
        thread_root_id: str,
        tag: str,
        tag_present: bool,
        state_event_id: str | None,
        actor: str | None,
        now: float,
        allow_enqueue: bool = True,
    ) -> None:
        """Apply transition logic for one thread_tag rule and atomically update checkpoint + optional firing."""
        assert self._store is not None
        assert isinstance(rule.source, ThreadTagSourceSpec)

        checkpoint = self._store.get_thread_checkpoint(rule.trigger_id)
        if not allow_enqueue or checkpoint is None:
            self._store.update_thread_checkpoint_and_enqueue(
                rule.trigger_id,
                tag_present=tag_present,
                state_event_id=state_event_id,
                firing_dedupe_key=None,
                event=None,
                expected_rule_uid=rule.rule_uid,
            )
            return

        if checkpoint.tag_present == tag_present:
            if state_event_id != checkpoint.state_event_id:
                self._store.update_thread_checkpoint_and_enqueue(
                    rule.trigger_id,
                    tag_present=tag_present,
                    state_event_id=state_event_id,
                    firing_dedupe_key=None,
                    event=None,
                    expected_rule_uid=rule.rule_uid,
                )
            return

        is_added = not checkpoint.tag_present and tag_present
        is_removed = checkpoint.tag_present and not tag_present
        matches_transition = (rule.source.transition == "added" and is_added) or (
            rule.source.transition == "removed" and is_removed
        )
        if not matches_transition:
            self._store.update_thread_checkpoint_and_enqueue(
                rule.trigger_id,
                tag_present=tag_present,
                state_event_id=state_event_id,
                firing_dedupe_key=None,
                event=None,
                expected_rule_uid=rule.rule_uid,
            )
            return

        dedupe_key = state_event_id or (
            f"tag:{room_id}:{thread_root_id}:{tag}:{rule.source.transition}:{int(now * 1000)}"
        )
        event = TriggerEvent(
            source_type="thread_tag",
            source_id=room_id,
            kind=f"thread_tag.{rule.source.transition}",
            event_id=dedupe_key,
            timestamp=now,
            room_id=room_id,
            thread_id=thread_root_id,
            actor=actor,
            data={
                "room_id": room_id,
                "thread_id": thread_root_id,
                "tag": tag,
                "transition": rule.source.transition,
                "state_event_id": state_event_id,
            },
        )
        enqueued = self._store.update_thread_checkpoint_and_enqueue(
            rule.trigger_id,
            tag_present=tag_present,
            state_event_id=state_event_id,
            firing_dedupe_key=dedupe_key,
            event=event,
            expected_rule_uid=rule.rule_uid,
        )
        if enqueued:
            self.notify_work()

    async def restore_room(self, bot: AgentBot, room_id: str) -> None:
        """Reconcile thread_tag rules against room state on startup/join without reading rooms lacking rules."""
        client = bot.client
        if self._store is None or client is None or not _reactive_enabled(self.config_provider()):
            return
        if self._still_watched(bot, room_id):
            return
        if not await self._enabled_thread_tag_rules_for_room(room_id, now=time.time()):
            return

        async with self._restore_locks.setdefault(room_id, asyncio.Lock()):
            await self._restore_room_locked(bot, client, room_id)

    async def _restore_room_locked(
        self,
        bot: AgentBot,
        client: nio.AsyncClient,
        room_id: str,
    ) -> None:
        """Read room state under the per-room lock and reconcile thread_tag rules not touched live."""
        if self._still_watched(bot, room_id):
            return
        rules = await self._enabled_thread_tag_rules_for_room(room_id, now=time.time())
        if not rules:
            return

        self._restored_rooms.discard(room_id)
        live_keys = self._live_during_restore[room_id] = set()
        try:
            response = await client.room_get_state(room_id)
            if not isinstance(response, nio.RoomGetStateResponse):
                logger.warning("reactive_triggers_room_state_unread", room_id=room_id, error=str(response))
                self._retry_restore_later(room_id)
                return
            self._reconcile_room_state_snapshot(room_id, rules, response.events, live_keys)
            self._restored_rooms.add(room_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("reactive_triggers_restore_failed", agent=bot.agent_name, room_id=room_id)
            self._retry_restore_later(room_id)
        finally:
            self._live_during_restore.pop(room_id, None)

    def _reconcile_room_state_snapshot(
        self,
        room_id: str,
        rules: Sequence[ReactiveTriggerRecord],
        events: Sequence[object],
        live_keys: set[tuple[str, str]],
    ) -> None:
        """Apply a room state snapshot to every enabled thread_tag rule not superseded by live events."""
        config = self.config_provider()
        snapshot_tags = _snapshot_thread_tags(events)
        joined_members = _joined_members_from_raw_events(events)
        self._room_member_cache[room_id] = (joined_members, time.monotonic())
        reconcile_now = time.time()
        for rule in rules:
            if not isinstance(rule.source, ThreadTagSourceSpec):
                continue
            key = (rule.source.thread_id, rule.source.tag.lower())
            if key in live_keys:
                continue
            tag_present, state_event_id, actor = snapshot_tags.get(key, (False, None, None))
            allow_enqueue = is_human_requester_id(
                rule.owner_user_id,
                config,
                self.runtime_paths,
            ) and _owner_joined_in_members(
                joined_members,
                rule.owner_user_id,
                config,
                self.runtime_paths,
            )
            self._handle_thread_tag_observation_for_rule(
                rule=rule,
                room_id=room_id,
                thread_root_id=rule.source.thread_id,
                tag=key[1],
                tag_present=tag_present,
                state_event_id=state_event_id,
                actor=actor,
                now=reconcile_now,
                allow_enqueue=allow_enqueue,
            )

    async def _enabled_thread_tag_rules_for_room(self, room_id: str, *, now: float) -> list[ReactiveTriggerRecord]:
        """Return enabled, unexpired thread_tag rules watching room_id."""
        assert self._store is not None
        rules = await asyncio.to_thread(
            self._store.list_triggers,
            include_disabled=False,
            source_type="thread_tag",
            source_room_id=room_id,
        )
        return [
            rule for rule in rules if isinstance(rule.source, ThreadTagSourceSpec) and not _is_rule_expired(rule, now)
        ]

    def _retry_restore_later(self, room_id: str) -> None:
        """Schedule a retry for room state restoration unless one is already pending."""
        pending = self._restore_retries.get(room_id)
        if pending is None or pending is asyncio.current_task():
            self._restore_retries[room_id] = asyncio.create_task(self._retry_restore(room_id))

    async def _retry_restore(self, room_id: str) -> None:
        """Retry restoring one room after a transient state read failure."""
        try:
            await asyncio.sleep(_RESTORE_RETRY_DELAY_SECONDS)
            joined = next(self._joined_bots(room_id), None)
            if joined is None or room_id in self._restored_rooms:
                return
            await self.restore_room(joined[0], room_id)
        finally:
            if self._restore_retries.get(room_id) is asyncio.current_task():
                del self._restore_retries[room_id]

    def _still_watched(self, bot: AgentBot, room_id: str) -> bool:
        """Return whether room_id was restored and another running bot is currently joined to it."""
        return room_id in self._restored_rooms and any(
            other is not bot for other, _client in self._joined_bots(room_id)
        )

    def _joined_bots(self, room_id: str) -> Iterator[tuple[AgentBot, nio.AsyncClient]]:
        """Yield running bots currently joined to room_id in deterministic name order."""
        bots = self.bots()
        for name in sorted(bots):
            bot = bots[name]
            client = bot.client
            if bot.running and client is not None and isinstance(client.rooms, Mapping) and room_id in client.rooms:
                yield bot, client

    def _pick_bot_for_room(self, room_id: str, *, preferred_agent: str | None = None) -> AgentBot | None:
        """Pick a running bot joined to room_id, preferring preferred_agent when available."""
        bots = self.bots()
        if preferred_agent is not None:
            preferred = bots.get(preferred_agent)
            if (
                preferred is not None
                and preferred.running
                and preferred.client is not None
                and isinstance(preferred.client.rooms, Mapping)
                and room_id in preferred.client.rooms
            ):
                return preferred
        joined = next(self._joined_bots(room_id), None)
        return joined[0] if joined is not None else None

    def _pick_bot_for_source(self, rule: ReactiveTriggerRecord) -> AgentBot | None:
        """Pick a running bot joined to the rule's watched source room, when the rule watches a Matrix room."""
        source = rule.source
        if isinstance(source, (RoomMessageSourceSpec, ThreadTagSourceSpec)):
            return self._pick_bot_for_room(source.room_id, preferred_agent=rule.target.agent)
        return None

    async def _get_room_joined_members(
        self,
        client: nio.AsyncClient,
        room_id: str,
        poll_failed_rooms: set[str],
    ) -> frozenset[str] | None:
        """Return joined member IDs for room_id using a short-TTL cache, failing closed on unreadable state."""
        if room_id in poll_failed_rooms:
            return None
        now_mono = time.monotonic()
        cached = self._room_member_cache.get(room_id)
        if cached is not None and now_mono - cached[1] < _ROOM_MEMBER_CACHE_TTL_SECONDS:
            return cached[0]

        room_state = await read_room_state(client, room_id)
        if room_state is None:
            self._room_member_cache.pop(room_id, None)
            poll_failed_rooms.add(room_id)
            return None

        members = _joined_members_from_snapshot(room_state)
        self._room_member_cache[room_id] = (members, time.monotonic())
        return members

    async def poll_room_messages_once(self) -> None:
        """Poll visible messages for all enabled room_message rules, doing zero journal reads when none exist."""
        if self._store is None:
            return
        config = self.config_provider()
        if not _reactive_enabled(config):
            return

        now = time.time()
        all_rules = await asyncio.to_thread(
            self._store.list_triggers,
            include_disabled=False,
            source_type="room_message",
        )
        active_rules = [
            rule
            for rule in all_rules
            if isinstance(rule.source, RoomMessageSourceSpec) and not _is_rule_expired(rule, now)
        ]
        if not active_rules:
            return

        rules_by_room: dict[str, list[ReactiveTriggerRecord]] = {}
        for rule in active_rules:
            assert isinstance(rule.source, RoomMessageSourceSpec)
            rules_by_room.setdefault(rule.source.room_id, []).append(rule)

        repaired_pairs: set[tuple[str, str]] = set()
        poll_failed_rooms: set[str] = set()

        for source_room_id, room_rules in rules_by_room.items():
            for rule in room_rules:
                bot = self._pick_bot_for_room(source_room_id, preferred_agent=rule.target.agent)
                if bot is None or bot.client is None:
                    continue
                try:
                    await self._poll_room_message_rule(
                        rule=rule,
                        bot=bot,
                        source_room_id=source_room_id,
                        config=config,
                        now=now,
                        repaired_pairs=repaired_pairs,
                        poll_failed_rooms=poll_failed_rooms,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "Reactive trigger room message rule poll failed",
                        trigger_id=rule.trigger_id,
                        room_id=source_room_id,
                    )

    async def _poll_room_message_rule(
        self,
        *,
        rule: ReactiveTriggerRecord,
        bot: AgentBot,
        source_room_id: str,
        config: Config,
        now: float,
        repaired_pairs: set[tuple[str, str]],
        poll_failed_rooms: set[str],
    ) -> None:
        """Poll one room_message rule from its durable cursor, enqueue matches, and advance its cursor."""
        assert self._store is not None
        assert isinstance(rule.source, RoomMessageSourceSpec)
        assert bot.client is not None

        reader = bot.conversation_reader
        cursor = await asyncio.to_thread(self._store.get_room_cursor, rule.trigger_id)
        if cursor is None:
            cursor = RoomCursor(created_ts=int(rule.created_at * 1000), logical_event_id="")
            if not await asyncio.to_thread(
                self._store.advance_room_cursor,
                rule.trigger_id,
                cursor,
                expected_rule_uid=rule.rule_uid,
            ):
                return

        repair_key = (bot.agent_name, source_room_id)
        if repair_key not in repaired_pairs:
            async with asyncio.timeout(_READ_TIMEOUT_SECONDS):
                await reader.hydrator.repair_room_history(source_room_id)
            repaired_pairs.add(repair_key)

        now_ms = int(now * 1000)
        after = ConversationCursor(created_ts=cursor.created_ts, logical_event_id=cursor.logical_event_id)
        async with asyncio.timeout(_READ_TIMEOUT_SECONDS):
            page = await reader.read_room_after(source_room_id, after, _MAX_MESSAGES_PER_POLL, now_ms)

        if page.refresh_pending:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(_READ_TIMEOUT_SECONDS):
                    await reader.hydrator.resolve_refreshes(page.refresh_pending)

        if await self._handle_stuck_refetch(
            rule=rule,
            bot=bot,
            source_room_id=source_room_id,
            after=after,
            now_ms=now_ms,
            last_cursor=page.last_cursor,
            stopped_at=page.stopped_at,
        ):
            return

        messages = projected_visible_messages(page)[:_MAX_MESSAGES_PER_POLL]
        if not messages:
            return

        observed_room = bot.client.rooms.get(source_room_id) if isinstance(bot.client.rooms, Mapping) else None
        for msg in messages:
            should_continue = await self._process_room_message(
                rule=rule,
                bot=bot,
                source_room_id=source_room_id,
                msg=msg,
                config=config,
                observed_room=observed_room,
                poll_failed_rooms=poll_failed_rooms,
            )
            if not should_continue:
                break

    async def _handle_stuck_refetch(
        self,
        *,
        rule: ReactiveTriggerRecord,
        bot: AgentBot,
        source_room_id: str,
        after: ConversationCursor,
        now_ms: int,
        last_cursor: ConversationCursor | None,
        stopped_at: ConversationCursor | None,
    ) -> bool:
        """Track refetch waits and skip a permanently stuck refetch after _REFETCH_WAIT_SECONDS."""
        assert self._store is not None
        if stopped_at is None:
            self._refetch_waits.pop(rule.trigger_id, None)
            return False

        now_mono = time.monotonic()
        wait = self._refetch_waits.get(rule.trigger_id)
        if wait is None or wait[0] != stopped_at.logical_event_id:
            self._refetch_waits[rule.trigger_id] = (stopped_at.logical_event_id, now_mono)
            waited = 0.0
        else:
            waited = now_mono - wait[1]

        if last_cursor is not None or waited < _REFETCH_WAIT_SECONDS:
            return False

        async with asyncio.timeout(_READ_TIMEOUT_SECONDS):
            retried = await bot.conversation_reader.read_room_after(source_room_id, after, 1, now_ms)
        if retried.last_cursor is None and retried.stopped_at == stopped_at:
            self._refetch_waits.pop(rule.trigger_id, None)
            logger.warning(
                "reactive_trigger_refetch_skipped",
                trigger_id=rule.trigger_id,
                room_id=source_room_id,
                event_id=stopped_at.logical_event_id,
            )
            await asyncio.to_thread(
                self._store.advance_room_cursor,
                rule.trigger_id,
                RoomCursor(created_ts=stopped_at.created_ts, logical_event_id=stopped_at.logical_event_id),
                expected_rule_uid=rule.rule_uid,
            )
        return True

    def _should_hold_unfinished_reply(self, trigger_id: str, msg: ResolvedVisibleMessage) -> bool:
        """Hold on an eligible unfinished reply until _UNFINISHED_WAIT_SECONDS elapses, then proceed."""
        if not is_unfinished_reply(msg.content):
            self._unfinished_waits.pop(trigger_id, None)
            return False

        now_mono = time.monotonic()
        wait = self._unfinished_waits.get(trigger_id)
        if wait is None or wait[0] != msg.event_id:
            self._unfinished_waits[trigger_id] = (msg.event_id, now_mono)
            return True
        if now_mono - wait[1] < _UNFINISHED_WAIT_SECONDS:
            return True

        self._unfinished_waits.pop(trigger_id, None)
        return False

    async def _process_room_message(
        self,
        *,
        rule: ReactiveTriggerRecord,
        bot: AgentBot,
        source_room_id: str,
        msg: ResolvedVisibleMessage,
        config: Config,
        observed_room: nio.MatrixRoom | None,
        poll_failed_rooms: set[str],
    ) -> bool:
        """Evaluate one visible room message for one rule and return whether polling should continue."""
        assert self._store is not None
        assert isinstance(rule.source, RoomMessageSourceSpec)

        msg_cursor = RoomCursor(created_ts=msg.timestamp, logical_event_id=msg.event_id)
        decision = await self._check_room_message_eligibility(
            rule=rule,
            bot=bot,
            source_room_id=source_room_id,
            msg=msg,
            config=config,
            observed_room=observed_room,
            poll_failed_rooms=poll_failed_rooms,
        )
        if decision == "hold":
            return False
        if decision == "skip":
            self._unfinished_waits.pop(rule.trigger_id, None)
            return await asyncio.to_thread(
                self._store.advance_room_cursor,
                rule.trigger_id,
                msg_cursor,
                expected_rule_uid=rule.rule_uid,
            )

        self._unfinished_waits.pop(rule.trigger_id, None)
        event = TriggerEvent(
            source_type="room_message",
            source_id=source_room_id,
            kind="room.message",
            event_id=msg.event_id,
            timestamp=msg.timestamp / 1000.0,
            room_id=source_room_id,
            thread_id=msg.thread_id,
            actor=msg.sender,
            text=msg.body,
            data={
                "room_id": source_room_id,
                "event_id": msg.event_id,
                "thread_id": msg.thread_id,
                "sender": msg.sender,
            },
        )
        if matches_room_message(rule.source, event, config=config, runtime_paths=self.runtime_paths):
            enqueued = await asyncio.to_thread(
                self._store.enqueue_firing,
                rule.trigger_id,
                msg.event_id,
                event,
                advance_cursor=msg_cursor,
                expected_rule_uid=rule.rule_uid,
            )
            if enqueued:
                self.notify_work()
        else:
            return await asyncio.to_thread(
                self._store.advance_room_cursor,
                rule.trigger_id,
                msg_cursor,
                expected_rule_uid=rule.rule_uid,
            )
        return True

    async def _check_room_message_eligibility(
        self,
        *,
        rule: ReactiveTriggerRecord,
        bot: AgentBot,
        source_room_id: str,
        msg: ResolvedVisibleMessage,
        config: Config,
        observed_room: nio.MatrixRoom | None,
        poll_failed_rooms: set[str],
    ) -> _MessageDecision:
        """Check loop guard, bot filter, owner membership, sender authorization, and bounded unfinished wait."""
        assert isinstance(rule.source, RoomMessageSourceSpec)
        assert bot.client is not None

        if (
            is_reactive_trigger_event_content(msg.content)
            or not is_human_requester_id(rule.owner_user_id, config, self.runtime_paths)
            or (
                not rule.source.include_bots
                and (
                    carries_agent_authored_marker(msg.content)
                    or not is_human_requester_id(msg.sender, config, self.runtime_paths)
                )
            )
        ):
            return "skip"

        joined_members = await self._get_room_joined_members(bot.client, source_room_id, poll_failed_rooms)
        if joined_members is None:
            return "hold"
        if not _owner_joined_in_members(joined_members, rule.owner_user_id, config, self.runtime_paths):
            return "skip"

        requester_id = self._classify_message_requester(bot, source_room_id, msg)
        try:
            sender_allowed = is_sender_allowed_for_agent_reply_in_room(
                requester_id,
                bot.agent_name,
                config,
                source_room_id,
                self.runtime_paths,
                self.memberships,
                require_resolved_membership=True,
                observed_room=observed_room,
            )
        except ReplyMembershipPendingError:
            return "hold"
        if not sender_allowed:
            return "skip"
        return "hold" if self._should_hold_unfinished_reply(rule.trigger_id, msg) else "allow"

    @staticmethod
    def _classify_message_requester(
        bot: AgentBot,
        room_id: str,
        msg: ResolvedVisibleMessage,
    ) -> str:
        """Resolve the sender requester ID via ingress_validator when available."""
        ingress_validator = getattr(bot, "ingress_validator", None)
        if ingress_validator is None:
            return msg.sender
        facts = ingress_validator.classify_source(msg.sender, {"content": msg.content}, room_id=room_id)
        return facts.run_requester_id

    async def drain_due_firings_once(self) -> None:
        """Drain due pending and claimed firings, serializing concurrent executions per trigger_id."""
        if self._store is None:
            return
        config = self.config_provider()
        due_firings = await asyncio.to_thread(self._store.list_due_firings, limit=_MAX_FIRINGS_PER_DRAIN)
        for firing in due_firings:
            if firing.id in self._in_flight_firing_ids or firing.trigger_id in self._in_flight_trigger_ids:
                continue
            self._in_flight_firing_ids.add(firing.id)
            self._in_flight_trigger_ids.add(firing.trigger_id)
            try:
                await self._process_firing(firing, config)
            finally:
                self._in_flight_firing_ids.discard(firing.id)
                self._in_flight_trigger_ids.discard(firing.trigger_id)

    async def _process_firing(self, firing: FiringRecord, config: Config) -> None:
        """Run pre-checks, triage, atomic claim, and Matrix dispatch for one due firing."""
        assert self._store is not None
        store = self._store
        claimed_now = firing.status == "claimed"
        try:
            rule = await asyncio.to_thread(store.get_trigger, firing.trigger_id)
            if rule is None:
                await self._mark_skipped_safe(firing.id, "rule_missing")
                return
            if rule.rule_uid != firing.rule_uid:
                await self._mark_skipped_safe(firing.id, "rule_replaced")
                return

            triage_reason = firing.triage_reason
            if firing.status == "pending":
                claimed_ok, triage_reason = await self._triage_and_claim_pending_firing(
                    firing=firing,
                    rule=rule,
                    config=config,
                )
                if not claimed_ok:
                    return
                claimed_now = True
            elif (not rule.enabled and rule.disabled_reason != "once_consumed") or _is_rule_expired(rule, time.time()):
                await self._mark_skipped_safe(firing.id, "disabled_or_expired")
                return

            result = await dispatch_reactive_trigger(
                rule=rule,
                firing=firing,
                triage_reason=triage_reason,
                store=store,
                target_bot=self.bots().get(rule.target.agent),
                source_bot=self._pick_bot_for_source(rule),
                config=config,
                runtime_paths=self.runtime_paths,
                memberships=self.memberships,
            )
            await self._record_dispatch_result(firing.id, result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                "Reactive trigger firing processing failed",
                firing_id=firing.id,
                trigger_id=firing.trigger_id,
            )
            if claimed_now:
                await self._mark_delivery_retry_safe(
                    firing.id,
                    f"unexpected_error:{type(exc).__name__}",
                )

    async def _check_pending_target_and_policy(
        self,
        *,
        firing: FiringRecord,
        rule: ReactiveTriggerRecord,
        config: Config,
    ) -> bool:
        """Return True when policy, rule state, and target bot are ready for pre-triage checks."""
        if not _reactive_enabled(config):
            await self._mark_skipped_safe(firing.id, "reactive_triggers_disabled")
            return False
        if not rule.enabled or _is_rule_expired(rule, time.time()):
            await self._mark_skipped_safe(firing.id, "disabled_or_expired")
            return False

        target_bot = self.bots().get(rule.target.agent)
        if target_bot is None or not target_bot.running or target_bot.client is None:
            await self._defer_pending_safe(firing.id, "target_bot_unavailable")
            return False
        target_rooms = getattr(target_bot.client, "rooms", None)
        if not isinstance(target_rooms, Mapping) or rule.target.room_id not in target_rooms:
            await self._defer_pending_safe(firing.id, "target_bot_not_joined")
            return False
        return True

    async def _check_pending_guards_and_source(
        self,
        *,
        firing: FiringRecord,
        rule: ReactiveTriggerRecord,
        config: Config,
    ) -> bool:
        """Return True when claim guards and source access allow proceeding to triage and claim."""
        assert self._store is not None
        policy_cap = config.external_trigger_policy.max_fires_per_hour
        guard_status = await asyncio.to_thread(
            self._store.can_claim_now,
            firing.id,
            policy_max_fires_per_hour=policy_cap,
        )
        if guard_status != "ok":
            if guard_status != "missing":
                await self._mark_skipped_safe(firing.id, guard_status)
            return False

        source_rejection = await check_reactive_source_access(
            rule=rule,
            event=firing.event,
            source_bot=self._pick_bot_for_source(rule),
            config=config,
            runtime_paths=self.runtime_paths,
            memberships=self.memberships,
        )
        if source_rejection is not None:
            if source_rejection.status == "skipped":
                await self._mark_skipped_safe(firing.id, source_rejection.reason or "source_not_authorized")
            else:
                await self._defer_pending_safe(firing.id, source_rejection.reason or "source_retry")
            return False
        return True

    async def _triage_and_claim_pending_firing(
        self,
        *,
        firing: FiringRecord,
        rule: ReactiveTriggerRecord,
        config: Config,
    ) -> tuple[bool, str | None]:
        """Validate policy/target/guards/source access, run optional triage, and atomically claim a pending firing."""
        assert self._store is not None
        if not await self._check_pending_target_and_policy(firing=firing, rule=rule, config=config):
            return False, None
        if not await self._check_pending_guards_and_source(firing=firing, rule=rule, config=config):
            return False, None

        triage_reason: str | None = None
        if rule.triage is not None:
            outcome = await evaluate_triage(
                rule,
                firing.event,
                config=config,
                runtime_paths=self.runtime_paths,
            )
            if not outcome.act:
                await self._mark_triaged_no_safe(firing.id, outcome.failure or outcome.reason)
                return False, None
            triage_reason = outcome.reason

        claim_status = await asyncio.to_thread(
            self._store.claim_firing,
            firing.id,
            triage_reason=triage_reason,
            policy_max_fires_per_hour=config.external_trigger_policy.max_fires_per_hour,
        )
        return claim_status == "claimed", triage_reason

    async def _record_dispatch_result(self, firing_id: int, result: DispatchResult) -> None:
        """Persist the outcome of one Matrix dispatch attempt without letting SQLite errors crash the drain loop."""
        if result.status == "delivered":
            if result.matrix_event_id is not None:
                await self._mark_delivered_safe(firing_id, result.matrix_event_id)
            else:
                await self._mark_delivery_retry_safe(firing_id, "missing_matrix_event_id")
            return
        if result.status == "skipped":
            await self._mark_skipped_safe(firing_id, result.reason or "skipped")
            return
        await self._mark_delivery_retry_safe(firing_id, result.reason or "retry")

    async def _mark_delivered_safe(self, firing_id: int, matrix_event_id: str) -> None:
        assert self._store is not None
        try:
            await asyncio.shield(asyncio.to_thread(self._store.mark_delivered, firing_id, matrix_event_id))
        except _STORE_PERSIST_ERRORS:
            logger.warning(
                "Failed to persist delivered status for reactive trigger firing",
                firing_id=firing_id,
                matrix_event_id=matrix_event_id,
                exc_info=True,
            )

    async def _mark_delivery_retry_safe(self, firing_id: int, reason: str) -> None:
        assert self._store is not None
        try:
            await asyncio.to_thread(self._store.mark_delivery_retry, firing_id, reason)
        except _STORE_PERSIST_ERRORS:
            logger.warning(
                "Failed to persist retry status for reactive trigger firing",
                firing_id=firing_id,
                reason=reason,
                exc_info=True,
            )

    async def _defer_pending_safe(self, firing_id: int, reason: str) -> None:
        assert self._store is not None
        try:
            await asyncio.to_thread(self._store.defer_pending_firing, firing_id, reason)
        except _STORE_PERSIST_ERRORS:
            logger.warning(
                "Failed to defer pending reactive trigger firing",
                firing_id=firing_id,
                reason=reason,
                exc_info=True,
            )

    async def _mark_skipped_safe(self, firing_id: int, reason: str) -> None:
        assert self._store is not None
        try:
            await asyncio.to_thread(self._store.mark_skipped, firing_id, reason)
        except _STORE_PERSIST_ERRORS:
            logger.warning(
                "Failed to persist skipped status for reactive trigger firing",
                firing_id=firing_id,
                reason=reason,
                exc_info=True,
            )

    async def _mark_triaged_no_safe(self, firing_id: int, reason: str | None) -> None:
        assert self._store is not None
        try:
            await asyncio.to_thread(self._store.mark_triaged_no, firing_id, reason)
        except _STORE_PERSIST_ERRORS:
            logger.warning(
                "Failed to persist triaged_no status for reactive trigger firing",
                firing_id=firing_id,
                reason=reason,
                exc_info=True,
            )
