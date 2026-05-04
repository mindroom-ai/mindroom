"""Scoped compaction."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from agno.db.base import SessionType
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.team import TeamRunOutput
from agno.session.summary import SessionSummary
from agno.session.team import TeamSession

from mindroom.cancellation import request_task_cancel
from mindroom.constants import MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS
from mindroom.history.compaction_provider_request import CompactionProviderRequest, CompactionProviderRequestBuilder
from mindroom.history.storage import (
    metadata_with_merged_seen_event_ids,
    read_scope_state,
    seen_event_ids_for_runs,
    update_scope_seen_event_ids,
    write_scope_state,
)
from mindroom.history.types import (
    CompactionLifecycleProgress,
    CompactionOutcome,
    HistoryPolicy,
    HistoryScope,
    HistoryScopeState,
    ResolvedHistorySettings,
)
from mindroom.hooks import EVENT_COMPACTION_AFTER, EVENT_COMPACTION_BEFORE, CompactionHookContext, emit
from mindroom.logging_config import get_logger
from mindroom.metadata_merge import deep_merge_metadata
from mindroom.prepared_conversation_chain import (
    CompactionSummaryRequest,
    build_compaction_summary_request,
    build_persisted_run_chain,
    estimate_history_messages_tokens,
    history_messages_for_session,
    plain_compaction_summary_messages,
    strip_stale_anthropic_replay_fields,
)
from mindroom.timing import timed
from mindroom.token_budget import estimate_text_tokens
from mindroom.tool_system.runtime_context import get_tool_runtime_context, resolve_tool_runtime_hook_bindings

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from agno.db.base import BaseDb
    from agno.models.base import Model
    from agno.models.message import Message
    from agno.models.response import ModelResponse
    from agno.session.agent import AgentSession
    from agno.tools.function import Function

    from mindroom.config.main import Config
    from mindroom.config.models import CompactionConfig
logger = get_logger(__name__)

_COMPACTION_CANCEL_DRAIN_TIMEOUT_SECONDS = 1.0


class _CompactionProviderTimeoutError(Exception):
    """Internal wrapper so provider TimeoutError does not look like our wait_for timeout."""

    def __init__(self, original: TimeoutError) -> None:
        super().__init__(str(original))
        self.original = original


def _consume_detached_compaction_request_result(
    response_task: asyncio.Task[ModelResponse],
    *,
    log_message: str,
) -> None:
    """Consume a detached request result so late failures do not surface unhandled."""
    try:
        response_task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.warning(log_message, exc_info=True)


def _warn_if_detached_compaction_request_still_running(
    response_task: asyncio.Task[ModelResponse],
    *,
    reason: str,
) -> None:
    """Log when a detached provider request ignored cancellation past the grace window."""
    if response_task.done():
        return
    logger.warning(
        "Compaction request still running after cancellation grace period",
        reason=reason,
        timeout_seconds=_COMPACTION_CANCEL_DRAIN_TIMEOUT_SECONDS,
    )


def _detach_cancelled_compaction_request(
    response_task: asyncio.Task[ModelResponse],
    *,
    reason: str,
) -> None:
    """Detach one cancelled provider request without blocking the caller or leaking cleanup tasks."""
    response_task.add_done_callback(
        partial(
            _consume_detached_compaction_request_result,
            log_message="Detached compaction request raised after caller moved on",
        ),
    )
    asyncio.get_running_loop().call_later(
        _COMPACTION_CANCEL_DRAIN_TIMEOUT_SECONDS,
        partial(
            _warn_if_detached_compaction_request_still_running,
            response_task,
            reason=reason,
        ),
    )


@dataclass(frozen=True)
class ResolvedCompactionRuntime:
    """Resolved model/window inputs needed for one compaction attempt."""

    model_name: str
    context_window: int | None


@dataclass(frozen=True)
class _CompactionRewriteResult:
    summary_text: str
    compacted_run_count: int
    compacted_run_ids: tuple[str, ...]
    compacted_messages: tuple[Message, ...]


@dataclass(frozen=True)
class _GeneratedSummaryChunk:
    summary: SessionSummary
    included_runs: list[RunOutput | TeamRunOutput]


def _persist_cleared_force_state_if_needed(
    *,
    storage: BaseDb,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    state: HistoryScopeState,
) -> HistoryScopeState:
    cleared_state = replace(state, force_compact_before_next_run=False)
    if cleared_state == state:
        return cleared_state
    session_type = SessionType.TEAM if isinstance(session, TeamSession) else SessionType.AGENT
    latest_session = storage.get_session(session_id=session.session_id, session_type=session_type)
    target_session = latest_session if isinstance(latest_session, type(session)) else session
    latest_state = read_scope_state(target_session, scope)
    if latest_state != state:
        session.metadata = target_session.metadata
        session.runs = target_session.runs
        session.summary = target_session.summary
        return latest_state
    write_scope_state(target_session, scope, cleared_state)
    storage.upsert_session(target_session)
    session.metadata = target_session.metadata
    session.runs = target_session.runs
    session.summary = target_session.summary
    return cleared_state


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
    summary_input_budget: int,
    compaction_context_window: int | None,
    summary_model: Model,
    summary_model_name: str,
    active_context_window: int | None,
    replay_window_tokens: int | None,
    threshold_tokens: int | None,
    reserve_tokens: int,
    timing_scope: str | None = None,
    lifecycle_notice_event_id: str | None = None,
    progress_callback: Callable[[CompactionLifecycleProgress], Awaitable[None]] | None = None,
    provider_request_builder: CompactionProviderRequestBuilder | None = None,
) -> tuple[HistoryScopeState, CompactionOutcome | None]:
    """Compact one scope by rewriting session.summary and session.runs."""
    visible_runs = runs_for_scope(completed_top_level_runs(session), scope)
    compactable_runs = _select_runs_to_compact(
        visible_runs=visible_runs,
        session=session,
        scope=scope,
        state=state,
        history_settings=history_settings,
        available_history_budget=available_history_budget,
    )
    if not compactable_runs:
        cleared_state = _persist_cleared_force_state_if_needed(
            storage=storage,
            session=session,
            scope=scope,
            state=state,
        )
        return cleared_state, None

    before_tokens = estimate_prompt_visible_history_tokens(
        session=session,
        scope=scope,
        history_settings=history_settings,
    )
    before_run_count = len(visible_runs)
    working_session = deepcopy(session)
    collect_compaction_hook_messages = _should_collect_compaction_hook_messages()

    async def emit_before_persist(included_runs: Sequence[RunOutput | TeamRunOutput]) -> None:
        await _emit_compaction_hook(
            event_name=EVENT_COMPACTION_BEFORE,
            scope=scope,
            messages=(
                build_persisted_run_chain(included_runs, history_settings=history_settings).messages
                if collect_compaction_hook_messages
                else ()
            ),
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
        summary_model_name=summary_model_name,
        session_id=session.session_id,
        scope=scope,
        state=state,
        history_settings=history_settings,
        available_history_budget=available_history_budget,
        summary_input_budget=summary_input_budget,
        compaction_context_window=compaction_context_window,
        before_tokens=before_tokens,
        runs_before=before_run_count,
        threshold_tokens=threshold_tokens,
        lifecycle_notice_event_id=lifecycle_notice_event_id,
        progress_callback=progress_callback,
        collect_compaction_hook_messages=collect_compaction_hook_messages,
        before_persist_callback=emit_before_persist,
        timing_scope=timing_scope,
        provider_request_builder=provider_request_builder,
    )
    if rewrite_result is None:
        cleared_state = _persist_cleared_force_state_if_needed(
            storage=storage,
            session=session,
            scope=scope,
            state=state,
        )
        return cleared_state, None

    compacted_at = _iso_utc_now()
    new_state = HistoryScopeState(
        last_compacted_at=compacted_at,
        last_summary_model=_model_identifier(summary_model),
        last_compacted_run_count=rewrite_result.compacted_run_count,
        force_compact_before_next_run=False,
    )
    write_scope_state(session, scope, new_state)
    write_scope_state(working_session, scope, new_state)
    _persist_compaction_progress(
        storage=storage,
        persisted_session=session,
        working_session=working_session,
        compacted_run_ids=set(rewrite_result.compacted_run_ids),
        sync_remaining_runs=True,
    )
    logger.info(
        "Compaction summary generated",
        session_id=session.session_id,
        scope=scope.key,
        compacted_runs=rewrite_result.compacted_run_count,
        model=_model_identifier(summary_model),
    )

    after_visible_runs = runs_for_scope(completed_top_level_runs(session), scope)
    after_tokens = estimate_prompt_visible_history_tokens(
        session=session,
        scope=scope,
        history_settings=history_settings,
    )
    resolved_window_tokens = replay_window_tokens or active_context_window or 0
    outcome = CompactionOutcome(
        mode="manual" if state.force_compact_before_next_run else "auto",
        session_id=session.session_id,
        scope=scope.key,
        summary=rewrite_result.summary_text,
        summary_model=summary_model_name,
        before_tokens=before_tokens,
        after_tokens=after_tokens,
        window_tokens=resolved_window_tokens,
        threshold_tokens=threshold_tokens or 0,
        reserve_tokens=reserve_tokens,
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
    return new_state, outcome


@timed("system_prompt_assembly.history_prepare.compaction.rewrite_working_session")
async def _rewrite_working_session_for_compaction(  # noqa: C901, PLR0912, PLR0915
    *,
    storage: BaseDb,
    persisted_session: AgentSession | TeamSession,
    working_session: AgentSession | TeamSession,
    summary_model: Model,
    summary_model_name: str,
    session_id: str,
    scope: HistoryScope,
    state: HistoryScopeState,
    history_settings: ResolvedHistorySettings,
    available_history_budget: int | None,
    summary_input_budget: int,
    compaction_context_window: int | None,
    before_tokens: int,
    runs_before: int,
    threshold_tokens: int | None,
    lifecycle_notice_event_id: str | None,
    progress_callback: Callable[[CompactionLifecycleProgress], Awaitable[None]] | None,
    collect_compaction_hook_messages: bool,
    before_persist_callback: Callable[[Sequence[RunOutput | TeamRunOutput]], Awaitable[None]] | None = None,
    timing_scope: str | None = None,
    provider_request_builder: CompactionProviderRequestBuilder | None = None,
) -> _CompactionRewriteResult | None:
    final_summary_text = _current_summary_text(working_session) or ""
    total_compacted_run_count = 0
    all_compacted_run_ids: set[str] = set()
    compacted_messages: list[Message] = []
    pending_selected_run_ids: set[str] | None = None
    per_call_summary_input_budget = effective_summary_input_budget_tokens(
        summary_input_budget,
        compaction_context_window,
    )

    while True:
        working_visible_runs = runs_for_scope(completed_top_level_runs(working_session), scope)
        if pending_selected_run_ids:
            # Once a pass selects "all visible runs", keep compacting that original
            # set even if the remaining raw history now fits the replay budget.
            compactable_runs = [
                run
                for run in working_visible_runs
                if isinstance(run.run_id, str) and run.run_id in pending_selected_run_ids
            ]
        else:
            selection_state = (
                state if total_compacted_run_count == 0 else replace(state, force_compact_before_next_run=False)
            )
            compactable_runs = _select_runs_to_compact(
                visible_runs=working_visible_runs,
                session=working_session,
                scope=scope,
                state=selection_state,
                history_settings=history_settings,
                available_history_budget=available_history_budget,
            )
            unremovable_run_count = sum(1 for run in compactable_runs if not _has_stable_run_id(run))
            if unremovable_run_count:
                logger.warning(
                    "Compaction skipped runs without stable run IDs",
                    session_id=session_id,
                    scope=scope.key,
                    skipped_runs=unremovable_run_count,
                )
                compactable_runs = [run for run in compactable_runs if _has_stable_run_id(run)]
            selected_run_ids = {run.run_id for run in compactable_runs if isinstance(run.run_id, str) and run.run_id}
            if (
                selection_state.force_compact_before_next_run
                and compactable_runs
                and len(selected_run_ids) == len(compactable_runs)
            ):
                pending_selected_run_ids = selected_run_ids
        if not compactable_runs:
            break

        summary_request, included_runs = _build_summary_request(
            previous_summary=_current_summary_text(working_session),
            compacted_runs=compactable_runs,
            history_settings=history_settings,
            max_input_tokens=per_call_summary_input_budget,
        )
        if summary_request is None or not included_runs:
            logger.warning(
                "Compaction skipped because no run fit the single-pass summary budget",
                session_id=session_id,
                scope=scope.key,
                candidate_runs=len(compactable_runs),
                summary_input_budget=per_call_summary_input_budget,
            )
            if total_compacted_run_count == 0:
                return None
            break

        new_summary = await _generate_compaction_summary_with_retry(
            model=summary_model,
            previous_summary=_current_summary_text(working_session),
            compactable_runs=compactable_runs,
            initial_summary_request=summary_request,
            initial_included_runs=included_runs,
            summary_input_budget=per_call_summary_input_budget,
            session_id=session_id,
            scope=scope,
            history_settings=history_settings,
            timing_scope=timing_scope,
            session=working_session,
            provider_request_builder=provider_request_builder,
        )
        included_runs = new_summary.included_runs
        generated_summary = new_summary.summary
        if before_persist_callback is not None:
            await before_persist_callback(included_runs)
        final_summary_text = generated_summary.summary
        compacted_run_ids = {run.run_id for run in included_runs if isinstance(run.run_id, str) and run.run_id}
        compacted_seen_event_ids = sorted(seen_event_ids_for_runs(included_runs))
        working_session.summary = SessionSummary(summary=generated_summary.summary, updated_at=datetime.now(UTC))
        if compacted_seen_event_ids:
            update_scope_seen_event_ids(working_session, scope, compacted_seen_event_ids)
        working_session.runs = _remove_runs_by_id(working_session.runs or [], compacted_run_ids)
        total_compacted_run_count += len(included_runs)
        all_compacted_run_ids.update(compacted_run_ids)
        if collect_compaction_hook_messages:
            compacted_messages.extend(
                build_persisted_run_chain(included_runs, history_settings=history_settings).messages,
            )
        if pending_selected_run_ids is not None:
            pending_selected_run_ids.difference_update(compacted_run_ids)

        _persist_compaction_progress(
            storage=storage,
            persisted_session=persisted_session,
            working_session=working_session,
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
            summary_model_name=summary_model_name,
            before_tokens=before_tokens,
            available_history_budget=available_history_budget,
            runs_before=runs_before,
            threshold_tokens=threshold_tokens,
            total_compacted_run_count=total_compacted_run_count,
        )

        if pending_selected_run_ids:
            continue

        if available_history_budget is None:
            break

        after_tokens = estimate_prompt_visible_history_tokens(
            session=working_session,
            scope=scope,
            history_settings=history_settings,
        )
        if after_tokens <= available_history_budget:
            break

    if total_compacted_run_count == 0:
        return None
    for run in runs_for_scope(completed_top_level_runs(working_session), scope):
        strip_stale_anthropic_replay_fields(run.messages or [])
    return _CompactionRewriteResult(
        summary_text=final_summary_text,
        compacted_run_count=total_compacted_run_count,
        compacted_run_ids=tuple(all_compacted_run_ids),
        compacted_messages=tuple(compacted_messages),
    )


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
) -> None:
    """Emit lifecycle progress after a compaction chunk has been durably persisted."""
    remaining_runs = runs_for_scope(completed_top_level_runs(working_session), scope)
    if progress_callback is None or not remaining_runs:
        return
    after_tokens = estimate_prompt_visible_history_tokens(
        session=working_session,
        scope=scope,
        history_settings=history_settings,
    )
    runs_remaining = len(remaining_runs)
    if (
        not state.force_compact_before_next_run
        and available_history_budget is not None
        and after_tokens <= available_history_budget
    ):
        runs_remaining = 0
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
            runs_remaining=runs_remaining,
            threshold_tokens=threshold_tokens,
        ),
    )


def _persist_compaction_progress(
    *,
    storage: BaseDb,
    persisted_session: AgentSession | TeamSession,
    working_session: AgentSession | TeamSession,
    compacted_run_ids: set[str],
    sync_remaining_runs: bool = False,
) -> None:
    """Save one successful compaction chunk before attempting the next chunk."""
    session_type = SessionType.TEAM if isinstance(persisted_session, TeamSession) else SessionType.AGENT
    latest_session = storage.get_session(session_id=persisted_session.session_id, session_type=session_type)
    target_session = latest_session if isinstance(latest_session, type(persisted_session)) else persisted_session
    target_session.summary = working_session.summary
    target_session.metadata = metadata_with_merged_seen_event_ids(
        deep_merge_metadata(target_session.metadata, working_session.metadata),
        target_session.metadata,
        working_session.metadata,
    )
    target_session.runs = _remove_runs_by_id(target_session.runs or [], compacted_run_ids)
    if sync_remaining_runs:
        target_session.runs = _sync_remaining_runs_from_working(
            target_session.runs or [],
            working_session.runs or [],
        )
    storage.upsert_session(target_session)
    persisted_session.summary = target_session.summary
    persisted_session.runs = target_session.runs
    persisted_session.metadata = target_session.metadata


def _sync_remaining_runs_from_working(
    target_runs: list[RunOutput | TeamRunOutput],
    working_runs: list[RunOutput | TeamRunOutput],
) -> list[RunOutput | TeamRunOutput]:
    working_by_id = {run.run_id: run for run in working_runs if isinstance(run.run_id, str) and run.run_id}
    synced_runs: list[RunOutput | TeamRunOutput] = []
    for run in target_runs:
        run_id = run.run_id
        if isinstance(run_id, str) and run_id in working_by_id:
            synced_runs.append(deepcopy(working_by_id[run_id]))
        else:
            synced_runs.append(run)
    return synced_runs


def resolve_effective_compaction_threshold(compaction_config: CompactionConfig, context_window: int) -> int:
    """Resolve the soft replay trigger budget in tokens."""
    threshold_tokens = compaction_config.threshold_tokens
    if threshold_tokens is not None:
        return threshold_tokens
    threshold_percent = compaction_config.threshold_percent
    if threshold_percent is not None:
        return int(context_window * threshold_percent)
    return int(context_window * 0.8)


def normalize_compaction_budget_tokens(tokens: int, context_window: int | None) -> int:
    """Clamp one compaction knob against half of the available model window."""
    if context_window is None or context_window <= 0:
        return tokens
    return min(tokens, context_window // 2)


def effective_summary_input_budget_tokens(summary_input_budget: int, compaction_context_window: int | None) -> int:
    """Return the conservative per-call summary input budget."""
    if compaction_context_window is None or compaction_context_window <= 0:
        return summary_input_budget
    per_call_cap = max(2_000, min(compaction_context_window // 4, 32_000))
    return min(summary_input_budget, per_call_cap)


def resolve_compaction_runtime_settings(
    *,
    config: Config,
    compaction_config: CompactionConfig,
    active_model_name: str,
    active_context_window: int | None,
) -> ResolvedCompactionRuntime:
    """Resolve the effective compaction model name and usable window for one run."""
    model_name = compaction_config.model or active_model_name
    model_context_window = config.get_model_context_window(model_name)
    if compaction_config.model is not None:
        return ResolvedCompactionRuntime(
            model_name=model_name,
            context_window=model_context_window,
        )
    return ResolvedCompactionRuntime(
        model_name=model_name,
        context_window=model_context_window or active_context_window,
    )


async def _generate_compaction_summary_with_retry(
    *,
    model: Model,
    session: AgentSession | TeamSession,
    previous_summary: str | None,
    compactable_runs: Sequence[RunOutput | TeamRunOutput],
    initial_summary_request: CompactionSummaryRequest,
    initial_included_runs: list[RunOutput | TeamRunOutput],
    summary_input_budget: int,
    session_id: str,
    scope: HistoryScope,
    history_settings: ResolvedHistorySettings,
    timing_scope: str | None = None,
    provider_request_builder: CompactionProviderRequestBuilder | None = None,
) -> _GeneratedSummaryChunk:
    """Generate one summary chunk, retrying once with a smaller input when safe."""
    summary_request = initial_summary_request
    included_runs = initial_included_runs
    budget = summary_input_budget
    last_error: Exception | None = None
    for attempt in (1, 2):
        estimated_input_tokens = summary_request.estimated_tokens
        started = asyncio.get_running_loop().time()
        logger.info(
            "Compaction summary chunk request",
            session_id=session_id,
            scope=scope.key,
            attempt=attempt,
            candidate_runs=len(compactable_runs),
            included_runs=len(included_runs),
            estimated_input_tokens=estimated_input_tokens,
            summary_input_budget=budget,
            timeout_seconds=MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS,
        )
        try:
            provider_request = await _build_compaction_provider_request(
                summary_request=summary_request,
                session=session,
                provider_request_builder=provider_request_builder,
            )
            summary = await _generate_compaction_summary(
                model=model,
                messages=list(provider_request.messages),
                tools=list(provider_request.tools),
                tool_choice=provider_request.tool_choice,
                timeout_seconds=MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS,
                timing_scope=timing_scope,
            )
        except Exception as exc:
            duration_ms = int((asyncio.get_running_loop().time() - started) * 1000)
            logger.warning(
                "Compaction summary chunk failed",
                session_id=session_id,
                scope=scope.key,
                attempt=attempt,
                candidate_runs=len(compactable_runs),
                included_runs=len(included_runs),
                estimated_input_tokens=estimated_input_tokens,
                summary_input_budget=budget,
                timeout_seconds=MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS,
                duration_ms=duration_ms,
                error=str(exc) or type(exc).__name__,
            )
            last_error = exc
            retry_budget = max(1_000, budget // 2)
            if attempt == 1 and retry_budget < budget and _should_retry_smaller_summary_chunk(exc):
                rebuilt_request, rebuilt_runs = _build_summary_request(
                    previous_summary=previous_summary,
                    compacted_runs=compactable_runs,
                    history_settings=history_settings,
                    max_input_tokens=retry_budget,
                )
                if rebuilt_request is not None and rebuilt_runs:
                    summary_request = rebuilt_request
                    included_runs = rebuilt_runs
                    budget = retry_budget
                    continue
            raise
        duration_ms = int((asyncio.get_running_loop().time() - started) * 1000)
        logger.info(
            "Compaction summary chunk completed",
            session_id=session_id,
            scope=scope.key,
            attempt=attempt,
            candidate_runs=len(compactable_runs),
            included_runs=len(included_runs),
            estimated_input_tokens=estimated_input_tokens,
            summary_input_budget=budget,
            timeout_seconds=MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS,
            duration_ms=duration_ms,
        )
        return _GeneratedSummaryChunk(summary=summary, included_runs=included_runs)
    assert last_error is not None
    raise last_error


def _should_retry_smaller_summary_chunk(error: Exception) -> bool:
    """Return whether a smaller compaction chunk may resolve the provider failure."""
    if isinstance(error, TimeoutError):
        return True
    message = str(error).lower()
    retry_fragments = (
        "timed out",
        "context length",
        "context_length_exceeded",
        "too many tokens",
        "max tokens",
        "too large",
        "too long",
        "input size",
        "input too large",
        "maximum length",
        "max length",
        "request too large",
        "reduce the length",
    )
    return any(fragment in message for fragment in retry_fragments)


async def _build_compaction_provider_request(
    *,
    summary_request: CompactionSummaryRequest,
    session: AgentSession | TeamSession,
    provider_request_builder: CompactionProviderRequestBuilder | None,
) -> CompactionProviderRequest:
    if provider_request_builder is None:
        return CompactionProviderRequest(
            messages=plain_compaction_summary_messages(summary_request.messages),
        )
    return await provider_request_builder(summary_request, session)


@timed("system_prompt_assembly.history_prepare.compaction.summary_model_request")
async def _generate_compaction_summary(
    *,
    model: Model,
    messages: Sequence[Message],
    tools: Sequence[dict] | None = None,
    tool_choice: str | dict[str, object] | None = None,
    timeout_seconds: float | None = None,
    timing_scope: str | None = None,
) -> SessionSummary:
    del timing_scope
    resolved_timeout = MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    request_messages = [message.model_copy(deep=True) for message in messages]

    async def _request_summary() -> ModelResponse:
        try:
            request_tools: list[dict] | None = list(tools) if tools else None
            return await model.aresponse(
                messages=request_messages,
                tools=cast("list[Function | dict] | None", request_tools),
                tool_choice=tool_choice,
            )
        except TimeoutError as exc:
            raise _CompactionProviderTimeoutError(exc) from exc

    response_task = asyncio.create_task(
        _request_summary(),
        name="compaction_summary_request",
    )
    try:
        done, _pending = await asyncio.wait(
            {response_task},
            timeout=resolved_timeout,
        )
    except asyncio.CancelledError:
        request_task_cancel(response_task)
        _detach_cancelled_compaction_request(
            response_task,
            reason="outer_cancellation",
        )
        raise

    if response_task not in done:
        request_task_cancel(response_task)
        _detach_cancelled_compaction_request(
            response_task,
            reason="timeout",
        )
        msg = f"compaction summary timed out after {resolved_timeout}s"
        raise RuntimeError(msg)

    try:
        response = response_task.result()
    except _CompactionProviderTimeoutError as exc:
        raise exc.original from exc
    raw_text = response.content if isinstance(response.content, str) else ""
    normalized_text = _normalize_compaction_summary_text(raw_text)
    if not normalized_text:
        msg = "summary generation returned no result"
        raise RuntimeError(msg)
    return SessionSummary(summary=normalized_text, updated_at=datetime.now(UTC))


def _normalize_compaction_summary_text(raw_text: str) -> str:
    normalized = raw_text.strip()
    if not normalized:
        return ""
    if normalized.startswith("```") and normalized.endswith("```"):
        first_newline = normalized.find("\n")
        if first_newline != -1:
            normalized = normalized[first_newline + 1 : -3].strip()
    return normalized


@timed("system_prompt_assembly.history_prepare.compaction.summary_request_build")
def _build_summary_request(
    *,
    previous_summary: str | None,
    compacted_runs: Sequence[RunOutput | TeamRunOutput],
    max_input_tokens: int,
    history_settings: ResolvedHistorySettings | None = None,
) -> tuple[CompactionSummaryRequest | None, list[RunOutput | TeamRunOutput]]:
    """Build the chain-shaped compaction summary request for one chunk."""
    resolved_history_settings = history_settings or _default_compaction_history_settings()
    return build_compaction_summary_request(
        previous_summary=previous_summary,
        compacted_runs=compacted_runs,
        history_settings=resolved_history_settings,
        max_input_tokens=max_input_tokens,
    )


def _default_compaction_history_settings() -> ResolvedHistorySettings:
    return ResolvedHistorySettings(
        policy=HistoryPolicy(mode="all"),
        max_tool_calls_from_history=None,
    )


def estimate_prompt_visible_history_tokens(
    *,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    history_settings: ResolvedHistorySettings,
) -> int:
    """Estimate the durable summary plus visible persisted history for one run."""
    summary_tokens = estimate_session_summary_tokens(_current_summary_text(session))
    history_messages = history_messages_for_session(
        session=session,
        scope=scope,
        history_settings=history_settings,
    )
    return summary_tokens + estimate_history_messages_tokens(history_messages)


def estimate_session_summary_tokens(summary_text: str | None) -> int:
    """Estimate prompt-visible tokens contributed by one stored session summary."""
    if summary_text is None:
        return 0
    normalized_summary = summary_text.strip()
    if not normalized_summary:
        return 0
    wrapper = (
        "Here is a brief summary of your previous interactions:\n\n"
        "<summary_of_previous_interactions>\n"
        f"{normalized_summary}\n"
        "</summary_of_previous_interactions>\n\n"
        "Note: this information is from previous interactions and may be outdated. "
        "You should ALWAYS prefer information from this conversation over the past summary.\n\n"
    )
    return estimate_text_tokens(wrapper)


def _select_runs_to_compact(
    *,
    visible_runs: list[RunOutput | TeamRunOutput],
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    state: HistoryScopeState,
    history_settings: ResolvedHistorySettings,
    available_history_budget: int | None,
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
    )
    return visible_runs if current_tokens > available_history_budget else []


def completed_top_level_runs(session: AgentSession | TeamSession) -> list[RunOutput | TeamRunOutput]:
    """Return completed top-level runs that can contribute to persisted replay."""
    skip_statuses = {RunStatus.paused, RunStatus.cancelled, RunStatus.error}
    return [
        run
        for run in session.runs or []
        if isinstance(run, (RunOutput, TeamRunOutput)) and run.parent_run_id is None and run.status not in skip_statuses
    ]


def runs_for_scope(
    runs: Sequence[RunOutput | TeamRunOutput],
    scope: HistoryScope,
) -> list[RunOutput | TeamRunOutput]:
    """Filter completed top-level runs down to one persisted history scope."""
    if scope.kind == "team":
        return [run for run in runs if isinstance(run, TeamRunOutput) and run.team_id == scope.scope_id]
    return [run for run in runs if isinstance(run, RunOutput) and run.agent_id == scope.scope_id]


def _current_summary_text(session: AgentSession | TeamSession) -> str | None:
    if session.summary is None:
        return None
    return session.summary.summary.strip() or None


def _has_stable_run_id(run: RunOutput | TeamRunOutput) -> bool:
    return isinstance(run.run_id, str) and bool(run.run_id)


def _remove_runs_by_id(
    runs: Sequence[RunOutput | TeamRunOutput],
    compacted_run_ids: set[str],
) -> list[RunOutput | TeamRunOutput]:
    if not compacted_run_ids:
        return list(runs)

    remove_ids = set(compacted_run_ids)
    changed = True
    while changed:
        changed = False
        for run in runs:
            parent_run_id = run.parent_run_id
            run_id = run.run_id
            if not isinstance(parent_run_id, str) or not isinstance(run_id, str):
                continue
            if parent_run_id in remove_ids and run_id not in remove_ids:
                remove_ids.add(run_id)
                changed = True

    return [
        run
        for run in runs
        if not (
            (isinstance(run.run_id, str) and run.run_id in remove_ids)
            or (isinstance(run.parent_run_id, str) and run.parent_run_id in remove_ids)
        )
    ]


def _model_identifier(model: Model) -> str:
    return model.id or model.__class__.__name__


def _iso_utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
