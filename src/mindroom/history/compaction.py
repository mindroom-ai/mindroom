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
from mindroom.error_handling import is_model_safeguard_refusal
from mindroom.history.claude_replay_compat import strip_stale_anthropic_replay_fields
from mindroom.history.replay import current_summary_text, estimate_prompt_visible_history_tokens, scope_visible_runs
from mindroom.history.storage import (
    FoldBasis,
    append_compaction_chunk,
    append_fold,
    ensure_scope_session_row,
    read_chunk_basis,
    record_summary_usage,
    save_working_run_changes,
    update_scope_state_on_latest,
)
from mindroom.history.summary_call import DEFAULT_SUMMARY_RETRY_POLICY, generate_compaction_summary
from mindroom.history.summary_input import build_summary_input, messages_for_runs, minimum_summary_input_tokens
from mindroom.history.types import (
    CompactionLifecycleProgress,
    CompactionOutcome,
    HistoryScope,
    HistoryScopeState,
    ResolvedHistoryExecutionPlan,
    ResolvedHistorySettings,
    ScopeSummary,
)
from mindroom.hooks import EVENT_COMPACTION_AFTER, EVENT_COMPACTION_BEFORE, CompactionHookContext, emit
from mindroom.llm.runs import RunRecord
from mindroom.logging_config import get_logger
from mindroom.providers.names import provider_route
from mindroom.providers.registry import build_model_provider
from mindroom.session_storage.lanes import run_session_storage_operation
from mindroom.session_storage.store import runs_without
from mindroom.timing import timed
from mindroom.token_budget import (
    CompactionEstimateKind,
    compaction_estimate_kind,
    estimate_compaction_input_tokens,
    estimate_text_tokens,
)
from mindroom.tool_system.runtime_context import (
    get_detached_requester_context,
    get_tool_runtime_context,
    resolve_tool_runtime_hook_bindings,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.history.summary_call import SummaryRetryDecision
    from mindroom.history.summary_rows import Segment
    from mindroom.llm.messages import Message
    from mindroom.llm.provider import ModelIdentity, ModelProvider, PortableReplayEstimator
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.llm.usage import MessageMetrics
    from mindroom.session_storage.store import ConversationStore
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


@timed("system_prompt_assembly.history_prepare.compaction_model_init")
def _load_compaction_model(config: Config, runtime_paths: RuntimePaths, model_name: str) -> ModelProvider:
    """Build the compaction model's provider with dedicated history-preparation timing."""
    return build_model_provider(config, runtime_paths, model_name)


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
) -> Callable[[ModelIdentity, MessageMetrics], Awaitable[None]]:
    """Record each summary response's usage in the scope's storage, under the reply's requester."""
    # An OpenAI-compatible request has no Matrix conversation, only its authenticated requester.
    requester_context = get_tool_runtime_context() or get_detached_requester_context()
    return partial(
        record_summary_usage,
        storage=storage,
        session_id=session_id,
        requester_id=requester_context.requester_id if requester_context is not None else None,
    )


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


async def _persist_cleared_force_state_if_needed(
    *,
    storage: ConversationStore,
    session: ConversationSession,
    scope: HistoryScope,
    state: HistoryScopeState,
) -> None:
    if not state.force_compact_before_next_run:
        return
    await run_session_storage_operation(
        lambda: storage,
        partial(
            update_scope_state_on_latest,
            session=session,
            scope=scope,
            # Only clear when the durable row still matches the state this run read;
            # a concurrent write (for example a fresh manual request) wins otherwise.
            update=lambda latest: replace(latest, force_compact_before_next_run=False) if latest == state else latest,
        ),
    )


