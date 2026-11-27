"""Orchestration bridge and fixed stdlib bootstrap for scheduled script_check triggers."""

from __future__ import annotations

import asyncio
import contextlib
import math
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from mindroom.authorization import ReplyMembershipPendingError, is_sender_allowed_for_agent_reply_in_room
from mindroom.delegation.lifecycle import accessed_entities
from mindroom.logging_config import get_logger
from mindroom.matrix.room_reconciliation import read_room_state
from mindroom.message_target import MessageTarget
from mindroom.reactive_triggers.models import ReactiveTriggerRecord, ScriptCheckCheckpoint, ScriptCheckSourceSpec
from mindroom.reactive_triggers.script_check import (
    check_script_check_source_access,
    is_owner_joined_in_state,
    serving_entity_for_identity,
)
from mindroom.reactive_triggers.script_check_poller import ScriptCheckTickOutcome, ScriptCheckTickRunner
from mindroom.script_runs.manager import (
    ScriptRunManager,
    ScriptRunManagerError,
    ScriptRuntimeNotReadyError,
)
from mindroom.script_runs.models import TERMINAL_SCRIPT_RUN_STATES, ScriptRunRecord, ScriptRunState
from mindroom.script_runs.reasons import MAX_RUNTIME_EXCEEDED, OWNER_CANCELLATION, RUNTIME_SHUTDOWN
from mindroom.script_runs.store import ScriptRunNotFoundError, ScriptRunStoreError
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, parse_tool_execution_identity_payload

if TYPE_CHECKING:
    import nio

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.bot import AgentBot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.script_runs.broker import ScriptCheckTickSlot, ScriptToolBroker
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

logger = get_logger(__name__)

_RECONCILE_POLL_SECONDS = 0.25
_EXIT_GRACE_SECONDS = 2.0
_EXIT_GRACE_POLL_SECONDS = 0.05
_CANCEL_CLEANUP_TIMEOUT_SECONDS = 5.0

