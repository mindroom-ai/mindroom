"""Scoped compaction."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from html import escape
from typing import TYPE_CHECKING, TypeGuard, cast
from uuid import uuid4

from agno.agent._tools import determine_tools_for_model
from agno.db.base import SessionType
from agno.models.message import Message
from agno.run import RunContext
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.summary import SessionSummary
from agno.session.team import TeamSession
from agno.team._tools import _determine_tools_for_model as determine_team_tools_for_model
from agno.tools import Toolkit
from agno.tools.function import Function
from agno.utils.message import filter_tool_calls
from pydantic import BaseModel

from mindroom.cancellation import request_task_cancel
from mindroom.constants import MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS
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
from mindroom.timing import timed
from mindroom.token_budget import estimate_text_tokens, stable_serialize
from mindroom.tool_system.runtime_context import get_tool_runtime_context, resolve_tool_runtime_hook_bindings

if TYPE_CHECKING:
    from agno.agent import Agent
    from agno.db.base import BaseDb
    from agno.models.base import Model
    from agno.models.response import ModelResponse
    from agno.team import Team

    from mindroom.config.main import Config
    from mindroom.config.models import CompactionConfig
logger = get_logger(__name__)

_WRAPPER_OVERHEAD_TOKENS = 200
_OVERSIZED_RUN_NOTE = "Run truncated to fit compaction budget."
_EXCERPT_METADATA_OMIT_KEYS = frozenset(
    {
        "model_params",
        "tools_schema",
    },
)
_STANDARD_HISTORY_ROLES = frozenset({"user", "assistant", "tool"})
_COMPACTION_CANCEL_DRAIN_TIMEOUT_SECONDS = 1.0
type _ToolDefinition = dict[str, object]
_COMPACTION_SUMMARY_PROMPT = """\
You are updating a durable conversation handoff summary for a future model call.

You will receive:
1. An optional <previous_summary> block that already contains everything summarized before this compaction.
2. A <new_conversation> block containing only the runs that became old enough to compact in this pass.

Your job is to produce one merged handoff summary as plain text.
Return only the summary text.

Rules:
- Preserve all still-relevant information from <previous_summary>.
- Add only the new information from <new_conversation>.
- Keep unchanged wording verbatim when it is still correct so future prompt prefixes remain stable.
- Never paraphrase away exact technical details such as file paths, function names, class names, commands, Matrix IDs, model names, config keys, numeric thresholds, ports, URLs, or error text.
- Preserve tool activity when it matters to current state, especially file edits, commands, and tool results.
- Do not invent facts.
- If a section has no content, write `None.`

Write a plain-text summary in exactly this markdown structure:
## Goal
## Constraints
## Progress
## Decisions
## Next Steps
## Critical Context
"""


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
class _ExcerptBlock:
    open_tag: str
    content: str
    close_tag: str

    def render(self, *, max_chars: int | None = None) -> str | None:
        snippet = self.content if max_chars is None else _truncate_excerpt(self.content, max_chars)
        if not snippet:
            return None
        return "\n".join([self.open_tag, _escape_xml_content(snippet), self.close_tag])


@dataclass(frozen=True)
class _ResolvedCompactionRuntime:
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
            messages=_messages_for_runs(included_runs, history_settings) if collect_compaction_hook_messages else (),
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
) -> _CompactionRewriteResult | None:
    final_summary_text = _current_summary_text(working_session) or ""
    total_compacted_run_count = 0
    all_compacted_run_ids: set[str] = set()
    compacted_messages: list[Message] = []
    pending_selected_run_ids: set[str] | None = None
    per_call_summary_input_budget = _effective_summary_input_budget_tokens(
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

        summary_input, included_runs = _build_summary_input(
            previous_summary=_current_summary_text(working_session),
            compacted_runs=compactable_runs,
            history_settings=history_settings,
            max_input_tokens=per_call_summary_input_budget,
        )
        if not included_runs:
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
            initial_summary_input=summary_input,
            initial_included_runs=included_runs,
            summary_input_budget=per_call_summary_input_budget,
            session_id=session_id,
            scope=scope,
            history_settings=history_settings,
            timing_scope=timing_scope,
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
            compacted_messages.extend(_messages_for_runs(included_runs, history_settings))
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
        _strip_stale_anthropic_replay_fields(run.messages or [])
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


def _estimate_static_tokens(agent: Agent, full_prompt: str) -> int:
    """Estimate system and current-user prompt tokens outside persisted replay."""
    static_chars = len(agent.role or "")
    instructions = agent.instructions
    if isinstance(instructions, str):
        static_chars += len(instructions)
    elif isinstance(instructions, list):
        for instruction in instructions:
            static_chars += len(str(instruction))
    static_chars += len(full_prompt)
    return (static_chars // 4) + _estimate_tool_definition_tokens(agent)


def estimate_agent_static_tokens(agent: Agent, full_prompt: str) -> int:
    """Estimate the non-history agent prompt using Agno's real system-message builder."""
    static_tokens = estimate_text_tokens(full_prompt)
    previous_tool_instructions = agent._tool_instructions
    try:
        session, run_context, prepared_tools = _prepare_agent_prompt_inputs_for_estimation(agent)
        system_message = agent.get_system_message(
            session=session,
            run_context=run_context,
            tools=prepared_tools or None,
            add_session_state_to_context=False,
        )
    finally:
        agent._tool_instructions = previous_tool_instructions
    if system_message is not None and system_message.content is not None:
        static_tokens += estimate_text_tokens(str(system_message.content))
    return static_tokens + _estimate_prepared_tool_definition_tokens(prepared_tools)