async def _emit_compaction_hook(
    *,
    event_name: str,
    scope: HistoryScope,
    messages: Sequence[Message],
    session_id: str,
    token_count_before: int,
    token_count_after: int | None = None,
    compaction_summary: str | None = None,
) -> None:
    runtime_context = get_tool_runtime_context()
    if runtime_context is None or not runtime_context.hook_registry.has_hooks(event_name):
        return

    bindings = resolve_tool_runtime_hook_bindings(runtime_context)
    correlation_id = runtime_context.correlation_id or f"{event_name}:{session_id}:{uuid4().hex}"
    context = CompactionHookContext(
        event_name=event_name,
        plugin_name="",
        settings={},
        config=runtime_context.config,
        runtime_paths=runtime_context.runtime_paths,
        logger=logger.bind(event_name=event_name, session_id=session_id),
        correlation_id=correlation_id,
        message_sender=bindings.message_sender,
        matrix_admin=bindings.matrix_admin,
        room_state_querier=bindings.room_state_querier,
        room_state_putter=bindings.room_state_putter,
        agent_name=scope.scope_id if scope.kind == "team" else runtime_context.agent_name,
        scope=scope,
        room_id=runtime_context.room_id,
        thread_id=runtime_context.resolved_thread_id,
        messages=list(messages),
        session_id=session_id,
        token_count_before=token_count_before,
        token_count_after=token_count_after,
        compaction_summary=compaction_summary,
        _hook_registry_state=runtime_context.hook_registry_state,
    )
    await emit(runtime_context.hook_registry, event_name, context)


def _should_collect_compaction_hook_messages() -> bool:
    runtime_context = get_tool_runtime_context()
    return runtime_context is not None and any(
        runtime_context.hook_registry.has_hooks(event_name)
        for event_name in (EVENT_COMPACTION_BEFORE, EVENT_COMPACTION_AFTER)
    )


@timed("system_prompt_assembly.history_prepare.compaction")
async def compact_scope_history(
    *,
    storage: ConversationStore,
    session: ConversationSession,
    scope: HistoryScope,
    state: HistoryScopeState,
    summarizer: Summarizer,
    available_history_budget: int | None,
    before_tokens: int,
    replay_window_tokens: int | None,
    threshold_tokens: int | None,
    progress: Callable[[CompactionLifecycleProgress], Awaitable[None]] | None,
    complete: Callable[[CompactionOutcome], Awaitable[CompactionOutcome]],
    replay_model: PortableReplayEstimator | None,
) -> CompactionOutcome | None:
    """Compact one scope by appending summary rows that fold its oldest runs, chunk by chunk.

    ``progress`` receives the progress after each stored chunk that leaves runs to replay, and ``complete`` the
    outcome after the last stored chunk.
    """
    history_settings = summarizer.history_settings
    visible_runs = scope_visible_runs(session, scope)
    over_budget = available_history_budget is not None and before_tokens > available_history_budget
    runs = (
        _runs_with_stable_ids(visible_runs, session_id=session.session_id, scope=scope)
        if state.force_compact_before_next_run or over_budget
        else []
    )
    if not runs:
        await _persist_cleared_force_state_if_needed(storage=storage, session=session, scope=scope, state=state)
        return None

    mode = "manual" if state.force_compact_before_next_run else "auto"
    loaded_runs = list(session.runs)
    working_session = deepcopy(session)
    collect_compaction_hook_messages = _should_collect_compaction_hook_messages()
    compacted_messages: list[Message] = []

    async def before_persist(included_runs: Sequence[AnyRunRecord]) -> None:
        await _emit_compaction_hook(
            event_name=EVENT_COMPACTION_BEFORE,
            scope=scope,
            messages=messages_for_runs(included_runs, history_settings) if collect_compaction_hook_messages else (),
            session_id=session.session_id,
            token_count_before=before_tokens,
        )

    async def after_persist(chunk: _SummaryChunk, compacted_run_count: int, runs_remaining: int) -> None:
        if collect_compaction_hook_messages:
            compacted_messages.extend(messages_for_runs(chunk.included_runs, history_settings))
        if progress is None or not scope_visible_runs(working_session, scope):
            return
        after_tokens = await asyncio.to_thread(
            estimate_prompt_visible_history_tokens,
            session=working_session,
            scope=scope,
            history_settings=history_settings,
            replay_model=replay_model,
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
            scope=scope,
            summarizer=summarizer,
            runs=runs,
            before_persist=before_persist,
            after_persist=after_persist,
        )
    except ConversationChangedError:
        # The reply replays what the conversation holds now; a manual request stays for the next reply.
        await run_session_storage_operation(lambda: storage, lambda opened: opened.reload_log(session))
        raise
    if rewrite_result is None:
        await _persist_cleared_force_state_if_needed(storage=storage, session=session, scope=scope, state=state)
        return None

    compacted_at = _iso_utc_now()
    await run_session_storage_operation(
        lambda: storage,
        partial(save_working_run_changes, session=session, loaded_runs=loaded_runs, working_session=working_session),
    )
    await _persist_cleared_force_state_if_needed(storage=storage, session=session, scope=scope, state=state)
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
            scope=scope,
            history_settings=history_settings,
            replay_model=replay_model,
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
            runs_after=len(scope_visible_runs(session, scope)),
            compacted_run_count=rewrite_result.compacted_run_count,
            compacted_at=compacted_at,
            history_budget_tokens=available_history_budget,
        )
        return await _report_committed(outcome, scope=scope, messages=compacted_messages, complete=complete)

    # Persistence has committed; finish reporting before releasing this request owner.
    return await run_coroutine_until_complete(report())


