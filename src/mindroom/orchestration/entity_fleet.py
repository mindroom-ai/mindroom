"""The one owner of every managed bot's lifecycle: start, retry, stop, replace, remove, and process shutdown.

Each entity has one slot holding its bot, its Matrix sync task, its background
start retry, and whether its startup failed permanently, so no stop or
replacement path can forget one of them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.background_tasks import retry_delay_seconds
from mindroom.logging_config import get_logger
from mindroom.runtime_shutdown import (
    ENTITY_REMOVED_SHUTDOWN,
    GENERIC_SHUTDOWN,
    ORDERLY_SHUTDOWN,
    RESPONSE_FINALIZATION_TIMEOUT_SECONDS,
    SYNC_RESTART_SHUTDOWN,
    ResponseShutdownTimeoutError,
    RuntimeShutdownIntent,
    gather_shutdown_phase,
)
from mindroom.startup_errors import PermanentStartupError

from .runtime import (
    STARTUP_RETRY_INITIAL_DELAY_SECONDS,
    STARTUP_RETRY_MAX_DELAY_SECONDS,
    cancel_task,
    create_logged_task,
    run_shutdown_step,
    sync_forever_with_restart,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Iterator

    from mindroom.bot import AgentBot, TeamBot
    from mindroom.config.main import Config

logger = get_logger(__name__)

_DEFERRED_RESPONSE_DIAGNOSTIC_INTERVAL_SECONDS = 5.0

type _Bot = AgentBot | TeamBot


@dataclass(slots=True)
class EntityStartResults:
    """Result of one pass trying to start a batch of entities."""

    started_bots: list[_Bot] = field(default_factory=list)
    retryable_entities: list[str] = field(default_factory=list)
    permanently_failed_entities: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EntityFleetHooks:
    """What the fleet asks of the rest of the runtime when entities change."""

    # Build bots for configured entities, preparing their Matrix accounts first.
    create_bots: Callable[[set[str], Config], Awaitable[dict[str, _Bot]]]
    # Entities that must not start yet because a required MCP server is unavailable.
    start_blocked: Callable[[set[str]], set[str]]
    # Withdraw capabilities bound to these entities before any of them stops.
    entities_changing: Callable[[set[str]], None]
    # Publish one batch of start outcomes: support bindings, approval cleanup, dispatch recovery.
    entities_started: Callable[[EntityStartResults], Awaitable[None]]
    # Reconcile rooms after a background retry started an entity, which no caller waits on.
    setup_rooms_after_retry: Callable[[str], Awaitable[None]]
    # Settle a removed entity's approval continuations while its bot still exists; return the unresolved ones.
    reconcile_removed: Callable[[set[str]], Awaitable[set[str]]]
    # End the journal replies of entities that no bot will ever finish.
    end_replies: Callable[[set[str]], Awaitable[None]]


@dataclass(frozen=True)
class _FleetShutdown:
    """What process shutdown left behind."""

    failures: list[BaseException]
    # A bot still owns a response or journal-callback cleanup, so shared stores must stay open.
    owns_resources: bool


@dataclass
class _EntitySlot:
    bot: _Bot
    sync_task: asyncio.Task[None] | None = None
    retry_task: asyncio.Task[None] | None = None
    disabled: bool = False


class _BotsView(Mapping[str, "_Bot"]):
    """Read-only live view of the fleet's bots by entity name."""

    def __init__(self, slots: dict[str, _EntitySlot]) -> None:
        self._slots = slots

    def __getitem__(self, entity_name: str) -> _Bot:
        return self._slots[entity_name].bot

    def __iter__(self) -> Iterator[str]:
        return iter(self._slots)

    def __len__(self) -> int:
        return len(self._slots)


