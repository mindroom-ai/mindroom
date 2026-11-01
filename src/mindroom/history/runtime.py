"""Runtime integration for history text compaction."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING

from mindroom.constants import AI_RUN_METADATA_KEY
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
    estimate_request_messages_tokens,
    estimate_summary_tokens,
    has_effective_persisted_replay,
    log_replay_plan,
    plan_replay_that_fits,
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
from mindroom.llm.messages import is_own_model_message
from mindroom.logging_config import get_logger
from mindroom.model_usage import message_context_tokens
from mindroom.native_compaction import native_checkpoint_id
from mindroom.redaction import redact_sensitive_text
from mindroom.session_storage.lanes import run_session_storage_operation
from mindroom.timing import timed
from mindroom.turn_model import ModelScope, resolve_turn_model

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.history.session_context import ScopeSessionContext
    from mindroom.history.storage import FoldBasis
    from mindroom.history.types import CompactionLifecycle, CompactionOutcome, NativeHistory
    from mindroom.llm.messages import Message
    from mindroom.llm.provider import ModelIdentity, ModelProvider, TokenCounter
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
        compaction_current_history_tokens=decision.current_history_tokens,
        compaction_trigger_budget_tokens=decision.trigger_budget_tokens,
        compaction_hard_budget_tokens=decision.hard_budget_tokens,
        compaction_fitted_replay_tokens=decision.fitted_replay_tokens,
        prepared_context_tokens=prepared_history.prepared_context_tokens,
        fitted_replay_tokens=(
            prepared_history.replay_plan.estimated_tokens if prepared_history.replay_plan is not None else None
        ),
    )


@dataclass(frozen=True)
class HistoryPreparationInputs:
    """Fully resolved policy/model/token inputs for one history preparation."""

    history_settings: ResolvedHistorySettings
    active_model_name: str
    active_context_window: int | None
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
    # How the route the run's requests go to counts tokens, which the final replay plan sizes with.
    counter: TokenCounter
    # The system prompt and tools tokens the run's requests are sized with.
    non_prompt_tokens: int
    compaction_reply_outcome: CompactionReplyOutcome = "none"
    # The history size the provider reported for the scope's latest request, when it still describes the history the
    # run replays; the final replay plan sizes with it as the compaction check did.
    reported_history_tokens: int | None = None


@timed("system_prompt_assembly.history_prepare.scope_history")
async def prepare_scope_history(
    *,
    model: ModelProvider,
    resolved_inputs: HistoryPreparationInputs,
    non_prompt_tokens: int,
    runtime_paths: RuntimePaths,
    config: Config,
    scope_context: ScopeSessionContext | None,
    compaction_lifecycle: CompactionLifecycle | None,
    pipeline_timing: DispatchPipelineTiming | None,
    allow_native_compaction: bool,
) -> PreparedScopeHistory:
    """Prepare durable scope history before final replay planning; ``model`` is the provider the run's requests go to."""
    counter = model.token_counter
    if scope_context is None or scope_context.session is None:
        return PreparedScopeHistory(
            session=None,
            resolved_inputs=resolved_inputs,
            compaction_decision=CompactionDecision(mode="none", reason="missing_session"),
            native_history=configure_native_history(
                model,
                plan=resolved_inputs.execution_plan,
                history_settings=resolved_inputs.history_settings,
                session=None,
                allowed=allow_native_compaction,
                runs=(),
            ),
            counter=counter,
            non_prompt_tokens=non_prompt_tokens,
        )

    execution_plan = resolved_inputs.execution_plan
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
    native_history = configure_native_history(
        model,
        plan=execution_plan,
        history_settings=resolved_inputs.history_settings,
        session=session,
        allowed=allow_native_compaction,
        runs=visible_runs,
    )
    if state.force_compact_before_next_run and native_history is not None:
        native_history = native_history.without_native_compaction()
    compaction_reply_outcome: CompactionReplyOutcome = "none"
    native_route = native_history_route(native_history)
    estimate_history_tokens = partial(
        estimate_prompt_visible_history_tokens,
        session=session,
        history_settings=resolved_inputs.history_settings,
        counter=counter,
    )
    # Large provider projections spend seconds in tokenizers that release the GIL.
    estimated_history_tokens = await asyncio.to_thread(estimate_history_tokens, native_route=native_route)
    reported_history_tokens = (
        await asyncio.to_thread(
            partial(
                _reported_history_tokens,
                session=session,
                visible_runs=visible_runs,
                resolved_inputs=resolved_inputs,
                config=config,
                model=model,
                non_prompt_tokens=non_prompt_tokens,
            ),
        )
        if visible_runs
        else None
    )
    # The provider's count of the latest request is closer than an estimate of the whole history.
    current_history_tokens = estimated_history_tokens if reported_history_tokens is None else reported_history_tokens
    compaction_decision = classify_compaction_decision(
        plan=execution_plan,
        force_compact_before_next_run=state.force_compact_before_next_run,
        current_history_tokens=current_history_tokens,
        # Only an active native route compacts between the trigger and hard budgets; without one, text compaction must.
        allow_native_compaction=native_route is not None,
        summary_tokens=await asyncio.to_thread(estimate_summary_tokens, session, counter),
    )
    logger.info(
        "History preparation check",
        scope=scope_context.scope.key,
        auto_enabled=execution_plan.automatic_compaction and execution_plan.text_compaction_available,
        compaction_available=execution_plan.text_compaction_available,
        trigger_budget=execution_plan.replay_budget_tokens,
        hard_budget=execution_plan.hard_replay_budget_tokens,
        replay_window=execution_plan.replay_window_tokens,
        static_prompt_tokens=execution_plan.static_prompt_tokens,
        current_tokens=current_history_tokens,
        estimated_tokens=estimated_history_tokens,
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
            assert native_history is not None
            native_history = native_history.without_native_compaction()
            current_history_tokens = await asyncio.to_thread(estimate_history_tokens)
        if pipeline_timing is not None:
            pipeline_timing.mark("required_compaction_start")
        # Compaction skips history that already fits its budget, so a trigger-level pass targets the trigger.
        compaction_budget = (
            execution_plan.replay_budget_tokens
            if compaction_decision.reason == "over_trigger_native_disabled"
            else execution_plan.hard_replay_budget_tokens
        )
        compaction_result = await _run_scope_compaction_with_lifecycle(
            mode=state.compaction_mode,
            session_id=session.session_id,
            scope=scope_context.scope,
            execution_plan=execution_plan,
            history_budget=compaction_budget,
            current_history_tokens=current_history_tokens,
            runs_before=len(visible_runs),
            compaction_lifecycle=compaction_lifecycle,
            compact=partial(
                _compact_before_reply,
                storage=scope_context.storage,
                session=session,
                state=state,
                resolved_inputs=resolved_inputs,
                history_budget=compaction_budget,
                before_tokens=current_history_tokens,
                counter=counter,
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
        counter=counter,
        non_prompt_tokens=non_prompt_tokens,
        # A compaction rewrote the history that request held.
        reported_history_tokens=reported_history_tokens if compaction_decision.mode != "required" else None,
    )


def _reported_history_tokens(
    *,
    session: ConversationSession,
    visible_runs: list[AnyRunRecord],
    resolved_inputs: HistoryPreparationInputs,
    config: Config,
    model: ModelProvider,
    non_prompt_tokens: int,
) -> int | None:
    """Size history from the request the provider reported for the scope's latest answer, or None when stale.

    That request held the system prompt and tools its run records it was sized with, and all the history this reply
    replays except the answer. The replay budgets are what the window leaves after today's static estimate, whose
    system prompt and tools are ``non_prompt_tokens``, so the smaller of the two is taken off: a system prompt grown
    since then counts in today's estimate rather than as history that shrank, and one that shrank stays counted.
    """
    latest_run = visible_runs[-1]
    if (
        # Replay limits drop the oldest content as runs accrue, so that request held more than will replay.
        not resolved_inputs.history_settings.replays_whole_history
        # Another model counts with another tokenizer.
        or (latest_run.model, latest_run.model_provider) != (model.identity.id, model.identity.provider)
    ):
        return None
    # Only a request that replayed the whole history records the system prompt and tools it was sized with.
    recorded = _recorded_non_prompt_tokens(latest_run)
    if recorded is None:
        return None
    messages = latest_run.messages
    anchor = next(
        (index for index in range(len(messages) - 1, -1, -1) if is_own_model_message(messages[index])),
        None,
    )
    # History replay drops or bounds media that request carried in full. A native checkpoint stands in for the transcript
    # before it on its own route only, and the response that created it was billed for that transcript, so a request
    # sizes this reply's history only while no checkpoint exists.
    if (
        anchor is None
        or any(message.images or message.audio or message.videos or message.files for message in messages)
        or any(native_checkpoint_id(message) for run in visible_runs for message in run.messages)
    ):
        return None
    answer = messages[anchor]
    summary = session.summary
    # A summary written after that request started folded runs it held. Both stamps are whole seconds, which cannot
    # order a summary written in the same second, so a tie counts as later too.
    if summary is not None and (summary.updated_at is None or summary.updated_at.timestamp() >= answer.created_at):
        return None
    reported = reported_context_tokens(answer, model=model, config=config, model_name=resolved_inputs.active_model_name)
    if reported is None:
        return None
    # Sized as the next request replays them: as history, whose earlier reasoning a model may render.
    replayed = [replace(message, from_history=True) for message in messages[anchor:]]
    newer_tokens = estimate_request_messages_tokens(replayed, counter=model.token_counter)
    return reported + newer_tokens - min(recorded, non_prompt_tokens)


def _recorded_non_prompt_tokens(run: AnyRunRecord) -> int | None:
    """Return the system prompt and tools tokens the run's published metadata records, or None when it records none."""
    ai_run = (run.metadata or {}).get(AI_RUN_METADATA_KEY)
    prepared_context = ai_run.get("prepared_context") if isinstance(ai_run, dict) else None
    tokens = prepared_context.get("non_prompt_tokens") if isinstance(prepared_context, dict) else None
    return tokens if isinstance(tokens, int) else None


def reported_context_tokens(message: Message, *, model: ModelProvider, config: Config, model_name: str) -> int | None:
    """Return the request-context size ``model`` reported with ``message``, configured as ``model_name``, if any."""
    configured = config.models.get(model_name)
    context = message_context_tokens(
        message,
        provider=model.identity.provider,
        configured_provider=configured.provider if configured is not None else None,
        model_id=model.identity.id,
    )
    return context.tokens if context is not None else None


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
    before_tokens: int,
    counter: TokenCounter,
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
            before_tokens=before_tokens,
            replay_window_tokens=resolved_inputs.execution_plan.replay_window_tokens,
            threshold_tokens=resolved_inputs.execution_plan.trigger_threshold_tokens,
            progress=progress,
            complete=complete,
            counter=counter,
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
    on_commit: Callable[[str], None],
    rewritten_request_tokens: Callable[[], int],
) -> CompactionOutcome | None:
    """Fold the stored runs after ``session``'s latest summary row and the running turn into one summary row."""
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
            available_history_budget=execution_plan.hard_replay_budget_tokens,
            on_commit=on_commit,
            rewritten_request_tokens=rewritten_request_tokens,
            complete=complete,
        )

    result = await _run_scope_compaction_with_lifecycle(
        mode="auto",
        session_id=session.session_id,
        scope=scope,
        execution_plan=execution_plan,
        history_budget=execution_plan.hard_replay_budget_tokens,
        current_history_tokens=before_tokens,
        runs_before=len(basis.runs) + 1,
        compaction_lifecycle=compaction_lifecycle,
        compact=compact,
    )
    return result.outcome