@timed("system_prompt_assembly.history_prepare.compaction.rewrite_working_session")
async def _rewrite_working_session_for_compaction(
    *,
    storage: ConversationStore,
    persisted_session: ConversationSession,
    working_session: ConversationSession,
    scope: HistoryScope,
    summarizer: Summarizer,
    runs: Sequence[AnyRunRecord],
    before_persist: Callable[[Sequence[AnyRunRecord]], Awaitable[None]],
    after_persist: Callable[[_SummaryChunk, int, int], Awaitable[None]],
) -> _CompactionRewriteResult | None:
    """Fold ``runs``, oldest first, into one appended summary row per chunk, stopping when the conversation changes."""
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
                scope,
                [run.run_id for run in remaining if run.run_id],
                previous_summary=previous_summary,
            ),
        )
        if basis is None:
            _stop_for_changed_conversation(persisted_session.session_id, scope, compacted_run_count)
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
        await before_persist(chunk.included_runs)
        compacted_run_ids = tuple(run.run_id for run in chunk.included_runs if run.run_id)
        appended = await run_session_storage_operation(
            lambda: storage,
            partial(
                append_compaction_chunk,
                session=persisted_session,
                scope=scope,
                basis=basis,
                included_run_ids=compacted_run_ids,
                summary=chunk.summary,
                summary_model=chunk.summarizer.model.model.identity.id,
            ),
        )
        if not appended:
            _stop_for_changed_conversation(persisted_session.session_id, scope, compacted_run_count)
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
    strip_stale_anthropic_replay_fields(
        [message for run in scope_visible_runs(working_session, scope) for message in run.messages],
    )
    return _CompactionRewriteResult(
        summary_text=summary_text,
        compacted_run_count=compacted_run_count,
        served_by=summarizer.model,
    )


class ConversationChangedError(RuntimeError):
    """Another writer changed a scope's history after compaction read it, so compaction stored nothing."""

    def __init__(self) -> None:
        super().__init__("The conversation changed during compaction.")


