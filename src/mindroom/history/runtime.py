"""Runtime integration for history text compaction."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING

from mindroom.failure_text import failure_text
from mindroom.history.compaction import (
    ConversationChangedError,
    clear_manual_compaction_request,
    compact_running_turn,
    compact_scope_history,
    load_summarizer,
    scope_usage_recorder,
    summarize_runs,
)
from mindroom.history.native import configure_native_history, native_history_route
from mindroom.history.policy import (
    classify_compaction_decision,
    describe_compaction_unavailability,
    resolve_history_execution_plan,
)
from mindroom.history.replay import (
    configured_replay_plan,
    estimate_prompt_visible_history_tokens,
    estimate_summary_tokens,
    has_effective_persisted_replay,
    log_replay_plan,
    plan_replay_that_fits,
    prompt_visible_history,
    scope_visible_runs,
)
from mindroom.history.storage import (
    new_scope_session,
    read_scope_state,
    set_force_compaction,
)
from mindroom.history.summary_call import is_summary_timeout
from mindroom.history.types import (
    CompactionDecision,
    CompactionLifecycleFailure,
    CompactionLifecycleProgress,
    CompactionLifecycleStart,
    CompactionLifecycleStatus,
    CompactionMode,
    CompactionReplyOutcome,
    HistoryScope,
    HistoryScopeState,
    PreparedHistoryState,
    ResolvedHistoryExecutionPlan,
    ResolvedHistorySettings,
    owner_scope,
)
from mindroom.logging_config import get_logger
from mindroom.redaction import redact_sensitive_text
from mindroom.session_storage.lanes import run_session_storage_operation
from mindroom.timing import timed
from mindroom.turn_model import ModelScope, resolve_turn_model

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.history.request_size import RequestFrame, RequestSize, RequestSizer
    from mindroom.history.session_context import ScopeSessionContext
    from mindroom.history.storage import FoldBasis
    from mindroom.history.types import CompactionLifecycle, CompactionOutcome, NativeHistory
    from mindroom.llm.provider import ModelIdentity, ModelProvider
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.llm.usage import MessageMetrics
    from mindroom.session_storage.store import ConversationStore
    from mindroom.session_storage.summary_rows import Fold
    from mindroom.session_storage.types import ConversationSession
    from mindroom.timing import DispatchPipelineTiming


logger = get_logger(__name__)


def _elapsed_ms(start: float) -> int:
    """Return elapsed monotonic milliseconds."""
    return int((time.monotonic() - start) * 1000)


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
        compaction_request_tokens=decision.request_tokens,
        compaction_trigger_tokens=decision.trigger_tokens,
        compaction_limit_tokens=decision.limit_tokens,
        prepared_context_tokens=prepared_history.prepared_context_tokens,
        fitted_replay_tokens=(
            prepared_history.replay_plan.estimated_tokens if prepared_history.replay_plan is not None else None
        ),
    )


@dataclass(frozen=True)
class HistoryPreparationInputs:
    """The resolved history settings and execution plan of one history preparation."""

    history_settings: ResolvedHistorySettings
    execution_plan: ResolvedHistoryExecutionPlan


@dataclass(frozen=True)
class _ScopeCompactionLifecycleResult:
    outcome: CompactionOutcome | None
    reply_outcome: CompactionReplyOutcome


@dataclass(frozen=True)
class PreparedScopeHistory:
    """Durable history preparation result before final replay planning; ``session`` names its scope."""

    session: ConversationSession | None
    resolved_inputs: HistoryPreparationInputs
    compaction_decision: CompactionDecision
    # The native settings the run's requests carry, when the provider route manages history of its own.
    native_history: NativeHistory | None
    # The model the run's requests go to, whose route sizes the final replay plan.
    model: ModelProvider
    # The size of the provisional first request without its summary and history.
    static_tokens: int
    compaction_reply_outcome: CompactionReplyOutcome = "none"
    # The size of the provisional first request, when no compaction rewrote the history it holds.
    request_size: RequestSize | None = None


@timed("system_prompt_assembly.history_prepare.scope_history")
async def prepare_scope_history(
    *,
    sizer: RequestSizer,
    frame: RequestFrame,
    resolved_inputs: HistoryPreparationInputs,
    runtime_paths: RuntimePaths,
    config: Config,
    scope_context: ScopeSessionContext | None,
    compaction_lifecycle: CompactionLifecycle | None,
    pipeline_timing: DispatchPipelineTiming | None,
    allow_native_compaction: bool,
) -> PreparedScopeHistory:
    """Prepare durable scope history before final replay planning; ``sizer`` sizes the run's requests.

    ``frame`` holds what the run's first request holds around the scope's history, with the turn as provisionally
    prepared; compaction is decided from that request's size.
    """
    model = sizer.model
    execution_plan = resolved_inputs.execution_plan
    # Encoding a long prompt would stall the replies sharing the event loop.
    static_tokens = await asyncio.to_thread(model.request_tokens, frame.request())
    configure_native = partial(
        configure_native_history,
        model,
        plan=execution_plan,
        history_settings=resolved_inputs.history_settings,
        allowed=allow_native_compaction,
        static_tokens=static_tokens,
    )
    if scope_context is None or scope_context.session is None:
        return PreparedScopeHistory(
            session=None,
            resolved_inputs=resolved_inputs,
            compaction_decision=CompactionDecision(mode="none", reason="missing_session"),
            native_history=configure_native(session=None, runs=()),
            model=model,
            static_tokens=static_tokens,
        )

    session = scope_context.session
    if pipeline_timing is not None:
        pipeline_timing.mark("history_classify_start")
    # Preparing the scope state may write the session row; it runs in the database's lane, behind the session's
    # other writes, and an in-flight write finishes before cancellation propagates.
    state = await run_session_storage_operation(
        lambda: scope_context.storage,
        partial(_prepare_scope_state_for_run, session=session, execution_plan=execution_plan),
    )
    visible_runs = scope_visible_runs(session)
    native_history = configure_native(session=session, runs=visible_runs)
    if state.force_compact_before_next_run and native_history is not None:
        native_history = native_history.without_native_compaction()
    compaction_reply_outcome: CompactionReplyOutcome = "none"
    history = prompt_visible_history(session, resolved_inputs.history_settings)
    request = partial(frame.request, summary=session.summary, history=history)
    # Large provider projections spend seconds in tokenizers that release the GIL.
    request_size = await asyncio.to_thread(sizer.size, request(native=native_history))
    summary_tokens = await asyncio.to_thread(estimate_summary_tokens, session, model)
    native_route = native_history_route(native_history)
    compaction_decision = classify_compaction_decision(
        plan=execution_plan,
        force_compact_before_next_run=state.force_compact_before_next_run,
        request_tokens=request_size.tokens,
        summary_request_tokens=static_tokens + summary_tokens,
        # Only an active native route compacts between the trigger and the limit; without one, text compaction must.
        allow_native_compaction=native_route is not None,
    )
    logger.info(
        "History preparation check",
        scope=scope_context.scope.key,
        auto_enabled=execution_plan.automatic_compaction and execution_plan.text_compaction_available,
        compaction_available=execution_plan.text_compaction_available,
        trigger_tokens=execution_plan.trigger_tokens,
        limit_tokens=execution_plan.limit_tokens,
        replay_window=execution_plan.replay_window_tokens,
        static_tokens=static_tokens,
        request_tokens=request_size.tokens,
        reported=request_size.reported,
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
            compaction_request_tokens=compaction_decision.request_tokens,
            compaction_trigger_tokens=compaction_decision.trigger_tokens,
            compaction_limit_tokens=compaction_decision.limit_tokens,
        )

    if compaction_decision.mode == "required":
        # The portable text owner always receives canonical history and counts.
        if native_route is not None:
            assert native_history is not None
            native_history = native_history.without_native_compaction()
            request_size = await asyncio.to_thread(sizer.size, request(native=native_history))
        if pipeline_timing is not None:
            pipeline_timing.mark("required_compaction_start")
        # A trigger-level pass reports its budget against the trigger, any other against the limit; notices show the
        # history's share of the request.
        target = (
            execution_plan.trigger_tokens
            if compaction_decision.reason == "over_trigger_native_disabled"
            else execution_plan.limit_tokens
        )
        history_budget = _history_share(target, static_tokens)
        history_tokens = max(0, request_size.tokens - static_tokens)
        compaction_result = await _run_scope_compaction_with_lifecycle(
            mode=state.compaction_mode,
            session_id=session.session_id,
            scope=scope_context.scope,
            execution_plan=execution_plan,
            history_budget=history_budget,
            current_history_tokens=history_tokens,
            runs_before=len(visible_runs),
            compaction_lifecycle=compaction_lifecycle,
            compact=partial(
                _compact_before_reply,
                storage=scope_context.storage,
                session=session,
                state=state,
                resolved_inputs=resolved_inputs,
                history_budget=history_budget,
                # A trigger-level pass may store a summary above its target, but never one the reply cannot replay.
                max_summary_tokens=_history_share(execution_plan.limit_tokens, static_tokens),
                before_tokens=history_tokens,
                model=model,
                config=config,
                runtime_paths=runtime_paths,
            ),
        )
        outcome = compaction_result.outcome
        compaction_reply_outcome = compaction_result.reply_outcome
        if outcome is not None:
            logger.info(
                "Compaction completed",
                scope=scope_context.scope.key,
                outcome_mode=outcome.mode,
                before_tokens=outcome.before_tokens,
                after_tokens=outcome.after_tokens,
                runs_compacted=outcome.compacted_run_count,
            )
        if pipeline_timing is not None:
            pipeline_timing.mark("required_compaction_ready")
            pipeline_timing.note(compaction_reply_outcome=compaction_reply_outcome)
    return PreparedScopeHistory(
        session=scope_context.session,
        resolved_inputs=resolved_inputs,
        compaction_decision=compaction_decision,
        compaction_reply_outcome=compaction_reply_outcome,
        native_history=native_history,
        model=model,
        static_tokens=static_tokens,
        # A compaction rewrote the history that request held.
        request_size=request_size if compaction_decision.mode != "required" else None,
    )


# Runs one compaction under the lifecycle wrapper, given the callbacks that report its progress and its success.
type _ScopeCompactor = Callable[
    [
        Callable[[CompactionLifecycleProgress], Awaitable[None]] | None,
        Callable[[CompactionOutcome], Awaitable[CompactionOutcome]],
    ],
    Awaitable[CompactionOutcome | None],
]


async def _run_scope_compaction_with_lifecycle(
    *,
    mode: CompactionMode,
    session_id: str,
    scope: HistoryScope,
    execution_plan: ResolvedHistoryExecutionPlan,
    history_budget: int | None,
    current_history_tokens: int,
    runs_before: int,
    compaction_lifecycle: CompactionLifecycle | None,
    compact: _ScopeCompactor,
) -> _ScopeCompactionLifecycleResult:
    lifecycle = compaction_lifecycle if runs_before else None
    compaction_start = time.monotonic()
    notify = partial(_send_notice, session_id=session_id, scope=scope)
    start = CompactionLifecycleStart(
        mode=mode,
        session_id=session_id,
        scope=scope.key,
        summary_model=execution_plan.compaction_model_name,
        before_tokens=current_history_tokens,
        history_budget_tokens=history_budget,
        runs_before=runs_before,
        threshold_tokens=execution_plan.trigger_threshold_tokens,
    )
    notice_event_id = await notify(lifecycle, "start", lambda lifecycle: lifecycle.start(start))
    # Later notices edit the start notice, so they are sent only when it was.
    notices = lifecycle if notice_event_id is not None else None

    # Progress events report the model that actually served each persisted
    # chunk, so failure notices after a fallback switch name the fallback
    # instead of the configured primary.
    serving_summary_model = execution_plan.compaction_model_name

    async def _fail(status: CompactionLifecycleStatus, failure_reason: str) -> None:
        failure = CompactionLifecycleFailure(
            notice_event_id=notice_event_id,
            mode=mode,
            session_id=session_id,
            scope=scope.key,
            summary_model=serving_summary_model,
            status=status,
            duration_ms=_elapsed_ms(compaction_start),
            failure_reason=redact_sensitive_text(failure_reason),
            history_budget_tokens=history_budget,
        )
        await notify(notices, f"failure:{status}", lambda lifecycle: lifecycle.complete_failure(failure))

    async def _progress(event: CompactionLifecycleProgress) -> None:
        nonlocal serving_summary_model
        serving_summary_model = event.summary_model
        event = replace(event, notice_event_id=notice_event_id, duration_ms=_elapsed_ms(compaction_start))
        await notify(notices, "progress", lambda lifecycle: lifecycle.progress(event))

    completed_successfully = False

    async def _complete(outcome: CompactionOutcome) -> CompactionOutcome:
        nonlocal completed_successfully
        outcome = replace(outcome, lifecycle_notice_event_id=notice_event_id, duration_ms=_elapsed_ms(compaction_start))
        await notify(notices, "success", lambda lifecycle: lifecycle.complete_success(outcome))
        completed_successfully = True
        return outcome

    try:
        outcome = await compact(_progress if lifecycle is not None else None, _complete)
    except asyncio.CancelledError as error:
        if not completed_successfully:
            await _fail("failed", failure_text(error))
        raise
    except Exception as error:
        status = "timeout" if is_summary_timeout(error) else "failed"
        await _fail(status, failure_text(error))
        logger.exception(
            "Compaction failed; continuing without compaction",
            session_id=session_id,
            scope=scope.key,
            mode=mode,
        )
        return _ScopeCompactionLifecycleResult(outcome=None, reply_outcome=status)

    if outcome is None:
        await _fail("failed", "No compactable history remained.")
        return _ScopeCompactionLifecycleResult(outcome=None, reply_outcome="failed")

    return _ScopeCompactionLifecycleResult(outcome=outcome, reply_outcome="success")


async def _send_notice[T](
    lifecycle: CompactionLifecycle | None,
    phase: str,
    send: Callable[[CompactionLifecycle], Awaitable[T]],
    *,
    session_id: str,
    scope: HistoryScope,
) -> T | None:
    """Send one compaction notice through ``lifecycle``, if any; notices are best-effort, so a failure is only logged."""
    if lifecycle is None:
        return None
    delivery = send(lifecycle)
    try:
        return await delivery
    except Exception:
        logger.exception(
            "Compaction lifecycle notice delivery failed",
            phase=phase,
            session_id=session_id,
            scope=scope.key,
        )
        return None


async def _compact_before_reply(
    progress: Callable[[CompactionLifecycleProgress], Awaitable[None]] | None,
    complete: Callable[[CompactionOutcome], Awaitable[CompactionOutcome]],
    *,
    storage: ConversationStore,
    session: ConversationSession,
    state: HistoryScopeState,
    resolved_inputs: HistoryPreparationInputs,
    history_budget: int | None,
    max_summary_tokens: int | None,
    before_tokens: int,
    model: ModelProvider,
    config: Config,
    runtime_paths: RuntimePaths,
) -> CompactionOutcome | None:
    try:
        return await compact_scope_history(
            storage=storage,
            session=session,
            state=state,
            summarizer=load_summarizer(
                resolved_inputs.execution_plan,
                resolved_inputs.history_settings,
                config=config,
                runtime_paths=runtime_paths,
                session_id=session.session_id,
                scope=owner_scope(session),
                on_usage=scope_usage_recorder(storage, session.session_id),
            ),
            available_history_budget=history_budget,
            max_summary_tokens=max_summary_tokens,
            before_tokens=before_tokens,
            replay_window_tokens=resolved_inputs.execution_plan.replay_window_tokens,
            threshold_tokens=resolved_inputs.execution_plan.trigger_threshold_tokens,
            progress=progress,
            complete=complete,
            model=model,
        )
    except ConversationChangedError:
        # A request the conversation outran stays, so the next reply compacts what the conversation holds then.
        raise
    except Exception:
        # A failing manual compaction must not retry on every reply.
        await clear_manual_compaction_request(storage=storage, session=session, state=state)
        raise


async def summarize_run_locally(
    *,
    resolved_inputs: HistoryPreparationInputs,
    previous_summary: str | None,
    snapshots: Sequence[AnyRunRecord],
    scope: HistoryScope,
    config: Config,
    runtime_paths: RuntimePaths,
    on_usage: Callable[[ModelIdentity, MessageMetrics], Awaitable[None]] | None,
) -> str:
    """Summarize one run's own folded messages, oldest first, with the scope's summary model, storing nothing."""
    summarizer = load_summarizer(
        resolved_inputs.execution_plan,
        resolved_inputs.history_settings,
        config=config,
        runtime_paths=runtime_paths,
        session_id=snapshots[0].session_id or "",
        scope=scope,
        on_usage=on_usage,
    )
    summary, _ = await summarize_runs(summarizer, previous_summary, snapshots)
    return summary


async def compact_scope_mid_turn(
    *,
    storage: ConversationStore,
    session: ConversationSession,
    resolved_inputs: HistoryPreparationInputs,
    basis: FoldBasis,
    fold: Fold,
    snapshots: Sequence[AnyRunRecord],
    before_tokens: int,
    config: Config,
    runtime_paths: RuntimePaths,
    compaction_lifecycle: CompactionLifecycle | None,
    max_summary_tokens: int,
    model: ModelProvider,
    on_commit: Callable[[str], None],
    rewritten_request_tokens: Callable[[], int],
) -> CompactionOutcome | None:
    """Fold the stored runs after ``session``'s latest summary row and the running turn into one summary row.

    A summary over ``max_summary_tokens``, as ``model``'s route sizes it, fails the compaction and stores nothing.
    """
    execution_plan = resolved_inputs.execution_plan
    scope = owner_scope(session)

    async def compact(
        _progress: Callable[[CompactionLifecycleProgress], Awaitable[None]] | None,
        complete: Callable[[CompactionOutcome], Awaitable[CompactionOutcome]],
    ) -> CompactionOutcome:
        # One fold writes one row, so there is no per-chunk progress to report.
        return await compact_running_turn(
            storage=storage,
            session=session,
            basis=basis,
            fold=fold,
            snapshots=snapshots,
            summarizer=load_summarizer(
                execution_plan,
                resolved_inputs.history_settings,
                config=config,
                runtime_paths=runtime_paths,
                session_id=session.session_id,
                scope=scope,
                # A first turn's fold records its summary usage before any run of the conversation is stored.
                on_usage=scope_usage_recorder(
                    storage,
                    session.session_id,
                    initial_session=new_scope_session(session_id=session.session_id, scope=scope),
                ),
            ),
            before_tokens=before_tokens,
            replay_window_tokens=execution_plan.replay_window_tokens,
            threshold_tokens=execution_plan.trigger_threshold_tokens,
            available_history_budget=execution_plan.limit_tokens,
            max_summary_tokens=max_summary_tokens,
            model=model,
            on_commit=on_commit,
            rewritten_request_tokens=rewritten_request_tokens,
            complete=complete,
        )

    result = await _run_scope_compaction_with_lifecycle(
        mode="auto",
        session_id=session.session_id,
        scope=scope,
        execution_plan=execution_plan,
        history_budget=execution_plan.limit_tokens,
        current_history_tokens=before_tokens,
        runs_before=len(basis.runs) + 1,
        compaction_lifecycle=compaction_lifecycle,
        compact=compact,
    )
    return result.outcome


def _history_share(total: int | None, static_tokens: int) -> int | None:
    """Return the history's share of a whole-request size: what it holds beyond the request without its history."""
    return None if total is None else max(0, total - static_tokens)


