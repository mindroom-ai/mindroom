"""Scoped compaction."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING
from uuid import uuid4

from mindroom.background_tasks import run_blocking_until_complete, run_coroutine_until_complete
from mindroom.failure_text import failure_text
from mindroom.history.conversation_text import utterances
from mindroom.history.replay import (
    current_summary_text,
    estimate_prompt_visible_history_tokens,
    estimate_summary_text_tokens,
    scope_visible_runs,
)
from mindroom.history.storage import (
    FoldBasis,
    append_compaction_chunk,
    append_fold,
    read_chunk_basis,
    record_summary_usage,
    set_force_compaction,
)
from mindroom.history.summary_call import DEFAULT_SUMMARY_RETRY_POLICY, generate_compaction_summary
from mindroom.history.summary_input import (
    build_summary_input,
    minimum_summary_input_tokens,
    whole_run_input_tokens,
)
from mindroom.history.types import (
    CompactionLifecycleProgress,
    CompactionOutcome,
    HistoryScope,
    HistoryScopeState,
    ResolvedHistoryExecutionPlan,
    ResolvedHistorySettings,
    ScopeSummary,
    owner_scope,
)
from mindroom.hooks import EVENT_COMPACTION_AFTER, EVENT_COMPACTION_BEFORE, CompactionHookContext, TranscriptLine, emit
from mindroom.llm.errors import ModelSafeguardRefusal
from mindroom.llm.runs import RunRecord
from mindroom.logging_config import get_logger
from mindroom.native_compaction import checkpoint_summary, replay_basis
from mindroom.providers import registry as provider_registry
from mindroom.providers.names import provider_route
from mindroom.session_storage.lanes import run_session_storage_operation
from mindroom.session_storage.store import runs_without
from mindroom.timing import timed
from mindroom.token_budget import (
    CompactionEstimateKind,
    compaction_estimate_kind,
    estimate_compaction_input_tokens,
)
from mindroom.tool_system.runtime_context import (
    current_requester_id,
    get_tool_runtime_context,
    scope_hook_context_kwargs,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.history.summary_call import SummaryRetryDecision
    from mindroom.llm.provider import ModelIdentity, ModelProvider, PortableReplayEstimator
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.llm.usage import MessageMetrics
    from mindroom.session_storage.store import ConversationStore
    from mindroom.session_storage.summary_rows import Fold
    from mindroom.session_storage.types import ConversationSession


logger = get_logger(__name__)


@dataclass(frozen=True)
class SummaryModel:
    """One serving model and its matching identity and serialized-input budget.

    A fallback switch replaces the whole value, so later chunks cannot use the previous model's budget or identity.
    """

    model: ModelProvider
    name: str
    input_budget_tokens: int


@dataclass(frozen=True)
class Summarizer:
    """The summary model and the request settings every summary chunk of one compaction shares."""

    model: SummaryModel
    fallback: SummaryModel | None
    prompt: str
    timeout_seconds: float
    history_settings: ResolvedHistorySettings
    session_id: str
    scope: HistoryScope
    # Receives the usage of every summary response that reports it, including rejected ones.
    on_usage: Callable[[ModelIdentity, MessageMetrics], Awaitable[None]] | None
    # The input budget a failed chunk shrank to; later chunks start there unless their first run needs more.
    shrunk_budget_tokens: int | None = None


@timed("system_prompt_assembly.history_prepare.compaction_model_init")
def _load_compaction_model(config: Config, runtime_paths: RuntimePaths, model_name: str) -> ModelProvider:
    """Build the compaction model's provider with dedicated history-preparation timing."""
    return provider_registry.build_model_provider(config, runtime_paths, model_name)


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
    ``provider_route``, the normalization model dispatch uses, so spelling
    variants like ``vertexai-claude`` and ``vertexai_claude`` and aliases like
    ``openai_codex`` and ``codex`` count as the same serving model.
    """
    if fallback_model_name != primary_model_name:
        primary_config = config.models.get(primary_model_name)
        fallback_config = config.models.get(fallback_model_name)
        if primary_config is None or fallback_config is None:
            return True
        if (provider_route(primary_config.provider), primary_config.id) != (
            provider_route(fallback_config.provider),
            fallback_config.id,
        ):
            return True
    logger.warning(
        "Compaction fallback resolves to the primary serving model; continuing without a fallback",
        compaction_model=primary_model_name,
        fallback_model=fallback_model_name,
    )
    return False


def load_summarizer(
    execution_plan: ResolvedHistoryExecutionPlan,
    history_settings: ResolvedHistorySettings,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    session_id: str,
    scope: HistoryScope,
    on_usage: Callable[[ModelIdentity, MessageMetrics], Awaitable[None]] | None,
) -> Summarizer:
    """Load the summary model and, when distinct and loadable, its safeguard fallback."""
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
        history_settings=history_settings,
        session_id=session_id,
        scope=scope,
        on_usage=on_usage,
    )


def scope_usage_recorder(
    storage: ConversationStore,
    session_id: str,
    *,
    initial_session: ConversationSession | None = None,
) -> Callable[[ModelIdentity, MessageMetrics], Awaitable[None]]:
    """Record each summary response's usage under the current request's requester, first creating ``initial_session``'s row."""
    return partial(
        record_summary_usage,
        storage=storage,
        session_id=session_id,
        requester_id=current_requester_id(),
        initial_session=initial_session,
    )