def finalize_history_preparation(
    *,
    prepared_scope_history: PreparedScopeHistory,
    config: Config,
    static_prompt_tokens: int,
    pipeline_timing: DispatchPipelineTiming | None,
) -> PreparedHistoryState:
    """Return the final persisted-replay decision after durable history prep, under the freshly resolved budget."""
    if pipeline_timing is not None:
        pipeline_timing.mark("replay_plan_start")
    resolved_inputs = prepared_scope_history.resolved_inputs
    history_settings = resolved_inputs.history_settings
    execution_plan = resolve_history_execution_plan(
        config=config,
        history_settings=history_settings,
        active_model_name=resolved_inputs.active_model_name,
        active_context_window=resolved_inputs.active_context_window,
        static_prompt_tokens=static_prompt_tokens,
    )
    history_budget = execution_plan.hard_replay_budget_tokens
    if execution_plan.automatic_compaction and execution_plan.unavailable_reason is not None:
        logger.warning(
            "Compaction unavailable for this run",
            compaction_model=execution_plan.compaction_model_name,
            reason=describe_compaction_unavailability(execution_plan),
        )

    session = prepared_scope_history.session
    native_history = prepared_scope_history.native_history
    counter = prepared_scope_history.counter
    if session is None:
        replay_plan = configured_replay_plan(history_settings=history_settings, estimated_tokens=0)
        replays_persisted_history = False
    else:
        estimate_history_tokens = partial(
            estimate_prompt_visible_history_tokens,
            session=session,
            history_settings=history_settings,
            counter=counter,
        )
        reported_history_tokens = prepared_scope_history.reported_history_tokens
        current_history_tokens = reported_history_tokens
        if current_history_tokens is None:
            current_history_tokens = estimate_history_tokens(native_route=native_history_route(native_history))
        fitting_budget = history_budget
        if history_budget is not None and current_history_tokens > history_budget:
            current_history_tokens = estimate_history_tokens()
            if reported_history_tokens is not None:
                # Trimming sizes candidate replays by estimate, which the provider's count of this conversation shows
                # to undercount by reported / estimated, so the budget shrinks into the estimate's units. It never
                # widens: that ratio fits the whole history, and the newest runs trimming keeps may overcount less.
                fitting_budget = min(history_budget, history_budget * current_history_tokens // reported_history_tokens)
            elif native_history is not None:
                # Never trim a native checkpoint by run/message count. Fall back to the
                # existing canonical guard if the final dynamic prompt exhausts its room.
                native_history = native_history.without_native_compaction()
        if fitting_budget is not None:
            replay_plan = plan_replay_that_fits(
                session=session,
                history_settings=history_settings,
                available_history_budget=fitting_budget,
                current_history_tokens=current_history_tokens,
                counter=counter,
            )
            log_replay_plan(
                replay_plan=replay_plan,
                scope=owner_scope(session),
                available_history_budget=fitting_budget,
                current_history_tokens=current_history_tokens,
            )
        else:
            replay_plan = configured_replay_plan(
                history_settings=history_settings,
                estimated_tokens=current_history_tokens,
            )
        replays_persisted_history = has_effective_persisted_replay(session=session, replay_plan=replay_plan)

    prepared_context_tokens = static_prompt_tokens + replay_plan.estimated_tokens
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
        non_prompt_tokens=prepared_scope_history.non_prompt_tokens if history_settings.replays_whole_history else None,
        native_history=native_history,
    )


def resolve_entity_preparation_inputs(
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    history_settings: ResolvedHistorySettings,
    static_prompt_tokens: int,
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
        active_model_name=runtime_model.model_name,
        active_context_window=runtime_model.context_window,
        execution_plan=resolve_history_execution_plan(
            config=config,
            history_settings=history_settings,
            active_model_name=runtime_model.model_name,
            active_context_window=runtime_model.context_window,
            static_prompt_tokens=static_prompt_tokens,
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
