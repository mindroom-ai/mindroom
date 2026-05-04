"""Runtime integration for destructive history compaction."""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Literal

from agno.db.base import SessionType
from agno.session.agent import AgentSession
from agno.session.team import TeamSession

from mindroom import model_loading
from mindroom.agent_storage import (
    create_session_storage,
    create_state_storage,
    get_agent_runtime_state_dbs,
    get_agent_session,
    get_team_session,
)
from mindroom.history.agno_compaction_request import (
    CompactionProviderRequestBuilder,
    build_agent_compaction_provider_request,
    build_team_compaction_provider_request,
    estimate_agent_static_tokens,
    estimate_team_static_tokens,
)
from mindroom.history.compaction import (
    compact_scope_history,
    completed_top_level_runs,
    estimate_prompt_visible_history_tokens,
    estimate_session_summary_tokens,
    runs_for_scope,
)
from mindroom.history.policy import (
    classify_compaction_decision,
    describe_compaction_unavailability,
    resolve_history_execution_plan,
)
from mindroom.history.storage import (
    clear_force_compaction_state,
    consume_pending_force_compaction_scope,
    read_scope_state,
    write_scope_state,
)
from mindroom.history.types import (
    CompactionDecision,
    CompactionLifecycleFailure,
    CompactionLifecycleProgress,
    CompactionLifecycleStart,
    CompactionLifecycleSuccess,
    CompactionReplyOutcome,
    HistoryPolicy,
    HistoryScope,
    HistoryScopeState,
    PreparedHistoryState,
    ResolvedHistoryExecutionPlan,
    ResolvedHistorySettings,
    ResolvedReplayPlan,
)
from mindroom.logging_config import get_logger
from mindroom.timing import timed
from mindroom.token_budget import estimate_text_tokens

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator
    from pathlib import Path

    from agno.agent import Agent
    from agno.db.base import BaseDb
    from agno.models.base import Model
    from agno.team import Team

    from mindroom.config.main import Config
    from mindroom.config.models import CompactionConfig
    from mindroom.constants import RuntimePaths
    from mindroom.history.types import CompactionLifecycle, CompactionOutcome
    from mindroom.timing import DispatchPipelineTiming
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)

_TEAM_STATE_ROOT_DIRNAME = "teams"
_TEAM_STORAGE_NAME_PATTERN = re.compile(r"[^a-zA-Z0-9_]+")


def _elapsed_ms(start: float) -> int:
    """Return elapsed monotonic milliseconds."""
    return int((time.monotonic() - start) * 1000)


def _compaction_failure_status(error: BaseException) -> Literal["failed", "timeout"]:
    failure_reason = str(error) or type(error).__name__
    if isinstance(error, TimeoutError) or "timed out" in failure_reason.casefold():
        return "timeout"
    return "failed"


@timed("system_prompt_assembly.history_prepare.compaction_model_init")
def _load_compaction_model(
    config: Config,
    runtime_paths: RuntimePaths,
    model_name: str,
) -> Model:
    """Load the compaction model with dedicated history-preparation timing."""
    return model_loading.get_model_instance(config, runtime_paths, model_name)


@dataclass(frozen=True)
class ScopeSessionContext:
    """Resolved storage/session context for one logical history scope."""

    scope: HistoryScope
    storage: BaseDb
    session: AgentSession | TeamSession | None
    session_id: str | None = None


@dataclass(frozen=True)
class BoundTeamScopeContext:
    """Resolved stable owner and scope for one live team run."""

    owner_agent: Agent
    owner_agent_name: str
    scope: HistoryScope


def note_prepared_history_timing(
    pipeline_timing: DispatchPipelineTiming | None,
    prepared_history: PreparedHistoryState,
) -> None:
    """Attach reply-level history metadata to the dispatch timing summary."""
    if pipeline_timing is None:
        return
    decision = prepared_history.compaction_decision
    pipeline_timing.note(
        compaction_decision=decision.mode,
        compaction_reply_outcome=prepared_history.compaction_reply_outcome,
        compaction_reason=decision.reason,
        compaction_current_history_tokens=decision.current_history_tokens,
        compaction_trigger_budget_tokens=decision.trigger_budget_tokens,
        compaction_hard_budget_tokens=decision.hard_budget_tokens,
        compaction_fitted_replay_tokens=decision.fitted_replay_tokens,
        prepared_context_tokens=prepared_history.prepared_context_tokens,
        fitted_replay_tokens=(
            prepared_history.replay_plan.estimated_tokens if prepared_history.replay_plan is not None else None
        ),
    )


def _sync_loaded_session(
    *,
    session: AgentSession | TeamSession,
    target_session: AgentSession | TeamSession,
) -> None:
    session.metadata = target_session.metadata
    session.runs = target_session.runs
    session.summary = target_session.summary


def _clear_forced_compaction_after_failure(
    *,
    storage: BaseDb,
    session: AgentSession | TeamSession | None,
    scope: HistoryScope,
    state: HistoryScopeState,
) -> None:
    """Clear a consumed manual force marker after a compaction failure."""
    if session is None or not state.force_compact_before_next_run:
        return
    session_type = SessionType.TEAM if isinstance(session, TeamSession) else SessionType.AGENT
    latest_session = storage.get_session(session_id=session.session_id, session_type=session_type)
    target_session = latest_session if isinstance(latest_session, type(session)) else session
    latest_state = read_scope_state(target_session, scope)
    cleared_state = replace(latest_state, force_compact_before_next_run=False)
    if cleared_state == latest_state:
        _sync_loaded_session(session=session, target_session=target_session)
        return
    write_scope_state(target_session, scope, cleared_state)
    storage.upsert_session(target_session)
    _sync_loaded_session(session=session, target_session=target_session)


@dataclass(frozen=True)
class _ResolvedPreparationInputs:
    history_settings: ResolvedHistorySettings
    compaction_config: CompactionConfig
    has_authored_compaction_config: bool
    active_model_name: str
    active_context_window: int | None
    static_prompt_tokens: int
    execution_plan: ResolvedHistoryExecutionPlan


@dataclass(frozen=True)
class _ScopeCompactionLifecycleResult:
    outcome: CompactionOutcome | None
    reply_outcome: CompactionReplyOutcome


@dataclass(frozen=True)
class PreparedScopeHistory:
    """Durable history preparation result before final replay planning."""

    scope: HistoryScope | None
    session: AgentSession | TeamSession | None
    resolved_inputs: _ResolvedPreparationInputs
    compaction_outcomes: list[CompactionOutcome] = field(default_factory=list)
    compaction_decision: CompactionDecision = field(
        default_factory=lambda: CompactionDecision(mode="none", reason="unclassified"),
    )
    compaction_reply_outcome: CompactionReplyOutcome = "none"
    prepared_context_tokens: int | None = None