def _estimate_tool_definition_tokens(agent: Agent) -> int:
    """Estimate the model-visible tool schema and tool instructions for one agent."""
    prepared_tools, tool_instructions = _prepare_tools_for_estimation(agent.tools)
    return _estimate_prepared_tool_definition_tokens(
        prepared_tools,
        tool_instructions=tool_instructions,
    )


def estimate_team_static_tokens(team: Team, full_prompt: str) -> int:
    """Estimate the non-history team prompt using Agno's team system-message builder."""
    static_tokens = estimate_text_tokens(full_prompt)
    previous_tool_instructions = team._tool_instructions
    try:
        session, prepared_tools = _prepare_team_prompt_inputs_for_estimation(team)
        system_message = team.get_system_message(
            session=session,
            tools=prepared_tools or None,
            add_session_state_to_context=False,
        )
    finally:
        team._tool_instructions = previous_tool_instructions
    if system_message is not None and system_message.content is not None:
        static_tokens += estimate_text_tokens(str(system_message.content))
    return static_tokens + _estimate_prepared_tool_definition_tokens(prepared_tools)


def agent_tool_definition_payloads_for_logging(agent: Agent) -> list[dict[str, object]]:
    """Return model-visible agent tool schemas using Agno's prompt-preparation path."""
    previous_tool_instructions = agent._tool_instructions
    try:
        _session, _run_context, prepared_tools = _prepare_agent_prompt_inputs_for_estimation(agent)
    finally:
        agent._tool_instructions = previous_tool_instructions
    return _prepared_tool_definition_payloads(prepared_tools)


def team_tool_definition_payloads_for_logging(team: Team) -> list[dict[str, object]]:
    """Return model-visible team tool schemas using Agno's prompt-preparation path."""
    previous_tool_instructions = team._tool_instructions
    try:
        _session, prepared_tools = _prepare_team_prompt_inputs_for_estimation(team)
    finally:
        team._tool_instructions = previous_tool_instructions
    return _prepared_tool_definition_payloads(prepared_tools)


def _estimate_prepared_tool_definition_tokens(
    prepared_tools: Sequence[Function | dict[str, object]],
    *,
    tool_instructions: Sequence[str] = (),
) -> int:
    tool_definitions = _prepared_tool_definition_payloads(prepared_tools)
    tool_definition_tokens = len(stable_serialize(tool_definitions)) // 4 if tool_definitions else 0
    instruction_tokens = sum(estimate_text_tokens(instruction) for instruction in tool_instructions)
    return tool_definition_tokens + instruction_tokens


