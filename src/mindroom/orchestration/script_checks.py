"""Orchestration bridge and fixed stdlib bootstrap for scheduled script_check triggers."""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from mindroom.authorization import ReplyMembershipPendingError, is_sender_allowed_for_agent_reply_in_room
from mindroom.logging_config import get_logger
from mindroom.matrix.room_reconciliation import read_room_state
from mindroom.message_target import MessageTarget
from mindroom.reactive_triggers.models import ReactiveTriggerRecord, ScriptCheckCheckpoint, ScriptCheckSourceSpec
from mindroom.reactive_triggers.script_check import (
    SCRIPT_CHECK_BOOTSTRAP_SOURCE,
    check_script_check_access,
    is_owner_joined_in_state,
)
from mindroom.reactive_triggers.script_check_poller import ScriptCheckTickOutcome, ScriptCheckTickRunner
from mindroom.script_runs.manager import ScriptRunManager, ScriptRunManagerError, ScriptRuntimeNotReadyError
from mindroom.script_runs.models import TERMINAL_SCRIPT_RUN_STATES, ScriptRunRecord, ScriptRunState
from mindroom.script_runs.reasons import (
    MAX_RUNTIME_EXCEEDED,
    OWNER_CANCELLATION,
    PROCESS_EXIT_OBSERVED,
    RUNTIME_SHUTDOWN,
)
from mindroom.script_runs.store import ScriptRunNotFoundError, ScriptRunStoreError
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, parse_tool_execution_identity_payload

if TYPE_CHECKING:
    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.bot import AgentBot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.script_runs.broker import ScriptCheckTickSlot, ScriptToolBroker
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

logger = get_logger(__name__)

_PREPARE_TIMEOUT_SECONDS = 15.0
_LAUNCH_TIMEOUT_SECONDS = 60.0
_RECONCILE_POLL_SECONDS = 0.25
_EXIT_GRACE_SECONDS = 2.0
_EXIT_GRACE_POLL_SECONDS = 0.05
_CLEANUP_TASK_TIMEOUT_SECONDS = 30.0
_STOP_DRAIN_TIMEOUT_SECONDS = 5.0


def _is_clean_exit(final: ScriptRunRecord | None) -> bool:
    return (
        final is not None
        and final.exit_code == 0
        and (
            final.state is ScriptRunState.EXITED
            or (final.finished_at is not None and final.cancellation_reason == PROCESS_EXIT_OBSERVED)
        )
    )


async def _retire_and_prune(
    manager: ScriptRunManager,
    run_id: str,
    *,
    reason: str | None,
    launch_task: asyncio.Task[ScriptRunRecord] | None = None,
) -> None:
    async def _run() -> None:
        if launch_task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.shield(launch_task)
        try:
            if reason is not None:
                with contextlib.suppress(ScriptRunNotFoundError):
                    await manager.revoke(run_id, reason=reason)
            final = await manager.reconcile_durable(run_id=run_id)
        except (ScriptRunManagerError, ScriptRunNotFoundError, ScriptRunStoreError):
            return
        if final.state in TERMINAL_SCRIPT_RUN_STATES and final.finished_at is not None:
            with contextlib.suppress(ScriptRunStoreError, OSError):
                await asyncio.to_thread(
                    manager.store.prune_terminal_run,
                    final.run_id,
                    finished_before=final.finished_at,
                )

    try:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(_run(), timeout=_CLEANUP_TASK_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        if launch_task is not None:
            launch_task.cancel()
        raise


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
        else None
        if _is_clean_exit(final)
        else "nonzero_exit"
    )
    if error_code is not None:
        return ScriptCheckTickOutcome(status="error", run_id=run_id, error_code=error_code)
    return ScriptCheckTickOutcome(
        status="ok",
        run_id=run_id,
        result=slot.result,
        state=cast("dict[str, Any]", slot.state) if slot.state_changed and slot.state is not None else None,
    )


async def _poll_exit(
    manager: ScriptRunManager,
    run_id: str,
    *,
    deadline: float,
    poll_interval: float,
    done_event: asyncio.Event | None = None,
) -> ScriptRunRecord | None:
    while (remaining := deadline - time.monotonic()) > 0:
        if done_event is not None:
            if done_event.is_set():
                return None
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(done_event.wait(), timeout=min(poll_interval, remaining))
            if done_event.is_set() or (remaining := deadline - time.monotonic()) <= 0:
                return None
        observed: ScriptRunRecord | None = None
        with contextlib.suppress(ScriptRunManagerError, TimeoutError):
            observed = await asyncio.wait_for(manager.observe_process_exit(run_id), timeout=remaining)
        if observed is not None:
            return observed
        if done_event is None and (remaining := deadline - time.monotonic()) > 0:
            await asyncio.sleep(min(poll_interval, remaining))
    return None