async def _start_compaction_lifecycle(
    lifecycle: CompactionLifecycle | None,
    event: CompactionLifecycleStart,
) -> str | None:
    if lifecycle is None:
        return None
    try:
        return await lifecycle.start(event)
    except Exception:
        logger.exception(
            "Failed to send compaction lifecycle notice",
            session_id=event.session_id,
            scope=event.scope,
        )
        return None


async def _complete_compaction_lifecycle_success(
    lifecycle: CompactionLifecycle | None,
    event: CompactionLifecycleSuccess,
) -> None:
    if lifecycle is None or event.notice_event_id is None:
        return
    try:
        await lifecycle.complete_success(event)
    except Exception:
        logger.exception(
            "Failed to edit compaction lifecycle success notice",
            session_id=event.outcome.session_id,
            scope=event.outcome.scope,
        )


async def _update_compaction_lifecycle_progress(
    lifecycle: CompactionLifecycle | None,
    event: CompactionLifecycleProgress,
) -> None:
    if lifecycle is None or event.notice_event_id is None:
        return
    try:
        await lifecycle.progress(event)
    except Exception:
        logger.exception(
            "Failed to edit compaction lifecycle progress notice",
            session_id=event.session_id,
            scope=event.scope,
        )


def _compaction_progress_callback(
    lifecycle: CompactionLifecycle | None,
    compaction_start: float,
) -> Callable[[CompactionLifecycleProgress], Awaitable[None]] | None:
    """Return a progress callback for visible compactions."""
    if lifecycle is None:
        return None

    async def progress_callback(event: CompactionLifecycleProgress) -> None:
        await _update_compaction_lifecycle_progress(
            lifecycle,
            replace(event, duration_ms=_elapsed_ms(compaction_start)),
        )

    return progress_callback


async def _complete_compaction_lifecycle_failure(
    lifecycle: CompactionLifecycle | None,
    event: CompactionLifecycleFailure,
) -> None:
    if lifecycle is None or event.notice_event_id is None:
        return
    try:
        await lifecycle.complete_failure(event)
    except Exception:
        logger.exception(
            "Failed to edit compaction lifecycle failure notice",
            session_id=event.session_id,
            scope=event.scope,
            status=event.status,
        )


async def _complete_no_compactable_history_failure(
    lifecycle: CompactionLifecycle | None,
    *,
    notice_event_id: str | None,
    mode: Literal["auto", "manual"],
    session_id: str,
    scope: str,
    summary_model: str,
    duration_ms: int,
    history_budget_tokens: int | None,
) -> None:
    await _complete_compaction_lifecycle_failure(
        lifecycle,
        CompactionLifecycleFailure(
            notice_event_id=notice_event_id,
            mode=mode,
            session_id=session_id,
            scope=scope,
            summary_model=summary_model,
            status="failed",
            duration_ms=duration_ms,
            failure_reason="No compactable history remained.",
            history_budget_tokens=history_budget_tokens,
        ),
    )


def resolve_history_scope(agent: Agent) -> HistoryScope | None:
    """Return the persisted history scope addressed by one live agent."""
    team_id = agent.team_id
    if isinstance(team_id, str) and team_id:
        return HistoryScope(kind="team", scope_id=team_id)
    agent_id = agent.id
    if isinstance(agent_id, str) and agent_id:
        return HistoryScope(kind="agent", scope_id=agent_id)
    return None


@timed("system_prompt_assembly.history_prepare.scope_history")
async def prepare_scope_history(  # noqa: C901
    *,
    agent: Agent,
    agent_name: str,
    full_prompt: str,
    runtime_paths: RuntimePaths,
    config: Config,
    compaction_outcomes_collector: list[CompactionOutcome] | None = None,
    scope_context: ScopeSessionContext | None = None,
    history_settings: ResolvedHistorySettings | None = None,
    compaction_config: CompactionConfig | None = None,
    has_authored_compaction_config: bool | None = None,
    active_model_name: str | None = None,
    active_context_window: int | None = None,
    static_prompt_tokens: int | None = None,
    available_history_budget: int | None = None,
    scope: HistoryScope | None = None,
    execution_plan: ResolvedHistoryExecutionPlan | None = None,
    timing_scope: str | None = None,
    compaction_lifecycle: CompactionLifecycle | None = None,
    pipeline_timing: DispatchPipelineTiming | None = None,
    active_summary_model: Model | None = None,
    provider_request_builder: CompactionProviderRequestBuilder | None = None,
) -> PreparedScopeHistory:
    """Prepare durable scope history before final replay planning."""
    resolved_inputs = _resolve_preparation_inputs(
        agent=agent,
        agent_name=agent_name,
        full_prompt=full_prompt,
        config=config,
        history_settings=history_settings,
        compaction_config=compaction_config,
        has_authored_compaction_config=has_authored_compaction_config,
        active_model_name=active_model_name,
        active_context_window=active_context_window,
        static_prompt_tokens=static_prompt_tokens,
        execution_plan=execution_plan,
    )
    resolved_scope = scope or resolve_history_scope(agent)
    active_provider_request_builder = provider_request_builder
    if active_provider_request_builder is None and active_summary_model is agent.model:
        active_provider_request_builder = partial(build_agent_compaction_provider_request, agent=agent)
    if scope_context is None or scope_context.session is None:
        return PreparedScopeHistory(
            scope=resolved_scope,
            session=None,
            resolved_inputs=resolved_inputs,
            compaction_decision=CompactionDecision(mode="none", reason="missing_session"),
        )

    execution_plan = resolved_inputs.execution_plan
    trigger_history_budget = available_history_budget
    if trigger_history_budget is None:
        trigger_history_budget = execution_plan.replay_budget_tokens
    hard_history_budget = available_history_budget
    if hard_history_budget is None:
        hard_history_budget = execution_plan.hard_replay_budget_tokens or execution_plan.replay_budget_tokens

    session = scope_context.session
    if pipeline_timing is not None:
        pipeline_timing.mark("history_classify_start")
    state = _prepare_scope_state_for_run(
        storage=scope_context.storage,
        session=session,
        scope=scope_context.scope,
        execution_plan=execution_plan,
    )
    compaction_outcomes: list[CompactionOutcome] = []
    compaction_reply_outcome: CompactionReplyOutcome = "none"
    current_history_tokens = estimate_prompt_visible_history_tokens(
        session=session,
        scope=scope_context.scope,
        history_settings=resolved_inputs.history_settings,
    )
    visible_runs = runs_for_scope(completed_top_level_runs(session), scope_context.scope)
    compaction_decision = classify_compaction_decision(
        plan=execution_plan,
        force_compact_before_next_run=state.force_compact_before_next_run,
        current_history_tokens=current_history_tokens,
        trigger_budget_tokens=trigger_history_budget,
        hard_budget_tokens=hard_history_budget,
    )
    logger.info(
        "History preparation check",
        agent=agent_name,
        auto_enabled=execution_plan.authored_compaction_enabled and execution_plan.destructive_compaction_available,
        compaction_available=execution_plan.destructive_compaction_available,
        trigger_budget=trigger_history_budget,
        hard_budget=hard_history_budget,
        replay_window=execution_plan.replay_window_tokens,
        static_prompt_tokens=execution_plan.static_prompt_tokens,
        current_tokens=current_history_tokens,
        force=state.force_compact_before_next_run,
        compaction_decision=compaction_decision.mode,
        compaction_reason=compaction_decision.reason,
        unavailable_reason=execution_plan.unavailable_reason,
    )
    if pipeline_timing is not None:
        pipeline_timing.mark("history_classify_ready")
        pipeline_timing.note(
            compaction_decision=compaction_decision.mode,
            compaction_reason=compaction_decision.reason,
            compaction_current_history_tokens=current_history_tokens,
            compaction_trigger_budget_tokens=compaction_decision.trigger_budget_tokens,
            compaction_hard_budget_tokens=compaction_decision.hard_budget_tokens,
            compaction_fitted_replay_tokens=compaction_decision.fitted_replay_tokens,
        )

    if compaction_decision.mode == "required":
        if pipeline_timing is not None:
            pipeline_timing.mark("required_compaction_start")
        compaction_result = await _run_scope_compaction_with_lifecycle(
            mode="manual" if state.force_compact_before_next_run else "auto",
            storage=scope_context.storage,
            session=session,
            scope=scope_context.scope,
            state=state,
            resolved_inputs=resolved_inputs,
            history_budget=hard_history_budget,
            current_history_tokens=current_history_tokens,
            runs_before=len(visible_runs),
            config=config,
            runtime_paths=runtime_paths,
            timing_scope=timing_scope,
            compaction_lifecycle=compaction_lifecycle,
            active_summary_model=active_summary_model,
            provider_request_builder=active_provider_request_builder,
        )
        outcome = compaction_result.outcome
        compaction_reply_outcome = compaction_result.reply_outcome
        if outcome is not None:
            compaction_outcomes.append(outcome)
            logger.info(
                "Compaction completed",
                agent=agent_name,
                outcome_mode=outcome.mode,
                before_tokens=outcome.before_tokens,
                after_tokens=outcome.after_tokens,
                runs_compacted=outcome.compacted_run_count,
            )
        if pipeline_timing is not None:
            pipeline_timing.mark("required_compaction_ready")
            pipeline_timing.note(compaction_reply_outcome=compaction_reply_outcome)
    if compaction_outcomes_collector is not None:
        compaction_outcomes_collector.extend(compaction_outcomes)
    return PreparedScopeHistory(
        scope=scope_context.scope,
        session=scope_context.session,
        resolved_inputs=resolved_inputs,
        compaction_outcomes=compaction_outcomes,
        compaction_decision=compaction_decision,
        compaction_reply_outcome=compaction_reply_outcome,
    )


