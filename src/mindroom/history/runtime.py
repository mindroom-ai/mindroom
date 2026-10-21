"""Runtime integration for history text compaction."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, Literal

from mindroom.background_tasks import run_blocking_until_complete
from mindroom.history.compaction import (
    ConversationChangedError,
    Summarizer,
    SummaryModel,
    compact_running_turn,
    compact_scope_history,
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
    has_effective_persisted_replay,
    log_replay_plan,
    plan_replay_that_fits,
    scope_visible_runs,
)
from mindroom.history.session_context import (
    BoundTeamScopeContext,
    ScopeSessionContext,
    resolve_bound_history_owner,
    resolve_bound_team_scope_context,
)
from mindroom.history.storage import (
    consume_pending_force_compaction_scope,
    read_scope_state,
    set_force_compaction_state,
    update_scope_state_on_latest,
)
from mindroom.history.team_scope import ad_hoc_team_has_private_member
from mindroom.history.types import (
    CompactionDecision,
    CompactionLifecycleFailure,
    CompactionLifecycleProgress,
    CompactionLifecycleStart,
    CompactionReplyOutcome,
    HistoryScope,
    HistoryScopeState,
    PreparedHistoryState,
    ResolvedHistoryExecutionPlan,
    ResolvedHistorySettings,
)
from mindroom.logging_config import get_logger
from mindroom.model_usage import response_context_tokens
from mindroom.providers import model_loading
from mindroom.providers.agno_compat_provider_errors import is_provider_timeout
from mindroom.redaction import redact_sensitive_text
from mindroom.timing import timed

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from agno.metrics import MessageMetrics
    from agno.models.base import Model
    from agno.models.message import Message
    from agno.run.agent import RunOutput
    from agno.run.team import TeamRunOutput

    from mindroom.config.main import Config
    from mindroom.config.models import CompactionConfig
    from mindroom.constants import RuntimePaths
    from mindroom.history.storage import FoldBasis
    from mindroom.history.summary_rows import Segment
    from mindroom.history.types import CompactionLifecycle, CompactionOutcome, NativeHistory
    from mindroom.llm.provider import ModelProvider
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.native_compaction import NativeCompactionModel
    from mindroom.session_storage.store import ConversationStore
    from mindroom.session_storage.types import ConversationSession
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
    storage: ConversationStore,
    session: ConversationSession,
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
    if not state.force_compact_before_next_run:
        return
    update_scope_state_on_latest(
        storage,
        session,
        scope,
        lambda latest: replace(latest, force_compact_before_next_run=False),
    )


@dataclass(frozen=True)
class HistoryPreparationInputs:
    """Fully resolved policy/model/token inputs for one history preparation."""

    history_settings: ResolvedHistorySettings
    compaction_config: CompactionConfig
    has_authored_compaction_config: bool
    active_model_name: str
    active_context_window: int | None
    execution_plan: ResolvedHistoryExecutionPlan


@dataclass(frozen=True)
class _ScopeCompactionLifecycleResult:
    outcome: CompactionOutcome | None
    reply_outcome: CompactionReplyOutcome


@dataclass(frozen=True)
class PreparedScopeHistory:
    """Durable history preparation result before final replay planning."""

    scope: HistoryScope | None
    session: ConversationSession | None
    resolved_inputs: HistoryPreparationInputs
    compaction_decision: CompactionDecision = field(
        default_factory=lambda: CompactionDecision(mode="none", reason="unclassified"),
    )
    compaction_reply_outcome: CompactionReplyOutcome = "none"
    # The native settings the run's requests carry, when the provider route manages history of its own.
    native_history: NativeHistory | None = None


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


def _native_history(
    model: ModelProvider | None,
    resolved_inputs: HistoryPreparationInputs,
    session: ConversationSession | None,
    *,
    allowed: bool,
) -> NativeHistory | None:
    """Return the native settings of the run's requests; a run sized by estimate alone sends none."""
    if model is None:
        return None
    return configure_native_history(
        model,
        plan=resolved_inputs.execution_plan,
        history_settings=resolved_inputs.history_settings,
        session=session,
        allowed=allowed,
    )