_SCRIPT_CHECK_BOOTSTRAP_SOURCE = """\
import hashlib, hmac, json, sys, traceback
from mindroom.script_runs.transport import ScriptGatewayTransport
from mindroom.script_sdk import MindRoomTools

_MAX_STATE_BYTES, _MAX_RESULT_BYTES, _MAX_ERROR_CHARS, _MISSING = 32 * 1024, 64 * 1024, 8000, object()

def _check_str_keys(value, label):
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise ValueError(f"script_check {label} objects must use string keys")
            _check_str_keys(v, label)
    elif isinstance(value, list):
        for item in value:
            _check_str_keys(item, label)

def _encode_json(value, label, max_bytes):
    _check_str_keys(value, label)
    raw = json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > max_bytes:
        raise ValueError(f"script_check {label} exceeds maximum size of {max_bytes} bytes")
    return raw

class _CheckState:
    def __init__(self, initial, is_active):
        self._data = json.loads(_encode_json(dict(initial), "state", _MAX_STATE_BYTES))
        self._is_active, self.changed = is_active, False

    def _require_active(self):
        if not self._is_active():
            raise RuntimeError("state mutations are not allowed during module load; mutate state inside check().")

    def _copy(self, value): return json.loads(json.dumps(value, allow_nan=False))
    def __contains__(self, key): return key in self._data
    def __getitem__(self, key): return self._copy(self._data[key])
    def get(self, key, default=None): return default if key not in self._data else self._copy(self._data[key])

    def set(self, key, value):
        self._require_active()
        if not isinstance(key, str):
            raise ValueError("state keys must be strings")
        candidate = {**self._data, key: value}
        self._data = json.loads(_encode_json(candidate, "state", _MAX_STATE_BYTES))
        self.changed = True

    def delete(self, key):
        self._require_active()
        if key in self._data:
            del self._data[key]
            self.changed = True

    def pop(self, key, default=_MISSING):
        self._require_active()
        if key in self._data:
            value = self.get(key)
            del self._data[key]
            self.changed = True
            return value
        if default is _MISSING:
            raise KeyError(key)
        return default

    def clear(self):
        self._require_active()
        if self._data:
            self._data.clear()
            self.changed = True

    def keys(self): return list(self._data.keys())
    def items(self): return [(k, self._copy(v)) for k, v in self._data.items()]
    def snapshot(self): return self._copy(self._data)

def _main():
    transport = ScriptGatewayTransport(timeout_seconds=10.0)
    run_id = transport.run_id
    payload = transport.request_json("GET", f"runs/{run_id}/check-input")
    if not isinstance(payload, dict):
        raise RuntimeError("Invalid check-input response.")
    source, digest, initial_state = payload.get("source"), payload.get("source_digest"), payload.get("state")
    if not isinstance(source, str) or not isinstance(digest, str) or not isinstance(initial_state, dict):
        raise RuntimeError("Malformed check-input payload.")
    if not hmac.compare_digest(hashlib.sha256(source.encode("utf-8")).hexdigest(), digest):
        raise RuntimeError("script_check source digest mismatch.")

    in_check = False
    state = _CheckState(initial_state, lambda: in_check)
    tools = MindRoomTools(http_timeout_seconds=10.0, poll_interval_seconds=0.1)

    def tool(toolkit_name, function_name, /, **arguments):
        if not in_check:
            raise RuntimeError("tool() is not allowed during module load; call tool() inside check().")
        return tools.call(toolkit_name, function_name, **arguments)

    def agent(*args, **kwargs):
        raise RuntimeError(
            "agent() is not allowed inside script_check triggers; "
            "return a dict payload from check() to wake the target agent."
        )

    try:
        ns = {"__name__": "__script_check__", "tool": tool, "state": state, "agent": agent}
        exec(compile(source, "<script_check>", "exec"), ns)
        check_fn = ns.get("check")
        if not callable(check_fn):
            raise RuntimeError("script_check source must define a callable check() function.")
        in_check = True
        try:
            result = check_fn()
        finally:
            in_check = False
        if result is not None:
            _encode_json(result, "result", _MAX_RESULT_BYTES)
        transport.request_json(
            "POST", f"runs/{run_id}/check-result",
            {"result": result, "state": state.snapshot(), "state_changed": state.changed, "error": None},
        )
    except Exception as exc:
        err = ("".join(traceback.format_exception_only(type(exc), exc)).strip() or repr(exc))[:_MAX_ERROR_CHARS]
        try:
            transport.request_json(
                "POST", f"runs/{run_id}/check-result",
                {"result": None, "state": {}, "state_changed": False, "error": err},
            )
        except Exception:
            pass
        print(err, file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    _main()
"""


def _observed_room_for_bot(bot: AgentBot, room_id: str) -> nio.MatrixRoom | None:
    rooms = getattr(bot.client, "rooms", None) if bot.client is not None else None
    return rooms.get(room_id) if isinstance(rooms, Mapping) else None


async def _prune_terminal(manager: ScriptRunManager, final: ScriptRunRecord | None) -> None:
    if final is None or final.state not in TERMINAL_SCRIPT_RUN_STATES or final.finished_at is None:
        return
    with contextlib.suppress(ScriptRunStoreError, OSError):
        await asyncio.to_thread(manager.store.prune_terminal_run, final.run_id, finished_before=final.finished_at)


async def _retire_and_prune(manager: ScriptRunManager, run_id: str, *, reason: str) -> ScriptRunRecord | None:
    async def _run() -> ScriptRunRecord | None:
        try:
            await manager.revoke(run_id, reason=reason)
            final = await manager.reconcile_durable(run_id=run_id)
        except (ScriptRunManagerError, ScriptRunNotFoundError, ScriptRunStoreError):
            return None
        await _prune_terminal(manager, final)
        return final

    with contextlib.suppress(TimeoutError):
        return await asyncio.wait_for(_run(), timeout=_CANCEL_CLEANUP_TIMEOUT_SECONDS)
    return None