async def _run_scope_compaction_with_lifecycle(
    *,
    mode: Literal["auto", "manual"],
    storage: BaseDb,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    state: HistoryScopeState,
    resolved_inputs: _ResolvedPreparationInputs,
    history_budget: int | None,
    current_history_tokens: int,
    runs_before: int,
    config: Config,
    runtime_paths: RuntimePaths,
    timing_scope: str | None,
    compaction_lifecycle: CompactionLifecycle | None,
    active_summary_model: Model | None,
    provider_request_builder: CompactionProviderRequestBuilder | None,
) -> _ScopeCompactionLifecycleResult:
    execution_plan = resolved_inputs.execution_plan
    assert execution_plan.summary_input_budget_tokens is not None
    visible_compaction_lifecycle = compaction_lifecycle if runs_before else None
    compaction_start = time.monotonic()
    notice_event_id = await _start_compaction_lifecycle(
        visible_compaction_lifecycle,
        CompactionLifecycleStart(
            mode=mode,
            session_id=session.session_id,
            scope=scope.key,
            summary_model=execution_plan.compaction_model_name,
            before_tokens=current_history_tokens,
            history_budget_tokens=history_budget,
            runs_before=runs_before,
            threshold_tokens=execution_plan.trigger_threshold_tokens,
        ),
    )
    try:
        outcome = await _run_scope_compaction(
            storage=storage,
            session=session,
            scope=scope,
            state=state,
            resolved_inputs=resolved_inputs,
            history_budget=history_budget,
            config=config,
            runtime_paths=runtime_paths,
            timing_scope=timing_scope,
            lifecycle_notice_event_id=notice_event_id,
            progress_callback=_compaction_progress_callback(visible_compaction_lifecycle, compaction_start),
            active_summary_model=active_summary_model,
            provider_request_builder=provider_request_builder,
        )
    except asyncio.CancelledError as error:
        duration_ms = _elapsed_ms(compaction_start)
        await _complete_compaction_lifecycle_failure(
            visible_compaction_lifecycle,
            CompactionLifecycleFailure(
                notice_event_id=notice_event_id,
                mode=mode,
                session_id=session.session_id,
                scope=scope.key,
                summary_model=execution_plan.compaction_model_name,
                status="failed",
                duration_ms=duration_ms,
                failure_reason=str(error) or type(error).__name__,
                history_budget_tokens=history_budget,
            ),
        )
        raise
    except Exception as error:
        _clear_forced_compaction_after_failure(
            storage=storage,
            session=session,
            scope=scope,
            state=state,
        )
        duration_ms = _elapsed_ms(compaction_start)
        failure_reason = str(error) or type(error).__name__
        status = _compaction_failure_status(error)
        await _complete_compaction_lifecycle_failure(
            visible_compaction_lifecycle,
            CompactionLifecycleFailure(
                notice_event_id=notice_event_id,
                mode=mode,
                session_id=session.session_id,
                scope=scope.key,
                summary_model=execution_plan.compaction_model_name,
                status=status,
                duration_ms=duration_ms,
                failure_reason=failure_reason,
                history_budget_tokens=history_budget,
            ),
        )
        logger.exception(
            "Compaction failed; continuing without compaction",
            session_id=session.session_id,
            scope=scope.key,
            force_compact_before_next_run=state.force_compact_before_next_run,
        )
        return _ScopeCompactionLifecycleResult(
            outcome=None,
            reply_outcome="timeout" if status == "timeout" else "failed",
        )

    duration_ms = _elapsed_ms(compaction_start)
    if outcome is None:
        await _complete_no_compactable_history_failure(
            visible_compaction_lifecycle,
            notice_event_id=notice_event_id,
            mode=mode,
            session_id=session.session_id,
            scope=scope.key,
            summary_model=execution_plan.compaction_model_name,
            duration_ms=duration_ms,
            history_budget_tokens=history_budget,
        )
        return _ScopeCompactionLifecycleResult(outcome=None, reply_outcome="failed")

    outcome = replace(
        outcome,
        lifecycle_notice_event_id=notice_event_id,
        duration_ms=duration_ms,
    )
    await _complete_compaction_lifecycle_success(
        visible_compaction_lifecycle,
        CompactionLifecycleSuccess(
            notice_event_id=notice_event_id,
            outcome=outcome,
            duration_ms=duration_ms,
        ),
    )
    return _ScopeCompactionLifecycleResult(outcome=outcome, reply_outcome="success")


