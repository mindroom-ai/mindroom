"""Bounded-concurrency poller and backoff scheduler for due script_check reactive triggers."""

from __future__ import annotations

import asyncio
import random
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol
from uuid import uuid4

from mindroom.logging_config import get_logger
from mindroom.reactive_triggers.models import ReactiveTriggerRecord, ScriptCheckCheckpoint, ScriptCheckSourceSpec
from mindroom.reactive_triggers.script_check import (
    check_script_check_source_access,
    normalize_script_check_event,
    validate_script_check_state,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

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
        max_concurrency: int = 4,
    ) -> None:
        self._store = store
        self._config_provider = config_provider
        self._runtime_paths = runtime_paths
        self._tick_runner = tick_runner
        self._on_enqueued = on_enqueued
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))
        self._owner_locks: dict[str, asyncio.Lock] = {}
        self._owner_waiters: dict[str, int] = {}
        self._in_flight_trigger_ids: set[str] = set()

    def set_tick_runner(self, tick_runner: ScriptCheckTickRunner | None) -> None:
        """Bind or replace the tick runner callable."""
        self._tick_runner = tick_runner

    @asynccontextmanager
    async def _owner_slot(self, owner_user_id: str) -> AsyncIterator[None]:
        lock = self._owner_locks.setdefault(owner_user_id, asyncio.Lock())
        self._owner_waiters[owner_user_id] = self._owner_waiters.get(owner_user_id, 0) + 1
        try:
            async with lock:
                yield
        finally:
            remaining = self._owner_waiters.get(owner_user_id, 1) - 1
            if remaining <= 0:
                self._owner_waiters.pop(owner_user_id, None)
                if self._owner_locks.get(owner_user_id) is lock:
                    self._owner_locks.pop(owner_user_id, None)
            else:
                self._owner_waiters[owner_user_id] = remaining

    async def poll_due_once(self, *, now: float | None = None) -> int:
        """Poll due script_check triggers once and return newly enqueued firing count."""
        pol = self._config_provider().external_trigger_policy
        if self._tick_runner is None or not (pol.enabled and pol.reactive_enabled and pol.script_check_enabled):
            return 0
        fixed_now = now is not None
        current_time = time.time() if now is None else now
        due = await asyncio.to_thread(self._store.list_due_script_check_triggers, now=current_time, limit=20)
        selected = [(r, cp) for r, cp in due if r.trigger_id not in self._in_flight_trigger_ids]
        if not selected:
            return 0
        self._in_flight_trigger_ids.update(r.trigger_id for r, _ in selected)
        outcomes = await asyncio.gather(
            *(self._poll_rule(r, cp, current_time, fixed_now=fixed_now) for r, cp in selected),
            return_exceptions=True,
        )
        for (rule, _), outcome in zip(selected, outcomes, strict=True):
            if isinstance(outcome, Exception):
                logger.warning("Unexpected script_check poll error", trigger_id=rule.trigger_id, error=str(outcome))
            elif isinstance(outcome, BaseException):
                raise outcome
        enqueued = sum(outcome is True for outcome in outcomes)
        if enqueued and self._on_enqueued is not None:
            self._on_enqueued()
        return enqueued

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

    async def _poll_rule(
        self,
        rule: ReactiveTriggerRecord,
        cp: ScriptCheckCheckpoint,
        now: float,
        *,
        fixed_now: bool,
    ) -> bool:
        try:
            async with self._owner_slot(rule.owner_user_id), self._semaphore:
                check_now = now if fixed_now else time.time()
                live = await asyncio.to_thread(self._load_live_rule_and_checkpoint, rule, cp, check_now)
                if live is None:
                    return False
                fresh_rule, fresh_cp = live
                return await self._execute_rule(fresh_rule, fresh_cp, check_now, fixed_now=fixed_now)
        finally:
            self._in_flight_trigger_ids.discard(rule.trigger_id)

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
        if unauth_code := check_script_check_source_access(
            owner_user_id=rule.owner_user_id,
            source_spec=rule.source,
            config=config,
            runtime_paths=self._runtime_paths,
        ):
            next_at = now + min(_UNAUTHORIZED_RECHECK_CAP_SECONDS, effective_interval)
            await self._save_non_ok(rule, cp, last_error_code=unauth_code, next_poll_at=next_at, updated_at=now)
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
        backoff = min(_MAX_BACKOFF_SECONDS, interval * (2 ** min(errors, 5)))
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
