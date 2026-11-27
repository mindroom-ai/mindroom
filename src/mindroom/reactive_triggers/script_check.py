"""Authoring validation, event normalization, access checks, and poller for script_check triggers."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import random
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, NoReturn, Protocol
from uuid import uuid4

from mindroom.logging_config import get_logger
from mindroom.reactive_triggers.models import (
    ReactiveTriggerRecord,
    ScriptCheckCheckpoint,
    ScriptCheckSourceSpec,
    TriggerEvent,
)
from mindroom.requester_identity import is_human_requester_id

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.reactive_triggers.store import ReactiveTriggerStore

logger = get_logger(__name__)

MAX_SCRIPT_CHECK_SOURCE_BYTES = 64 * 1024
MAX_SCRIPT_CHECK_STATE_BYTES = 32 * 1024
MAX_SCRIPT_CHECK_RESULT_BYTES = 64 * 1024
SCRIPT_CHECK_EVENT_KIND = "script_check.match"

_MAX_BACKOFF_SECONDS = 3600.0
_DEFERRED_RETRY_SECONDS = 30.0
_UNAUTHORIZED_RECHECK_CAP_SECONDS = 300.0
_ENVELOPE_STR_LIMITS = {"title": 500, "summary": 2000, "text": 8000, "actor": 256, "dedupe_key": 256}
_ENVELOPE_KEYS = frozenset(_ENVELOPE_STR_LIMITS) | {"data"}


def _reject(msg: str) -> NoReturn:
    raise ValueError(msg)


def validate_script_check_source(source_bytes: bytes) -> tuple[str, str]:
    """Validate script_check Python bytes and return ``(source_text, sha256_hex)``."""
    if len(source_bytes) > MAX_SCRIPT_CHECK_SOURCE_BYTES:
        _reject(f"script_check source exceeds maximum size of {MAX_SCRIPT_CHECK_SOURCE_BYTES} bytes")
    try:
        source_text = source_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        msg = f"script_check source must be valid UTF-8: {exc}"
        raise ValueError(msg) from exc
    if not source_text.strip():
        _reject("script_check source must not be empty")
    try:
        tree = ast.parse(source_text, filename="<script_check>")
    except (SyntaxError, ValueError) as exc:
        msg = f"Invalid Python syntax in script_check source: {exc}"
        raise ValueError(msg) from exc
    _validate_check_function(tree)
    return source_text, hashlib.sha256(source_bytes).hexdigest()


def _validate_check_function(tree: ast.Module) -> None:
    check_node: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "check":
            check_node = node
    match check_node:
        case None:
            _reject("script_check source must define a top-level synchronous 'def check():' function")
        case ast.AsyncFunctionDef():
            _reject("script_check check() must be synchronous (use 'def check():', not 'async def check():')")
        case ast.FunctionDef(args=args):
            if args.posonlyargs or args.args or any(d is None for d in args.kw_defaults):
                _reject("script_check check() must take no required or positional arguments; use global tool()/state")


def _ensure_json_str_keys(value: object, *, label: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                _reject(f"script_check {label} objects must use string keys")
            _ensure_json_str_keys(item, label=label)
    elif isinstance(value, list):
        for item in value:
            _ensure_json_str_keys(item, label=label)


def _validate_json_object(value: object, *, label: str, max_bytes: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        _reject(f"script_check {label} must be a dict, got {type(value).__name__}")
    _ensure_json_str_keys(value, label=label)
    try:
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        msg = f"script_check {label} must be strict finite JSON: {exc}"
        raise ValueError(msg) from exc
    if len(encoded) > max_bytes:
        _reject(f"script_check {label} exceeds maximum size of {max_bytes} bytes")
    parsed = json.loads(encoded)
    assert isinstance(parsed, dict)
    return parsed


def validate_script_check_state(state: object) -> dict[str, Any]:
    """Validate strict finite JSON state object within MAX_SCRIPT_CHECK_STATE_BYTES."""
    return _validate_json_object(state, label="state", max_bytes=MAX_SCRIPT_CHECK_STATE_BYTES)


def _extract_envelope_strings(payload: dict[str, Any]) -> dict[str, str | None]:
    envelope: dict[str, str | None] = {}
    for field, max_len in _ENVELOPE_STR_LIMITS.items():
        raw_val = payload.get(field)
        if raw_val is None:
            envelope[field] = None
            continue
        if not isinstance(raw_val, str):
            _reject(f"script_check result field '{field}' must be a string")
        if len(raw_val) > max_len:
            _reject(f"script_check result field '{field}' exceeds maximum length of {max_len}")
        stripped = raw_val.strip()
        if field == "dedupe_key" and not stripped:
            _reject("script_check result field 'dedupe_key' must not be empty")
        envelope[field] = stripped or None
    return envelope


def _extract_event_data(payload: dict[str, Any]) -> dict[str, Any]:
    extra = {k: v for k, v in payload.items() if k not in _ENVELOPE_KEYS}
    if "data" not in payload:
        return extra
    raw_data = payload["data"]
    if raw_data is not None and not isinstance(raw_data, dict):
        _reject("script_check result field 'data' must be a dict")
    if extra:
        _reject(f"script_check result cannot combine 'data' with extra top-level keys: {', '.join(sorted(extra))}")
    return {} if raw_data is None else raw_data


def normalize_script_check_event(
    trigger_id: str,
    run_id: str,
    raw_result: object,
    *,
    now: float,
) -> TriggerEvent | None:
    """Normalize a check() return value into a TriggerEvent or None."""
    if raw_result is None:
        return None
    payload = _validate_json_object(raw_result, label="result", max_bytes=MAX_SCRIPT_CHECK_RESULT_BYTES)
    env = _extract_envelope_strings(payload)
    event_data = _extract_event_data(payload)
    dedupe_key = env["dedupe_key"]
    if dedupe_key is not None:
        event_id = f"sc:key:{hashlib.sha256(f'{trigger_id}:{dedupe_key}'.encode()).hexdigest()[:32]}"
    else:
        event_id = f"sc:tick:{run_id}"
    return TriggerEvent(
        source_type="script_check",
        source_id=trigger_id,
        kind=SCRIPT_CHECK_EVENT_KIND,
        event_id=event_id,
        timestamp=now,
        actor=env["actor"],
        title=env["title"],
        summary=env["summary"],
        text=env["text"],
        data=event_data,
    )


def clamp_script_check_poll_interval(raw: int | None, *, min_seconds: int) -> int:
    """Validate and clamp a script_check poll_interval_seconds value."""
    if raw is not None and (isinstance(raw, bool) or not isinstance(raw, int)):
        msg = "poll_interval_seconds must be an integer."
        raise TypeError(msg)
    return max(min_seconds, min(300 if raw is None else raw, 86400))


def script_check_health_summary(checkpoint: ScriptCheckCheckpoint | None) -> dict[str, object] | None:
    """Return owner-visible script_check health counters without exposing code, identity, or state."""
    if checkpoint is None:
        return None
    return {
        "consecutive_errors": checkpoint.consecutive_errors,
        "last_error_code": checkpoint.last_error_code,
        "last_run_at": checkpoint.last_run_at,
    }


def build_script_check_dry_run_event(
    trigger_id: str,
    *,
    actor: str | None,
    text: str | None,
    sample_data: Mapping[str, Any] | None,
    now: float,
) -> TriggerEvent:
    """Build a synthetic script_check TriggerEvent for dry-run triage without running Python."""
    data = dict(sample_data) if sample_data is not None else {}
    raw_title = data.get("title")
    raw_summary = data.get("summary")
    return TriggerEvent(
        source_type="script_check",
        source_id=trigger_id,
        kind=SCRIPT_CHECK_EVENT_KIND,
        event_id="dry_run",
        timestamp=now,
        actor=actor,
        title=raw_title if isinstance(raw_title, str) else None,
        summary=raw_summary if isinstance(raw_summary, str) else None,
        text=text,
        data=data,
    )


def agent_supports_script_check(config: Config, agent_name: str) -> bool:
    """Return whether ``agent_name`` is configured with triggers plus script or workflow tools."""
    if agent_name not in config.agents:
        return False
    resolved = set(config.resolve_agent(agent_name).tools.tool_names)
    return "triggers" in resolved and bool({"script", "workflow"} & resolved)


def check_script_check_source_access(
    *,
    owner_user_id: str,
    source_spec: ScriptCheckSourceSpec,
    config: Config,
    runtime_paths: RuntimePaths,
) -> str | None:
    """Verify current policy, human owner, and executing-agent capability for a script_check rule."""
    pol = config.external_trigger_policy
    if not pol.enabled:
        return "external_triggers_disabled"
    if not pol.reactive_enabled:
        return "reactive_triggers_disabled"
    if not pol.script_check_enabled:
        return "script_check_disabled"
    if not is_human_requester_id(owner_user_id, config, runtime_paths):
        return "owner_not_human"
    if not agent_supports_script_check(config, source_spec.agent):
        return "agent_script_check_unavailable"
    return None


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
                return await self._execute_rule(rule, cp, now, fixed_now=fixed_now)
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
        if check_script_check_source_access(
            owner_user_id=rule.owner_user_id,
            source_spec=rule.source,
            config=config,
            runtime_paths=self._runtime_paths,
        ):
            await self._save_non_ok(
                rule,
                cp,
                next_poll_at=now + min(_UNAUTHORIZED_RECHECK_CAP_SECONDS, effective_interval),
                updated_at=now,
            )
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
            await self._save_non_ok(rule, cp, next_poll_at=now + _DEFERRED_RETRY_SECONDS, updated_at=now)
            return False
        if outcome.status == "unauthorized":
            await self._save_non_ok(
                rule,
                cp,
                next_poll_at=now + min(_UNAUTHORIZED_RECHECK_CAP_SECONDS, effective_interval),
                updated_at=now,
            )
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