async def _run_scope_compaction(
    *,
    storage: BaseDb,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    state: HistoryScopeState,
    resolved_inputs: _ResolvedPreparationInputs,
    history_budget: int | None,
    config: Config,
    runtime_paths: RuntimePaths,
    timing_scope: str | None,
    lifecycle_notice_event_id: str | None = None,
    progress_callback: Callable[[CompactionLifecycleProgress], Awaitable[None]] | None = None,
    active_summary_model: Model | None = None,
    provider_request_builder: CompactionProviderRequestBuilder | None = None,
) -> CompactionOutcome | None:
    execution_plan = resolved_inputs.execution_plan
    assert execution_plan.summary_input_budget_tokens is not None
    use_active_summary_model = (
        active_summary_model is not None and execution_plan.compaction_model_name == resolved_inputs.active_model_name
    )
    if use_active_summary_model:
        summary_model = active_summary_model
    else:
        summary_model = _load_compaction_model(
            config,
            runtime_paths,
            execution_plan.compaction_model_name,
        )
    assert summary_model is not None
    _next_state, outcome = await compact_scope_history(
        storage=storage,
        session=session,
        scope=scope,
        state=state,
        history_settings=resolved_inputs.history_settings,
        available_history_budget=history_budget,
        summary_input_budget=execution_plan.summary_input_budget_tokens,
        summary_model=summary_model,
        summary_model_name=execution_plan.compaction_model_name,
        compaction_context_window=execution_plan.compaction_context_window,
        active_context_window=resolved_inputs.active_context_window,
        replay_window_tokens=execution_plan.replay_window_tokens,
        threshold_tokens=execution_plan.trigger_threshold_tokens,
        reserve_tokens=execution_plan.reserve_tokens,
        timing_scope=timing_scope,
        lifecycle_notice_event_id=lifecycle_notice_event_id,
        progress_callback=progress_callback,
        provider_request_builder=provider_request_builder if use_active_summary_model else None,
    )
    return outcome


def _estimated_context_tokens(
    *,
    static_prompt_tokens: int | None,
    replay_plan: ResolvedReplayPlan | None,
) -> int | None:
    if static_prompt_tokens is None:
        return None
    replay_tokens = replay_plan.estimated_tokens if replay_plan is not None else 0
    return static_prompt_tokens + replay_tokens


def finalize_history_preparation(
    *,
    prepared_scope_history: PreparedScopeHistory,
    config: Config,
    static_prompt_tokens: int | None = None,
    available_history_budget: int | None = None,
    pipeline_timing: DispatchPipelineTiming | None = None,
) -> PreparedHistoryState:
    """Return the final persisted-replay decision after durable history prep."""
    if pipeline_timing is not None:
        pipeline_timing.mark("replay_plan_start")
    resolved_inputs = prepared_scope_history.resolved_inputs
    resolved_static_prompt_tokens = (
        resolved_inputs.static_prompt_tokens if static_prompt_tokens is None else static_prompt_tokens
    )
    execution_plan = resolve_history_execution_plan(
        config=config,
        compaction_config=resolved_inputs.compaction_config,
        has_authored_compaction_config=resolved_inputs.has_authored_compaction_config,
        active_model_name=resolved_inputs.active_model_name,
        active_context_window=resolved_inputs.active_context_window,
        static_prompt_tokens=resolved_static_prompt_tokens,
    )
    history_budget = available_history_budget
    if history_budget is None:
        history_budget = (
            execution_plan.hard_replay_budget_tokens
            if execution_plan.authored_compaction_enabled
            else execution_plan.replay_budget_tokens
        )
        if history_budget is None:
            history_budget = execution_plan.replay_budget_tokens
        if execution_plan.authored_compaction_enabled and execution_plan.unavailable_reason is not None:
            description = describe_compaction_unavailability(execution_plan)
            logger.warning(
                "Compaction unavailable for this run",
                compaction_model=execution_plan.compaction_model_name,
                reason=description,
            )

    if prepared_scope_history.scope is None or prepared_scope_history.session is None:
        replay_plan = _configured_replay_plan(
            history_settings=resolved_inputs.history_settings,
            estimated_tokens=0,
        )
        prepared_context_tokens = resolved_static_prompt_tokens + replay_plan.estimated_tokens
        if pipeline_timing is not None:
            pipeline_timing.mark("replay_plan_ready")
            pipeline_timing.note(
                compaction_reply_outcome=prepared_scope_history.compaction_reply_outcome,
                prepared_context_tokens=prepared_context_tokens,
                fitted_replay_tokens=replay_plan.estimated_tokens,
            )
        return PreparedHistoryState(
            compaction_outcomes=prepared_scope_history.compaction_outcomes,
            replay_plan=replay_plan,
            estimated_context_tokens=_estimated_context_tokens(
                static_prompt_tokens=resolved_static_prompt_tokens,
                replay_plan=replay_plan,
            ),
            replays_persisted_history=False,
            compaction_decision=prepared_scope_history.compaction_decision,
            compaction_reply_outcome=prepared_scope_history.compaction_reply_outcome,
            prepared_context_tokens=prepared_context_tokens,
        )

    current_history_tokens = estimate_prompt_visible_history_tokens(
        session=prepared_scope_history.session,
        scope=prepared_scope_history.scope,
        history_settings=resolved_inputs.history_settings,
    )
    if history_budget is not None:
        replay_plan = plan_replay_that_fits(
            session=prepared_scope_history.session,
            scope=prepared_scope_history.scope,
            history_settings=resolved_inputs.history_settings,
            available_history_budget=history_budget,
        )
        _log_replay_plan(
            replay_plan=replay_plan,
            scope=prepared_scope_history.scope,
            available_history_budget=history_budget,
            current_history_tokens=current_history_tokens,
        )
    else:
        replay_plan = _configured_replay_plan(
            history_settings=resolved_inputs.history_settings,
            estimated_tokens=current_history_tokens,
        )

    prepared_context_tokens = resolved_static_prompt_tokens + replay_plan.estimated_tokens
    if pipeline_timing is not None:
        pipeline_timing.mark("replay_plan_ready")
        pipeline_timing.note(
            compaction_reply_outcome=prepared_scope_history.compaction_reply_outcome,
            prepared_context_tokens=prepared_context_tokens,
            fitted_replay_tokens=replay_plan.estimated_tokens,
        )
    return PreparedHistoryState(
        compaction_outcomes=prepared_scope_history.compaction_outcomes,
        replay_plan=replay_plan,
        estimated_context_tokens=_estimated_context_tokens(
            static_prompt_tokens=resolved_static_prompt_tokens,
            replay_plan=replay_plan,
        ),
        replays_persisted_history=_has_effective_persisted_replay(
            session=prepared_scope_history.session,
            scope=prepared_scope_history.scope,
            replay_plan=replay_plan,
        ),
        compaction_decision=prepared_scope_history.compaction_decision,
        compaction_reply_outcome=prepared_scope_history.compaction_reply_outcome,
        prepared_context_tokens=prepared_context_tokens,
    )


