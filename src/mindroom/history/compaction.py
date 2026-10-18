"""Scoped compaction."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING
from uuid import uuid4

from agno.run.agent import RunOutput
from agno.session.summary import SessionSummary

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
from mindroom.history.summary_provider_compat import effective_summary_timeout_seconds
from mindroom.history.types import (
    CompactionLifecycleProgress,
    CompactionOutcome,
    HistoryScope,
    HistoryScopeState,
    ResolvedHistorySettings,
)
from mindroom.hooks import EVENT_COMPACTION_AFTER, EVENT_COMPACTION_BEFORE, CompactionHookContext, emit
from mindroom.logging_config import get_logger
from mindroom.providers.claude_prompt_cache import as_anthropic_claude
from mindroom.session_storage.agent_storage import runs_without
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

    from agno.db.base import BaseDb
    from agno.models.base import Model
    from agno.models.message import Message
    from agno.models.response import ModelResponse
    from agno.run.team import TeamRunOutput
    from agno.session.agent import AgentSession
    from agno.session.team import TeamSession

    from mindroom.history.summary_call import SummaryRetryDecision
    from mindroom.history.summary_rows import Segment
    from mindroom.native_compaction import NativeCompactionModel


logger = get_logger(__name__)


@dataclass(frozen=True)
class SummaryModel:
    """One serving model and its matching identity and serialized-input budget.

    A fallback switch replaces the whole value, so later chunks cannot use the previous model's budget or identity.
    """

    model: Model
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
    # Receives every summary response, including rejected ones, so its usage is recorded.
    on_response: Callable[[Model, ModelResponse], Awaitable[None]] | None = None


def scope_usage_recorder(storage: BaseDb, session_id: str) -> Callable[[Model, ModelResponse], Awaitable[None]]:
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
    summary: SessionSummary
    included_runs: list[RunOutput | TeamRunOutput]
    # What later chunks use: once a fallback served a chunk, it serves the rest too.
    summarizer: Summarizer


@dataclass(frozen=True)
class _CompactionRewriteResult:
    summary_text: str
    compacted_run_count: int
    # The model that served the final persisted chunk, the fallback after a refusal switch.
    served_by: SummaryModel


async def _persist_cleared_force_state_if_needed(
    *,
    storage: BaseDb,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    state: HistoryScopeState,
) -> None:
    if not state.force_compact_before_next_run:
        return
    await run_blocking_until_complete(
        update_scope_state_on_latest,
        storage,
        session,
        scope,
        # Only clear when the durable row still matches the state this run read;
        # a concurrent write (for example a fresh manual request) wins otherwise.
        lambda latest: replace(latest, force_compact_before_next_run=False) if latest == state else latest,
    )


async def _emit_compaction_hook(
    *,
    event_name: str,
    scope: HistoryScope,
    messages: Sequence[Message],
    session_id: str,
    token_count_before: int,
    token_count_after: int | None,
    compaction_summary: str | None,
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
    if runtime_context is None:
        return False
    return runtime_context.hook_registry.has_hooks(EVENT_COMPACTION_BEFORE) or runtime_context.hook_registry.has_hooks(
        EVENT_COMPACTION_AFTER,
    )


@timed("system_prompt_assembly.history_prepare.compaction")
async def compact_scope_history(
    *,
    storage: BaseDb,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    state: HistoryScopeState,
    summarizer: Summarizer,
    available_history_budget: int | None,
    before_tokens: int,
    replay_window_tokens: int | None,
    threshold_tokens: int | None,
    progress: Callable[[CompactionLifecycleProgress], Awaitable[None]] | None,
    complete: Callable[[CompactionOutcome], Awaitable[CompactionOutcome]],
    replay_model: NativeCompactionModel | None = None,
) -> CompactionOutcome | None:
    """Compact one scope by appending summary rows that fold its oldest runs, chunk by chunk.

    ``progress`` receives each persisted chunk's progress and ``complete`` the outcome once every chunk is stored.
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
    loaded_runs = list(session.runs or [])
    working_session = deepcopy(session)
    collect_compaction_hook_messages = _should_collect_compaction_hook_messages()
    compacted_messages: list[Message] = []

    async def before_persist(included_runs: Sequence[RunOutput | TeamRunOutput]) -> None:
        await _emit_compaction_hook(
            event_name=EVENT_COMPACTION_BEFORE,
            scope=scope,
            messages=messages_for_runs(included_runs, history_settings) if collect_compaction_hook_messages else (),
            session_id=session.session_id,
            token_count_before=before_tokens,
            token_count_after=None,
            compaction_summary=None,
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
    if rewrite_result is None:
        await _persist_cleared_force_state_if_needed(storage=storage, session=session, scope=scope, state=state)
        return None

    compacted_at = _iso_utc_now()
    await run_blocking_until_complete(save_working_run_changes, storage, session, loaded_runs, working_session)
    await _persist_cleared_force_state_if_needed(storage=storage, session=session, scope=scope, state=state)
    logger.info(
        "Compaction summary generated",
        session_id=session.session_id,
        scope=scope.key,
        compacted_runs=rewrite_result.compacted_run_count,
        model=_model_identifier(rewrite_result.served_by.model),
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
    storage: BaseDb,
    persisted_session: AgentSession | TeamSession,
    working_session: AgentSession | TeamSession,
    scope: HistoryScope,
    summarizer: Summarizer,
    runs: Sequence[RunOutput | TeamRunOutput],
    before_persist: Callable[[Sequence[RunOutput | TeamRunOutput]], Awaitable[None]],
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
            _log_conversation_changed(persisted_session.session_id, scope, compacted_run_count)
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
        summarizer = chunk.summarizer
        await before_persist(chunk.included_runs)
        compacted_run_ids = tuple(run.run_id for run in chunk.included_runs if run.run_id)
        chunk_summary = SessionSummary(summary=chunk.summary.summary, updated_at=datetime.now(UTC))
        appended = await run_blocking_until_complete(
            partial(
                append_compaction_chunk,
                storage=storage,
                session=persisted_session,
                scope=scope,
                basis=basis,
                included_run_ids=compacted_run_ids,
                summary=chunk_summary,
                summary_model=_model_identifier(summarizer.model.model),
            ),
        )
        if not appended:
            _log_conversation_changed(persisted_session.session_id, scope, compacted_run_count)
            break
        summary_text = chunk_summary.summary
        working_session.summary = chunk_summary
        working_session.runs = runs_without(working_session.runs or [], compacted_run_ids)
        compacted_run_count += len(chunk.included_runs)
        remaining = remaining[len(chunk.included_runs) :]
        await after_persist(chunk, compacted_run_count, len(remaining))

    if summary_text is None:
        return None
    strip_stale_anthropic_replay_fields(
        [message for run in scope_visible_runs(working_session, scope) for message in run.messages or []],
    )
    return _CompactionRewriteResult(
        summary_text=summary_text,
        compacted_run_count=compacted_run_count,
        served_by=summarizer.model,
    )


def _log_conversation_changed(session_id: str, scope: HistoryScope, compacted_run_count: int) -> None:
    # Another writer changed this scope's history since the session loaded; earlier chunks stand.
    logger.warning(
        "Compaction stopped because the conversation changed",
        session_id=session_id,
        scope=scope.key,
        compacted_runs=compacted_run_count,
    )


async def summarize_runs(
    summarizer: Summarizer,
    previous_summary: str | None,
    runs: Sequence[RunOutput | TeamRunOutput],
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
        summary = chunk.summary.summary
        remaining = remaining[len(chunk.included_runs) :]
    assert summary is not None
    return summary, summarizer


async def compact_running_turn(
    *,
    storage: BaseDb,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    basis: FoldBasis,
    segment: Segment,
    snapshots: Sequence[RunOutput | TeamRunOutput],
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

    Nothing is written until every chunk has been summarized. ``on_commit`` receives the summary once the row
    is stored, even when the turn is cancelled meanwhile, so the turn's in-memory state always matches the log.
    Raises when the conversation changed since ``basis`` was read.
    """
    runs = [*basis.runs, *snapshots]
    # The summary's usage row and summary row reference the session row, which a first turn has not written yet.
    await run_blocking_until_complete(partial(ensure_scope_session_row, storage, session, scope))
    collect_compaction_hook_messages = _should_collect_compaction_hook_messages()
    # Hooks see the folded messages themselves, not the per-call units the summarizer reads.
    hook_runs = [*basis.runs, RunOutput(messages=list(folded_messages))]
    messages = (
        tuple(messages_for_runs(hook_runs, summarizer.history_settings)) if collect_compaction_hook_messages else ()
    )
    await _emit_compaction_hook(
        event_name=EVENT_COMPACTION_BEFORE,
        scope=scope,
        messages=messages,
        session_id=session.session_id,
        token_count_before=before_tokens,
        token_count_after=None,
        compaction_summary=None,
    )
    summary, summarizer = await summarize_runs(summarizer, basis.summary, runs)

    async def commit() -> bool:
        appended = await asyncio.to_thread(
            append_fold,
            storage,
            session=session,
            scope=scope,
            basis=basis,
            summary=summary,
            summary_model=_model_identifier(summarizer.model.model),
            segment=segment,
        )
        if appended:
            on_commit(summary)
        return appended

    if not await run_coroutine_until_complete(commit()):
        msg = "The conversation changed during compaction."
        raise RuntimeError(msg)
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
    # The fold is stored; finish reporting it before a stopped turn's cancellation lands.
    return await run_coroutine_until_complete(
        _report_committed(outcome, scope=scope, messages=messages, complete=complete),
    )


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


def _compaction_sizing(summary_model: Model) -> tuple[Callable[[str], int], CompactionEstimateKind]:
    """Resolve one estimator together with the kind describing its arithmetic."""
    conservative_fallback = as_anthropic_claude(summary_model) is not None
    estimator = partial(
        estimate_compaction_input_tokens,
        model_id=summary_model.id,
        conservative_fallback=conservative_fallback,
    )
    kind = compaction_estimate_kind(summary_model.id, conservative_fallback=conservative_fallback)
    return estimator, kind


async def _summarize_chunk(  # noqa: C901, PLR0915
    summarizer: Summarizer,
    previous_summary: str | None,
    runs: Sequence[RunOutput | TeamRunOutput],
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
    ) -> Awaitable[tuple[str, list[RunOutput | TeamRunOutput]]]:
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
            "effective_timeout_seconds": effective_summary_timeout_seconds(
                summary_model.model,
                timeout_seconds=summarizer.timeout_seconds,
            ),
        }
        started = asyncio.get_running_loop().time()
        logger.info("Compaction summary chunk request", **attempt_fields)
        try:
            summary = await generate_compaction_summary(
                model=summary_model.model,
                summary_input=summary_input,
                summary_prompt=summarizer.prompt,
                timeout_seconds=summarizer.timeout_seconds,
                on_response=(
                    partial(summarizer.on_response, summary_model.model) if summarizer.on_response is not None else None
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
            if retry_decision is not None:
                if retry_decision.kind == "same-budget-transient":
                    await asyncio.sleep(retry_policy.same_input_retry_delay_seconds)
                    attempt += 1
                    continue
                rebuilt_input, rebuilt_runs = await build(retry_decision.budget, token_estimator)
                if rebuilt_runs:
                    rebuilt_input_tokens = await asyncio.to_thread(token_estimator, rebuilt_input)
                    if retry_decision.kind == "shrink" and rebuilt_input_tokens >= summary_input_estimate:
                        raise
                    summary_input = rebuilt_input
                    included_runs = rebuilt_runs
                    budget = retry_decision.budget
                    attempt += 1
                    continue
            raise
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
    runs: Sequence[RunOutput | TeamRunOutput],
    *,
    session_id: str,
    scope: HistoryScope,
) -> list[RunOutput | TeamRunOutput]:
    stable_runs = [run for run in runs if _has_stable_run_id(run)]
    if len(stable_runs) < len(runs):
        logger.warning(
            "Compaction skipped runs without stable run IDs",
            session_id=session_id,
            scope=scope.key,
            skipped_runs=len(runs) - len(stable_runs),
        )
    return stable_runs


def _has_stable_run_id(run: RunOutput | TeamRunOutput) -> bool:
    return isinstance(run.run_id, str) and bool(run.run_id)


def _model_identifier(model: Model) -> str:
    return model.id or model.__class__.__name__


def _iso_utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