class EntityFleet:
    """Own every managed entity's bot, sync task, start retry, and disabled state."""

    def __init__(self, hooks: EntityFleetHooks) -> None:
        self._hooks = hooks
        self._slots: dict[str, _EntitySlot] = {}
        self.bots: Mapping[str, _Bot] = _BotsView(self._slots)

    @property
    def names(self) -> frozenset[str]:
        """Return every entity that currently has a bot."""
        return frozenset(self._slots)

    def is_disabled(self, entity_name: str) -> bool:
        """Return whether this entity's startup failed permanently until a reload changes it."""
        slot = self._slots.get(entity_name)
        return slot is not None and slot.disabled

    def add(self, entity_name: str, bot: _Bot) -> None:
        """Register one built bot that has not started yet."""
        if entity_name in self._slots:
            msg = f"Entity {entity_name} already has a bot"
            raise RuntimeError(msg)
        self._slots[entity_name] = _EntitySlot(bot)

    async def create(self, entity_names: Iterable[str], config: Config) -> None:
        """Build and register bots for configured entities that have none."""
        names = set(entity_names) - set(self._slots)
        if not names:
            return
        for entity_name, bot in (await self._hooks.create_bots(names, config)).items():
            self.add(entity_name, bot)

    async def try_start(self, entity_names: Iterable[str]) -> EntityStartResults:
        """Try each entity's start once without starting sync, scheduling retries, or publishing outcomes."""
        slots = [(name, self._slots[name]) for name in sorted(set(entity_names)) if name in self._slots]
        results = EntityStartResults()
        blocked = self._hooks.start_blocked({name for name, _slot in slots})
        results.retryable_entities.extend(sorted(blocked))
        slots = [(name, slot) for name, slot in slots if name not in blocked]
        outcomes = await asyncio.gather(*(_start_once(name, slot.bot) for name, slot in slots))
        for (name, slot), outcome in zip(slots, outcomes, strict=True):
            slot.disabled = outcome is None
            if outcome is None:
                results.permanently_failed_entities.append(name)
            elif outcome:
                results.started_bots.append(slot.bot)
            else:
                results.retryable_entities.append(name)
        return results

    async def start(self, entity_names: Iterable[str]) -> EntityStartResults:
        """Start entities once, run sync for the started ones, retry the rest, and publish the outcome."""
        results = await self.try_start(entity_names)
        self.start_sync(results.started_bots)
        self.schedule_retries(results.retryable_entities)
        await self._hooks.entities_started(results)
        return results

    async def replace(self, entity_names: Iterable[str], config: Config) -> EntityStartResults:
        """Stop entities, then rebuild and start them on ``config``."""
        names = set(entity_names)
        await self.stop(names)
        await self.create(names, config)
        return await self.start(names)

    def start_sync(self, bots: Iterable[_Bot]) -> None:
        """Ensure one Matrix sync task runs for each started bot."""
        for bot in bots:
            slot = self._slots[bot.agent_name]
            if slot.sync_task is None or slot.sync_task.done():
                slot.sync_task = asyncio.create_task(sync_forever_with_restart(bot), name=f"sync_{bot.agent_name}")

    def schedule_retries(self, entity_names: Iterable[str]) -> None:
        """Retry each entity's start in the background unless a retry already runs."""
        for entity_name in entity_names:
            slot = self._slots.get(entity_name)
            if slot is None or (slot.retry_task is not None and not slot.retry_task.done()):
                continue
            slot.retry_task = create_logged_task(
                self._retry_start(entity_name, slot),
                name=f"retry_start_{entity_name}",
                failure_message="Background bot start task failed",
            )

    async def _retry_start(self, entity_name: str, slot: _EntitySlot) -> None:
        """Retry one entity's start with backoff until it starts, fails permanently, or is cancelled."""
        attempt = 0
        try:
            while True:
                attempt += 1
                retry_in_seconds = retry_delay_seconds(
                    attempt,
                    initial_delay_seconds=STARTUP_RETRY_INITIAL_DELAY_SECONDS,
                    max_delay_seconds=STARTUP_RETRY_MAX_DELAY_SECONDS,
                )
                logger.warning(
                    "Bot startup failed; retrying in background",
                    agent_name=entity_name,
                    attempt=attempt,
                    retry_in_seconds=retry_in_seconds,
                )
                await asyncio.sleep(retry_in_seconds)
                if entity_name in self._hooks.start_blocked({entity_name}):
                    continue
                outcome = await _start_once(entity_name, slot.bot)
                if outcome is False:
                    continue
                slot.disabled = outcome is None
                if outcome is None:
                    await self._hooks.entities_started(EntityStartResults(permanently_failed_entities=[entity_name]))
                    return
                logger.info("Bot recovered after startup failure", agent_name=entity_name)
                self.start_sync([slot.bot])
                await self._hooks.entities_started(EntityStartResults(started_bots=[slot.bot]))
                await self._hooks.setup_rooms_after_retry(entity_name)
                return
        finally:
            if slot.retry_task is asyncio.current_task():
                slot.retry_task = None

    async def stop(self, entity_names: Iterable[str]) -> None:
        """Stop entities for a restart and drop their slots; a failed stop keeps every slot and raises."""
        slots = self._existing(entity_names)
        if not slots:
            return
        await self._prepare_change(slots)
        intent = SYNC_RESTART_SHUTDOWN
        bots = [slot.bot for slot in slots.values()]
        cancellations: list[asyncio.CancelledError] = []
        quiesce_results = await _phase(cancellations, *(bot._quiesce_matrix_ingestion() for bot in bots))
        # Stop sync loops before certifying callback drains; otherwise fresh
        # callbacks can appear after the checkpoint decision.
        cancel_results = await _phase(cancellations, *(_cancel_sync(slot, intent) for slot in slots.values()))
        prepare_results = await _phase(
            cancellations,
            *(bot.prepare_for_sync_shutdown(shutdown_intent=intent) for bot in bots),
        )
        stop_results = await _phase(cancellations, *(bot.stop(shutdown_intent=intent) for bot in bots))
        bot_failures: list[BaseException] = []
        drain_timeouts: list[str] = []
        for name, prepared, stopped in zip(slots, prepare_results, stop_results, strict=True):
            errors = _failures((prepared, stopped))
            # A restarting bot releases every resource even when a reply outlives the
            # bounded drain, and the cancelled reply stays pending for its replacement
            # to replay, so any other failure still keeps it from being replaced.
            other_errors = [error for error in errors if not isinstance(error, ResponseShutdownTimeoutError)]
            bot_failures.extend(other_errors)
            if errors and not other_errors:
                drain_timeouts.append(name)
        failures = [
            *_failures(quiesce_results),
            *cancellations,
            *_failures(cancel_results),
            *bot_failures,
        ]
        if failures:
            raise failures[0]
        if drain_timeouts:
            logger.warning("restart_replacing_entities_after_response_drain_timeout", entities=drain_timeouts)
        for name in slots:
            del self._slots[name]

    async def remove(self, entity_names: Iterable[str]) -> set[str]:
        """Remove entities no longer configured: settle approvals, leave rooms, stop, and end their replies.

        Each step that acts on Matrix or the journal runs only for entities whose
        earlier steps succeeded, so a failed preparation never leaves rooms and a
        reply ends only once its bot is gone. Returns the removed entities whose
        approval settlement is still unresolved.
        """
        slots = self._existing(entity_names)
        if not slots:
            return set()
        await self._prepare_change(slots)
        intent = ENTITY_REMOVED_SHUTDOWN
        failures: list[BaseException] = []
        cancellations: list[asyncio.CancelledError] = []
        prepare_results = await _phase(
            cancellations,
            *(slot.bot.prepare_for_sync_shutdown(shutdown_intent=intent) for slot in slots.values()),
        )
        prepared = _succeeded(slots, prepare_results, failures)
        # A frozen successful FINAL must be recovered while the original sender
        # and its lifecycle collaborators still exist; an entity whose recovery
        # is unresolved stays in its rooms so the retry can still deliver there.
        unresolved = await self._hooks.reconcile_removed(set(prepared)) if prepared else set()
        if unresolved:
            logger.warning("removed_entities_keep_rooms_for_approval_recovery", entities=sorted(unresolved))
        # A bot that never started has no Matrix client to leave with; the router's orphan cleanup removes it later.
        leave_results = await _phase(
            cancellations,
            *(
                _leave_then_cancel_sync(slot, leave=name not in unresolved and slot.bot.client is not None)
                for name, slot in prepared.items()
            ),
        )
        left = _succeeded(prepared, leave_results, failures)
        stop_results = await _phase(cancellations, *(slot.bot.stop(shutdown_intent=intent) for slot in left.values()))
        stopped = _succeeded(left, stop_results, failures)
        for name in stopped:
            del self._slots[name]
        if stopped:
            await self._hooks.end_replies(set(stopped))
        failures = [*cancellations, *failures]
        if failures:
            raise failures[0]
        return unresolved

    def begin_process_shutdown(self) -> None:
        """Close response and callback admission on every bot before shutdown teardown yields."""
        for slot in self._slots.values():
            slot.bot.begin_process_shutdown()

    async def cancel_retries(self) -> None:
        """Cancel every background start retry."""
        for slot in self._slots.values():
            await cancel_task(slot.retry_task)
            slot.retry_task = None

    async def stop_all(self) -> _FleetShutdown:
        """Stop every bot for process shutdown once ``cancel_retries`` ran; the slots stay, so later cleanup still finds every bot."""
        slots = dict(self._slots)
        bots = [slot.bot for slot in slots.values()]
        cancellations: list[asyncio.CancelledError] = []
        quiesce_results = await run_shutdown_step(
            "source_quiesce",
            _phase(cancellations, *(bot._quiesce_matrix_ingestion() for bot in bots)),
        )
        # Cancel sync tasks first so shutdown does not race with active sync loops.
        cancel_results = await run_shutdown_step(
            "sync_tasks",
            _phase(cancellations, *(_cancel_sync(slot, GENERIC_SHUTDOWN) for slot in slots.values())),
        )
        for bot in bots:
            bot.running = False
        stop_results, cancellation = await run_shutdown_step(
            "bot_stop",
            _gather_with_periodic_log(
                bots,
                "orchestrator_bot_stop_pending",
                "pending_bot_stop_phase_counts",
                *(bot.stop(shutdown_intent=ORDERLY_SHUTDOWN) for bot in bots),
            ),
        )
        if cancellation is not None:
            cancellations.append(cancellation)
        deferred_bots = [bot for bot in bots if bot.deferred_stop_required is True]
        pending_owner_count = sum(bot.pending_response_owner_count for bot in bots)
        if pending_owner_count > 0:
            logger.warning(
                "orchestrator_response_shutdown_owners_pending",
                live_response_owner_count=pending_owner_count,
                pending_response_phase_counts=_response_phase_counts(bots),
            )
        deferred_results: list[object] = []
        if deferred_bots:
            deferred_results, cancellation = await run_shutdown_step(
                "deferred_response_owners",
                _gather_with_periodic_log(
                    deferred_bots,
                    "orchestrator_deferred_response_owners_pending",
                    "pending_deferred_stop_phase_counts",
                    *(
                        bot.finish_deferred_stop(
                            shutdown_intent=ORDERLY_SHUTDOWN,
                            timeout_seconds=RESPONSE_FINALIZATION_TIMEOUT_SECONDS,
                        )
                        for bot in deferred_bots
                    ),
                ),
            )
            if cancellation is not None:
                cancellations.append(cancellation)
        finished_deferred = {
            id(bot)
            for bot, result in zip(deferred_bots, deferred_results, strict=True)
            if not isinstance(result, BaseException) and bot.deferred_stop_required is False
        }
        cleanup_failures = [
            result
            for bot, result in zip(bots, stop_results, strict=True)
            if isinstance(result, BaseException)
            and not (id(bot) in finished_deferred and isinstance(result, ResponseShutdownTimeoutError))
        ]
        pending_owner_count = sum(bot.pending_response_owner_count for bot in bots)
        callback_cleanup_pending = any(bot.deferred_stop_required is True for bot in bots)
        if pending_owner_count > 0 or callback_cleanup_pending:
            logger.warning(
                "orchestrator_shared_journal_close_deferred",
                live_response_owner_count=pending_owner_count,
                pending_response_phase_counts=_response_phase_counts(bots),
            )
        return _FleetShutdown(
            failures=[
                *_failures(quiesce_results),
                *cancellations,
                *cleanup_failures,
                *_failures(cancel_results),
                *_failures(deferred_results),
            ],
            owns_resources=pending_owner_count > 0 or callback_cleanup_pending,
        )

    def _existing(self, entity_names: Iterable[str]) -> dict[str, _EntitySlot]:
        return {name: self._slots[name] for name in sorted(set(entity_names)) if name in self._slots}

    async def _prepare_change(self, slots: dict[str, _EntitySlot]) -> None:
        for slot in slots.values():
            await cancel_task(slot.retry_task)
            slot.retry_task = None
        self._hooks.entities_changing(set(slots))