@dataclass(frozen=True)
class _SummarySource:
    """What a summary input builds on: the previous summary and the runs after it, plus the runs it folds unread."""

    unread_runs: list[AnyRunRecord]
    previous_summary: str | None
    runs: list[AnyRunRecord]


@dataclass(frozen=True)
class _SummaryChunk:
    summary: str
    included_runs: list[AnyRunRecord]
    # The summarizer that served this chunk, which later chunks use: once a fallback served one, it serves the rest.
    summarizer: Summarizer


@dataclass(frozen=True)
class _CompactionRewriteResult:
    summary_text: str
    compacted_run_count: int
    # The model that served the final persisted chunk, the fallback after a refusal switch.
    served_by: SummaryModel


async def clear_manual_compaction_request(
    *,
    storage: ConversationStore,
    session: ConversationSession,
    state: HistoryScopeState,
) -> None:
    """Clear the manual compaction request this run read in ``state``, if it read one.

    The request is one flag, so a request written after this run read it clears with it.
    """
    if state.force_compact_before_next_run:
        await run_session_storage_operation(
            lambda: storage,
            partial(set_force_compaction, session=session, forced=False),
        )


def _transcript(runs: Sequence[AnyRunRecord]) -> tuple[TranscriptLine, ...]:
    """Return what was said in ``runs``, oldest first, as compaction hooks read it."""
    return tuple(
        TranscriptLine(
            role="user" if said.kind == "said" else "assistant",
            sender=said.sender,
            display_name=said.display_name,
            text=said.unredacted,
        )
        for run in runs
        for said in utterances(run)
    )


async def _emit_compaction_hook(
    *,
    event_name: str,
    scope: HistoryScope,
    transcript: Sequence[TranscriptLine],
    session_id: str,
    token_count_before: int,
    token_count_after: int | None = None,
    compaction_summary: str | None = None,
) -> None:
    runtime_context = get_tool_runtime_context()
    if runtime_context is None or not runtime_context.hooks.registry.has_hooks(event_name):
        return

    context = CompactionHookContext(
        **scope_hook_context_kwargs(
            runtime_context,
            event_name=event_name,
            correlation_id=runtime_context.correlation_id or f"{event_name}:{session_id}:{uuid4().hex}",
            scope=scope,
            session_id=session_id,
        ),
        transcript=tuple(transcript),
        token_count_before=token_count_before,
        token_count_after=token_count_after,
        compaction_summary=compaction_summary,
    )
    await emit(runtime_context.hooks.registry, event_name, context)