async def prepare_history_for_run(
    *,
    agent: Agent,
    agent_name: str,
    full_prompt: str,
    session_id: str | None,
    runtime_paths: RuntimePaths,
    config: Config,
    execution_identity: ToolExecutionIdentity | None,
    compaction_outcomes_collector: list[CompactionOutcome] | None = None,
    storage: BaseDb | None = None,
    session: AgentSession | TeamSession | None = None,
    history_settings: ResolvedHistorySettings | None = None,
    compaction_config: CompactionConfig | None = None,
    has_authored_compaction_config: bool | None = None,
    active_model_name: str | None = None,
    active_context_window: int | None = None,
    static_prompt_tokens: int | None = None,
    available_history_budget: int | None = None,
    scope: HistoryScope | None = None,
    execution_plan: ResolvedHistoryExecutionPlan | None = None,
    compaction_lifecycle: CompactionLifecycle | None = None,
    pipeline_timing: DispatchPipelineTiming | None = None,
) -> PreparedHistoryState:
    """Prepare one scope by compacting durable history and planning safe replay for the run."""
    resolved_scope = scope or resolve_history_scope(agent)
    if storage is not None and resolved_scope is not None and session_id is not None:
        persisted_session = session
        if persisted_session is None:
            persisted_session = (
                get_team_session(storage, session_id)
                if resolved_scope.kind == "team"
                else get_agent_session(storage, session_id)
            )
        scope_context: ScopeSessionContext | None = ScopeSessionContext(
            scope=resolved_scope,
            storage=storage,
            session=persisted_session,
            session_id=session_id,
        )
        prepared_scope_history = await prepare_scope_history(
            agent=agent,
            agent_name=agent_name,
            full_prompt=full_prompt,
            runtime_paths=runtime_paths,
            config=config,
            compaction_outcomes_collector=compaction_outcomes_collector,
            scope_context=scope_context,
            history_settings=history_settings,
            compaction_config=compaction_config,
            has_authored_compaction_config=has_authored_compaction_config,
            active_model_name=active_model_name,
            active_context_window=active_context_window,
            static_prompt_tokens=static_prompt_tokens,
            available_history_budget=available_history_budget,
            scope=resolved_scope,
            execution_plan=execution_plan,
            compaction_lifecycle=compaction_lifecycle,
            pipeline_timing=pipeline_timing,
            active_summary_model=agent.model,
        )
    else:
        with open_scope_session_context(
            agent=agent,
            agent_name=agent_name,
            session_id=session_id,
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=execution_identity,
            scope=resolved_scope,
        ) as scope_context:
            prepared_scope_history = await prepare_scope_history(
                agent=agent,
                agent_name=agent_name,
                full_prompt=full_prompt,
                runtime_paths=runtime_paths,
                config=config,
                compaction_outcomes_collector=compaction_outcomes_collector,
                scope_context=scope_context,
                history_settings=history_settings,
                compaction_config=compaction_config,
                has_authored_compaction_config=has_authored_compaction_config,
                active_model_name=active_model_name,
                active_context_window=active_context_window,
                static_prompt_tokens=static_prompt_tokens,
                available_history_budget=available_history_budget,
                scope=resolved_scope,
                execution_plan=execution_plan,
                compaction_lifecycle=compaction_lifecycle,
                pipeline_timing=pipeline_timing,
                active_summary_model=agent.model,
            )
    return finalize_history_preparation(
        prepared_scope_history=prepared_scope_history,
        config=config,
        static_prompt_tokens=static_prompt_tokens,
        available_history_budget=available_history_budget,
        pipeline_timing=pipeline_timing,
    )


@timed("system_prompt_assembly.history_prepare.scope_history")
async def prepare_bound_scope_history(
    *,
    agents: list[Agent],
    team: Team | None = None,
    full_prompt: str,
    runtime_paths: RuntimePaths,
    config: Config,
    compaction_outcomes_collector: list[CompactionOutcome] | None = None,
    scope_context: ScopeSessionContext | None = None,
    team_name: str | None = None,
    active_model_name: str | None = None,
    active_context_window: int | None = None,
    compaction_lifecycle: CompactionLifecycle | None = None,
    pipeline_timing: DispatchPipelineTiming | None = None,
) -> PreparedScopeHistory:
    """Prepare one team-owned scope by compacting its persisted session before the run."""
    bound_scope = resolve_bound_team_scope_context(
        agents=agents,
        config=config,
        team_name=team_name,
    )
    if bound_scope is None:
        resolved_inputs = _resolve_entity_preparation_inputs(
            config=config,
            entity_name=team_name if team_name in config.teams else None,
            static_prompt_tokens=(
                estimate_preparation_static_tokens_for_team(
                    team,
                    full_prompt=full_prompt,
                )
                if team is not None
                else estimate_preparation_prompt_tokens(
                    full_prompt=full_prompt,
                )
            ),
            active_model_name=active_model_name,
            active_context_window=active_context_window,
        )
        return PreparedScopeHistory(
            scope=None,
            session=None,
            resolved_inputs=resolved_inputs,
        )

    static_prompt_tokens = (
        estimate_preparation_static_tokens_for_team(
            team,
            full_prompt=full_prompt,
        )
        if team is not None
        else estimate_preparation_prompt_tokens(
            full_prompt=full_prompt,
        )
    )
    resolved_inputs = _resolve_entity_preparation_inputs(
        config=config,
        entity_name=team_name if team_name in config.teams else None,
        static_prompt_tokens=static_prompt_tokens,
        active_model_name=active_model_name,
        active_context_window=active_context_window,
    )
    return await prepare_scope_history(
        agent=bound_scope.owner_agent,
        agent_name=bound_scope.owner_agent_name,
        full_prompt=full_prompt,
        runtime_paths=runtime_paths,
        config=config,
        compaction_outcomes_collector=compaction_outcomes_collector,
        scope_context=scope_context,
        history_settings=resolved_inputs.history_settings,
        compaction_config=resolved_inputs.compaction_config,
        has_authored_compaction_config=resolved_inputs.execution_plan.authored_compaction_config,
        active_model_name=resolved_inputs.active_model_name,
        active_context_window=resolved_inputs.active_context_window,
        static_prompt_tokens=static_prompt_tokens,
        scope=bound_scope.scope,
        execution_plan=resolved_inputs.execution_plan,
        compaction_lifecycle=compaction_lifecycle,
        pipeline_timing=pipeline_timing,
        active_summary_model=team.model if team is not None else None,
        provider_request_builder=(
            partial(build_team_compaction_provider_request, team=team) if team is not None else None
        ),
    )