def _stop_for_changed_conversation(session_id: str, scope: HistoryScope, compacted_run_count: int) -> None:
    """Stop compacting after another writer changed the scope's history; with nothing stored yet, compaction failed."""
    if compacted_run_count == 0:
        raise ConversationChangedError
    # Earlier chunks stand.
    logger.warning(
        "Compaction stopped because the conversation changed",
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
    scope: HistoryScope,
    basis: FoldBasis,
    segment: Segment,
    snapshots: Sequence[AnyRunRecord],
    folded_messages: Sequence[Message],
    summarizer: Summarizer,
    before_tokens: int,
    replay_window_tokens: int | None,
    threshold_tokens: int | None,
    available_history_budget: int | None,
    on_commit: Callable[[str], None],
    complete: Callable[[CompactionOutcome], Awaitable[CompactionOutcome]],
) -> CompactionOutcome:
    """Summarize the basis's stored runs and the running turn's ``snapshots``, then append one row with ``segment``.

    Nothing is written until every chunk has been summarized. The append leaves ``session`` holding what the store
    does; once it starts, ``on_commit`` receives the stored summary and the compaction is reported even when the turn
    is cancelled meanwhile, so the turn's in-memory state always matches the log and a stored fold never reads as
    failed. Raises when the conversation changed since ``basis`` was read.
    """
    runs = [*basis.runs, *snapshots]
    # The summary's usage row and summary row reference the session row, which a first turn has not written yet.
    await run_blocking_until_complete(partial(ensure_scope_session_row, storage, session, scope))
    collect_compaction_hook_messages = _should_collect_compaction_hook_messages()
    # Hooks see the folded messages themselves, not the per-call units the summarizer reads.
    hook_runs = [*basis.runs, RunRecord(run_id="", messages=list(folded_messages))]
    messages = (
        tuple(messages_for_runs(hook_runs, summarizer.history_settings)) if collect_compaction_hook_messages else ()
    )
    await _emit_compaction_hook(
        event_name=EVENT_COMPACTION_BEFORE,
        scope=scope,
        messages=messages,
        session_id=session.session_id,
        token_count_before=before_tokens,
    )
    summary, summarizer = await summarize_runs(summarizer, basis.summary, runs)

    async def commit_and_report() -> CompactionOutcome:
        # The append runs in the database's lane behind the session's other writes, and leaves the turn's session
        # holding what the store does.
        appended = await run_session_storage_operation(
            lambda: storage,
            partial(
                append_fold,
                session=session,
                scope=scope,
                basis=basis,
                summary=summary,
                summary_model=summarizer.model.model.identity.id,
                segment=segment,
            ),
        )
        if not appended:
            raise ConversationChangedError
        on_commit(summary)
        outcome = CompactionOutcome(
            mode="auto",
            session_id=session.session_id,
            scope=scope.key,
            summary=summary,
            summary_model=summarizer.model.name,
            before_tokens=before_tokens,
            after_tokens=estimate_text_tokens(summary),
            window_tokens=replay_window_tokens or 0,
            threshold_tokens=threshold_tokens or 0,
            # The running turn counts once beside the stored runs it folded with.
            runs_before=len(basis.runs) + 1,
            runs_after=1,
            compacted_run_count=len(basis.runs) + 1,
            compacted_at=_iso_utc_now(),
            history_budget_tokens=available_history_budget,
        )
        return await _report_committed(outcome, scope=scope, messages=messages, complete=complete)

    # A stop that lands once the append started still lets a stored fold apply and report, so it never reads as failed.
    return await run_coroutine_until_complete(commit_and_report())


async def _report_committed(
    outcome: CompactionOutcome,
    *,
    scope: HistoryScope,
    messages: Sequence[Message],
    complete: Callable[[CompactionOutcome], Awaitable[CompactionOutcome]],
) -> CompactionOutcome:
    """Tell compaction:after hooks, then the reply's lifecycle, about a stored compaction."""
    await _emit_compaction_hook(
        event_name=EVENT_COMPACTION_AFTER,
        scope=scope,
        messages=messages,
        session_id=outcome.session_id,
        token_count_before=outcome.before_tokens,
        token_count_after=outcome.after_tokens,
        compaction_summary=outcome.summary,
    )
    return await complete(outcome)


def _compaction_sizing(summary_model: ModelProvider) -> tuple[Callable[[str], int], CompactionEstimateKind]:
    """Resolve one estimator together with the kind describing its arithmetic."""
    model_id = summary_model.identity.id
    conservative_fallback = summary_model.capabilities.claude_family
    estimator = partial(
        estimate_compaction_input_tokens,
        model_id=model_id,
        conservative_fallback=conservative_fallback,
    )
    kind = compaction_estimate_kind(model_id, conservative_fallback=conservative_fallback)
    return estimator, kind