def _compaction_hooks_registered() -> bool:
    runtime_context = get_tool_runtime_context()
    return runtime_context is not None and any(
        runtime_context.hooks.registry.has_hooks(event_name)
        for event_name in (EVENT_COMPACTION_BEFORE, EVENT_COMPACTION_AFTER)
    )


@timed("system_prompt_assembly.history_prepare.compaction")
async def compact_scope_history(
    *,
    storage: ConversationStore,
    session: ConversationSession,
    state: HistoryScopeState,
    summarizer: Summarizer,
    available_history_budget: int | None,
    max_summary_tokens: int | None,
    before_tokens: int,
    replay_window_tokens: int | None,
    threshold_tokens: int | None,
    progress: Callable[[CompactionLifecycleProgress], Awaitable[None]] | None,
    complete: Callable[[CompactionOutcome], Awaitable[CompactionOutcome]],
    replay_estimator: PortableReplayEstimator | None,
) -> CompactionOutcome | None:
    """Compact the scope owning ``session`` by appending summary rows that fold its oldest runs, chunk by chunk.

    ``max_summary_tokens`` is the most history the reply can replay; no chunk whose summary exceeds it is stored.
    ``progress`` receives the progress after each stored chunk that leaves runs to replay, and ``complete`` the
    outcome after the last stored chunk.
    """
    scope = owner_scope(session)
    history_settings = summarizer.history_settings
    visible_runs = scope_visible_runs(session)
    over_budget = available_history_budget is not None and before_tokens > available_history_budget
    runs = visible_runs if state.force_compact_before_next_run or over_budget else []
    if not runs:
        await clear_manual_compaction_request(storage=storage, session=session, state=state)
        return None

    mode = state.compaction_mode
    working_session = deepcopy(session)
    hooks_registered = _compaction_hooks_registered()
    compacted_transcript: list[TranscriptLine] = []

    async def before_persist(included_runs: Sequence[AnyRunRecord]) -> None:
        await _emit_compaction_hook(
            event_name=EVENT_COMPACTION_BEFORE,
            scope=scope,
            transcript=_transcript(included_runs) if hooks_registered else (),
            session_id=session.session_id,
            token_count_before=before_tokens,
        )

    async def after_persist(chunk: _SummaryChunk, compacted_run_count: int, runs_remaining: int) -> None:
        if hooks_registered:
            compacted_transcript.extend(_transcript(chunk.included_runs))
        if progress is None or not scope_visible_runs(working_session):
            return
        after_tokens = await asyncio.to_thread(
            estimate_prompt_visible_history_tokens,
            session=working_session,
            history_settings=history_settings,
            replay_estimator=replay_estimator,
        )
        await progress(
            CompactionLifecycleProgress(
                mode=mode,
                session_id=session.session_id,
                scope=scope.key,
                summary_model=chunk.summarizer.model.name,
                before_tokens=before_tokens,
                after_tokens=after_tokens,
                history_budget_tokens=available_history_budget,
                runs_before=len(visible_runs),
                compacted_run_count=compacted_run_count,
                runs_remaining=runs_remaining,
                threshold_tokens=threshold_tokens,
            ),
        )

    try:
        rewrite_result = await _rewrite_working_session_for_compaction(
            storage=storage,
            persisted_session=session,
            working_session=working_session,
            summarizer=summarizer,
            runs=runs,
            max_summary_tokens=max_summary_tokens,
            before_persist=before_persist,
            after_persist=after_persist,
        )
    except ConversationChangedError:
        # The reply replays what the conversation holds now; a manual request stays for the next reply.
        await run_session_storage_operation(lambda: storage, lambda opened: opened.reload_log(session))
        raise
    await clear_manual_compaction_request(storage=storage, session=session, state=state)
    if rewrite_result is None:
        return None

    compacted_at = _iso_utc_now()
    logger.info(
        "Compaction summary generated",
        session_id=session.session_id,
        scope=scope.key,
        compacted_runs=rewrite_result.compacted_run_count,
        model=rewrite_result.served_by.model.identity.id,
    )

    async def report() -> CompactionOutcome:
        after_tokens = await asyncio.to_thread(
            estimate_prompt_visible_history_tokens,
            session=session,
            history_settings=history_settings,
            replay_estimator=replay_estimator,
        )
        outcome = CompactionOutcome(
            mode=mode,
            session_id=session.session_id,
            scope=scope.key,
            summary=rewrite_result.summary_text,
            summary_model=rewrite_result.served_by.name,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            window_tokens=replay_window_tokens or 0,
            threshold_tokens=threshold_tokens or 0,
            runs_before=len(visible_runs),
            runs_after=len(scope_visible_runs(session)),
            compacted_run_count=rewrite_result.compacted_run_count,
            compacted_at=compacted_at,
            history_budget_tokens=available_history_budget,
        )
        return await _report_committed(
            outcome,
            scope=scope,
            transcript=compacted_transcript,
            complete=complete,
        )

    # Persistence has committed; finish reporting before releasing this request owner.
    return await run_coroutine_until_complete(report())