def resolve_bound_history_owner(agents: list[Agent]) -> tuple[Agent | None, str | None]:
    """Return the canonical storage owner for one bound team run."""
    candidates = [(agent_id, agent) for agent in agents if isinstance((agent_id := agent.id), str) and agent_id]
    if not candidates:
        return None, None

    owner_agent_name = min(agent_id for agent_id, _agent in candidates)
    for agent_id, agent in candidates:
        if agent_id == owner_agent_name:
            return agent, owner_agent_name
    return None, None


def resolve_bound_team_scope_context(
    *,
    agents: list[Agent],
    config: Config,
    team_name: str | None = None,
) -> BoundTeamScopeContext | None:
    """Resolve the stable owner and scope backing one live team run."""
    owner_agent, owner_agent_name = resolve_bound_history_owner(agents)
    if owner_agent is None or owner_agent_name is None:
        return None

    team_scope_id = team_name if team_name is not None and team_name in config.teams else _ad_hoc_team_scope_id(agents)
    if team_scope_id is None:
        return None
    scope = HistoryScope(kind="team", scope_id=team_scope_id)
    return BoundTeamScopeContext(
        owner_agent=owner_agent,
        owner_agent_name=owner_agent_name,
        scope=scope,
    )


def estimate_preparation_static_tokens(
    agent: Agent,
    *,
    full_prompt: str,
) -> int:
    """Estimate static prompt tokens for persisted replay planning."""
    return estimate_agent_static_tokens(agent, full_prompt)


def estimate_preparation_prompt_tokens(
    *,
    full_prompt: str,
) -> int:
    """Estimate prompt-only tokens for persisted replay planning."""
    return estimate_text_tokens(full_prompt)


def estimate_preparation_static_tokens_for_team(
    team: Team,
    *,
    full_prompt: str,
) -> int:
    """Estimate team static tokens for persisted replay planning."""
    return estimate_team_static_tokens(team, full_prompt)


@contextmanager
def open_scope_storage(
    *,
    agent_name: str,
    scope: HistoryScope,
    runtime_paths: RuntimePaths,
    config: Config,
    execution_identity: ToolExecutionIdentity | None,
) -> Iterator[BaseDb]:
    """Open the canonical storage for one persisted history scope."""
    storage = create_scope_session_storage(
        agent_name=agent_name,
        scope=scope,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
    )
    try:
        yield storage
    finally:
        storage.close()


def _build_scope_session_context(
    *,
    scope: HistoryScope | None,
    session_id: str | None,
    storage: BaseDb,
    create_session_if_missing: bool = False,
) -> ScopeSessionContext | None:
    """Build one scope/session context from an already-open storage handle."""
    if session_id is None or scope is None:
        return None

    session = get_team_session(storage, session_id) if scope.kind == "team" else get_agent_session(storage, session_id)
    if session is None and create_session_if_missing:
        created_at = int(datetime.now(UTC).timestamp())
        if scope.kind == "team":
            session = TeamSession(
                session_id=session_id,
                team_id=scope.scope_id,
                metadata={},
                runs=[],
                created_at=created_at,
                updated_at=created_at,
            )
        else:
            session = AgentSession(
                session_id=session_id,
                agent_id=_scope_session_agent_id(scope),
                metadata={},
                runs=[],
                created_at=created_at,
                updated_at=created_at,
            )
    return ScopeSessionContext(
        scope=scope,
        storage=storage,
        session=session,
        session_id=session_id,
    )


@contextmanager
def open_resolved_scope_session_context(
    *,
    agent_name: str,
    scope: HistoryScope | None,
    session_id: str | None,
    runtime_paths: RuntimePaths,
    config: Config,
    execution_identity: ToolExecutionIdentity | None,
    create_session_if_missing: bool = False,
) -> Iterator[ScopeSessionContext | None]:
    """Open one already-resolved persisted history scope for the current request."""
    if session_id is None:
        yield None
        return
    if scope is None:
        yield None
        return
    with open_scope_storage(
        agent_name=agent_name,
        scope=scope,
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=execution_identity,
    ) as storage:
        yield _build_scope_session_context(
            scope=scope,
            session_id=session_id,
            storage=storage,
            create_session_if_missing=create_session_if_missing,
        )


@contextmanager
def open_scope_session_context(
    *,
    agent: Agent,
    agent_name: str,
    session_id: str | None,
    runtime_paths: RuntimePaths,
    config: Config,
    execution_identity: ToolExecutionIdentity | None,
    scope: HistoryScope | None = None,
    create_session_if_missing: bool = False,
) -> Iterator[ScopeSessionContext | None]:
    """Open the canonical persisted history scope for one live agent."""
    resolved_scope = scope or resolve_history_scope(agent)
    with open_resolved_scope_session_context(
        agent_name=agent_name,
        scope=resolved_scope,
        session_id=session_id,
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=execution_identity,
        create_session_if_missing=create_session_if_missing,
    ) as scope_context:
        yield scope_context


@contextmanager
def open_bound_scope_session_context(
    *,
    agents: list[Agent],
    session_id: str | None,
    runtime_paths: RuntimePaths,
    config: Config,
    execution_identity: ToolExecutionIdentity | None,
    team_name: str | None = None,
    create_session_if_missing: bool = False,
) -> Iterator[ScopeSessionContext | None]:
    """Open the canonical scope-backed session context for one bound team run."""
    if not agents and team_name is not None and team_name in config.teams:
        with open_resolved_scope_session_context(
            agent_name=team_name,
            scope=HistoryScope(kind="team", scope_id=team_name),
            session_id=session_id,
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=execution_identity,
            create_session_if_missing=create_session_if_missing,
        ) as scope_context:
            yield scope_context
        return

    bound_scope = resolve_bound_team_scope_context(
        agents=agents,
        config=config,
        team_name=team_name,
    )
    if bound_scope is None:
        yield None
        return
    with open_resolved_scope_session_context(
        agent_name=bound_scope.owner_agent_name,
        scope=bound_scope.scope,
        session_id=session_id,
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=execution_identity,
        create_session_if_missing=create_session_if_missing,
    ) as scope_context:
        yield scope_context