async def _summarize_chunk(  # noqa: PLR0915
    summarizer: Summarizer,
    previous_summary: str | None,
    runs: Sequence[AnyRunRecord],
) -> _SummaryChunk | None:
    """Summarize the longest prefix of ``runs`` that fits one summary input; None when not even one run fits.

    A failed attempt retries the same or a smaller input when safe. A safeguard
    refusal from the primary model switches once to the fallback model. The
    input remains unchanged when it fits the fallback model, otherwise it is
    rebuilt under that model's own budget. A refusal or failure from the
    fallback propagates. The switch shares the retry policy's attempt bound,
    so a refusal after an earlier shrink or transient retry propagates without
    a fallback call. All other failures keep the shrink and transient
    same-input retry behavior.
    """
    summary_model = summarizer.model
    fallback_model = summarizer.fallback
    budget = summary_model.input_budget_tokens
    token_estimator, estimate_kind = _compaction_sizing(summary_model.model)

    def build(
        max_input_tokens: int,
        estimator: Callable[[str], int],
    ) -> Awaitable[tuple[str, list[AnyRunRecord]]]:
        return asyncio.to_thread(
            build_summary_input,
            previous_summary=previous_summary,
            compacted_runs=runs,
            history_settings=summarizer.history_settings,
            max_input_tokens=max_input_tokens,
            token_estimator=estimator,
        )

    summary_input, included_runs = await build(budget, token_estimator)
    if not included_runs:
        return None
    retry_policy = DEFAULT_SUMMARY_RETRY_POLICY
    minimum_progress_input_tokens = await asyncio.to_thread(
        minimum_summary_input_tokens,
        previous_summary=previous_summary,
        first_run=runs[0],
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
            "candidate_runs": len(runs),
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
                error=str(exc) or type(exc).__name__,
            )
            # The attempt bound covers the fallback call too: a refusal after an
            # earlier shrink or transient retry propagates instead of issuing a
            # third provider call.
            if fallback_model is not None and attempt < retry_policy.max_attempts and is_model_safeguard_refusal(exc):
                fallback_token_estimator, fallback_estimate_kind = _compaction_sizing(fallback_model.model)
                fits_fallback = (
                    await asyncio.to_thread(fallback_token_estimator, summary_input)
                    <= fallback_model.input_budget_tokens
                )
                rebuilt_input, rebuilt_runs = (
                    (summary_input, included_runs)
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
                budget = fallback_model.input_budget_tokens
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
            rebuilt_input, rebuilt_runs = await build(retry_decision.budget, token_estimator)
            # A shrink that does not rebuild a smaller input with some run in it cannot help.
            if not rebuilt_runs or await asyncio.to_thread(token_estimator, rebuilt_input) >= summary_input_estimate:
                raise
            summary_input = rebuilt_input
            included_runs = rebuilt_runs
            budget = retry_decision.budget
            attempt += 1
            continue
        logger.info(
            "Compaction summary chunk completed",
            **attempt_fields,
            duration_ms=int((asyncio.get_running_loop().time() - started) * 1000),
        )
        later = (
            summarizer
            if summary_model is summarizer.model
            else replace(summarizer, model=replace(summary_model, input_budget_tokens=budget), fallback=None)
        )
        return _SummaryChunk(summary=summary, included_runs=included_runs, summarizer=later)


def _runs_with_stable_ids(
    runs: Sequence[AnyRunRecord],
    *,
    session_id: str,
    scope: HistoryScope,
) -> list[AnyRunRecord]:
    stable_runs = [run for run in runs if run.run_id]
    if len(stable_runs) < len(runs):
        logger.warning(
            "Compaction skipped runs without stable run IDs",
            session_id=session_id,
            scope=scope.key,
            skipped_runs=len(runs) - len(stable_runs),
        )
    return stable_runs


def _iso_utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = [
    "ConversationChangedError",
    "Summarizer",
    "SummaryModel",
    "compact_running_turn",
    "compact_scope_history",
    "load_summarizer",
    "scope_usage_recorder",
    "summarize_runs",
]