async def _start_once(entity_name: str, bot: _Bot) -> bool | None:
    """Start one bot once: True when started, False when worth retrying, None when it can never start."""
    try:
        await bot.start()
    except PermanentStartupError as exc:
        logger.error(  # noqa: TRY400 - a permanent configuration error needs no traceback
            "Bot startup failed permanently; leaving bot disabled until configuration changes",
            agent_name=entity_name,
            error=str(exc),
        )
        return None
    except Exception:
        logger.exception("agent_start_failed", agent=entity_name)
        return False
    return True


async def _cancel_sync(slot: _EntitySlot, intent: RuntimeShutdownIntent) -> None:
    task, slot.sync_task = slot.sync_task, None
    await cancel_task(task, shutdown_intent=intent)


async def _leave_then_cancel_sync(slot: _EntitySlot, *, leave: bool) -> None:
    try:
        if leave:
            await slot.bot.leave_rooms()
    finally:
        # Membership commands need the source and admission pump, but both must
        # stop before the session and journal stores close.
        await _cancel_sync(slot, ENTITY_REMOVED_SHUTDOWN)


async def _phase(cancellations: list[asyncio.CancelledError], *awaitables: Awaitable[object]) -> list[object]:
    results, cancellation = await gather_shutdown_phase(*awaitables)
    if cancellation is not None:
        cancellations.append(cancellation)
    return results