def _prepare_tools_for_estimation(tools: object) -> tuple[list[Function | _ToolDefinition], list[str]]:
    if not isinstance(tools, Sequence):
        return [], []

    prepared_tools: list[Function | _ToolDefinition] = []
    tool_instructions: list[str] = []
    seen_names: set[str] = set()
    for tool in tools:
        for prepared_tool in _prepare_tool_for_estimation(tool):
            tool_name = _prepared_tool_name(prepared_tool)
            if tool_name is None or tool_name in seen_names:
                continue
            seen_names.add(tool_name)
            prepared_tools.append(prepared_tool)

        if isinstance(tool, Toolkit) and tool.add_instructions and tool.instructions is not None:
            tool_instructions.append(tool.instructions)
        if isinstance(tool, Function) and tool.add_instructions and tool.instructions is not None:
            tool_instructions.append(tool.instructions)
    return prepared_tools, tool_instructions


def _prepare_tool_for_estimation(tool: object) -> list[Function | _ToolDefinition]:
    if isinstance(tool, Function):
        return [_prepare_function_for_estimation(tool)]
    if isinstance(tool, Toolkit):
        return [_prepare_function_for_estimation(function) for function in _toolkit_functions(tool).values()]
    if _is_tool_definition_dict(tool):
        return [tool]
    if callable(tool):
        return [Function.from_callable(tool)]
    return []


def _toolkit_functions(toolkit: Toolkit) -> dict[str, Function]:
    functions = dict(toolkit.functions)
    if not functions:
        for raw_tool in toolkit.tools:
            if isinstance(raw_tool, Function):
                functions[raw_tool.name] = raw_tool
    for name, function in toolkit.async_functions.items():
        functions.setdefault(name, function)
    return functions


def _prepare_function_for_estimation(function: Function) -> Function:
    prepared_function = function.model_copy(deep=True)
    if not prepared_function.skip_entrypoint_processing and prepared_function.entrypoint is not None:
        effective_strict = False if prepared_function.strict is None else prepared_function.strict
        prepared_function.process_entrypoint(strict=effective_strict)
    return prepared_function


def _prepared_tool_definition_payloads(
    prepared_tools: Sequence[Function | _ToolDefinition],
) -> list[dict[str, object]]:
    payloads_by_name: dict[str, dict[str, object]] = {}
    for tool in prepared_tools:
        payload = _function_payload(tool) if isinstance(tool, Function) else _dict_tool_payload(tool)
        tool_name = payload.get("name")
        if isinstance(tool_name, str) and tool_name:
            payloads_by_name[tool_name] = payload
    return list(payloads_by_name.values())


def _prepared_tool_name(tool: Function | _ToolDefinition) -> str | None:
    if isinstance(tool, Function):
        return tool.name
    tool_name = tool.get("name")
    if isinstance(tool_name, str) and tool_name:
        return tool_name
    return None


def _function_payload(function: Function) -> dict[str, object]:
    return {
        "name": function.name,
        "description": function.description or "",
        "parameters": function.parameters or _default_function_parameters(),
    }


def _is_tool_definition_dict(tool: object) -> TypeGuard[_ToolDefinition]:
    if not isinstance(tool, dict):
        return False
    candidate_tool = cast("_ToolDefinition", tool)
    tool_name = candidate_tool.get("name")
    return isinstance(tool_name, str) and bool(tool_name)


def _dict_tool_payload(tool: _ToolDefinition) -> dict[str, object]:
    parameters = tool.get("parameters")
    return {
        "name": str(tool["name"]),
        "description": str(tool.get("description", "")),
        "parameters": parameters if isinstance(parameters, dict) else _default_function_parameters(),
    }


def _default_function_parameters() -> dict[str, object]:
    return {"type": "object", "properties": {}, "required": []}


