"""Bounded-concurrency poller and backoff scheduler for due script_check reactive triggers."""

from __future__ import annotations

import asyncio
import inspect
import random
import time
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any, Literal, Protocol
from uuid import uuid4

from mindroom.logging_config import get_logger
from mindroom.reactive_triggers.models import ReactiveTriggerRecord, ScriptCheckCheckpoint, ScriptCheckSourceSpec
from mindroom.reactive_triggers.script_check import (
    check_script_check_access,
    normalize_script_check_event,
    validate_script_check_state,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.reactive_triggers.store import ReactiveTriggerStore

logger = get_logger(__name__)

_MAX_BACKOFF_SECONDS = 3600.0
_DEFERRED_RETRY_SECONDS = 30.0
_UNAUTHORIZED_RECHECK_CAP_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class ScriptCheckTickOutcome:
    """Result returned by one script_check tick execution."""

    status: Literal["ok", "error", "deferred", "unauthorized"]
    run_id: str | None = None
    result: object = None
    state: dict[str, Any] | None = None
    error_code: str | None = None


class ScriptCheckTickRunner(Protocol):
    """Callable protocol executed by ScriptCheckPoller to run one due tick."""

    async def __call__(
        self,
        rule: ReactiveTriggerRecord,
        checkpoint: ScriptCheckCheckpoint,
        config: Config,
        now: float,
        /,
    ) -> ScriptCheckTickOutcome:
        """Execute one due tick and return its outcome."""
        ...


class ScriptCheckPoller:
    """Bounded-concurrency poller for due script_check reactive triggers."""

    def __init__(
        self,
        *,
        store: ReactiveTriggerStore,
        config_provider: Callable[[], Config],
        runtime_paths: RuntimePaths,
        tick_runner: ScriptCheckTickRunner | None = None,
        on_enqueued: Callable[[], None] | None = None,
        on_slot_freed: Callable[[], None] | None = None,
        max_concurrency: int = 4,
    ) -> None:
        self._store = store
        self._config_provider = config_provider
        self._runtime_paths = runtime_paths
        self._tick_runner = tick_runner
        self._on_enqueued = on_enqueued
        self._on_slot_freed = on_slot_freed
        self._max_concurrency = max(1, max_concurrency)
        self._active_tasks: set[asyncio.Task[bool]] = set()
        self._active_owners: set[str] = set()
        self._in_flight_trigger_ids: set[str] = set()
        self._stopped = False

    def set_tick_runner(self, tick_runner: ScriptCheckTickRunner | None) -> None:
        """Bind or replace the tick runner callable."""
        self._tick_runner = tick_runner

    def resume(self) -> None:
        """Allow new script_check admissions after a previous stop."""
        self._stopped = False

    async def stop(self) -> None:
        """Cancel active poller tasks and drain tracked runner cleanup tasks."""
        self._stopped = True
        while tasks := list(self._active_tasks):
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        stop_fn = getattr(self._tick_runner, "stop", None)
        if callable(stop_fn):
            result = stop_fn()
            if inspect.isawaitable(result):
                await result

    async def admit_due(self, *, now: float | None = None) -> list[asyncio.Task[bool]]:
        """Admit due script_check triggers into available slots without blocking; return spawned tasks."""
        pol = self._config_provider().external_trigger_policy
        if (
            self._stopped
            or self._tick_runner is None
            or not (pol.is_reactive_active and pol.script_check_enabled)
            or len(self._active_tasks) >= self._max_concurrency
        ):
            return []
        fixed_now = now is not None
        current_time = time.time() if now is None else now
        scan_limit = max(20, self._max_concurrency * 5)
        active_owners = tuple(self._active_owners)
        due = await asyncio.to_thread(
            self._store.list_due_script_check_triggers,
            now=current_time,
            limit=scan_limit,
            exclude_owners=active_owners,
        )
        if self._stopped or self._tick_runner is None:
            return []
        spawned: list[asyncio.Task[bool]] = []
        for rule, cp in due:
            if len(self._active_tasks) >= self._max_concurrency:
                break
            if rule.trigger_id in self._in_flight_trigger_ids or rule.owner_user_id in self._active_owners:
                continue
            self._in_flight_trigger_ids.add(rule.trigger_id)
            self._active_owners.add(rule.owner_user_id)
            task = asyncio.create_task(
                self._run_admitted(rule, cp, current_time, fixed_now=fixed_now),
                name=f"script_check_poll_{rule.trigger_id}",
            )
            self._active_tasks.add(task)
            task.add_done_callback(
                partial(
                    self._release_slot,
                    trigger_id=rule.trigger_id,
                    owner_user_id=rule.owner_user_id,
                    enqueued=False,
                ),
            )
            spawned.append(task)
        return spawned

    def _release_slot(
        self,
        task: asyncio.Task[bool],
        *,
        trigger_id: str,
        owner_user_id: str,
        enqueued: bool,
    ) -> None:
        if task not in self._active_tasks:
            return
        self._active_tasks.discard(task)
        self._in_flight_trigger_ids.discard(trigger_id)
        self._active_owners.discard(owner_user_id)
        if enqueued and self._on_enqueued is not None:
            self._on_enqueued()
        if not self._stopped and self._on_slot_freed is not None:
            self._on_slot_freed()

    async def poll_due_once(self, *, now: float | None = None) -> int:
        """Admit due script_check triggers once, await the admitted tasks, and return newly enqueued firing count."""
        tasks = await self.admit_due(now=now)
        if not tasks:
            return 0
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        for outcome in outcomes:
            if isinstance(outcome, BaseException) and not isinstance(outcome, Exception):
                raise outcome
        return sum(outcome is True for outcome in outcomes)

    def _load_live_rule_and_checkpoint(
        self,
        rule: ReactiveTriggerRecord,
        cp: ScriptCheckCheckpoint,
        check_now: float,
    ) -> tuple[ReactiveTriggerRecord, ScriptCheckCheckpoint] | None:
        fresh_rule = self._store.get_trigger(rule.trigger_id)
        fresh_cp = self._store.get_script_check_checkpoint(rule.trigger_id)
        if (
            fresh_rule is None
            or fresh_cp is None
            or not fresh_rule.enabled
            or (fresh_rule.expires_at is not None and fresh_rule.expires_at <= check_now)
            or fresh_rule.rule_uid != rule.rule_uid
            or fresh_cp.revision != cp.revision
            or fresh_cp.next_poll_at is None
            or fresh_cp.next_poll_at > check_now
            or not isinstance(fresh_rule.source, ScriptCheckSourceSpec)
        ):
            return None
        return fresh_rule, fresh_cp

    async def _run_admitted(
        self,
        rule: ReactiveTriggerRecord,
        cp: ScriptCheckCheckpoint,
        now: float,
        *,
        fixed_now: bool,
    ) -> bool:
        enqueued = False
        try:
            check_now = now if fixed_now else time.time()
            live = await asyncio.to_thread(self._load_live_rule_and_checkpoint, rule, cp, check_now)
            if live is not None:
                fresh_rule, fresh_cp = live
                enqueued = await self._execute_rule(fresh_rule, fresh_cp, check_now, fixed_now=fixed_now)
        except Exception as exc:
            logger.warning("Unexpected script_check poll error", trigger_id=rule.trigger_id, error=str(exc))
        finally:
            current = asyncio.current_task()
            if current is not None:
                self._release_slot(
                    current,
                    trigger_id=rule.trigger_id,
                    owner_user_id=rule.owner_user_id,
                    enqueued=enqueued,
                )
        return enqueued

    async def _execute_rule(
        self,
        rule: ReactiveTriggerRecord,
        cp: ScriptCheckCheckpoint,
        now: float,
        *,
        fixed_now: bool,
    ) -> bool:
        assert isinstance(rule.source, ScriptCheckSourceSpec)
        config = self._config_provider()
        min_poll = float(config.external_trigger_policy.min_script_check_poll_interval_seconds)
        effective_interval = max(min_poll, float(rule.source.poll_interval_seconds))
        if failure := check_script_check_access(
            config,
            self._runtime_paths,
            rule.source.agent,
            owner_user_id=rule.owner_user_id,
            require_placement=False,
        ):
            next_at = now + min(_UNAUTHORIZED_RECHECK_CAP_SECONDS, effective_interval)
            await self._save_non_ok(rule, cp, last_error_code=failure.code, next_poll_at=next_at, updated_at=now)
            return False
        if self._tick_runner is None:
            return False
        try:
            outcome = await self._tick_runner(rule, cp, config, now)
        except Exception as exc:
            logger.warning("script_check tick runner failed", trigger_id=rule.trigger_id, error=str(exc))
            outcome = ScriptCheckTickOutcome(status="error", error_code="tick_runner_error")
        finished_at = now if fixed_now else time.time()
        return await self._apply_outcome(rule, cp, outcome, min_poll, effective_interval, finished_at)

    async def _apply_outcome(
        self,
        rule: ReactiveTriggerRecord,
        cp: ScriptCheckCheckpoint,
        outcome: ScriptCheckTickOutcome,
        min_poll: float,
        effective_interval: float,
        now: float,
    ) -> bool:
        if outcome.status == "deferred":
            extra = {"last_error_code": outcome.error_code} if outcome.error_code is not None else {}
            await self._save_non_ok(rule, cp, next_poll_at=now + _DEFERRED_RETRY_SECONDS, updated_at=now, **extra)
            return False
        if outcome.status == "unauthorized":
            next_at = now + min(_UNAUTHORIZED_RECHECK_CAP_SECONDS, effective_interval)
            await self._save_non_ok(rule, cp, last_error_code=outcome.error_code, next_poll_at=next_at, updated_at=now)
            return False
        if outcome.status == "error":
            await self._record_error(rule, cp, effective_interval, outcome.error_code or "tick_error", now)
            return False
        try:
            next_state = cp.state if outcome.state is None else validate_script_check_state(outcome.state)
        except ValueError as exc:
            logger.warning("Invalid script_check state", trigger_id=rule.trigger_id, error=str(exc))
            await self._record_error(rule, cp, effective_interval, "invalid_state", now)
            return False
        try:
            event = normalize_script_check_event(
                rule.trigger_id,
                outcome.run_id or f"tick_{uuid4().hex[:12]}",
                outcome.result,
                now=now,
            )
        except ValueError as exc:
            logger.warning("Invalid script_check result", trigger_id=rule.trigger_id, error=str(exc))
            await self._record_error(rule, cp, effective_interval, "invalid_result", now)
            return False
        delay = max(min_poll, effective_interval * random.uniform(0.9, 1.1))  # noqa: S311
        next_cp = cp.model_copy(
            update={
                "state": next_state,
                "next_poll_at": now + delay,
                "consecutive_errors": 0,
                "last_error_code": None,
                "last_run_at": now,
                "updated_at": now,
            },
        )
        return await asyncio.to_thread(
            self._store.update_script_check_checkpoint_and_enqueue,
            rule.trigger_id,
            next_cp,
            event=event,
            expected_rule_uid=rule.rule_uid,
            expected_revision=cp.revision,
        )

    async def _record_error(
        self,
        rule: ReactiveTriggerRecord,
        cp: ScriptCheckCheckpoint,
        interval: float,
        code: str,
        now: float,
    ) -> None:
        errors = cp.consecutive_errors + 1
        backoff = min(max(_MAX_BACKOFF_SECONDS, interval), interval * (2 ** min(errors, 5)))
        await self._save_non_ok(
            rule,
            cp,
            consecutive_errors=errors,
            last_error_code=code,
            last_run_at=now,
            next_poll_at=now + backoff,
            updated_at=now,
        )

    async def _save_non_ok(self, rule: ReactiveTriggerRecord, cp: ScriptCheckCheckpoint, **updates: object) -> None:
        await asyncio.to_thread(
            self._store.upsert_script_check_checkpoint,
            rule.trigger_id,
            cp.model_copy(update=updates),
            expected_rule_uid=rule.rule_uid,
            expected_revision=cp.revision,
            require_enabled=True,
        )