@timed("system_prompt_assembly.history_prepare.compaction.rewrite_working_session")
async def _rewrite_working_session_for_compaction(
    *,
    storage: ConversationStore,
    persisted_session: ConversationSession,
    working_session: ConversationSession,
    summarizer: Summarizer,
    runs: Sequence[AnyRunRecord],
    max_summary_tokens: int | None,
    before_persist: Callable[[Sequence[AnyRunRecord]], Awaitable[None]],
    after_persist: Callable[[_SummaryChunk, int, int], Awaitable[None]],
) -> _CompactionRewriteResult | None:
    """Fold ``runs``, oldest first, into one appended summary row per chunk.

    Stops when the conversation changes or a chunk's summary exceeds ``max_summary_tokens``; with nothing stored yet,
    either stop fails the compaction.
    """
    scope = owner_scope(persisted_session)
    summary_text: str | None = None
    compacted_run_count = 0
    remaining = list(runs)
    while remaining:
        previous_summary = current_summary_text(working_session)
        basis = await run_blocking_until_complete(
            partial(
                read_chunk_basis,
                storage,
                persisted_session,
                [run.run_id for run in remaining],
                previous_summary=previous_summary,
            ),
        )
        if basis is None:
            _stop_compaction(ConversationChangedError(), persisted_session.session_id, scope, compacted_run_count)
            break
        chunk = await _summarize_chunk(summarizer, previous_summary, remaining)
        if chunk is None:
            logger.warning(
                "Compaction skipped because no run fit the single-pass summary budget",
                session_id=persisted_session.session_id,
                scope=scope.key,
                candidate_runs=len(remaining),
                summary_input_budget_tokens=summarizer.model.input_budget_tokens,
            )
            break
        if (error := _summary_budget_error(chunk.summary, max_summary_tokens)) is not None:
            _stop_compaction(error, persisted_session.session_id, scope, compacted_run_count)
            break
        await before_persist(chunk.included_runs)
        compacted_run_ids = tuple(run.run_id for run in chunk.included_runs)
        appended = await run_session_storage_operation(
            lambda: storage,
            partial(
                append_compaction_chunk,
                session=persisted_session,
                basis=basis,
                included_run_ids=compacted_run_ids,
                summary=chunk.summary,
            ),
        )
        if not appended:
            _stop_compaction(ConversationChangedError(), persisted_session.session_id, scope, compacted_run_count)
            break
        summarizer = chunk.summarizer
        summary_text = chunk.summary
        working_session.summary = ScopeSummary(text=chunk.summary, updated_at=datetime.now(UTC))
        working_session.runs = runs_without(working_session.runs, compacted_run_ids)
        compacted_run_count += len(chunk.included_runs)
        remaining = remaining[len(chunk.included_runs) :]
        await after_persist(chunk, compacted_run_count, len(remaining))

    if summary_text is None:
        return None
    return _CompactionRewriteResult(
        summary_text=summary_text,
        compacted_run_count=compacted_run_count,
        served_by=summarizer.model,
    )