@timed("system_prompt_assembly.history_prepare.scope_history")
async def prepare_scope_history(
    *,
    agent_name: str,
    scope: HistoryScope | None,
    model: ModelProvider | None,
    resolved_inputs: HistoryPreparationInputs,
    non_prompt_tokens: int,
    runtime_paths: RuntimePaths,
    config: Config,
    scope_context: ScopeSessionContext | None = None,
    compaction_lifecycle: CompactionLifecycle | None = None,
    pipeline_timing: DispatchPipelineTiming | None = None,
    allow_native_compaction: bool = True,
) -> PreparedScopeHistory:
    """Prepare durable scope history before final replay planning.

    ``model`` is the provider the run's requests go to; None sizes history by estimate alone and sends no native
    settings.
    """
    if scope_context is None or scope_context.session is None:
        return PreparedScopeHistory(
            scope=scope,
            session=None,
            resolved_inputs=resolved_inputs,
            compaction_decision=CompactionDecision(mode="none", reason="missing_session"),
            native_history=_native_history(model, resolved_inputs, None, allowed=allow_native_compaction),
        )

    execution_plan = resolved_inputs.execution_plan
    session = scope_context.session
    if pipeline_timing is not None:
        pipeline_timing.mark("history_classify_start")
    # Preparing the scope state may write the session row; keep it off the event loop and
    # let an in-flight write finish before cancellation propagates.
    state = await run_blocking_until_complete(
        partial(
            _prepare_scope_state_for_run,
            storage=scope_context.storage,
            session=session,
            scope=scope_context.scope,
            execution_plan=execution_plan,
        ),
    )
    native_history = _native_history(model, resolved_inputs, session, allowed=allow_native_compaction)
    if state.force_compact_before_next_run and native_history is not None:
        native_history = native_history.without_native_compaction()
    compaction_reply_outcome: CompactionReplyOutcome = "none"
    native_route = native_history_route(native_history)
    # Large provider projections spend seconds in tokenizers that release the GIL.
    estimated_history_tokens = await asyncio.to_thread(
        estimate_prompt_visible_history_tokens,
        session=session,
        scope=scope_context.scope,
        history_settings=resolved_inputs.history_settings,
        native_route=native_route,
        replay_model=None,
    )
    visible_runs = scope_visible_runs(session, scope_context.scope)
    # A native checkpoint response is billed for the transcript it replaced, so native routes keep the estimate.
    reported_history_tokens = (
        _reported_history_tokens(
            session=session,
            latest_run=visible_runs[-1],
            resolved_inputs=resolved_inputs,
            config=config,
            model=model,
            non_prompt_tokens=non_prompt_tokens,
        )
        if native_route is None and visible_runs
        else None
    )
    # The provider's count only ever raises the estimate, so a count that misses replayed content cannot hide it.
    current_history_tokens = max(estimated_history_tokens, reported_history_tokens or 0)
    compaction_decision = classify_compaction_decision(
        plan=execution_plan,
        force_compact_before_next_run=state.force_compact_before_next_run,
        current_history_tokens=current_history_tokens,
        allow_native_compaction=allow_native_compaction,
    )
    logger.info(
        "History preparation check",
        agent=agent_name,
        auto_enabled=execution_plan.authored_compaction_enabled and execution_plan.text_compaction_available,
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
            current_history_tokens = await asyncio.to_thread(
                estimate_prompt_visible_history_tokens,
                session=session,
                scope=scope_context.scope,
                history_settings=resolved_inputs.history_settings,
                replay_model=None,
            )
        if pipeline_timing is not None:
            pipeline_timing.mark("required_compaction_start")
        # Compaction skips history that already fits its budget, so a trigger-level pass targets the trigger.
        compaction_budget = (
            execution_plan.replay_budget_tokens
            if compaction_decision.reason == "over_trigger_native_disabled"
            else execution_plan.hard_replay_budget_tokens
        )
        compaction_result = await _run_scope_compaction_with_lifecycle(
            mode="manual" if state.force_compact_before_next_run else "auto",
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
                scope=scope_context.scope,
                state=state,
                resolved_inputs=resolved_inputs,
                history_budget=compaction_budget,
                before_tokens=current_history_tokens,
                replay_model=None,
                config=config,
                runtime_paths=runtime_paths,
            ),
        )
        outcome = compaction_result.outcome
        compaction_reply_outcome = compaction_result.reply_outcome
        if outcome is not None:
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
        compaction_decision=compaction_decision,
        compaction_reply_outcome=compaction_reply_outcome,
        native_history=native_history,
    )