def create_scope_session_storage(
    *,
    agent_name: str,
    scope: HistoryScope,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
) -> BaseDb:
    """Create the canonical storage for one persisted history scope."""
    if scope.kind == "agent":
        return create_session_storage(
            agent_name,
            config,
            runtime_paths,
            execution_identity=execution_identity,
        )

    storage_name = _scope_session_storage_name(scope)
    return create_state_storage(
        storage_name=storage_name,
        state_root=_team_scope_state_root(storage_name=storage_name, runtime_paths=runtime_paths),
        subdir="sessions",
        session_table=f"{storage_name}_sessions",
    )


def close_unique_state_dbs(*storages: BaseDb | None) -> None:
    """Close each distinct state DB handle at most once."""
    seen: set[int] = set()
    for storage in storages:
        if storage is None:
            continue
        storage_id = id(storage)
        if storage_id in seen:
            continue
        seen.add(storage_id)
        storage.close()


def close_agent_runtime_state_dbs(
    agent: Agent | None,
    *,
    shared_scope_storage: BaseDb | None = None,
) -> None:
    """Close one agent's runtime-owned state DB handles except a shared scope storage."""
    if agent is None:
        return
    close_unique_state_dbs(
        *(storage for storage in get_agent_runtime_state_dbs(agent) if storage is not shared_scope_storage),
    )


def close_team_runtime_state_dbs(
    *,
    agents: list[Agent],
    team_db: BaseDb | None,
    shared_scope_storage: BaseDb | None = None,
) -> None:
    """Close all runtime-owned state DB handles for one team request."""
    close_unique_state_dbs(
        *(
            storage
            for agent in agents
            for storage in get_agent_runtime_state_dbs(agent)
            if storage is not shared_scope_storage
        ),
        team_db if team_db is not shared_scope_storage else None,
    )


def _scope_session_storage_name(scope: HistoryScope) -> str:
    if scope.kind == "agent":
        return scope.scope_id
    normalized_scope_id = _TEAM_STORAGE_NAME_PATTERN.sub("_", scope.scope_id).strip("_") or "team"
    digest = hashlib.sha256(scope.key.encode()).hexdigest()[:12]
    return f"team_{normalized_scope_id}_{digest}"


def _team_scope_state_root(
    *,
    storage_name: str,
    runtime_paths: RuntimePaths,
) -> Path:
    return runtime_paths.storage_root / _TEAM_STATE_ROOT_DIRNAME / storage_name


def _scope_session_agent_id(scope: HistoryScope) -> str:
    if scope.kind == "agent":
        return scope.scope_id
    return _scope_session_storage_name(scope)


def _ad_hoc_team_scope_id(agents: list[Agent]) -> str | None:
    agent_names = [agent_id for agent in agents if isinstance((agent_id := agent.id), str) and agent_id]
    if not agent_names:
        return None
    return f"team_{'+'.join(sorted(agent_names))}"


def _history_settings_from_agent(agent: Agent) -> ResolvedHistorySettings:
    if agent.num_history_messages is not None:
        policy = HistoryPolicy(mode="messages", limit=agent.num_history_messages)
    elif agent.num_history_runs is not None:
        policy = HistoryPolicy(mode="runs", limit=agent.num_history_runs)
    else:
        policy = HistoryPolicy(mode="all")
    return ResolvedHistorySettings(
        policy=policy,
        max_tool_calls_from_history=agent.max_tool_calls_from_history,
        system_message_role=agent.system_message_role,
        skip_history_system_role=True,
    )


def _resolve_entity_preparation_inputs(
    *,
    config: Config,
    entity_name: str | None,
    static_prompt_tokens: int,
    active_model_name: str | None,
    active_context_window: int | None,
    history_settings: ResolvedHistorySettings | None = None,
    compaction_config: CompactionConfig | None = None,
    has_authored_compaction_config: bool | None = None,
    execution_plan: ResolvedHistoryExecutionPlan | None = None,
) -> _ResolvedPreparationInputs:
    resolved_history_settings = history_settings
    if resolved_history_settings is None:
        resolved_history_settings = (
            config.get_entity_history_settings(entity_name)
            if entity_name is not None
            else config.get_default_history_settings()
        )

    resolved_compaction_config = compaction_config
    if resolved_compaction_config is None:
        resolved_compaction_config = (
            config.get_entity_compaction_config(entity_name)
            if entity_name is not None
            else config.get_default_compaction_config()
        )

    resolved_has_authored_compaction_config = has_authored_compaction_config
    if resolved_has_authored_compaction_config is None:
        resolved_has_authored_compaction_config = (
            config.has_authored_entity_compaction_config(entity_name)
            if entity_name is not None
            else config.has_authored_default_compaction_config()
        )

    runtime_model = config.resolve_runtime_model(
        entity_name=entity_name,
        active_model_name=active_model_name,
        active_context_window=active_context_window,
    )
    resolved_execution_plan = execution_plan or resolve_history_execution_plan(
        config=config,
        compaction_config=resolved_compaction_config,
        has_authored_compaction_config=resolved_has_authored_compaction_config,
        active_model_name=runtime_model.model_name,
        active_context_window=runtime_model.context_window,
        static_prompt_tokens=static_prompt_tokens,
    )

    return _ResolvedPreparationInputs(
        history_settings=resolved_history_settings,
        compaction_config=resolved_compaction_config,
        has_authored_compaction_config=resolved_has_authored_compaction_config,
        active_model_name=runtime_model.model_name,
        active_context_window=runtime_model.context_window,
        static_prompt_tokens=static_prompt_tokens,
        execution_plan=resolved_execution_plan,
    )


def _resolve_preparation_inputs(
    *,
    agent: Agent,
    agent_name: str,
    full_prompt: str,
    config: Config,
    history_settings: ResolvedHistorySettings | None,
    compaction_config: CompactionConfig | None,
    has_authored_compaction_config: bool | None,
    active_model_name: str | None,
    active_context_window: int | None,
    static_prompt_tokens: int | None,
    execution_plan: ResolvedHistoryExecutionPlan | None,
) -> _ResolvedPreparationInputs:
    resolved_static_prompt_tokens = static_prompt_tokens
    if resolved_static_prompt_tokens is None:
        resolved_static_prompt_tokens = estimate_agent_static_tokens(agent, full_prompt)
    resolved_history_settings = history_settings
    if resolved_history_settings is None and agent_name not in config.agents:
        resolved_history_settings = _history_settings_from_agent(agent)
    return _resolve_entity_preparation_inputs(
        config=config,
        entity_name=agent_name if agent_name in config.agents else None,
        static_prompt_tokens=resolved_static_prompt_tokens,
        active_model_name=active_model_name,
        active_context_window=active_context_window,
        history_settings=resolved_history_settings,
        compaction_config=compaction_config,
        has_authored_compaction_config=has_authored_compaction_config,
        execution_plan=execution_plan,
    )