def _prepare_team_prompt_inputs_for_estimation(
    team: Team,
) -> tuple[TeamSession, list[Function | _ToolDefinition]]:
    """Reuse Agno's own team tool-preparation path for prompt budgeting.

    Agno exposes `Team.get_system_message()` publicly, but the exact prepared tool
    payload and `_tool_instructions` state that feed that prompt are only built by
    the internal `_determine_tools_for_model()` path. Using that single internal
    entrypoint is less brittle than re-implementing several private team helpers in
    MindRoom. This logic is verified against `agno==2.5.13`; if Agno changes those
    internals, update this estimator to match the new team prompt builder.
    """
    budget_session_id = "history-budget"
    session = TeamSession(session_id=budget_session_id, team_id=team.id)
    run_response = TeamRunOutput(
        run_id=budget_session_id,
        team_id=team.id,
        session_id=budget_session_id,
        session_state={},
    )
    run_context = RunContext(
        run_id=budget_session_id,
        session_id=budget_session_id,
        session_state={},
    )
    model = team.model
    assert model is not None
    prepared_tools = determine_team_tools_for_model(
        team=team,
        model=model,
        run_response=run_response,
        run_context=run_context,
        team_run_context={},
        session=session,
        check_mcp_tools=False,
    )
    return session, [tool for tool in prepared_tools if isinstance(tool, Function) or _is_tool_definition_dict(tool)]


def _prepare_agent_prompt_inputs_for_estimation(
    agent: Agent,
) -> tuple[AgentSession, RunContext, list[Function | _ToolDefinition]]:
    """Reuse Agno's agent tool-preparation path for prompt budgeting.

    Agno exposes `Agent.get_system_message()` publicly, but the prepared tool
    payload and `_tool_instructions` that feed that prompt are only finalized by
    the shared `agno.agent._tools.determine_tools_for_model()` path. Using that
    single internal entrypoint keeps MindRoom aligned with Agno without
    re-implementing several private agent helpers.
    """
    budget_session_id = "history-budget"
    budget_user_id = "history-budget-user"
    session = AgentSession(
        session_id=budget_session_id,
        agent_id=agent.id,
        user_id=budget_user_id,
    )
    run_response = RunOutput(
        run_id=budget_session_id,
        agent_id=agent.id,
        agent_name=agent.name,
        session_id=budget_session_id,
        user_id=budget_user_id,
        session_state={},
    )
    run_context = RunContext(
        run_id=budget_session_id,
        session_id=budget_session_id,
        user_id=budget_user_id,
        session_state={},
    )
    model = agent.model
    assert model is not None
    processed_tools = agent.get_tools(
        run_response=run_response,
        run_context=run_context,
        session=session,
        user_id=budget_user_id,
    )
    prepared_tools = determine_tools_for_model(
        agent=agent,
        model=model,
        processed_tools=processed_tools,
        run_response=run_response,
        run_context=run_context,
        session=session,
        async_mode=False,
    )
    return (
        session,
        run_context,
        [tool for tool in prepared_tools if isinstance(tool, Function) or _is_tool_definition_dict(tool)],
    )


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


def _effective_summary_input_budget_tokens(summary_input_budget: int, compaction_context_window: int | None) -> int:
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
) -> _ResolvedCompactionRuntime:
    """Resolve the effective compaction model name and usable window for one run."""
    model_name = compaction_config.model or active_model_name
    model_context_window = config.get_model_context_window(model_name)
    if compaction_config.model is not None:
        return _ResolvedCompactionRuntime(
            model_name=model_name,
            context_window=model_context_window,
        )
    return _ResolvedCompactionRuntime(
        model_name=model_name,
        context_window=model_context_window or active_context_window,
    )