def _reported_history_tokens(
    *,
    session: ConversationSession,
    latest_run: AnyRunRecord,
    resolved_inputs: HistoryPreparationInputs,
    config: Config,
    model: ModelProvider | None,
    non_prompt_tokens: int,
) -> int | None:
    """Size history from the request the provider reported for the scope's latest answer, or None when stale.

    That request held the system prompt, the tools, and all the history this reply replays except the answer.
    The replay budgets are what the window leaves after the static estimate, so taking off that estimate's system
    prompt and tools share, ``non_prompt_tokens``, converts the count to their units.
    """
    history_settings = resolved_inputs.history_settings
    if (
        model is None
        # Replay limits drop the oldest content as runs accrue, so that request held more than will replay.
        or history_settings.policy.mode != "all"
        or history_settings.max_tool_calls_from_history is not None
        # Another model counts with another tokenizer.
        or (latest_run.model, latest_run.model_provider) != (model.identity.id, model.identity.provider)
    ):
        return None
    messages = [message for message in latest_run.messages or [] if not message.from_history]
    anchor = next((index for index in range(len(messages) - 1, -1, -1) if messages[index].role == "assistant"), None)
    # History replay drops or bounds media that request carried in full.
    if anchor is None or any(
        message.images or message.audio or message.videos or message.files for message in messages
    ):
        return None
    answer = messages[anchor]
    summary = session.summary
    # A summary written after that request started folded runs it held. Agno creates the answer's message before a
    # mid-turn fold of that request runs, so such a fold also counts as later and the estimate decides. Both stamps
    # are whole seconds, which cannot order a summary written in the same second, so a tie counts as later too.
    if summary is not None and (summary.updated_at is None or summary.updated_at.timestamp() >= answer.created_at):
        return None
    configured = config.models.get(resolved_inputs.active_model_name)
    reported = response_context_tokens(
        answer,
        provider=model.identity.provider_name,
        configured_provider=configured.provider if configured is not None else None,
        model_id=model.identity.id,
    )
    if reported is None:
        return None
    newer_tokens = estimate_request_messages_tokens(messages[anchor:], replay_model=None)
    return reported + newer_tokens - non_prompt_tokens


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
    mode: Literal["auto", "manual"],
    session_id: str,
    scope: HistoryScope,
    execution_plan: ResolvedHistoryExecutionPlan,
    history_budget: int | None,
    current_history_tokens: int,
    runs_before: int,
    compaction_lifecycle: CompactionLifecycle | None,
    compact: _ScopeCompactor,
) -> _ScopeCompactionLifecycleResult:
    lifecycle = _SafeCompactionLifecycle(compaction_lifecycle if runs_before else None)
    compaction_start = time.monotonic()
    notice_event_id = await lifecycle.start(
        CompactionLifecycleStart(
            mode=mode,
            session_id=session_id,
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
            session_id=session_id,
            scope=scope.key,
            summary_model=serving_summary_model,
            status=status,
            duration_ms=_elapsed_ms(compaction_start),
            failure_reason=redact_sensitive_text(failure_reason),
            history_budget_tokens=history_budget,
        )

    async def _progress(event: CompactionLifecycleProgress) -> None:
        nonlocal serving_summary_model
        serving_summary_model = event.summary_model
        await lifecycle.progress(
            replace(event, notice_event_id=notice_event_id, duration_ms=_elapsed_ms(compaction_start)),
        )

    completed_successfully = False

    async def _complete(outcome: CompactionOutcome) -> CompactionOutcome:
        nonlocal completed_successfully
        outcome = replace(outcome, lifecycle_notice_event_id=notice_event_id, duration_ms=_elapsed_ms(compaction_start))
        await lifecycle.complete_success(outcome)
        completed_successfully = True
        return outcome

    try:
        outcome = await compact(_progress if lifecycle.enabled else None, _complete)
    except asyncio.CancelledError as error:
        if not completed_successfully:
            await lifecycle.complete_failure(_failure_event("failed", str(error) or type(error).__name__))
        raise
    except Exception as error:
        status = _compaction_failure_status(error)
        await lifecycle.complete_failure(_failure_event(status, str(error) or type(error).__name__))
        logger.exception(
            "Compaction failed; continuing without compaction",
            session_id=session_id,
            scope=scope.key,
            mode=mode,
        )
        return _ScopeCompactionLifecycleResult(outcome=None, reply_outcome=status)

    if outcome is None:
        await lifecycle.complete_failure(_failure_event("failed", "No compactable history remained."))
        return _ScopeCompactionLifecycleResult(outcome=None, reply_outcome="failed")

    return _ScopeCompactionLifecycleResult(outcome=outcome, reply_outcome="success")


async def _compact_before_reply(
    progress: Callable[[CompactionLifecycleProgress], Awaitable[None]] | None,
    complete: Callable[[CompactionOutcome], Awaitable[CompactionOutcome]],
    *,
    storage: ConversationStore,
    session: ConversationSession,
    scope: HistoryScope,
    state: HistoryScopeState,
    resolved_inputs: HistoryPreparationInputs,
    history_budget: int | None,
    before_tokens: int,
    replay_model: NativeCompactionModel | None,
    config: Config,
    runtime_paths: RuntimePaths,
) -> CompactionOutcome | None:
    try:
        return await compact_scope_history(
            storage=storage,
            session=session,
            scope=scope,
            state=state,
            summarizer=_load_summarizer(
                resolved_inputs,
                config=config,
                runtime_paths=runtime_paths,
                session_id=session.session_id,
                scope=scope,
                on_usage=scope_usage_recorder(storage, session.session_id),
            ),
            available_history_budget=history_budget,
            before_tokens=before_tokens,
            replay_window_tokens=resolved_inputs.execution_plan.replay_window_tokens,
            threshold_tokens=resolved_inputs.execution_plan.trigger_threshold_tokens,
            progress=progress,
            complete=complete,
            replay_model=replay_model,
        )
    except ConversationChangedError:
        # A request the conversation outran stays, so the next reply compacts what the conversation holds then.
        raise
    except Exception:
        # A failing manual compaction must not retry on every reply.
        _clear_forced_compaction_after_failure(storage=storage, session=session, scope=scope, state=state)
        raise


def _load_summarizer(
    resolved_inputs: HistoryPreparationInputs,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    session_id: str,
    scope: HistoryScope,
    on_usage: Callable[[Model, MessageMetrics], Awaitable[None]] | None,
) -> Summarizer:
    """Load the summary model and, when distinct and loadable, its safeguard fallback."""
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
                session_id=session_id,
                scope=scope.key,
                compaction_model=execution_plan.compaction_model_name,
                fallback_model=fallback_model_name,
                exc_info=True,
            )
    return Summarizer(
        model=summary_model,
        fallback=fallback_model,
        prompt=config.get_prompt("COMPACTION_SUMMARY_PROMPT"),
        timeout_seconds=execution_plan.compaction_timeout_seconds,
        history_settings=resolved_inputs.history_settings,
        session_id=session_id,
        scope=scope,
        on_usage=on_usage,
    )