def _prepare_scope_state_for_run(
    *,
    storage: BaseDb,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    execution_plan: ResolvedHistoryExecutionPlan,
) -> HistoryScopeState:
    state = read_scope_state(session, scope)
    if consume_pending_force_compaction_scope(session, scope):
        state = replace(state, force_compact_before_next_run=True)
        write_scope_state(session, scope, state)
        storage.upsert_session(session)
    if state.force_compact_before_next_run and not execution_plan.destructive_compaction_available:
        state = clear_force_compaction_state(session, scope, state)
        storage.upsert_session(session)
        description = describe_compaction_unavailability(execution_plan)
        logger.warning(
            "Forced compaction skipped because destructive compaction is unavailable",
            session_id=session.session_id,
            scope=scope.key,
            reason=description,
        )
    return state


def plan_replay_that_fits(
    *,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    history_settings: ResolvedHistorySettings,
    available_history_budget: int,
) -> ResolvedReplayPlan:
    """Return the safest persisted-replay plan that fits the current run budget."""
    current_tokens = estimate_prompt_visible_history_tokens(
        session=session,
        scope=scope,
        history_settings=history_settings,
    )
    if current_tokens <= available_history_budget:
        return _configured_replay_plan(
            history_settings=history_settings,
            estimated_tokens=current_tokens,
        )

    limit_mode, max_limit = _context_window_guard_limit_bounds(
        session=session,
        scope=scope,
        history_settings=history_settings,
    )
    fitting_limit, fitting_tokens = _find_fitting_history_limit_for_budget(
        session=session,
        scope=scope,
        history_settings=history_settings,
        available_history_budget=available_history_budget,
        limit_mode=limit_mode,
        max_limit=max_limit,
    )
    if fitting_limit > 0:
        num_history_runs, num_history_messages = _history_limit_fields(limit_mode, fitting_limit)
        return ResolvedReplayPlan(
            mode="limited",
            estimated_tokens=fitting_tokens,
            add_history_to_context=True,
            num_history_runs=num_history_runs,
            num_history_messages=num_history_messages,
            history_limit_mode=limit_mode,
            history_limit=fitting_limit,
        )

    return ResolvedReplayPlan(
        mode="disabled",
        estimated_tokens=_session_summary_replay_tokens(session),
        add_history_to_context=False,
    )


def apply_replay_plan(
    *,
    target: Agent | Team,
    replay_plan: ResolvedReplayPlan,
) -> None:
    """Apply one resolved persisted-replay plan to a live Agent or Team."""
    target.add_history_to_context = replay_plan.add_history_to_context
    target.num_history_runs = replay_plan.num_history_runs
    target.num_history_messages = replay_plan.num_history_messages


def _context_window_guard_limit_bounds(
    *,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    history_settings: ResolvedHistorySettings,
) -> tuple[Literal["runs", "messages"], int]:
    configured_limit = history_settings.policy.limit or 0
    if history_settings.policy.mode == "messages":
        return "messages", configured_limit

    visible_run_count = len(runs_for_scope(completed_top_level_runs(session), scope))
    if history_settings.policy.mode == "all":
        return "runs", visible_run_count
    return "runs", min(configured_limit, visible_run_count)


def _find_fitting_history_limit_for_budget(
    *,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    history_settings: ResolvedHistorySettings,
    available_history_budget: int,
    limit_mode: Literal["runs", "messages"],
    max_limit: int,
) -> tuple[int, int]:
    if max_limit <= 0 or available_history_budget <= 0:
        return 0, 0

    low = 1
    high = max_limit
    best = 0
    best_tokens = 0
    while low <= high:
        mid = (low + high) // 2
        candidate_tokens = estimate_prompt_visible_history_tokens(
            session=session,
            scope=scope,
            history_settings=_history_settings_with_limit(
                history_settings,
                mode=limit_mode,
                limit=mid,
            ),
        )
        if candidate_tokens <= available_history_budget:
            best = mid
            best_tokens = candidate_tokens
            low = mid + 1
        else:
            high = mid - 1
    return best, best_tokens


def _log_replay_plan(
    *,
    replay_plan: ResolvedReplayPlan,
    scope: HistoryScope,
    available_history_budget: int,
    current_history_tokens: int,
) -> None:
    if replay_plan.mode == "configured":
        return

    if replay_plan.mode == "limited":
        logger.warning(
            "Replay planner reduced persisted replay for this run",
            scope=scope.key,
            limit_mode=replay_plan.history_limit_mode,
            new_limit=replay_plan.history_limit,
            estimated_tokens=current_history_tokens,
            fitted_tokens=replay_plan.estimated_tokens,
            available_history_budget=available_history_budget,
        )
        return

    logger.warning(
        "Replay planner disabled raw persisted replay for this run",
        scope=scope.key,
        estimated_tokens=current_history_tokens,
        fitted_tokens=replay_plan.estimated_tokens,
        available_history_budget=available_history_budget,
    )


def _configured_replay_plan(
    *,
    history_settings: ResolvedHistorySettings,
    estimated_tokens: int,
) -> ResolvedReplayPlan:
    num_history_runs, num_history_messages = _history_limit_fields(
        history_settings.policy.mode,
        history_settings.policy.limit,
    )
    return ResolvedReplayPlan(
        mode="configured",
        estimated_tokens=estimated_tokens,
        add_history_to_context=True,
        num_history_runs=num_history_runs,
        num_history_messages=num_history_messages,
    )


def _history_settings_with_limit(
    history_settings: ResolvedHistorySettings,
    *,
    mode: Literal["runs", "messages"],
    limit: int,
) -> ResolvedHistorySettings:
    return ResolvedHistorySettings(
        policy=HistoryPolicy(mode=mode, limit=limit),
        max_tool_calls_from_history=history_settings.max_tool_calls_from_history,
        system_message_role=history_settings.system_message_role,
        skip_history_system_role=history_settings.skip_history_system_role,
    )


def _history_limit_fields(
    mode: Literal["all", "runs", "messages"],
    limit: int | None,
) -> tuple[int | None, int | None]:
    if mode == "runs":
        return limit, None
    if mode == "messages":
        return None, limit
    return None, None


def _has_effective_persisted_replay(
    *,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    replay_plan: ResolvedReplayPlan,
) -> bool:
    if _session_has_summary_replay(session):
        return True
    if not replay_plan.add_history_to_context:
        return False
    return bool(runs_for_scope(completed_top_level_runs(session), scope))


def _session_has_summary_replay(session: AgentSession | TeamSession) -> bool:
    if session.summary is None:
        return False
    return bool(session.summary.summary.strip())


def _session_summary_replay_tokens(session: AgentSession | TeamSession) -> int:
    if session.summary is None:
        return 0
    return estimate_session_summary_tokens(session.summary.summary)