@dataclass(slots=True)
class _ScriptCheckRunner:
    runtime_paths: RuntimePaths
    bots: Callable[[], Mapping[str, AgentBot]]
    memberships: AgentReplyMembershipIndex
    manager_provider: Callable[[], ScriptRunManager | None]
    broker: ScriptToolBroker
    _cleanup_tasks: set[asyncio.Task[object]] = field(default_factory=set, init=False, repr=False)

    def _schedule_cleanup(
        self,
        manager: ScriptRunManager,
        run_id: str,
        *,
        reason: str | None = None,
        launch_task: asyncio.Task[ScriptRunRecord] | None = None,
    ) -> None:
        task: asyncio.Task[object] = asyncio.create_task(
            _retire_and_prune(manager, run_id, reason=reason, launch_task=launch_task),
            name=f"script_check_cleanup_{run_id}",
        )
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    async def _revoke_and_cleanup(
        self,
        manager: ScriptRunManager,
        run_id: str,
        *,
        reason: str,
        launch_task: asyncio.Task[ScriptRunRecord] | None = None,
    ) -> None:
        with contextlib.suppress(ScriptRunNotFoundError, ScriptRunStoreError):
            await asyncio.to_thread(manager.request_revocation, run_id, reason=reason)
        with contextlib.suppress(Exception):
            await self.broker.cancel_run(run_id)
        if launch_task is not None:
            launch_task.cancel()
        self._schedule_cleanup(manager, run_id, reason=reason, launch_task=launch_task)

    async def stop(self) -> None:
        """Drain tracked launch and worker-retirement cleanup tasks with a bounded shutdown timeout."""
        if not (tasks := list(self._cleanup_tasks)):
            return
        _, pending = await asyncio.wait(tasks, timeout=_STOP_DRAIN_TIMEOUT_SECONDS)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    @staticmethod
    def _parse_identity(
        rule: ReactiveTriggerRecord,
        source_spec: ScriptCheckSourceSpec,
        checkpoint: ScriptCheckCheckpoint,
    ) -> ToolExecutionIdentity | ScriptCheckTickOutcome:
        try:
            identity = parse_tool_execution_identity_payload(
                cast("Mapping[str, Any]", checkpoint.identity_json),
                error_prefix="script_check execution_identity",
            )
        except (TypeError, ValueError):
            return ScriptCheckTickOutcome(status="unauthorized", error_code="invalid_identity")
        if (
            identity.channel != "matrix"
            or identity.agent_name != source_spec.agent
            or identity.recipient != source_spec.agent
            or identity.requester_id != rule.owner_user_id
            or identity.room_id != source_spec.room_id
            or identity.session_id is None
        ):
            return ScriptCheckTickOutcome(status="unauthorized", error_code="identity_mismatch")
        return identity

    @staticmethod
    def _rebuild_context(
        rule: ReactiveTriggerRecord,
        identity: ToolExecutionIdentity,
        agent_bot: AgentBot,
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
        context = agent_bot.tool_runtime_support.build_context(
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

    async def _check_room_access(
        self,
        rule: ReactiveTriggerRecord,
        source_spec: ScriptCheckSourceSpec,
        agent_bot: AgentBot,
        config: Config,
    ) -> ScriptCheckTickOutcome | None:
        rooms = getattr(agent_bot.client, "rooms", None) if agent_bot.client is not None else None
        observed_room = rooms.get(source_spec.room_id) if isinstance(rooms, Mapping) else None
        room_state = (
            None
            if observed_room is None or agent_bot.client is None
            else await read_room_state(agent_bot.client, source_spec.room_id)
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
        try:
            allowed = is_sender_allowed_for_agent_reply_in_room(
                rule.owner_user_id,
                source_spec.agent,
                config,
                source_spec.room_id,
                self.runtime_paths,
                self.memberships,
                require_resolved_membership=True,
                observed_room=observed_room,
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
        if failure := check_script_check_access(
            config,
            self.runtime_paths,
            source_spec.agent,
            owner_user_id=rule.owner_user_id,
            require_placement=True,
        ):
            return ScriptCheckTickOutcome(status="unauthorized", error_code=failure.code)
        manager, agent_bot = self.manager_provider(), self.bots().get(source_spec.agent)
        if manager is None or not (agent_bot and agent_bot.running and agent_bot.client):
            return ScriptCheckTickOutcome(status="deferred", error_code="runtime_unavailable")
        if room_err := await self._check_room_access(rule, source_spec, agent_bot, config):
            return room_err
        rebuilt = self._rebuild_context(rule, parsed, agent_bot, run_id)
        return rebuilt if isinstance(rebuilt, ScriptCheckTickOutcome) else (manager, rebuilt, source_spec)

    async def _launch_tick(
        self,
        manager: ScriptRunManager,
        context: ToolRuntimeContext,
        rule: ReactiveTriggerRecord,
        run_id: str,
        *,
        timeout_seconds: int,
    ) -> ScriptCheckTickOutcome | None:
        launch_task: asyncio.Task[ScriptRunRecord] = asyncio.create_task(
            manager.run(
                context,
                source=SCRIPT_CHECK_BOOTSTRAP_SOURCE,
                run_id=run_id,
                script_check_rule_uid=rule.rule_uid,
                max_runtime_seconds=timeout_seconds,
            ),
            name=f"script_check_launch_{run_id}",
        )
        try:
            done, _ = await asyncio.wait({launch_task}, timeout=_LAUNCH_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            await self._revoke_and_cleanup(manager, run_id, reason=RUNTIME_SHUTDOWN, launch_task=launch_task)
            raise
        if not done:
            await self._revoke_and_cleanup(manager, run_id, reason=MAX_RUNTIME_EXCEEDED, launch_task=launch_task)
            return ScriptCheckTickOutcome(status="error", run_id=run_id, error_code="launch_timeout")
        try:
            await launch_task
        except ScriptRuntimeNotReadyError:
            return ScriptCheckTickOutcome(status="deferred", run_id=run_id, error_code="runtime_unavailable")
        except ScriptRunManagerError as exc:
            logger.warning("script_check launch failed", trigger_id=rule.trigger_id, run_id=run_id, error=str(exc))
            await self._revoke_and_cleanup(manager, run_id, reason=OWNER_CANCELLATION)
            return ScriptCheckTickOutcome(status="error", run_id=run_id, error_code="launch_failed")
        return None

    async def _wait_for_tick(
        self,
        manager: ScriptRunManager,
        run_id: str,
        slot: ScriptCheckTickSlot,
        *,
        deadline: float,
    ) -> tuple[ScriptRunRecord | None, bool]:
        observed = await _poll_exit(
            manager,
            run_id,
            deadline=deadline,
            poll_interval=_RECONCILE_POLL_SECONDS,
            done_event=slot.done,
        )
        if slot.done.is_set():
            grace = time.monotonic() + _EXIT_GRACE_SECONDS
            observed = await _poll_exit(manager, run_id, deadline=grace, poll_interval=_EXIT_GRACE_POLL_SECONDS)
        if observed is not None:
            self._schedule_cleanup(manager, run_id)
            return observed, False
        await self._revoke_and_cleanup(manager, run_id, reason=MAX_RUNTIME_EXCEEDED)
        return None, True

    async def __call__(
        self,
        rule: ReactiveTriggerRecord,
        checkpoint: ScriptCheckCheckpoint,
        config: Config,
        _now: float,
    ) -> ScriptCheckTickOutcome:
        run_id = f"script-{uuid.uuid4().hex}"
        timeout_seconds = config.external_trigger_policy.script_check_timeout_seconds
        try:
            prepared = await asyncio.wait_for(
                self._prepare(rule, checkpoint, config, run_id),
                timeout=min(_PREPARE_TIMEOUT_SECONDS, float(timeout_seconds)),
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
            if launch_err := await self._launch_tick(manager, context, rule, run_id, timeout_seconds=timeout_seconds):
                return launch_err
            try:
                exec_deadline = time.monotonic() + float(timeout_seconds)
                final, timed_out = await self._wait_for_tick(manager, run_id, slot, deadline=exec_deadline)
            except asyncio.CancelledError:
                await self._revoke_and_cleanup(manager, run_id, reason=RUNTIME_SHUTDOWN)
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
