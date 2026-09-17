"""Runtime integration for destructive history compaction."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal

from mindroom import model_loading
from mindroom.history.compaction import SummaryModel, compact_scope_history
from mindroom.history.native import configure_native_history, native_history_route
from mindroom.history.policy import (
    classify_compaction_decision,
    describe_compaction_unavailability,
    resolve_history_execution_plan,
)
from mindroom.history.prompt_tokens import estimate_agent_static_tokens, estimate_team_static_tokens
from mindroom.history.replay import (
    configured_replay_plan,
    estimate_prompt_visible_history_tokens,
    has_effective_persisted_replay,
    log_replay_plan,
    plan_replay_that_fits,
    scope_visible_runs,
)
from mindroom.history.session_context import (
    BoundTeamScopeContext,
    ScopeSessionContext,
    ad_hoc_team_agent_names,
    resolve_bound_history_owner,
    resolve_bound_team_scope_context,
    resolve_history_scope,
)
from mindroom.history.storage import (
    clear_force_compaction_state,
    consume_pending_force_compaction_scope,
    prune_reintroduced_runs,
    read_scope_state,
    set_force_compaction_state,
    update_scope_state_on_latest,
)
from mindroom.history.types import (
    CompactionDecision,
    CompactionLifecycleFailure,
    CompactionLifecycleProgress,
    CompactionLifecycleStart,
    CompactionReplyOutcome,
    HistoryPolicy,
    HistoryScope,
    HistoryScopeState,
    PreparedHistoryState,
    ResolvedHistoryExecutionPlan,
    ResolvedHistorySettings,
)
from mindroom.logging_config import get_logger
from mindroom.provider_error_compat import is_provider_timeout
from mindroom.team_scope import ad_hoc_team_has_private_member
from mindroom.timing import timed
from mindroom.token_budget import estimate_text_tokens

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from agno.agent import Agent
    from agno.db.base import BaseDb
    from agno.models.base import Model
    from agno.session.agent import AgentSession
    from agno.session.team import TeamSession
    from agno.team import Team

    from mindroom.config.main import Config
    from mindroom.config.models import CompactionConfig
    from mindroom.constants import RuntimePaths
    from mindroom.history.types import CompactionLifecycle, CompactionOutcome
    from mindroom.native_compaction import NativeCompactionModel
    from mindroom.timing import DispatchPipelineTiming


logger = get_logger(__name__)


def _elapsed_ms(start: float) -> int:
    """Return elapsed monotonic milliseconds."""
    return int((time.monotonic() - start) * 1000)


def _compaction_failure_status(error: BaseException) -> Literal["failed", "timeout"]:
    if isinstance(error, TimeoutError) or is_provider_timeout(error):
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


def _compaction_fallback_is_distinct(
    config: Config,
    *,
    primary_model_name: str,
    fallback_model_name: str,
) -> bool:
    """Return whether the configured fallback targets a different serving model.

    A fallback that names the primary alias, or a different alias resolving to
    the same ``(provider, id)``, would resend the refused request to the same
    model, so it is not loaded at all. Providers are compared through
    ``model_loading.canonical_provider`` — the same normalization model
    dispatch uses — so spelling variants like ``vertexai-claude`` and
    ``vertexai_claude`` count as the same serving model.
    """
    if fallback_model_name == primary_model_name:
        is_distinct = False
    else:
        primary_config = config.models.get(primary_model_name)
        fallback_config = config.models.get(fallback_model_name)
        if primary_config is None or fallback_config is None:
            return True
        is_distinct = (model_loading.canonical_provider(primary_config.provider), primary_config.id) != (
            model_loading.canonical_provider(fallback_config.provider),
            fallback_config.id,
        )
    if not is_distinct:
        logger.warning(
            "Compaction fallback resolves to the primary serving model; continuing without a fallback",
            compaction_model=primary_model_name,
            fallback_model=fallback_model_name,
        )
    return is_distinct


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


def _clear_forced_compaction_after_failure(
    *,
    storage: BaseDb,
    session: AgentSession | TeamSession | None,
    scope: HistoryScope,
    state: HistoryScopeState,
) -> None:
    """Clear a consumed manual force marker after a compaction failure.

    Clears against the freshest row unconditionally so a failing manual
    compaction cannot retry-loop on every reply. This deliberately differs
    from the no-candidates path (_persist_cleared_force_state_if_needed in
    compaction.py), which refuses to clear when a concurrent write moved the
    durable row, so a fresh manual request placed mid-run survives.
    """
    if session is None or not state.force_compact_before_next_run:
        return
    update_scope_state_on_latest(
        storage,
        session,
        scope,
        lambda latest: replace(latest, force_compact_before_next_run=False),
    )


@dataclass(frozen=True)
class _HistoryPreparationInputs:
    """Fully resolved policy/model/token inputs for one history preparation."""

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
    resolved_inputs: _HistoryPreparationInputs
    compaction_outcomes: list[CompactionOutcome] = field(default_factory=list)
    compaction_decision: CompactionDecision = field(
        default_factory=lambda: CompactionDecision(mode="none", reason="unclassified"),
    )
    compaction_reply_outcome: CompactionReplyOutcome = "none"
    native_model: NativeCompactionModel | None = None


@dataclass(frozen=True)
class _SafeCompactionLifecycle:
    """Best-effort compaction notice delivery: failures are logged, never raised."""

    lifecycle: CompactionLifecycle | None

    @property
    def enabled(self) -> bool:
        """Return whether lifecycle notices are delivered at all."""
        return self.lifecycle is not None

    async def start(self, event: CompactionLifecycleStart) -> str | None:
        """Send the initial compaction notice and return its Matrix event id."""
        if self.lifecycle is None:
            return None
        return await self._deliver(
            self.lifecycle.start(event),
            phase="start",
            session_id=event.session_id,
            scope=event.scope,
        )

    async def complete_success(self, outcome: CompactionOutcome) -> None:
        """Edit the lifecycle notice after successful compaction."""
        if self.lifecycle is None or outcome.lifecycle_notice_event_id is None:
            return
        await self._deliver(
            self.lifecycle.complete_success(outcome),
            phase="success",
            session_id=outcome.session_id,
            scope=outcome.scope,
        )

    async def progress(self, event: CompactionLifecycleProgress) -> None:
        """Edit the lifecycle notice after persisted compaction progress."""
        if self.lifecycle is None or event.notice_event_id is None:
            return
        await self._deliver(
            self.lifecycle.progress(event),
            phase="progress",
            session_id=event.session_id,
            scope=event.scope,
        )

    async def complete_failure(self, event: CompactionLifecycleFailure) -> None:
        """Edit the lifecycle notice after failed compaction."""
        if self.lifecycle is None or event.notice_event_id is None:
            return
        await self._deliver(
            self.lifecycle.complete_failure(event),
            phase=f"failure:{event.status}",
            session_id=event.session_id,
            scope=event.scope,
        )

    @staticmethod
    async def _deliver[T](delivery: Awaitable[T], *, phase: str, session_id: str, scope: str) -> T | None:
        try:
            return await delivery
        except Exception:
            logger.exception(
                "Compaction lifecycle notice delivery failed",
                phase=phase,
                session_id=session_id,
                scope=scope,
            )
            return None


@timed("system_prompt_assembly.history_prepare.scope_history")
async def prepare_scope_history(
    *,
    agent: Agent,
    agent_name: str,
    resolved_inputs: _HistoryPreparationInputs,
    runtime_paths: RuntimePaths,
    config: Config,
    scope_context: ScopeSessionContext | None = None,
    scope: HistoryScope | None = None,
    compaction_lifecycle: CompactionLifecycle | None = None,
    pipeline_timing: DispatchPipelineTiming | None = None,
    active_model: Model | None = None,
    allow_native_compaction: bool = True,
) -> PreparedScopeHistory:
    """Prepare durable scope history before final replay planning."""
    resolved_scope = scope or resolve_history_scope(agent)
    native_model = configure_native_history(
        active_model if active_model is not None else agent.model,
        plan=resolved_inputs.execution_plan,
        history_settings=resolved_inputs.history_settings,
        session=scope_context.session if scope_context is not None else None,
        allowed=allow_native_compaction,
    )
    if scope_context is None or scope_context.session is None:
        return PreparedScopeHistory(
            scope=resolved_scope,
            session=None,
            resolved_inputs=resolved_inputs,
            compaction_decision=CompactionDecision(mode="none", reason="missing_session"),
            native_model=native_model,
        )

    execution_plan = resolved_inputs.execution_plan
    session = scope_context.session
    if pipeline_timing is not None:
        pipeline_timing.mark("history_classify_start")
    state = _prepare_scope_state_for_run(
        storage=scope_context.storage,
        session=session,
        scope=scope_context.scope,
        execution_plan=execution_plan,
    )
    if state.force_compact_before_next_run and native_model is not None:
        native_model.configure_native_compaction(threshold=None)
    compaction_outcomes: list[CompactionOutcome] = []
    compaction_reply_outcome: CompactionReplyOutcome = "none"
    native_route = native_history_route(native_model)
    # Large provider projections spend seconds in tokenizers that release the GIL.
    current_history_tokens = await asyncio.to_thread(
        estimate_prompt_visible_history_tokens,
        session=session,
        scope=scope_context.scope,
        history_settings=resolved_inputs.history_settings,
        native_route=native_route,
        replay_model=native_model,
    )
    visible_runs = scope_visible_runs(session, scope_context.scope)
    compaction_decision = classify_compaction_decision(
        plan=execution_plan,
        force_compact_before_next_run=state.force_compact_before_next_run,
        current_history_tokens=current_history_tokens,
    )
    logger.info(
        "History preparation check",
        agent=agent_name,
        auto_enabled=execution_plan.authored_compaction_enabled and execution_plan.destructive_compaction_available,
        compaction_available=execution_plan.destructive_compaction_available,
        trigger_budget=execution_plan.replay_budget_tokens,
        hard_budget=execution_plan.hard_replay_budget_tokens,
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
        # The portable text owner always receives canonical history and counts.
        if native_route is not None:
            assert native_model is not None
            native_model.configure_native_compaction(threshold=None)
            current_history_tokens = await asyncio.to_thread(
                estimate_prompt_visible_history_tokens,
                session=session,
                scope=scope_context.scope,
                history_settings=resolved_inputs.history_settings,
                replay_model=native_model,
            )
        if pipeline_timing is not None:
            pipeline_timing.mark("required_compaction_start")
        compaction_result = await _run_scope_compaction_with_lifecycle(
            mode="manual" if state.force_compact_before_next_run else "auto",
            storage=scope_context.storage,
            session=session,
            scope=scope_context.scope,
            state=state,
            resolved_inputs=resolved_inputs,
            history_budget=execution_plan.hard_replay_budget_tokens,
            current_history_tokens=current_history_tokens,
            runs_before=len(visible_runs),
            replay_model=native_model,
            config=config,
            runtime_paths=runtime_paths,
            compaction_lifecycle=compaction_lifecycle,
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
    return PreparedScopeHistory(
        scope=scope_context.scope,
        session=scope_context.session,
        resolved_inputs=resolved_inputs,
        compaction_outcomes=compaction_outcomes,
        compaction_decision=compaction_decision,
        compaction_reply_outcome=compaction_reply_outcome,
        native_model=native_model,
    )


async def _run_scope_compaction_with_lifecycle(
    *,
    mode: Literal["auto", "manual"],
    storage: BaseDb,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    state: HistoryScopeState,
    resolved_inputs: _HistoryPreparationInputs,
    history_budget: int | None,
    current_history_tokens: int,
    runs_before: int,
    config: Config,
    runtime_paths: RuntimePaths,
    compaction_lifecycle: CompactionLifecycle | None,
    replay_model: NativeCompactionModel | None = None,
) -> _ScopeCompactionLifecycleResult:
    execution_plan = resolved_inputs.execution_plan
    assert execution_plan.summary_input_budget_tokens is not None
    lifecycle = _SafeCompactionLifecycle(compaction_lifecycle if runs_before else None)
    compaction_start = time.monotonic()
    notice_event_id = await lifecycle.start(
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

    # Progress events report the model that actually served each persisted
    # chunk, so failure notices after a fallback switch name the fallback
    # instead of the configured primary.
    serving_summary_model = execution_plan.compaction_model_name

    def _failure_event(status: Literal["failed", "timeout"], failure_reason: str) -> CompactionLifecycleFailure:
        return CompactionLifecycleFailure(
            notice_event_id=notice_event_id,
            mode=mode,
            session_id=session.session_id,
            scope=scope.key,
            summary_model=serving_summary_model,
            status=status,
            duration_ms=_elapsed_ms(compaction_start),
            failure_reason=failure_reason,
            history_budget_tokens=history_budget,
        )

    async def _progress(event: CompactionLifecycleProgress) -> None:
        nonlocal serving_summary_model
        serving_summary_model = event.summary_model
        await lifecycle.progress(replace(event, duration_ms=_elapsed_ms(compaction_start)))

    completed_successfully = False

    async def _complete(outcome: CompactionOutcome) -> CompactionOutcome:
        nonlocal completed_successfully
        outcome = replace(outcome, lifecycle_notice_event_id=notice_event_id, duration_ms=_elapsed_ms(compaction_start))
        await lifecycle.complete_success(outcome)
        completed_successfully = True
        return outcome

    progress_callback = _progress if lifecycle.enabled else None
    try:
        outcome = await _run_scope_compaction(
            storage=storage,
            session=session,
            scope=scope,
            state=state,
            resolved_inputs=resolved_inputs,
            history_budget=history_budget,
            before_tokens=current_history_tokens,
            config=config,
            runtime_paths=runtime_paths,
            lifecycle_notice_event_id=notice_event_id,
            progress_callback=progress_callback,
            completion_callback=_complete,
            replay_model=replay_model,
        )
    except asyncio.CancelledError as error:
        if not completed_successfully:
            await lifecycle.complete_failure(_failure_event("failed", str(error) or type(error).__name__))
        raise
    except Exception as error:
        _clear_forced_compaction_after_failure(
            storage=storage,
            session=session,
            scope=scope,
            state=state,
        )
        status = _compaction_failure_status(error)
        await lifecycle.complete_failure(_failure_event(status, str(error) or type(error).__name__))
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

    if outcome is None:
        await lifecycle.complete_failure(_failure_event("failed", "No compactable history remained."))
        return _ScopeCompactionLifecycleResult(outcome=None, reply_outcome="failed")

    return _ScopeCompactionLifecycleResult(outcome=outcome, reply_outcome="success")


async def _run_scope_compaction(
    *,
    storage: BaseDb,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    state: HistoryScopeState,
    resolved_inputs: _HistoryPreparationInputs,
    history_budget: int | None,
    before_tokens: int,
    config: Config,
    runtime_paths: RuntimePaths,
    lifecycle_notice_event_id: str | None = None,
    progress_callback: Callable[[CompactionLifecycleProgress], Awaitable[None]] | None = None,
    completion_callback: Callable[[CompactionOutcome], Awaitable[CompactionOutcome]] | None = None,
    replay_model: NativeCompactionModel | None = None,
) -> CompactionOutcome | None:
    execution_plan = resolved_inputs.execution_plan
    assert execution_plan.summary_input_budget_tokens is not None
    summary_model = SummaryModel(
        model=_load_compaction_model(config, runtime_paths, execution_plan.compaction_model_name),
        name=execution_plan.compaction_model_name,
        input_budget_tokens=execution_plan.summary_input_budget_tokens,
    )
    fallback_model_name = execution_plan.compaction_fallback_model_name
    fallback_summary_input_budget = execution_plan.compaction_fallback_summary_input_budget_tokens
    fallback_model: SummaryModel | None = None
    if (
        fallback_model_name is not None
        and fallback_summary_input_budget is not None
        and _compaction_fallback_is_distinct(
            config,
            primary_model_name=execution_plan.compaction_model_name,
            fallback_model_name=fallback_model_name,
        )
    ):
        # The fallback is an optional resilience knob: when its construction
        # fails (missing SDK, credentials, client setup), compaction still
        # runs on the healthy primary instead of aborting before any call.
        try:
            fallback_model = SummaryModel(
                model=_load_compaction_model(config, runtime_paths, fallback_model_name),
                name=fallback_model_name,
                input_budget_tokens=fallback_summary_input_budget,
            )
        except Exception:
            logger.warning(
                "Compaction fallback model failed to load; continuing without a fallback",
                session_id=session.session_id,
                scope=scope.key,
                compaction_model=execution_plan.compaction_model_name,
                fallback_model=fallback_model_name,
                exc_info=True,
            )
    return await compact_scope_history(
        storage=storage,
        replay_model=replay_model,
        session=session,
        scope=scope,
        state=state,
        history_settings=resolved_inputs.history_settings,
        available_history_budget=history_budget,
        before_tokens=before_tokens,
        summary_model=summary_model,
        replay_window_tokens=execution_plan.replay_window_tokens,
        threshold_tokens=execution_plan.trigger_threshold_tokens,
        summary_prompt=config.get_prompt("COMPACTION_SUMMARY_PROMPT"),
        summary_timeout_seconds=execution_plan.compaction_timeout_seconds,
        fallback_summary_model=fallback_model,
        lifecycle_notice_event_id=lifecycle_notice_event_id,
        progress_callback=progress_callback,
        completion_callback=completion_callback,
    )


def finalize_history_preparation(
    *,
    prepared_scope_history: PreparedScopeHistory,
    config: Config,
    static_prompt_tokens: int | None = None,
    available_history_budget: int | None = None,
    pipeline_timing: DispatchPipelineTiming | None = None,
) -> PreparedHistoryState:
    """Return the final persisted-replay decision after durable history prep.

    ``available_history_budget`` is an explicit replay-budget override; when
    None the budget derives from the freshly resolved execution plan.
    """
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
        # hard_replay_budget_tokens and replay_budget_tokens are resolved together,
        # so no further fallback is needed when the hard budget is unset.
        history_budget = (
            execution_plan.hard_replay_budget_tokens
            if execution_plan.authored_compaction_enabled
            else execution_plan.replay_budget_tokens
        )
        if execution_plan.authored_compaction_enabled and execution_plan.unavailable_reason is not None:
            description = describe_compaction_unavailability(execution_plan)
            logger.warning(
                "Compaction unavailable for this run",
                compaction_model=execution_plan.compaction_model_name,
                reason=description,
            )

    if prepared_scope_history.scope is None or prepared_scope_history.session is None:
        replay_plan = configured_replay_plan(
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
            replays_persisted_history=False,
            compaction_decision=prepared_scope_history.compaction_decision,
            compaction_reply_outcome=prepared_scope_history.compaction_reply_outcome,
            prepared_context_tokens=prepared_context_tokens,
        )

    current_history_tokens = estimate_prompt_visible_history_tokens(
        session=prepared_scope_history.session,
        scope=prepared_scope_history.scope,
        history_settings=resolved_inputs.history_settings,
        native_route=native_history_route(prepared_scope_history.native_model),
        replay_model=prepared_scope_history.native_model,
    )
    if history_budget is not None and current_history_tokens > history_budget:
        # Never trim a native checkpoint by run/message count. Fall back to the
        # existing canonical guard if the final dynamic prompt exhausts its room.
        if prepared_scope_history.native_model is not None:
            prepared_scope_history.native_model.configure_native_compaction(threshold=None)
        current_history_tokens = estimate_prompt_visible_history_tokens(
            session=prepared_scope_history.session,
            scope=prepared_scope_history.scope,
            history_settings=resolved_inputs.history_settings,
            replay_model=prepared_scope_history.native_model,
        )
    if history_budget is not None:
        replay_plan = plan_replay_that_fits(
            session=prepared_scope_history.session,
            scope=prepared_scope_history.scope,
            history_settings=resolved_inputs.history_settings,
            available_history_budget=history_budget,
            current_history_tokens=current_history_tokens,
            replay_model=prepared_scope_history.native_model,
        )
        log_replay_plan(
            replay_plan=replay_plan,
            scope=prepared_scope_history.scope,
            available_history_budget=history_budget,
            current_history_tokens=current_history_tokens,
        )
    else:
        replay_plan = configured_replay_plan(
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
        replays_persisted_history=has_effective_persisted_replay(
            session=prepared_scope_history.session,
            scope=prepared_scope_history.scope,
            replay_plan=replay_plan,
        ),
        compaction_decision=prepared_scope_history.compaction_decision,
        compaction_reply_outcome=prepared_scope_history.compaction_reply_outcome,
        prepared_context_tokens=prepared_context_tokens,
    )


@timed("system_prompt_assembly.history_prepare.scope_history")
async def prepare_bound_scope_history(
    *,
    agents: list[Agent],
    team: Team | None = None,
    full_prompt: str,
    runtime_paths: RuntimePaths,
    config: Config,
    scope_context: ScopeSessionContext | None = None,
    team_name: str | None = None,
    active_model_name: str | None = None,
    active_context_window: int | None = None,
    static_prompt_tokens: int | None = None,
    compaction_lifecycle: CompactionLifecycle | None = None,
    pipeline_timing: DispatchPipelineTiming | None = None,
    allow_native_compaction: bool = True,
) -> PreparedScopeHistory:
    """Prepare one team-owned scope by compacting its persisted session before the run."""
    if scope_context is not None:
        owner_agent, owner_agent_name = resolve_bound_history_owner(agents)
        bound_scope = (
            BoundTeamScopeContext(
                owner_agent=owner_agent,
                owner_agent_name=owner_agent_name,
                scope=scope_context.scope,
            )
            if owner_agent is not None and owner_agent_name is not None
            else None
        )
    elif team_name is None and ad_hoc_team_has_private_member(ad_hoc_team_agent_names(agents), config.agents):
        bound_scope = None
    else:
        bound_scope = resolve_bound_team_scope_context(
            agents=agents,
            config=config,
            team_name=team_name,
        )
    resolved_static_prompt_tokens = (
        static_prompt_tokens
        if static_prompt_tokens is not None
        else (
            _estimate_preparation_static_tokens_for_team(
                team,
                full_prompt=full_prompt,
            )
            if team is not None
            else _estimate_preparation_prompt_tokens(
                full_prompt=full_prompt,
            )
        )
    )
    resolved_inputs = _resolve_entity_preparation_inputs(
        config=config,
        entity_name=team_name if team_name in config.teams else None,
        static_prompt_tokens=resolved_static_prompt_tokens,
        active_model_name=active_model_name,
        active_context_window=active_context_window,
    )
    if bound_scope is None:
        return PreparedScopeHistory(
            scope=None,
            session=None,
            resolved_inputs=resolved_inputs,
        )

    return await prepare_scope_history(
        agent=bound_scope.owner_agent,
        agent_name=bound_scope.owner_agent_name,
        resolved_inputs=resolved_inputs,
        runtime_paths=runtime_paths,
        config=config,
        scope_context=scope_context,
        scope=bound_scope.scope,
        compaction_lifecycle=compaction_lifecycle,
        pipeline_timing=pipeline_timing,
        active_model=team.model if team is not None else None,
        allow_native_compaction=allow_native_compaction,
    )


def _estimate_preparation_prompt_tokens(
    *,
    full_prompt: str,
) -> int:
    """Estimate prompt-only tokens for persisted replay planning."""
    return estimate_text_tokens(full_prompt)


def _estimate_preparation_static_tokens_for_team(
    team: Team,
    *,
    full_prompt: str,
) -> int:
    """Estimate team static tokens for persisted replay planning."""
    return estimate_team_static_tokens(team, full_prompt)


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
) -> _HistoryPreparationInputs:
    resolved_entity = config.resolve_entity(entity_name)
    resolved_history_settings = history_settings
    if resolved_history_settings is None:
        resolved_history_settings = resolved_entity.history_settings

    resolved_compaction_config = compaction_config
    if resolved_compaction_config is None:
        resolved_compaction_config = resolved_entity.compaction_config

    resolved_has_authored_compaction_config = has_authored_compaction_config
    if resolved_has_authored_compaction_config is None:
        resolved_has_authored_compaction_config = resolved_entity.has_authored_compaction_config

    runtime_model = config.resolve_runtime_model(
        entity_name=entity_name,
        active_model_name=active_model_name,
        active_context_window=active_context_window,
    )
    resolved_execution_plan = (
        execution_plan
        if execution_plan is not None
        else resolve_history_execution_plan(
            config=config,
            compaction_config=resolved_compaction_config,
            has_authored_compaction_config=resolved_has_authored_compaction_config,
            active_model_name=runtime_model.model_name,
            active_context_window=runtime_model.context_window,
            static_prompt_tokens=static_prompt_tokens,
        )
    )

    return _HistoryPreparationInputs(
        history_settings=resolved_history_settings,
        compaction_config=resolved_compaction_config,
        has_authored_compaction_config=resolved_has_authored_compaction_config,
        active_model_name=runtime_model.model_name,
        active_context_window=runtime_model.context_window,
        static_prompt_tokens=static_prompt_tokens,
        execution_plan=resolved_execution_plan,
    )


def resolve_agent_preparation_inputs(
    *,
    agent: Agent,
    agent_name: str,
    full_prompt: str,
    config: Config,
    history_settings: ResolvedHistorySettings | None = None,
    compaction_config: CompactionConfig | None = None,
    has_authored_compaction_config: bool | None = None,
    active_model_name: str | None = None,
    active_context_window: int | None = None,
    static_prompt_tokens: int | None = None,
    execution_plan: ResolvedHistoryExecutionPlan | None = None,
) -> _HistoryPreparationInputs:
    """Resolve every history-preparation input for one agent run in one place.

    Explicitly provided values win; everything else falls back to the agent's
    authored config (or the live Agent object for unconfigured agents).
    """
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
    # Persists its own deletes; the session row has nothing new to write.
    prune_reintroduced_runs(storage, session, state)
    if consume_pending_force_compaction_scope(session, scope):
        state = set_force_compaction_state(session, scope, state, force=True)
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