async def _wait_for_exit_grace(manager: ScriptRunManager, run_id: str) -> ScriptRunRecord | None:
    grace_deadline = time.monotonic() + _EXIT_GRACE_SECONDS
    final: ScriptRunRecord | None = None
    while (remaining := grace_deadline - time.monotonic()) > 0:
        with contextlib.suppress(ScriptRunManagerError, TimeoutError):
            final = await asyncio.wait_for(manager.reconcile_durable(run_id=run_id), timeout=remaining)
        if final is not None and final.state in TERMINAL_SCRIPT_RUN_STATES:
            await _prune_terminal(manager, final)
            return final
        if (remaining := grace_deadline - time.monotonic()) <= 0:
            break
        await asyncio.sleep(min(_EXIT_GRACE_POLL_SECONDS, remaining))
    return await _retire_and_prune(manager, run_id, reason=MAX_RUNTIME_EXCEEDED)


async def _wait_for_tick(
    manager: ScriptRunManager,
    run_id: str,
    slot: ScriptCheckTickSlot,
    *,
    deadline: float,
) -> tuple[ScriptRunRecord | None, bool]:
    final: ScriptRunRecord | None = None
    while not slot.done.is_set() and (remaining := deadline - time.monotonic()) > 0:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(slot.done.wait(), timeout=min(_RECONCILE_POLL_SECONDS, remaining))
        if slot.done.is_set():
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        with contextlib.suppress(ScriptRunManagerError, TimeoutError):
            reconciled = await asyncio.wait_for(manager.reconcile_durable(run_id=run_id), timeout=remaining)
            if reconciled.state in TERMINAL_SCRIPT_RUN_STATES:
                final = reconciled
                break
    if slot.done.is_set():
        return await _wait_for_exit_grace(manager, run_id), False
    if final is not None:
        await _prune_terminal(manager, final)
        return final, False
    return await _retire_and_prune(manager, run_id, reason=MAX_RUNTIME_EXCEEDED), True


def _evaluate_outcome(
    run_id: str,
    slot: ScriptCheckTickSlot,
    final: ScriptRunRecord | None,
    *,
    timed_out: bool,
) -> ScriptCheckTickOutcome:
    error_code = (
        "approval_required"
        if slot.approval_denied
        else "timeout"
        if timed_out or (final is not None and final.cancellation_reason == MAX_RUNTIME_EXCEEDED)
        else "process_exited_without_result"
        if not slot.submitted
        else "script_error"
        if slot.error is not None
        else "nonzero_exit"
        if final is None or final.state is not ScriptRunState.EXITED or final.exit_code != 0
        else None
    )
    if error_code is not None:
        return ScriptCheckTickOutcome(status="error", run_id=run_id, error_code=error_code)
    return ScriptCheckTickOutcome(
        status="ok",
        run_id=run_id,
        result=slot.result,
        state=cast("dict[str, Any]", slot.state) if slot.state_changed and slot.state is not None else None,
    )