async def _generate_compaction_summary_with_retry(
    *,
    model: Model,
    previous_summary: str | None,
    compactable_runs: Sequence[RunOutput | TeamRunOutput],
    initial_summary_input: str,
    initial_included_runs: list[RunOutput | TeamRunOutput],
    summary_input_budget: int,
    session_id: str,
    scope: HistoryScope,
    history_settings: ResolvedHistorySettings,
    timing_scope: str | None = None,
) -> _GeneratedSummaryChunk:
    """Generate one summary chunk, retrying once with a smaller input when safe."""
    summary_input = initial_summary_input
    included_runs = initial_included_runs
    budget = summary_input_budget
    last_error: Exception | None = None
    for attempt in (1, 2):
        estimated_input_tokens = estimate_text_tokens(summary_input)
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
            summary = await _generate_compaction_summary(
                model=model,
                summary_input=summary_input,
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
                rebuilt_input, rebuilt_runs = _build_summary_input(
                    previous_summary=previous_summary,
                    compacted_runs=compactable_runs,
                    history_settings=history_settings,
                    max_input_tokens=retry_budget,
                )
                if rebuilt_runs:
                    summary_input = rebuilt_input
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


@timed("system_prompt_assembly.history_prepare.compaction.summary_model_request")
async def _generate_compaction_summary(
    *,
    model: Model,
    summary_input: str,
    timeout_seconds: float | None = None,
    timing_scope: str | None = None,
) -> SessionSummary:
    del timing_scope
    resolved_timeout = MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds

    async def _request_summary() -> ModelResponse:
        try:
            return await model.aresponse(
                messages=[
                    Message(role="system", content=_COMPACTION_SUMMARY_PROMPT),
                    Message(role="user", content=summary_input),
                ],
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


@timed("system_prompt_assembly.history_prepare.compaction.summary_input_build")
def _build_summary_input(
    *,
    previous_summary: str | None,
    compacted_runs: Sequence[RunOutput | TeamRunOutput],
    max_input_tokens: int,
    history_settings: ResolvedHistorySettings | None = None,
) -> tuple[str, list[RunOutput | TeamRunOutput]]:
    resolved_history_settings = history_settings or _default_compaction_history_settings()
    summary_block = ""
    if previous_summary is not None and previous_summary.strip():
        escaped_summary = _escape_xml_content(previous_summary)
        summary_block = f"<previous_summary>\n{escaped_summary}\n</previous_summary>"

    remaining = max_input_tokens - estimate_text_tokens(summary_block) - _WRAPPER_OVERHEAD_TOKENS

    if remaining <= 0:
        return summary_block, []

    included_runs: list[RunOutput | TeamRunOutput] = []
    for run in compacted_runs:
        run_tokens = _estimate_serialized_run_tokens(run, resolved_history_settings)
        if run_tokens > remaining:
            if not included_runs:
                return _build_oversized_summary_input(
                    summary_block=summary_block,
                    compacted_runs=[run],
                    history_settings=resolved_history_settings,
                    max_input_tokens=max_input_tokens,
                )
            break
        included_runs.append(run)
        remaining -= run_tokens

    if not included_runs:
        return summary_block, []

    serialized_runs = "\n\n".join(
        _serialize_run(run, index, resolved_history_settings) for index, run in enumerate(included_runs)
    )
    return _compose_summary_input(summary_block, serialized_runs), included_runs


def _build_oversized_summary_input(
    *,
    summary_block: str,
    compacted_runs: Sequence[RunOutput | TeamRunOutput],
    history_settings: ResolvedHistorySettings,
    max_input_tokens: int,
) -> tuple[str, list[RunOutput | TeamRunOutput]]:
    if not compacted_runs:
        return summary_block, []
    first_run = compacted_runs[0]
    oversized_excerpt = _serialize_oversized_run_excerpt(
        first_run,
        index=0,
        history_settings=history_settings,
        max_tokens=_remaining_excerpt_budget(max_input_tokens, summary_block),
    )
    if oversized_excerpt is None:
        return summary_block, []
    return _compose_summary_input(summary_block, oversized_excerpt), [first_run]


def _serialize_oversized_run_excerpt(
    run: RunOutput | TeamRunOutput,
    *,
    index: int,
    history_settings: ResolvedHistorySettings,
    max_tokens: int,
) -> str | None:
    if max_tokens <= 0:
        return None

    full_run = _serialize_run(run, index, history_settings)
    if estimate_text_tokens(full_run) <= max_tokens:
        return full_run

    blocks = _excerpt_blocks(run, history_settings)
    budget_chars = max_tokens * 4
    while budget_chars > 0:
        excerpt = _serialize_run_excerpt(run, index=index, blocks=blocks, content_budget_chars=budget_chars)
        if estimate_text_tokens(excerpt) <= max_tokens:
            return excerpt
        budget_chars //= 2

    minimal_excerpt = _serialize_run_excerpt(run, index=index, blocks=blocks, content_budget_chars=0)
    if estimate_text_tokens(minimal_excerpt) <= max_tokens:
        return minimal_excerpt
    return None


def _serialize_run_excerpt(
    run: RunOutput | TeamRunOutput,
    *,
    index: int,
    blocks: Sequence[_ExcerptBlock],
    content_budget_chars: int,
) -> str:
    lines = [_run_open_tag(run, index), f"<note>{_OVERSIZED_RUN_NOTE}</note>"]
    remaining_chars = content_budget_chars
    for block in blocks:
        if remaining_chars <= 0:
            break
        rendered = block.render(max_chars=remaining_chars)
        if rendered is None:
            continue
        lines.append(rendered)
        if len(block.content) <= remaining_chars:
            remaining_chars -= len(block.content)
        else:
            break

    lines.append("</run>")
    return "\n".join(lines)


def _default_compaction_history_settings() -> ResolvedHistorySettings:
    return ResolvedHistorySettings(
        policy=HistoryPolicy(mode="all"),
        max_tool_calls_from_history=None,
    )


def _compaction_replay_messages(
    run: RunOutput | TeamRunOutput,
    history_settings: ResolvedHistorySettings,
) -> list[Message]:
    skip_roles = set(_history_skip_roles(history_settings) or [])
    messages = [deepcopy(message) for message in run.messages or [] if message.role not in skip_roles]
    if history_settings.max_tool_calls_from_history is not None and messages:
        filter_tool_calls(messages, history_settings.max_tool_calls_from_history)
    _strip_stale_anthropic_replay_fields(messages)
    return messages


def _excerpt_blocks(run: RunOutput | TeamRunOutput, history_settings: ResolvedHistorySettings) -> list[_ExcerptBlock]:
    blocks: list[_ExcerptBlock] = []
    if run.metadata:
        blocks.append(
            _ExcerptBlock("<run_metadata>", stable_serialize(_metadata_for_excerpt(run.metadata)), "</run_metadata>"),
        )
    for message in _compaction_replay_messages(run, history_settings):
        content = _render_message_content(message)
        if not content:
            continue
        blocks.append(_ExcerptBlock(_message_open_tag(message), content, "</message>"))
    return blocks


def _metadata_for_excerpt(metadata: dict[str, object]) -> dict[str, object]:
    """Keep compact identity metadata for oversized excerpts without tool schema bulk."""
    return {key: value for key, value in metadata.items() if key not in _EXCERPT_METADATA_OMIT_KEYS}


def _truncate_excerpt(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    if max_chars == 1:
        return "…"
    return f"{text[: max_chars - 1].rstrip()}…"


def _remaining_excerpt_budget(max_input_tokens: int, summary_block: str) -> int:
    return (
        max_input_tokens
        - estimate_text_tokens(summary_block)
        - estimate_text_tokens(
            "<new_conversation>\n\n</new_conversation>",
        )
    )


def _compose_summary_input(summary_block: str, serialized_runs: str) -> str:
    parts: list[str] = []
    if summary_block:
        parts.append(summary_block)
    parts.append(f"<new_conversation>\n{serialized_runs}\n</new_conversation>")
    return "\n\n".join(parts)


def _estimate_serialized_run_tokens(run: RunOutput | TeamRunOutput, history_settings: ResolvedHistorySettings) -> int:
    return estimate_text_tokens(_serialize_run(run, 0, history_settings))


def _messages_for_runs(
    runs: Sequence[RunOutput | TeamRunOutput],
    history_settings: ResolvedHistorySettings,
) -> list[Message]:
    messages: list[Message] = []
    for run in runs:
        messages.extend(_compaction_replay_messages(run, history_settings))
    return messages


def _serialize_run(run: RunOutput | TeamRunOutput, index: int, history_settings: ResolvedHistorySettings) -> str:
    lines = [_run_open_tag(run, index)]
    if run.metadata:
        lines.extend(["<run_metadata>", _escape_xml_content(stable_serialize(run.metadata)), "</run_metadata>"])
    for message in _compaction_replay_messages(run, history_settings):
        lines.extend(_serialize_message(message))
    lines.append("</run>")
    return "\n".join(lines)


def _serialize_message(message: Message) -> list[str]:
    lines = [_message_open_tag(message), _escape_xml_content(_render_message_content(message)), "</message>"]
    if message.tool_calls:
        lines.extend(["<tool_calls>", _escape_xml_content(stable_serialize(message.tool_calls)), "</tool_calls>"])
    for tag, media_value in _message_media_entries(message):
        serialized = _serialize_media_payload(media_value)
        if not serialized:
            continue
        lines.extend([f"<{tag}>", _escape_xml_content(serialized), f"</{tag}>"])
    return lines


def _run_open_tag(run: RunOutput | TeamRunOutput, index: int) -> str:
    attrs = [f'index="{index}"']
    if run.run_id:
        attrs.append(f'run_id="{escape(str(run.run_id), quote=True)}"')
    if run.status is not None:
        attrs.append(f'status="{escape(str(run.status), quote=True)}"')
    return f"<run {' '.join(attrs)}>"


def _message_open_tag(message: Message) -> str:
    attrs = [f'role="{escape(message.role, quote=True)}"']
    if message.name:
        attrs.append(f'name="{escape(message.name, quote=True)}"')
    if message.tool_call_id:
        attrs.append(f'tool_call_id="{escape(message.tool_call_id, quote=True)}"')
    return f"<message {' '.join(attrs)}>"


def _message_media_entries(message: Message) -> tuple[tuple[str, object | None], ...]:
    return (
        ("images", message.images),
        ("audio", message.audio),
        ("videos", message.videos),
        ("files", message.files),
        ("audio_output", message.audio_output),
        ("image_output", message.image_output),
        ("video_output", message.video_output),
        ("file_output", message.file_output),
    )


def _serialize_media_payload(media_value: object | None) -> str:
    if media_value is None:
        return ""
    return stable_serialize(_media_payload_snapshot(media_value))


def _media_payload_snapshot(media_value: object) -> object:
    if isinstance(media_value, BaseModel):
        payload = cast("dict[str, object]", media_value.model_dump(exclude_none=True))
        payload.pop("content", None)
        return payload
    if isinstance(media_value, Sequence) and not isinstance(media_value, (str, bytes, bytearray)):
        return [_media_payload_snapshot(item) for item in media_value]
    return media_value


def _render_message_content(message: Message) -> str:
    """Render one replayable string form of a message body."""
    content = message.compressed_content if message.compressed_content is not None else message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(stable_serialize(part) for part in content)
    if content is None:
        return ""
    return stable_serialize(content)


def _unescape_xml_content(text: str) -> str:
    return text.replace("&gt;", ">").replace("&lt;", "<").replace("&amp;", "&")


def _escape_xml_content(text: str) -> str:
    return escape(_unescape_xml_content(text), quote=False)


def estimate_prompt_visible_history_tokens(
    *,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    history_settings: ResolvedHistorySettings,
) -> int:
    """Estimate the durable summary plus visible persisted history for one run."""
    summary_tokens = estimate_session_summary_tokens(_current_summary_text(session))
    history_messages = _history_messages_for_session(
        session=session,
        scope=scope,
        history_settings=history_settings,
    )
    return summary_tokens + _estimate_history_messages_tokens(history_messages)


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


def _estimate_history_messages_tokens(messages: list[Message]) -> int:
    """Estimate the token count of materialized history messages."""
    if not messages:
        return 0
    return sum(_estimated_message_chars(message) for message in messages) // 4


def _strip_stale_anthropic_replay_fields(messages: list[Message]) -> int:
    """Strip stale Anthropic thinking replay fields from completed turns."""
    last_user_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].role == "user":
            last_user_idx = i
            break
    if last_user_idx < 0:
        return 0
    modified = 0
    for msg in messages[:last_user_idx]:
        if msg.role != "assistant":
            continue
        pd = msg.provider_data
        if not isinstance(pd, dict) or "signature" not in pd:
            continue
        msg.reasoning_content = None
        msg.redacted_reasoning_content = None
        del pd["signature"]
        modified += 1
    return modified


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


def _history_messages_for_session(
    *,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    history_settings: ResolvedHistorySettings,
) -> list[Message]:
    session_messages = _session_history_messages(
        session=session,
        scope=scope,
        history_settings=history_settings,
    )
    history_messages = [deepcopy(message) for message in session_messages]
    if history_settings.max_tool_calls_from_history is not None and history_messages:
        filter_tool_calls(history_messages, history_settings.max_tool_calls_from_history)
    _strip_stale_anthropic_replay_fields(history_messages)
    return history_messages


def _session_history_messages(
    *,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    history_settings: ResolvedHistorySettings,
) -> list[Message]:
    limit = history_settings.policy.limit
    if scope.kind == "team":
        return _team_session_history_messages(
            session=cast("TeamSession", session),
            scope_id=scope.scope_id,
            history_settings=history_settings,
            limit=limit,
        )
    return _agent_session_history_messages(
        session=cast("AgentSession", session),
        scope_id=scope.scope_id,
        history_settings=history_settings,
        limit=limit,
    )


def _agent_session_history_messages(
    *,
    session: AgentSession,
    scope_id: str,
    history_settings: ResolvedHistorySettings,
    limit: int | None,
) -> list[Message]:
    skip_roles = _history_skip_roles(history_settings)
    if history_settings.policy.mode == "runs":
        return session.get_messages(agent_id=scope_id, last_n_runs=limit, skip_roles=skip_roles)
    if history_settings.policy.mode == "messages":
        return session.get_messages(agent_id=scope_id, limit=limit, skip_roles=skip_roles)
    return session.get_messages(agent_id=scope_id, skip_roles=skip_roles)


def _team_session_history_messages(
    *,
    session: TeamSession,
    scope_id: str,
    history_settings: ResolvedHistorySettings,
    limit: int | None,
) -> list[Message]:
    skip_roles = _history_skip_roles(history_settings)
    if history_settings.policy.mode == "runs":
        return session.get_messages(team_id=scope_id, last_n_runs=limit, skip_roles=skip_roles)
    if history_settings.policy.mode == "messages":
        return session.get_messages(team_id=scope_id, limit=limit, skip_roles=skip_roles)
    return session.get_messages(team_id=scope_id, skip_roles=skip_roles)


def _history_skip_roles(history_settings: ResolvedHistorySettings) -> list[str] | None:
    """Return the effective Agno skip_roles filter for persisted history replay."""
    if not history_settings.skip_history_system_role:
        return None
    if history_settings.system_message_role in _STANDARD_HISTORY_ROLES:
        return None
    return [history_settings.system_message_role]


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


def _estimated_message_chars(message: Message) -> int:
    content_chars = len(_render_message_content(message))
    tool_call_chars = len(stable_serialize(message.tool_calls)) if message.tool_calls else 0
    return content_chars + tool_call_chars + _estimate_message_media_chars(message)


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


def _estimate_message_media_chars(message: Message) -> int:
    """Estimate serialized character cost for a message's media payloads."""
    media_chars = 0
    for _tag, media_value in _message_media_entries(message):
        if media_value is None:
            continue
        media_chars += len(stable_serialize(_media_payload_snapshot(media_value)))
    return media_chars


def _model_identifier(model: Model) -> str:
    return model.id or model.__class__.__name__


def _iso_utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def compute_prompt_token_breakdown(
    agent: Agent | None = None,
    team: Team | None = None,
    full_prompt: str | None = None,
) -> dict[str, int]:
    """Compute token breakdown for system prompt, tool defs, and current prompt."""
    breakdown: dict[str, int] = {}

    if agent is not None:
        sys_chars = len(agent.role or "")
        instructions = agent.instructions
        if isinstance(instructions, str):
            sys_chars += len(instructions)
        elif isinstance(instructions, list):
            for instruction in instructions:
                sys_chars += len(str(instruction))
        breakdown["role_instructions_tokens"] = sys_chars // 4

    tool_tokens = 0
    if agent is not None:
        tool_tokens = _estimate_tool_definition_tokens(agent)
    elif team is not None:
        prepared_tools, _tool_instructions = _prepare_tools_for_estimation(team.tools)
        tool_tokens = _estimate_prepared_tool_definition_tokens(prepared_tools)
    breakdown["tool_definition_tokens"] = tool_tokens

    if full_prompt is not None:
        breakdown["current_prompt_tokens"] = len(full_prompt) // 4

    return breakdown