def _summary_budget_error(summary: str, max_summary_tokens: int | None) -> RuntimeError | None:
    """Return why ``summary`` must not be stored when it exceeds ``max_summary_tokens``, the most history a reply replays.

    Replay never drops a saved summary, so storing a larger one would fail a reply that can still fit raw runs.
    """
    summary_tokens = estimate_summary_text_tokens(summary)
    if max_summary_tokens is None or summary_tokens <= max_summary_tokens:
        return None
    return RuntimeError(
        f"The summary exceeds the history budget ({summary_tokens} estimated tokens; {max_summary_tokens} available).",
    )


class ConversationChangedError(RuntimeError):
    """Another writer changed a scope's history after compaction read it, so compaction stored nothing."""

    def __init__(self) -> None:
        super().__init__("The conversation changed during compaction.")


def _stop_compaction(error: Exception, session_id: str, scope: HistoryScope, compacted_run_count: int) -> None:
    """Stop compacting because of ``error``; with nothing stored yet, compaction failed with it."""
    if compacted_run_count == 0:
        raise error
    # Earlier chunks stand.
    logger.warning(
        "Compaction stopped early",
        reason=str(error),
        session_id=session_id,
        scope=scope.key,
        compacted_runs=compacted_run_count,
    )


async def summarize_runs(
    summarizer: Summarizer,
    previous_summary: str | None,
    runs: Sequence[AnyRunRecord],
) -> tuple[str, Summarizer]:
    """Summarize runs into one cumulative summary without writing anything.

    Runs that do not fit one summary input are summarized in further chunks, each merged into the summary so far.
    Returns the summary and the summarizer that served its last chunk.
    """
    summary = previous_summary
    remaining = list(runs)
    while remaining:
        chunk = await _summarize_chunk(summarizer, summary, remaining)
        if chunk is None:
            msg = "No compactable content fit the summary input budget."
            raise RuntimeError(msg)
        summarizer = chunk.summarizer
        summary = chunk.summary
        remaining = remaining[len(chunk.included_runs) :]
    assert summary is not None
    return summary, summarizer


