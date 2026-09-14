"""Scoped compaction."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING
from uuid import uuid4

from agno.session.summary import SessionSummary

from mindroom.claude_prompt_cache import as_anthropic_claude
from mindroom.error_handling import is_model_safeguard_refusal
from mindroom.history.claude_replay_compat import strip_stale_anthropic_replay_fields
from mindroom.history.replay import current_summary_text, estimate_prompt_visible_history_tokens, scope_visible_runs
from mindroom.history.storage import (
    compacted_run_ids_with,
    record_compaction_chunk,
    remove_runs_by_id,
    seen_event_ids_for_runs,
    update_scope_seen_event_ids,
    update_scope_state_on_latest,
    write_scope_state,
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
from mindroom.timing import timed
from mindroom.token_budget import CompactionEstimateKind, compaction_estimate_kind, estimate_compaction_input_tokens
from mindroom.tool_system.runtime_context import get_tool_runtime_context, resolve_tool_runtime_hook_bindings

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from agno.db.base import BaseDb
    from agno.models.base import Model
    from agno.models.message import Message
    from agno.run.agent import RunOutput
    from agno.run.team import TeamRunOutput
    from agno.session.agent import AgentSession
    from agno.session.team import TeamSession

    from mindroom.history.summary_call import SummaryRetryDecision
    from mindroom.native_compaction import NativeCompactionModel


logger = get_logger(__name__)


@dataclass(frozen=True)
class SummaryModel:
    """One serving model and its matching identity and serialized-input budget."""

    model: Model
    name: str
    input_budget_tokens: int


@dataclass(frozen=True)
class _CompactionRewriteResult:
    summary_text: str
    compacted_run_count: int
    compacted_run_ids: tuple[str, ...]
    compacted_messages: tuple[Message, ...]
    # The model that actually served the final persisted summary chunk; differs
    # from the configured primary after a safeguard-refusal fallback switch.
    served_by: SummaryModel


@dataclass(frozen=True)
class _GeneratedSummaryChunk:
    summary: SessionSummary
    included_runs: list[RunOutput | TeamRunOutput]
    # The model that actually served this chunk (fallback after a refusal switch).
    served_by: SummaryModel


def _persist_cleared_force_state_if_needed(
    *,
    storage: BaseDb,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    state: HistoryScopeState,
) -> HistoryScopeState:
    if not state.force_compact_before_next_run:
        return state
    return update_scope_state_on_latest(
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
    history_settings: ResolvedHistorySettings,
    available_history_budget: int | None,
    summary_model: SummaryModel,
    replay_window_tokens: int | None,
    threshold_tokens: int | None,
    summary_prompt: str,
    summary_timeout_seconds: float,
    fallback_summary_model: SummaryModel | None = None,
    lifecycle_notice_event_id: str | None = None,
    progress_callback: Callable[[CompactionLifecycleProgress], Awaitable[None]] | None = None,
    replay_model: NativeCompactionModel | None = None,
) -> CompactionOutcome | None:
    """Compact one scope by rewriting session.summary and session.runs."""
    visible_runs = scope_visible_runs(session, scope)
    compactable_runs = _select_compaction_candidates(
        visible_runs=visible_runs,
        session=session,
        scope=scope,
        state=state,
        history_settings=history_settings,
        available_history_budget=available_history_budget,
        replay_model=replay_model,
    )
    if not compactable_runs:
        _persist_cleared_force_state_if_needed(
            storage=storage,
            session=session,
            scope=scope,
            state=state,
        )
        return None
    selected_run_ids = _stable_compaction_run_ids(
        compactable_runs,
        session_id=session.session_id,
        scope=scope,
    )
    if not selected_run_ids:
        _persist_cleared_force_state_if_needed(
            storage=storage,
            session=session,
            scope=scope,
            state=state,
        )
        return None

    before_tokens = estimate_prompt_visible_history_tokens(
        session=session,
        scope=scope,
        history_settings=history_settings,
        replay_model=replay_model,
    )
    before_run_count = len(visible_runs)
    working_session = deepcopy(session)
    collect_compaction_hook_messages = _should_collect_compaction_hook_messages()

    async def emit_before_persist(included_runs: Sequence[RunOutput | TeamRunOutput]) -> None:
        await _emit_compaction_hook(
            event_name=EVENT_COMPACTION_BEFORE,
            scope=scope,
            messages=messages_for_runs(included_runs, history_settings) if collect_compaction_hook_messages else (),
            session_id=session.session_id,
            token_count_before=before_tokens,
            token_count_after=None,
            compaction_summary=None,
        )

    rewrite_result = await _rewrite_working_session_for_compaction(
        storage=storage,
        persisted_session=session,
        working_session=working_session,
        summary_model=summary_model,
        fallback_summary_model=fallback_summary_model,
        session_id=session.session_id,
        scope=scope,
        state=state,
        history_settings=history_settings,
        available_history_budget=available_history_budget,
        selected_run_ids=selected_run_ids,
        before_tokens=before_tokens,
        runs_before=before_run_count,
        threshold_tokens=threshold_tokens,
        summary_prompt=summary_prompt,
        summary_timeout_seconds=summary_timeout_seconds,
        lifecycle_notice_event_id=lifecycle_notice_event_id,
        progress_callback=progress_callback,
        collect_compaction_hook_messages=collect_compaction_hook_messages,
        before_persist_callback=emit_before_persist,
        replay_model=replay_model,
    )
    if rewrite_result is None:
        _persist_cleared_force_state_if_needed(
            storage=storage,
            session=session,
            scope=scope,
            state=state,
        )
        return None

    compacted_at = _iso_utc_now()
    new_state = HistoryScopeState(
        last_compacted_at=compacted_at,
        last_summary_model=_model_identifier(rewrite_result.served_by.model),
        last_compacted_run_count=rewrite_result.compacted_run_count,
        compacted_run_ids=compacted_run_ids_with(state, rewrite_result.compacted_run_ids),
        force_compact_before_next_run=False,
    )
    write_scope_state(session, scope, new_state)
    write_scope_state(working_session, scope, new_state)
    record_compaction_chunk(
        storage=storage,
        persisted_session=session,
        working_session=working_session,
        scope=scope,
        compacted_run_ids=rewrite_result.compacted_run_ids,
        sync_remaining_runs=True,
    )
    logger.info(
        "Compaction summary generated",
        session_id=session.session_id,
        scope=scope.key,
        compacted_runs=rewrite_result.compacted_run_count,
        model=_model_identifier(rewrite_result.served_by.model),
    )

    after_visible_runs = scope_visible_runs(session, scope)
    after_tokens = estimate_prompt_visible_history_tokens(
        session=session,
        scope=scope,
        history_settings=history_settings,
        replay_model=replay_model,
    )
    outcome = CompactionOutcome(
        mode="manual" if state.force_compact_before_next_run else "auto",
        session_id=session.session_id,
        scope=scope.key,
        summary=rewrite_result.summary_text,
        summary_model=rewrite_result.served_by.name,
        before_tokens=before_tokens,
        after_tokens=after_tokens,
        window_tokens=replay_window_tokens or 0,
        threshold_tokens=threshold_tokens or 0,
        runs_before=before_run_count,
        runs_after=len(after_visible_runs),
        compacted_run_count=rewrite_result.compacted_run_count,
        compacted_at=compacted_at,
        history_budget_tokens=available_history_budget,
    )
    await _emit_compaction_hook(
        event_name=EVENT_COMPACTION_AFTER,
        scope=scope,
        messages=rewrite_result.compacted_messages,
        session_id=session.session_id,
        token_count_before=before_tokens,
        token_count_after=after_tokens,
        compaction_summary=rewrite_result.summary_text,
    )
    return outcome


@timed("system_prompt_assembly.history_prepare.compaction.rewrite_working_session")
async def _rewrite_working_session_for_compaction(  # noqa: C901
    *,
    storage: BaseDb,
    persisted_session: AgentSession | TeamSession,
    working_session: AgentSession | TeamSession,
    summary_model: SummaryModel,
    session_id: str,
    scope: HistoryScope,
    state: HistoryScopeState,
    history_settings: ResolvedHistorySettings,
    available_history_budget: int | None,
    selected_run_ids: Sequence[str],
    before_tokens: int,
    runs_before: int,
    threshold_tokens: int | None,
    lifecycle_notice_event_id: str | None,
    progress_callback: Callable[[CompactionLifecycleProgress], Awaitable[None]] | None,
    collect_compaction_hook_messages: bool,
    summary_prompt: str,
    summary_timeout_seconds: float,
    fallback_summary_model: SummaryModel | None = None,
    before_persist_callback: Callable[[Sequence[RunOutput | TeamRunOutput]], Awaitable[None]] | None = None,
    replay_model: NativeCompactionModel | None = None,
) -> _CompactionRewriteResult | None:
    final_summary_text = current_summary_text(working_session) or ""
    token_estimator, _estimate_kind = _compaction_sizing(summary_model.model)
    total_compacted_run_count = 0
    all_compacted_run_ids: list[str] = []
    all_compacted_run_id_set: set[str] = set()
    compacted_messages: list[Message] = []
    pending_selected_run_ids = set(selected_run_ids)

    while pending_selected_run_ids:
        working_visible_runs = scope_visible_runs(working_session, scope)
        compactable_runs = [
            run
            for run in working_visible_runs
            if isinstance(run.run_id, str) and run.run_id in pending_selected_run_ids
        ]
        if not compactable_runs:
            break

        summary_input, included_runs = build_summary_input(
            previous_summary=current_summary_text(working_session),
            compacted_runs=compactable_runs,
            history_settings=history_settings,
            max_input_tokens=summary_model.input_budget_tokens,
            token_estimator=token_estimator,
        )
        if not included_runs:
            logger.warning(
                "Compaction skipped because no run fit the single-pass summary budget",
                session_id=session_id,
                scope=scope.key,
                candidate_runs=len(compactable_runs),
                summary_input_budget_tokens=summary_model.input_budget_tokens,
            )
            if total_compacted_run_count == 0:
                return None
            break

        new_summary = await _generate_compaction_summary_with_retry(
            summary_model=summary_model,
            previous_summary=current_summary_text(working_session),
            compactable_runs=compactable_runs,
            initial_summary_input=summary_input,
            initial_included_runs=included_runs,
            session_id=session_id,
            scope=scope,
            history_settings=history_settings,
            summary_prompt=summary_prompt,
            timeout_seconds=summary_timeout_seconds,
            fallback_model=fallback_summary_model,
        )
        if new_summary.served_by.model is not summary_model.model:
            # A fallback serving this chunk owns sizing and identity for later chunks.
            summary_model = new_summary.served_by
            token_estimator, _estimate_kind = _compaction_sizing(summary_model.model)
            fallback_summary_model = None
        included_runs = new_summary.included_runs
        generated_summary = new_summary.summary
        if before_persist_callback is not None:
            await before_persist_callback(included_runs)
        final_summary_text = generated_summary.summary
        compacted_run_ids = tuple(run.run_id for run in included_runs if isinstance(run.run_id, str) and run.run_id)
        compacted_seen_event_ids = sorted(seen_event_ids_for_runs(included_runs))
        working_session.summary = SessionSummary(summary=generated_summary.summary, updated_at=datetime.now(UTC))
        if compacted_seen_event_ids:
            update_scope_seen_event_ids(working_session, scope, compacted_seen_event_ids)
        working_session.runs = remove_runs_by_id(working_session.runs or [], compacted_run_ids)
        total_compacted_run_count += len(included_runs)
        for run_id in compacted_run_ids:
            if run_id not in all_compacted_run_id_set:
                all_compacted_run_id_set.add(run_id)
                all_compacted_run_ids.append(run_id)
        if collect_compaction_hook_messages:
            compacted_messages.extend(messages_for_runs(included_runs, history_settings))
        pending_selected_run_ids.difference_update(compacted_run_ids)

        record_compaction_chunk(
            storage=storage,
            persisted_session=persisted_session,
            working_session=working_session,
            scope=scope,
            compacted_run_ids=compacted_run_ids,
        )

        await _emit_lifecycle_progress_after_persist(
            working_session=working_session,
            scope=scope,
            state=state,
            history_settings=history_settings,
            lifecycle_notice_event_id=lifecycle_notice_event_id,
            progress_callback=progress_callback,
            session_id=session_id,
            summary_model_name=summary_model.name,
            before_tokens=before_tokens,
            available_history_budget=available_history_budget,
            runs_before=runs_before,
            threshold_tokens=threshold_tokens,
            total_compacted_run_count=total_compacted_run_count,
            selected_runs_remaining=len(pending_selected_run_ids),
            replay_model=replay_model,
        )

    if total_compacted_run_count == 0:
        return None
    strip_stale_anthropic_replay_fields(
        [message for run in scope_visible_runs(working_session, scope) for message in run.messages or []],
    )
    return _CompactionRewriteResult(
        summary_text=final_summary_text,
        compacted_run_count=total_compacted_run_count,
        compacted_run_ids=tuple(all_compacted_run_ids),
        compacted_messages=tuple(compacted_messages),
        served_by=summary_model,
    )


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


async def _emit_lifecycle_progress_after_persist(
    *,
    working_session: AgentSession | TeamSession,
    scope: HistoryScope,
    state: HistoryScopeState,
    history_settings: ResolvedHistorySettings,
    lifecycle_notice_event_id: str | None,
    progress_callback: Callable[[CompactionLifecycleProgress], Awaitable[None]] | None,
    session_id: str,
    summary_model_name: str,
    before_tokens: int,
    available_history_budget: int | None,
    runs_before: int,
    threshold_tokens: int | None,
    total_compacted_run_count: int,
    selected_runs_remaining: int,
    replay_model: NativeCompactionModel | None = None,
) -> None:
    """Emit lifecycle progress after a compaction chunk has been durably persisted."""
    remaining_runs = scope_visible_runs(working_session, scope)
    if progress_callback is None or not remaining_runs:
        return
    after_tokens = estimate_prompt_visible_history_tokens(
        session=working_session,
        scope=scope,
        history_settings=history_settings,
        replay_model=replay_model,
    )
    await progress_callback(
        CompactionLifecycleProgress(
            notice_event_id=lifecycle_notice_event_id,
            mode="manual" if state.force_compact_before_next_run else "auto",
            session_id=session_id,
            scope=scope.key,
            summary_model=summary_model_name,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            history_budget_tokens=available_history_budget,
            runs_before=runs_before,
            compacted_run_count=total_compacted_run_count,
            runs_remaining=selected_runs_remaining,
            threshold_tokens=threshold_tokens,
        ),
    )


def _sizing_log_fields(*, kind: CompactionEstimateKind, estimate: int, budget_tokens: int) -> dict[str, object]:
    """Return unit-explicit fields shared by compaction chunk log events."""
    return {
        "summary_input_estimate": estimate,
        "summary_input_estimate_kind": kind,
        "summary_input_budget_tokens": budget_tokens,
    }


async def _generate_compaction_summary_with_retry(  # noqa: PLR0915
    *,
    summary_model: SummaryModel,
    previous_summary: str | None,
    compactable_runs: Sequence[RunOutput | TeamRunOutput],
    initial_summary_input: str,
    initial_included_runs: list[RunOutput | TeamRunOutput],
    session_id: str,
    scope: HistoryScope,
    history_settings: ResolvedHistorySettings,
    summary_prompt: str,
    timeout_seconds: float,
    fallback_model: SummaryModel | None = None,
) -> _GeneratedSummaryChunk:
    """Generate one summary chunk, retrying the same or smaller input when safe.

    A safeguard refusal from the primary model switches once to
    ``fallback_model``. The input remains unchanged when it fits the fallback
    model, otherwise it is rebuilt under that model's own budget. A refusal or
    failure from the fallback propagates. The switch shares the retry policy's
    attempt bound, so a
    refusal after an earlier shrink or transient retry propagates without a
    fallback call. All other failures keep the existing shrink and transient
    same-input retry behavior.
    """
    summary_input = initial_summary_input
    included_runs = initial_included_runs
    budget = summary_model.input_budget_tokens
    token_estimator, estimate_kind = _compaction_sizing(summary_model.model)
    retry_policy = DEFAULT_SUMMARY_RETRY_POLICY
    minimum_progress_input_tokens = minimum_summary_input_tokens(
        previous_summary=previous_summary,
        first_run=compactable_runs[0],
        token_estimator=token_estimator,
    )
    attempt = 1
    while True:
        summary_input_estimate = token_estimator(summary_input)
        effective_timeout_seconds = effective_summary_timeout_seconds(
            summary_model.model,
            timeout_seconds=timeout_seconds,
        )
        started = asyncio.get_running_loop().time()
        logger.info(
            "Compaction summary chunk request",
            session_id=session_id,
            scope=scope.key,
            model_name=summary_model.name,
            attempt=attempt,
            candidate_runs=len(compactable_runs),
            included_runs=len(included_runs),
            **_sizing_log_fields(kind=estimate_kind, estimate=summary_input_estimate, budget_tokens=budget),
            timeout_seconds=timeout_seconds,
            effective_timeout_seconds=effective_timeout_seconds,
        )
        try:
            summary = await generate_compaction_summary(
                model=summary_model.model,
                summary_input=summary_input,
                summary_prompt=summary_prompt,
                timeout_seconds=timeout_seconds,
            )
        except Exception as exc:
            duration_ms = int((asyncio.get_running_loop().time() - started) * 1000)
            logger.warning(
                "Compaction summary chunk failed",
                session_id=session_id,
                scope=scope.key,
                model_name=summary_model.name,
                attempt=attempt,
                candidate_runs=len(compactable_runs),
                included_runs=len(included_runs),
                **_sizing_log_fields(kind=estimate_kind, estimate=summary_input_estimate, budget_tokens=budget),
                timeout_seconds=timeout_seconds,
                effective_timeout_seconds=effective_timeout_seconds,
                duration_ms=duration_ms,
                error=str(exc) or type(exc).__name__,
            )
            # The attempt bound covers the fallback call too: a refusal after an
            # earlier shrink or transient retry propagates instead of issuing a
            # third provider call.
            if fallback_model is not None and attempt < retry_policy.max_attempts and is_model_safeguard_refusal(exc):
                fallback_token_estimator, fallback_estimate_kind = _compaction_sizing(fallback_model.model)
                if fallback_token_estimator(summary_input) <= fallback_model.input_budget_tokens:
                    rebuilt_input, rebuilt_runs = summary_input, included_runs
                else:
                    rebuilt_input, rebuilt_runs = build_summary_input(
                        previous_summary=previous_summary,
                        compacted_runs=compactable_runs,
                        history_settings=history_settings,
                        max_input_tokens=fallback_model.input_budget_tokens,
                        token_estimator=fallback_token_estimator,
                    )
                if not rebuilt_runs:
                    raise
                logger.info(
                    "Compaction summary refused; switching to fallback model",
                    session_id=session_id,
                    scope=scope.key,
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
                rebuilt_input, rebuilt_runs = build_summary_input(
                    previous_summary=previous_summary,
                    compacted_runs=compactable_runs,
                    history_settings=history_settings,
                    max_input_tokens=retry_decision.budget,
                    token_estimator=token_estimator,
                )
                if rebuilt_runs:
                    rebuilt_input_tokens = token_estimator(rebuilt_input)
                    if retry_decision.kind == "shrink" and rebuilt_input_tokens >= summary_input_estimate:
                        raise
                    summary_input = rebuilt_input
                    included_runs = rebuilt_runs
                    budget = retry_decision.budget
                    attempt += 1
                    continue
            raise
        duration_ms = int((asyncio.get_running_loop().time() - started) * 1000)
        logger.info(
            "Compaction summary chunk completed",
            session_id=session_id,
            scope=scope.key,
            model_name=summary_model.name,
            attempt=attempt,
            candidate_runs=len(compactable_runs),
            included_runs=len(included_runs),
            **_sizing_log_fields(kind=estimate_kind, estimate=summary_input_estimate, budget_tokens=budget),
            timeout_seconds=timeout_seconds,
            effective_timeout_seconds=effective_timeout_seconds,
            duration_ms=duration_ms,
        )
        return _GeneratedSummaryChunk(
            summary=summary,
            included_runs=included_runs,
            served_by=replace(summary_model, input_budget_tokens=budget),
        )


def _select_compaction_candidates(
    *,
    visible_runs: list[RunOutput | TeamRunOutput],
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    state: HistoryScopeState,
    history_settings: ResolvedHistorySettings,
    available_history_budget: int | None,
    replay_model: NativeCompactionModel | None = None,
) -> list[RunOutput | TeamRunOutput]:
    if not visible_runs:
        return []
    if state.force_compact_before_next_run:
        return visible_runs
    if available_history_budget is None:
        return []
    current_tokens = estimate_prompt_visible_history_tokens(
        session=session,
        scope=scope,
        history_settings=history_settings,
        replay_model=replay_model,
    )
    return visible_runs if current_tokens > available_history_budget else []


def _stable_compaction_run_ids(
    runs: Sequence[RunOutput | TeamRunOutput],
    *,
    session_id: str,
    scope: HistoryScope,
) -> tuple[str, ...]:
    unremovable_run_count = sum(1 for run in runs if not _has_stable_run_id(run))
    if unremovable_run_count:
        logger.warning(
            "Compaction skipped runs without stable run IDs",
            session_id=session_id,
            scope=scope.key,
            skipped_runs=unremovable_run_count,
        )
    return tuple(run.run_id for run in runs if isinstance(run.run_id, str) and run.run_id)


def _has_stable_run_id(run: RunOutput | TeamRunOutput) -> bool:
    return isinstance(run.run_id, str) and bool(run.run_id)


def _model_identifier(model: Model) -> str:
    return model.id or model.__class__.__name__


def _iso_utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