async def summarize_run_locally(
    *,
    resolved_inputs: HistoryPreparationInputs,
    previous_summary: str | None,
    snapshots: Sequence[RunOutput | TeamRunOutput],
    scope: HistoryScope,
    config: Config,
    runtime_paths: RuntimePaths,
    on_usage: Callable[[Model, MessageMetrics], Awaitable[None]] | None,
) -> str:
    """Summarize one run's own folded messages, oldest first, with the scope's summary model, storing nothing."""
    summarizer = _load_summarizer(
        resolved_inputs,
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
    scope: HistoryScope,
    resolved_inputs: HistoryPreparationInputs,
    basis: FoldBasis,
    segment: Segment,
    snapshots: Sequence[RunOutput | TeamRunOutput],
    folded_messages: Sequence[Message],
    before_tokens: int,
    config: Config,
    runtime_paths: RuntimePaths,
    compaction_lifecycle: CompactionLifecycle | None,
    on_commit: Callable[[str], None],
) -> CompactionOutcome | None:
    """Fold the stored runs after the latest summary row and the running turn into one summary row."""
    execution_plan = resolved_inputs.execution_plan

    async def compact(
        _progress: Callable[[CompactionLifecycleProgress], Awaitable[None]] | None,
        complete: Callable[[CompactionOutcome], Awaitable[CompactionOutcome]],
    ) -> CompactionOutcome:
        # One fold writes one row, so there is no per-chunk progress to report.
        return await compact_running_turn(
            storage=storage,
            session=session,
            scope=scope,
            basis=basis,
            segment=segment,
            snapshots=snapshots,
            folded_messages=folded_messages,
            summarizer=_load_summarizer(
                resolved_inputs,
                config=config,
                runtime_paths=runtime_paths,
                session_id=session.session_id,
                scope=scope,
                on_usage=scope_usage_recorder(storage, session.session_id),
            ),
            before_tokens=before_tokens,
            replay_window_tokens=execution_plan.replay_window_tokens,
            threshold_tokens=execution_plan.trigger_threshold_tokens,
            available_history_budget=execution_plan.hard_replay_budget_tokens,
            on_commit=on_commit,
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
    pipeline_timing: DispatchPipelineTiming | None = None,
) -> PreparedHistoryState:
    """Return the final persisted-replay decision after durable history prep, under the freshly resolved budget."""
    if pipeline_timing is not None:
        pipeline_timing.mark("replay_plan_start")
    resolved_inputs = prepared_scope_history.resolved_inputs
    execution_plan = resolve_history_execution_plan(
        config=config,
        compaction_config=resolved_inputs.compaction_config,
        has_authored_compaction_config=resolved_inputs.has_authored_compaction_config,
        active_model_name=resolved_inputs.active_model_name,
        active_context_window=resolved_inputs.active_context_window,
        static_prompt_tokens=static_prompt_tokens,
    )
    # hard_replay_budget_tokens and replay_budget_tokens are resolved together,
    # so no further fallback is needed when the hard budget is unset.
    history_budget = (
        execution_plan.hard_replay_budget_tokens
        if execution_plan.authored_compaction_enabled
        else execution_plan.replay_budget_tokens
    )
    if execution_plan.authored_compaction_enabled and execution_plan.unavailable_reason is not None:
        logger.warning(
            "Compaction unavailable for this run",
            compaction_model=execution_plan.compaction_model_name,
            reason=describe_compaction_unavailability(execution_plan),
        )

    history_settings = resolved_inputs.history_settings
    session, scope, native_history = (
        prepared_scope_history.session,
        prepared_scope_history.scope,
        prepared_scope_history.native_history,
    )
    if scope is None or session is None:
        replay_plan = configured_replay_plan(history_settings=history_settings, estimated_tokens=0)
        replays_persisted_history = False
    else:
        current_history_tokens = estimate_prompt_visible_history_tokens(
            session=session,
            scope=scope,
            history_settings=history_settings,
            native_route=native_history_route(native_history),
            replay_model=None,
        )
        if history_budget is not None and current_history_tokens > history_budget:
            # Never trim a native checkpoint by run/message count. Fall back to the
            # existing canonical guard if the final dynamic prompt exhausts its room.
            if native_history is not None:
                native_history = native_history.without_native_compaction()
            current_history_tokens = estimate_prompt_visible_history_tokens(
                session=session,
                scope=scope,
                history_settings=history_settings,
                replay_model=None,
            )
        if history_budget is not None:
            replay_plan = plan_replay_that_fits(
                session=session,
                scope=scope,
                history_settings=history_settings,
                available_history_budget=history_budget,
                current_history_tokens=current_history_tokens,
                replay_model=None,
            )
            log_replay_plan(
                replay_plan=replay_plan,
                scope=scope,
                available_history_budget=history_budget,
                current_history_tokens=current_history_tokens,
            )
        else:
            replay_plan = configured_replay_plan(
                history_settings=history_settings,
                estimated_tokens=current_history_tokens,
            )
        replays_persisted_history = has_effective_persisted_replay(
            session=session,
            scope=scope,
            replay_plan=replay_plan,
        )

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
        native_history=native_history,
    )