@dataclass(frozen=True, slots=True)
class _ScriptCheckRunner:
    runtime_paths: RuntimePaths
    bots: Callable[[], Mapping[str, AgentBot]]
    memberships: AgentReplyMembershipIndex
    manager_provider: Callable[[], ScriptRunManager | None]
    broker: ScriptToolBroker

    @staticmethod
    def _parse_identity(
        rule: ReactiveTriggerRecord,
        source_spec: ScriptCheckSourceSpec,
        checkpoint: ScriptCheckCheckpoint,
    ) -> ToolExecutionIdentity | ScriptCheckTickOutcome:
        try:
            identity = parse_tool_execution_identity_payload(
                checkpoint.identity_json,
                error_prefix="script_check execution_identity",
            )
        except (TypeError, ValueError):
            return ScriptCheckTickOutcome(status="unauthorized", error_code="invalid_execution_identity")
        if (
            identity.channel != "matrix"
            or identity.agent_name != source_spec.agent
            or identity.requester_id != rule.owner_user_id
            or identity.room_id != source_spec.room_id
            or identity.room_id is None
            or identity.session_id is None
        ):
            return ScriptCheckTickOutcome(status="unauthorized", error_code="identity_mismatch")
        return identity

    @staticmethod
    def _rebuild_context(
        rule: ReactiveTriggerRecord,
        identity: ToolExecutionIdentity,
        serving_bot: AgentBot,
        run_id: str,
    ) -> ToolRuntimeContext | ScriptCheckTickOutcome:
        assert identity.room_id is not None
        assert identity.session_id is not None
        target = MessageTarget(
            room_id=identity.room_id,
            source_thread_id=identity.thread_id,
            resolved_thread_id=identity.resolved_thread_id,
            reply_to_event_id=None,
            session_id=identity.session_id,
        )
        context = serving_bot.tool_runtime_support.build_context(
            target,
            user_id=identity.requester_id,
            agent_name=identity.agent_name,
            correlation_id=f"script-check:{rule.trigger_id}:{run_id}",
        )
        if context is None:
            return ScriptCheckTickOutcome(status="deferred", error_code="runtime_unavailable")
        if context.execution_identity.resolved_thread_id != identity.resolved_thread_id:
            return ScriptCheckTickOutcome(status="unauthorized", error_code="identity_mismatch")
        return context

    async def _check_live_room_authorization(
        self,
        rule: ReactiveTriggerRecord,
        source_spec: ScriptCheckSourceSpec,
        serving_entity: str,
        serving_bot: AgentBot,
        config: Config,
    ) -> ScriptCheckTickOutcome | None:
        observed_room = _observed_room_for_bot(serving_bot, source_spec.room_id)
        room_state = (
            None
            if observed_room is None or serving_bot.client is None
            else await read_room_state(serving_bot.client, source_spec.room_id)
        )
        if room_state is None:
            return ScriptCheckTickOutcome(status="deferred", error_code="source_room_state_unreadable")
        if not is_owner_joined_in_state(
            room_state,
            owner_user_id=rule.owner_user_id,
            config=config,
            runtime_paths=self.runtime_paths,
        ):
            return ScriptCheckTickOutcome(status="unauthorized", error_code="source_room_not_joined")
        entities = sorted(accessed_entities(config, serving_entity, source_spec.agent))
        try:
            allowed = all(
                is_sender_allowed_for_agent_reply_in_room(
                    rule.owner_user_id,
                    entity,
                    config,
                    source_spec.room_id,
                    self.runtime_paths,
                    self.memberships,
                    require_resolved_membership=True,
                    observed_room=observed_room,
                )
                for entity in entities
            )
        except ReplyMembershipPendingError:
            return ScriptCheckTickOutcome(status="deferred", error_code="runtime_unavailable")
        return None if allowed else ScriptCheckTickOutcome(status="unauthorized", error_code="owner_not_authorized")

    async def _prepare(
        self,
        rule: ReactiveTriggerRecord,
        checkpoint: ScriptCheckCheckpoint,
        config: Config,
        run_id: str,
    ) -> tuple[ScriptRunManager, ToolRuntimeContext, ScriptCheckSourceSpec] | ScriptCheckTickOutcome:
        if not isinstance(rule.source, ScriptCheckSourceSpec):
            return ScriptCheckTickOutcome(status="error", error_code="invalid_source_spec")
        source_spec = rule.source
        parsed = self._parse_identity(rule, source_spec, checkpoint)
        if isinstance(parsed, ScriptCheckTickOutcome):
            return parsed
        unauth_err = check_script_check_source_access(
            owner_user_id=rule.owner_user_id,
            source_spec=source_spec,
            config=config,
            runtime_paths=self.runtime_paths,
            require_placement=True,
        )
        if unauth_err is not None:
            return ScriptCheckTickOutcome(status="unauthorized", error_code=unauth_err)
        serving_entity = serving_entity_for_identity(parsed, source_spec.agent, config)
        bots = self.bots()
        manager, agent_bot, serving_bot = self.manager_provider(), bots.get(source_spec.agent), bots.get(serving_entity)
        if (
            manager is None
            or not (agent_bot and agent_bot.running)
            or not (serving_bot and serving_bot.running and serving_bot.client)
        ):
            return ScriptCheckTickOutcome(status="deferred", error_code="runtime_unavailable")
        if room_err := await self._check_live_room_authorization(
            rule,
            source_spec,
            serving_entity,
            serving_bot,
            config,
        ):
            return room_err
        rebuilt = self._rebuild_context(rule, parsed, serving_bot, run_id)
        return rebuilt if isinstance(rebuilt, ScriptCheckTickOutcome) else (manager, rebuilt, source_spec)

    async def _launch_tick(
        self,
        manager: ScriptRunManager,
        context: ToolRuntimeContext,
        rule: ReactiveTriggerRecord,
        run_id: str,
        *,
        timeout_seconds: int,
        deadline: float,
    ) -> ScriptCheckTickOutcome | None:
        if (remaining := deadline - time.monotonic()) <= 0.0:
            return ScriptCheckTickOutcome(status="error", run_id=run_id, error_code="launch_timeout")
        try:
            await asyncio.wait_for(
                manager.run(
                    context,
                    source=_SCRIPT_CHECK_BOOTSTRAP_SOURCE,
                    run_id=run_id,
                    script_check_rule_uid=rule.rule_uid,
                    max_runtime_seconds=max(1, min(timeout_seconds, math.ceil(remaining))),
                ),
                timeout=remaining,
            )
        except ScriptRuntimeNotReadyError:
            return ScriptCheckTickOutcome(status="deferred", run_id=run_id, error_code="runtime_unavailable")
        except TimeoutError:
            await _retire_and_prune(manager, run_id, reason=MAX_RUNTIME_EXCEEDED)
            return ScriptCheckTickOutcome(status="error", run_id=run_id, error_code="launch_timeout")
        except ScriptRunManagerError as exc:
            logger.warning("script_check launch failed", trigger_id=rule.trigger_id, run_id=run_id, error=str(exc))
            await _retire_and_prune(manager, run_id, reason=OWNER_CANCELLATION)
            return ScriptCheckTickOutcome(status="error", run_id=run_id, error_code="launch_failed")
        return None

    async def __call__(
        self,
        rule: ReactiveTriggerRecord,
        checkpoint: ScriptCheckCheckpoint,
        config: Config,
        _now: float,
    ) -> ScriptCheckTickOutcome:
        run_id = f"script-{uuid.uuid4().hex}"
        timeout_seconds = config.external_trigger_policy.script_check_timeout_seconds
        deadline = time.monotonic() + float(timeout_seconds)
        try:
            prepared = await asyncio.wait_for(
                self._prepare(rule, checkpoint, config, run_id),
                timeout=max(0.0, deadline - time.monotonic()),
            )
        except TimeoutError:
            return ScriptCheckTickOutcome(status="error", run_id=run_id, error_code="timeout")
        if isinstance(prepared, ScriptCheckTickOutcome):
            return prepared
        manager, context, source_spec = prepared
        with self.broker.check_tick(
            run_id,
            source=checkpoint.source_utf8,
            source_digest=source_spec.source_digest,
            initial_state=dict(checkpoint.state),
        ) as slot:
            try:
                if (
                    launch_err := await self._launch_tick(
                        manager,
                        context,
                        rule,
                        run_id,
                        timeout_seconds=timeout_seconds,
                        deadline=deadline,
                    )
                ) is not None:
                    return launch_err
                final, timed_out = await _wait_for_tick(manager, run_id, slot, deadline=deadline)
            except asyncio.CancelledError:
                with contextlib.suppress(Exception):
                    await _retire_and_prune(manager, run_id, reason=RUNTIME_SHUTDOWN)
                raise
        return _evaluate_outcome(run_id, slot, final, timed_out=timed_out)


def build_script_check_tick_runner(
    *,
    runtime_paths: RuntimePaths,
    bots: Callable[[], Mapping[str, AgentBot]],
    memberships: AgentReplyMembershipIndex,
    manager_provider: Callable[[], ScriptRunManager | None],
    broker: ScriptToolBroker,
) -> ScriptCheckTickRunner:
    """Build the orchestration tick runner bridging ScriptCheckPoller into ScriptRunManager."""
    return _ScriptCheckRunner(
        runtime_paths=runtime_paths,
        bots=bots,
        memberships=memberships,
        manager_provider=manager_provider,
        broker=broker,
    )