def finalize_history_preparation(
    *,
    prepared_scope_history: PreparedScopeHistory,
    frame: RequestFrame,
    pipeline_timing: DispatchPipelineTiming | None,
) -> PreparedHistoryState:
    """Return the final persisted-replay decision after durable history prep, for the run's final ``frame``."""
    if pipeline_timing is not None:
        pipeline_timing.mark("replay_plan_start")
    resolved_inputs = prepared_scope_history.resolved_inputs
    history_settings = resolved_inputs.history_settings
    execution_plan = resolved_inputs.execution_plan
    if execution_plan.automatic_compaction and execution_plan.unavailable_reason is not None:
        logger.warning(
            "Compaction unavailable for this run",
            compaction_model=execution_plan.compaction_model_name,
            reason=describe_compaction_unavailability(execution_plan),
        )
    model = prepared_scope_history.model
    static_tokens = model.request_tokens(frame.request())
    session = prepared_scope_history.session
    native_history = prepared_scope_history.native_history
    if session is None:
        # A scope's first request holds no summary and no history.
        replay_plan = configured_replay_plan(history_settings=history_settings, estimated_tokens=0)
        replays_persisted_history = False
    else:
        estimate_history_tokens = partial(
            estimate_prompt_visible_history_tokens,
            session=session,
            history_settings=history_settings,
            model=model,
        )
        request_size = prepared_scope_history.request_size
        estimated: int | None = None
        if request_size is not None:
            # The final request holds the prepared one's history beside what the final frame holds, such as the
            # prompt's attachments.
            request_tokens = request_size.tokens + static_tokens - prepared_scope_history.static_tokens
        else:
            # Only a required compaction leaves no size, and it turned native compaction off.
            estimated = estimate_history_tokens()
            request_tokens = static_tokens + estimated
        # The history's share, which a reported size below the static estimate leaves at nothing.
        history_tokens = max(0, request_tokens - static_tokens)
        limit = execution_plan.limit_tokens
        if limit is not None and request_tokens > limit:
            if estimated is None:
                estimated = estimate_history_tokens()
            available_history_budget = _fitting_history_budget(
                limit=limit,
                static_tokens=static_tokens,
                history_tokens=history_tokens,
                estimated_history_tokens=estimated,
                reported=request_size is not None and request_size.reported,
            )
            if native_history is not None:
                # Never trim a native checkpoint by run/message count, and never let the provider checkpoint a trimmed
                # request, which would replace history it never held. Fall back to the canonical guard.
                native_history = native_history.without_native_compaction()
            replay_plan = plan_replay_that_fits(
                session=session,
                history_settings=history_settings,
                available_history_budget=available_history_budget,
                current_history_tokens=estimated,
                model=model,
            )
            log_replay_plan(
                replay_plan=replay_plan,
                scope=owner_scope(session),
                available_history_budget=available_history_budget,
                current_history_tokens=estimated,
            )
        else:
            replay_plan = configured_replay_plan(history_settings=history_settings, estimated_tokens=history_tokens)
        replays_persisted_history = has_effective_persisted_replay(session=session, replay_plan=replay_plan)

    prepared_context_tokens = static_tokens + replay_plan.estimated_tokens
    if pipeline_timing is not None:
        pipeline_timing.mark("replay_plan_ready")
        pipeline_timing.note(
            compaction_reply_outcome=prepared_scope_history.compaction_reply_outcome,
            prepared_context_tokens=prepared_context_tokens,
            fitted_replay_tokens=replay_plan.estimated_tokens,
        )
    return PreparedHistoryState(
        replay_plan=replay_plan,
        replays_persisted_history=replays_persisted_history,
        compaction_decision=prepared_scope_history.compaction_decision,
        compaction_reply_outcome=prepared_scope_history.compaction_reply_outcome,
        prepared_context_tokens=prepared_context_tokens,
        native_history=native_history,
    )