@timed("system_prompt_assembly.history_prepare.scope_history")
async def prepare_bound_scope_history(
    *,
    member_names: Sequence[str],
    model: ModelProvider | None,
    runtime_paths: RuntimePaths,
    config: Config,
    static_prompt_tokens: int,
    non_prompt_tokens: int,
    scope_context: ScopeSessionContext | None = None,
    team_name: str | None = None,
    active_model_name: str | None = None,
    active_context_window: int | None = None,
    compaction_lifecycle: CompactionLifecycle | None = None,
    pipeline_timing: DispatchPipelineTiming | None = None,
    allow_native_compaction: bool = True,
) -> PreparedScopeHistory:
    """Prepare one team-owned scope by compacting its persisted session before the run.

    ``model`` is the leader's provider, whose native compaction the scope may use.
    """
    if scope_context is not None:
        owner_agent_name = resolve_bound_history_owner(member_names)
        bound_scope = (
            BoundTeamScopeContext(owner_agent_name=owner_agent_name, scope=scope_context.scope)
            if owner_agent_name is not None
            else None
        )
    elif team_name is None and ad_hoc_team_has_private_member(member_names, config.agents):
        bound_scope = None
    else:
        bound_scope = resolve_bound_team_scope_context(
            member_names=member_names,
            config=config,
            team_name=team_name,
        )
    resolved_inputs = resolve_entity_preparation_inputs(
        config=config,
        entity_name=team_name if team_name in config.teams else None,
        static_prompt_tokens=static_prompt_tokens,
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
        agent_name=bound_scope.owner_agent_name,
        scope=bound_scope.scope,
        model=model,
        resolved_inputs=resolved_inputs,
        runtime_paths=runtime_paths,
        config=config,
        scope_context=scope_context,
        compaction_lifecycle=compaction_lifecycle,
        pipeline_timing=pipeline_timing,
        allow_native_compaction=allow_native_compaction,
        non_prompt_tokens=non_prompt_tokens,
    )