async def compact_running_turn(
    *,
    storage: ConversationStore,
    session: ConversationSession,
    basis: FoldBasis,
    fold: Fold,
    snapshots: Sequence[AnyRunRecord],
    summarizer: Summarizer,
    before_tokens: int,
    replay_window_tokens: int | None,
    threshold_tokens: int | None,
    available_history_budget: int | None,
    max_summary_tokens: int,
    on_commit: Callable[[str], None],
    rewritten_request_tokens: Callable[[], int],
    complete: Callable[[CompactionOutcome], Awaitable[CompactionOutcome]],
) -> CompactionOutcome:
    """Summarize the basis's stored runs and the running turn's ``snapshots``, then append one row storing ``fold``.

    Nothing is written until every chunk has been summarized. The append leaves ``session`` holding what the store
    does; once it starts, ``on_commit`` receives the stored summary and the compaction is reported even when the turn
    is cancelled meanwhile, so the turn's in-memory state always matches the log and a stored fold never reads as
    failed. ``before_tokens`` sizes the request that triggered the fold, and ``rewritten_request_tokens`` sizes it
    after ``on_commit`` rewrote it, so the report compares one request before and after. Raises, writing nothing,
    when the summary exceeds ``max_summary_tokens`` or the conversation changed since ``basis`` was read.
    """
    scope = owner_scope(session)
    runs = [*basis.runs, *snapshots]
    summary, summarizer = await summarize_runs(summarizer, basis.summary, runs)
    if (error := _summary_budget_error(summary, max_summary_tokens)) is not None:
        raise error
    # Hooks see the folded messages themselves, not the per-call units the summarizer reads, under the running turn's
    # requester and metadata, which decide who said each message.
    folded_run = RunRecord(
        run_id="",
        user_id=fold.run.user_id,
        messages=list(fold.messages),
        metadata=fold.run.metadata,
    )
    transcript = _transcript([*basis.runs, folded_run]) if _compaction_hooks_registered() else ()
    # Like a compaction before a reply, hooks hear only of a summary that is about to be stored.
    await _emit_compaction_hook(
        event_name=EVENT_COMPACTION_BEFORE,
        scope=scope,
        transcript=transcript,
        session_id=session.session_id,
        token_count_before=before_tokens,
    )

    async def commit_and_report() -> CompactionOutcome:
        # The append runs in the database's lane behind the session's other writes, and leaves the turn's session
        # holding what the store does.
        appended = await run_session_storage_operation(
            lambda: storage,
            partial(
                append_fold,
                session=session,
                basis=basis,
                summary=summary,
                fold=fold,
            ),
        )
        if not appended:
            raise ConversationChangedError
        on_commit(summary)
        # Sizing can tokenize the whole request, so it stays off the event loop.
        after_tokens = await asyncio.to_thread(rewritten_request_tokens)
        outcome = CompactionOutcome(
            mode="auto",
            session_id=session.session_id,
            scope=scope.key,
            summary=summary,
            summary_model=summarizer.model.name,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            window_tokens=replay_window_tokens or 0,
            threshold_tokens=threshold_tokens or 0,
            # The running turn counts once beside the stored runs it folded with.
            runs_before=len(basis.runs) + 1,
            runs_after=1,
            compacted_run_count=len(basis.runs) + 1,
            compacted_at=_iso_utc_now(),
            history_budget_tokens=available_history_budget,
        )
        return await _report_committed(
            outcome,
            scope=scope,
            transcript=transcript,
            complete=complete,
        )

    # A stop that lands once the append started still lets a stored fold apply and report, so it never reads as failed.
    return await run_coroutine_until_complete(commit_and_report())


async def _report_committed(
    outcome: CompactionOutcome,
    *,
    scope: HistoryScope,
    transcript: Sequence[TranscriptLine],
    complete: Callable[[CompactionOutcome], Awaitable[CompactionOutcome]],
) -> CompactionOutcome:
    """Tell compaction:after hooks, then the reply's lifecycle, about a stored compaction."""
    await _emit_compaction_hook(
        event_name=EVENT_COMPACTION_AFTER,
        scope=scope,
        transcript=transcript,
        session_id=outcome.session_id,
        token_count_before=outcome.before_tokens,
        token_count_after=outcome.after_tokens,
        compaction_summary=outcome.summary,
    )
    return await complete(outcome)


def _compaction_sizing(summary_model: ModelProvider) -> tuple[Callable[[str], int], CompactionEstimateKind]:
    """Resolve one estimator together with the kind describing its arithmetic."""
    model_id = summary_model.identity.id
    conservative_fallback = summary_model.capabilities.conservative_token_estimates
    estimator = partial(
        estimate_compaction_input_tokens,
        model_id=model_id,
        conservative_fallback=conservative_fallback,
    )
    kind = compaction_estimate_kind(model_id, conservative_fallback=conservative_fallback)
    return estimator, kind