def _fitting_history_budget(
    *,
    limit: int,
    static_tokens: int,
    history_tokens: int,
    estimated_history_tokens: int,
    reported: bool,
) -> int:
    """Return the most history, in estimate units, a trimmed replay may keep beside ``static_tokens`` within ``limit``.

    Trimming sizes candidate replays by estimate. When the provider's count of this conversation (``history_tokens``,
    reported) shows the estimate undercounts by ``history_tokens / estimated_history_tokens``, the budget shrinks into
    the estimate's units; it never widens, since that ratio fits the whole history and the newest runs trimming keeps
    may overcount less. A static part at or over the limit leaves no room.
    """
    budget = max(0, limit - static_tokens)
    if not reported or history_tokens <= 0:
        return budget
    return min(budget, budget * estimated_history_tokens // history_tokens)


def resolve_entity_preparation_inputs(
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    history_settings: ResolvedHistorySettings,
    active_model_name: str,
    active_context_window: int | None,
) -> HistoryPreparationInputs:
    """Resolve the runtime model and plan for one agent or team scope.

    ``active_model_name`` is the model the turn resolved, which every scope prepares under, and
    ``active_context_window`` its window when the turn set one. ``history_settings`` are the ones the entity's build
    resolved, whose replay policy and compaction config the scope follows.
    """
    runtime_model = resolve_turn_model(
        config,
        runtime_paths,
        ModelScope(
            entity=None,
            explicit=active_model_name,
            explicit_context_window=active_context_window,
        ),
        None,
    )
    return HistoryPreparationInputs(
        history_settings=history_settings,
        execution_plan=resolve_history_execution_plan(
            config=config,
            history_settings=history_settings,
            active_model_name=runtime_model.model_name,
            active_context_window=runtime_model.context_window,
        ),
    )


def _prepare_scope_state_for_run(
    storage: ConversationStore,
    *,
    session: ConversationSession,
    execution_plan: ResolvedHistoryExecutionPlan,
) -> HistoryScopeState:
    state = read_scope_state(session)
    if state.force_compact_before_next_run and not execution_plan.text_compaction_available:
        set_force_compaction(storage, session=session, forced=False)
        state = HistoryScopeState()
        logger.warning(
            "Forced compaction skipped because text compaction is unavailable",
            session_id=session.session_id,
            scope=owner_scope(session).key,
            reason=describe_compaction_unavailability(execution_plan),
        )
    return state