def resolve_entity_preparation_inputs(
    *,
    config: Config,
    entity_name: str | None,
    static_prompt_tokens: int,
    active_model_name: str | None,
    active_context_window: int | None,
    history_settings: ResolvedHistorySettings | None = None,
) -> HistoryPreparationInputs:
    """Resolve history settings, compaction config, runtime model, and plan for one agent or team scope.

    ``history_settings`` replaces the entity's authored settings, for an agent that is not configured.
    """
    resolved_entity = config.resolve_entity(entity_name)
    runtime_model = config.resolve_runtime_model(
        entity_name=entity_name,
        active_model_name=active_model_name,
        active_context_window=active_context_window,
    )
    return HistoryPreparationInputs(
        history_settings=history_settings or resolved_entity.history_settings,
        compaction_config=resolved_entity.compaction_config,
        has_authored_compaction_config=resolved_entity.has_authored_compaction_config,
        active_model_name=runtime_model.model_name,
        active_context_window=runtime_model.context_window,
        execution_plan=resolve_history_execution_plan(
            config=config,
            compaction_config=resolved_entity.compaction_config,
            has_authored_compaction_config=resolved_entity.has_authored_compaction_config,
            active_model_name=runtime_model.model_name,
            active_context_window=runtime_model.context_window,
            static_prompt_tokens=static_prompt_tokens,
        ),
    )


def resolve_agent_preparation_inputs(
    *,
    agent_name: str,
    history_settings: ResolvedHistorySettings,
    config: Config,
    static_prompt_tokens: int,
    active_model_name: str | None = None,
    active_context_window: int | None = None,
) -> HistoryPreparationInputs:
    """Resolve every history-preparation input for one agent run under the history settings its build resolved."""
    return resolve_entity_preparation_inputs(
        config=config,
        entity_name=agent_name if agent_name in config.agents else None,
        static_prompt_tokens=static_prompt_tokens,
        active_model_name=active_model_name,
        active_context_window=active_context_window,
        history_settings=history_settings,
    )


def _prepare_scope_state_for_run(
    *,
    storage: ConversationStore,
    session: ConversationSession,
    scope: HistoryScope,
    execution_plan: ResolvedHistoryExecutionPlan,
) -> HistoryScopeState:
    state = read_scope_state(session, scope)
    if consume_pending_force_compaction_scope(session, scope):
        state = set_force_compaction_state(session, scope, state, force=True)
        storage.save_session_fields(session, "metadata", "session_state")
    if state.force_compact_before_next_run and not execution_plan.text_compaction_available:
        state = set_force_compaction_state(session, scope, state, force=False)
        storage.save_session_fields(session, "metadata")
        description = describe_compaction_unavailability(execution_plan)
        logger.warning(
            "Forced compaction skipped because text compaction is unavailable",
            session_id=session.session_id,
            scope=scope.key,
            reason=description,
        )
    return state