def _failures(results: Iterable[object]) -> list[BaseException]:
    return [result for result in results if isinstance(result, BaseException)]


def _succeeded(
    slots: dict[str, _EntitySlot],
    results: list[object],
    failures: list[BaseException],
) -> dict[str, _EntitySlot]:
    succeeded: dict[str, _EntitySlot] = {}
    for (name, slot), result in zip(slots.items(), results, strict=True):
        if isinstance(result, BaseException):
            failures.append(result)
        else:
            succeeded[name] = slot
    return succeeded


def _response_phase_counts(bots: Iterable[_Bot]) -> dict[str, int]:
    """Sum fixed response phase counts across bots for one shutdown warning."""
    aggregate: dict[str, int] = {}
    for bot in bots:
        for phase, count in bot.pending_response_phase_counts.items():
            aggregate[phase] = aggregate.get(phase, 0) + count
    return dict(sorted(aggregate.items()))


def _deferred_stop_phase_counts(bots: Iterable[_Bot]) -> dict[str, int]:
    """Count fixed deferred-release phases without exposing bot identities."""
    aggregate: dict[str, int] = {}
    for bot in bots:
        phase = bot.deferred_stop_phase
        if phase is not None:
            aggregate[phase] = aggregate.get(phase, 0) + 1
    return dict(sorted(aggregate.items()))


async def _gather_with_periodic_log(
    bots: list[_Bot],
    event: str,
    phase_count_field: str,
    *awaitables: Awaitable[object],
) -> tuple[list[object], asyncio.CancelledError | None]:
    """Finish one shutdown phase while periodically exposing which fixed phases still run."""
    phase = asyncio.gather(*awaitables, return_exceptions=True)
    loop = asyncio.get_running_loop()
    timer: asyncio.TimerHandle | None = None

    def log_pending_phases() -> None:
        nonlocal timer
        if phase.done():
            return
        logger.warning(
            event,
            live_response_owner_count=sum(bot.pending_response_owner_count for bot in bots),
            pending_response_phase_counts=_response_phase_counts(bots),
            **{phase_count_field: _deferred_stop_phase_counts(bots)},
        )
        timer = loop.call_later(_DEFERRED_RESPONSE_DIAGNOSTIC_INTERVAL_SECONDS, log_pending_phases)

    timer = loop.call_later(_DEFERRED_RESPONSE_DIAGNOSTIC_INTERVAL_SECONDS, log_pending_phases)
    try:
        try:
            return list(await asyncio.shield(phase)), None
        except asyncio.CancelledError as cancellation:
            return list(await phase), cancellation
    finally:
        timer.cancel()