def _after_native_checkpoint(previous_summary: str | None, runs: Sequence[AnyRunRecord]) -> _SummarySource | None:
    """Split off the runs before the latest readable native checkpoint, with its text as their summary.

    A provider checkpoint summarizes everything its request replayed before it, including the previous summary,
    whichever model wrote it. It stands in only while that is still ``previous_summary`` and the runs before its run:
    a run that waited for approval sorts after the turns it waited for, and retiring a summary keeps its runs.
    Its text also covers its own run's prompt, so that run stays in the input: the summary built on the text then
    always folds it, and an edit or redaction of that run removes the summary too.
    """
    for index in range(len(runs) - 1, 0, -1):
        basis = replay_basis(previous_summary, runs[:index])
        for message in reversed(runs[index].messages):
            if (summary := checkpoint_summary(message, basis)) is not None:
                return _SummarySource(list(runs[:index]), summary, list(runs[index:]))
    return None


async def _summarize_chunk(  # noqa: C901, PLR0915
    summarizer: Summarizer,
    previous_summary: str | None,
    runs: Sequence[AnyRunRecord],
) -> _SummaryChunk | None:
    """Summarize the longest prefix of ``runs`` that fits one summary input; None when not even one run fits.

    The runs before the latest readable native checkpoint fold without being read, since its text summarizes them.

    A failed attempt retries the same or a smaller input when safe, and a shrunk size carries to later chunks. A
    safeguard refusal from the primary model switches once to the fallback model, rebuilding the input only when it
    exceeds the fallback's own budget; a refusal or failure from the fallback propagates. The switch counts against the
    retry policy's attempt bound, so a refusal after an earlier shrink or transient retry propagates without a
    fallback call.
    """
    summary_model = summarizer.model
    fallback_model = summarizer.fallback
    shrunk_budget = summarizer.shrunk_budget_tokens
    token_estimator, estimate_kind = _compaction_sizing(summary_model.model)
    original = _SummarySource([], previous_summary, list(runs))
    sources = [source for source in (_after_native_checkpoint(previous_summary, runs), original) if source is not None]

    async def build(
        max_input_tokens: int,
        estimator: Callable[[str], int],
    ) -> tuple[str, list[AnyRunRecord], _SummarySource]:
        # A checkpoint too large to leave room for its own run gives way to the history it summarized.
        for candidate in sources:
            built_input, built_runs = await asyncio.to_thread(
                build_summary_input,
                previous_summary=candidate.previous_summary,
                compacted_runs=candidate.runs,
                history_settings=summarizer.history_settings,
                max_input_tokens=max_input_tokens,
                token_estimator=estimator,
            )
            if built_runs:
                return built_input, built_runs, candidate
        return "", [], original

    budget = summary_model.input_budget_tokens
    if shrunk_budget is not None:
        whole_first_run = await asyncio.to_thread(
            whole_run_input_tokens,
            previous_summary=sources[0].previous_summary,
            first_run=sources[0].runs[0],
            history_settings=summarizer.history_settings,
            token_estimator=token_estimator,
        )
        # Start where an earlier failure shrank the input, without truncating a run the full budget holds whole.
        budget = min(budget, max(shrunk_budget, whole_first_run))
    summary_input, included_runs, source = await build(budget, token_estimator)
    if not included_runs:
        return None
    retry_policy = DEFAULT_SUMMARY_RETRY_POLICY
    minimum_progress_input_tokens = await asyncio.to_thread(
        minimum_summary_input_tokens,
        previous_summary=source.previous_summary,
        first_run=source.runs[0],
        token_estimator=token_estimator,
    )
    attempt = 1
    while True:
        summary_input_estimate = await asyncio.to_thread(token_estimator, summary_input)
        attempt_fields = {
            "session_id": summarizer.session_id,
            "scope": summarizer.scope.key,
            "model_name": summary_model.name,
            "attempt": attempt,
            "native_checkpoint_runs": len(source.unread_runs),
            "candidate_runs": len(source.runs),
            "included_runs": len(included_runs),
            "summary_input_estimate": summary_input_estimate,
            "summary_input_estimate_kind": estimate_kind,
            "summary_input_budget_tokens": budget,
            "timeout_seconds": summarizer.timeout_seconds,
        }
        started = asyncio.get_running_loop().time()
        logger.info("Compaction summary chunk request", **attempt_fields)
        try:
            summary = await generate_compaction_summary(
                model=summary_model.model,
                summary_input=summary_input,
                summary_prompt=summarizer.prompt,
                timeout_seconds=summarizer.timeout_seconds,
                on_usage=(
                    partial(summarizer.on_usage, summary_model.model.identity)
                    if summarizer.on_usage is not None
                    else None
                ),
            )
        except Exception as exc:
            logger.warning(
                "Compaction summary chunk failed",
                **attempt_fields,
                duration_ms=int((asyncio.get_running_loop().time() - started) * 1000),
                error=failure_text(exc),
            )
            if (
                fallback_model is not None
                and attempt < retry_policy.max_attempts
                and isinstance(exc, ModelSafeguardRefusal)
            ):
                fallback_token_estimator, fallback_estimate_kind = _compaction_sizing(fallback_model.model)
                fits_fallback = (
                    await asyncio.to_thread(fallback_token_estimator, summary_input)
                    <= fallback_model.input_budget_tokens
                )
                rebuilt_input, rebuilt_runs, rebuilt_source = (
                    (summary_input, included_runs, source)
                    if fits_fallback
                    else await build(fallback_model.input_budget_tokens, fallback_token_estimator)
                )
                if not rebuilt_runs:
                    raise
                logger.info(
                    "Compaction summary refused; switching to fallback model",
                    session_id=summarizer.session_id,
                    scope=summarizer.scope.key,
                    attempt=attempt,
                    refused_model=summary_model.name,
                    fallback_model=fallback_model.name,
                    fallback_summary_input_budget_tokens=fallback_model.input_budget_tokens,
                )
                summary_model = fallback_model
                summary_input = rebuilt_input
                included_runs = rebuilt_runs
                source = rebuilt_source
                budget = fallback_model.input_budget_tokens
                shrunk_budget = None
                token_estimator = fallback_token_estimator
                estimate_kind = fallback_estimate_kind
                fallback_model = None
                attempt += 1
                continue
            retry_decision: SummaryRetryDecision | None = retry_policy.retry_budget(
                attempt=attempt,
                budget=budget,
                input_tokens=summary_input_estimate,
                minimum_progress_input_tokens=minimum_progress_input_tokens,
                error=exc,
            )
            if retry_decision is None:
                raise
            if retry_decision.kind == "same-budget-transient":
                await asyncio.sleep(retry_policy.same_input_retry_delay_seconds)
                attempt += 1
                continue
            rebuilt_input, rebuilt_runs, rebuilt_source = await build(retry_decision.budget, token_estimator)
            # A shrink that does not rebuild a smaller input with some run in it cannot help.
            if not rebuilt_runs or await asyncio.to_thread(token_estimator, rebuilt_input) >= summary_input_estimate:
                raise
            summary_input = rebuilt_input
            included_runs = rebuilt_runs
            source = rebuilt_source
            budget = shrunk_budget = retry_decision.budget
            attempt += 1
            continue
        logger.info(
            "Compaction summary chunk completed",
            **attempt_fields,
            duration_ms=int((asyncio.get_running_loop().time() - started) * 1000),
        )
        later = replace(summarizer, model=summary_model, fallback=fallback_model, shrunk_budget_tokens=shrunk_budget)
        return _SummaryChunk(summary=summary, included_runs=[*source.unread_runs, *included_runs], summarizer=later)


def _iso_utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = [
    "ConversationChangedError",
    "Summarizer",
    "SummaryModel",
    "clear_manual_compaction_request",
    "compact_running_turn",
    "compact_scope_history",
    "load_summarizer",
    "scope_usage_recorder",
    "summarize_runs",
]
