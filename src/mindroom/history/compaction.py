"""Scoped compaction."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from html import escape
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.team import TeamRunOutput
from agno.session.summary import SessionSummary
from agno.utils.message import filter_tool_calls
from pydantic import BaseModel

from mindroom.constants import MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS, prompt_roles_for_history_storage
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
from mindroom.timing import timed
from mindroom.token_budget import estimate_text_tokens, stable_serialize
from mindroom.tool_system.runtime_context import get_tool_runtime_context, resolve_tool_runtime_hook_bindings

if TYPE_CHECKING:
    from agno.db.base import BaseDb
    from agno.models.base import Model
    from agno.models.message import Message
    from agno.session.agent import AgentSession
    from agno.session.team import TeamSession

logger = get_logger(__name__)

_WRAPPER_OVERHEAD_TOKENS = 200
_OVERSIZED_RUN_NOTE = "Run truncated to fit compaction budget."
_EXCERPT_METADATA_OMIT_KEYS = frozenset(
    {
        "model_params",
        "tools_schema",
    },
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
    summary_model: Model,
    summary_model_name: str,
    active_context_window: int | None,
    replay_window_tokens: int | None,
    threshold_tokens: int | None,
    reserve_tokens: int,
    summary_prompt: str,
    timing_scope: str | None = None,
    lifecycle_notice_event_id: str | None = None,
    progress_callback: Callable[[CompactionLifecycleProgress], Awaitable[None]] | None = None,
) -> tuple[HistoryScopeState, CompactionOutcome | None]:
    """Compact one scope by rewriting session.summary and session.runs."""
    visible_runs = runs_for_scope(completed_top_level_runs(session), scope)
    compactable_runs = _select_compaction_candidates(
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
    selected_run_ids = _stable_compaction_run_ids(
        compactable_runs,
        session_id=session.session_id,
        scope=scope,
    )
    if not selected_run_ids:
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
        selected_run_ids=selected_run_ids,
        summary_input_budget=summary_input_budget,
        before_tokens=before_tokens,
        runs_before=before_run_count,
        threshold_tokens=threshold_tokens,
        summary_prompt=summary_prompt,
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
async def _rewrite_working_session_for_compaction(  # noqa: C901
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
    selected_run_ids: Sequence[str],
    summary_input_budget: int,
    before_tokens: int,
    runs_before: int,
    threshold_tokens: int | None,
    lifecycle_notice_event_id: str | None,
    progress_callback: Callable[[CompactionLifecycleProgress], Awaitable[None]] | None,
    collect_compaction_hook_messages: bool,
    summary_prompt: str,
    before_persist_callback: Callable[[Sequence[RunOutput | TeamRunOutput]], Awaitable[None]] | None = None,
    timing_scope: str | None = None,
) -> _CompactionRewriteResult | None:
    final_summary_text = _current_summary_text(working_session) or ""
    total_compacted_run_count = 0
    all_compacted_run_ids: list[str] = []
    all_compacted_run_id_set: set[str] = set()
    compacted_messages: list[Message] = []
    pending_selected_run_ids = set(selected_run_ids)

    while pending_selected_run_ids:
        working_visible_runs = runs_for_scope(completed_top_level_runs(working_session), scope)
        compactable_runs = [
            run
            for run in working_visible_runs
            if isinstance(run.run_id, str) and run.run_id in pending_selected_run_ids
        ]
        if not compactable_runs:
            break

        summary_input, included_runs = _build_summary_input(
            previous_summary=_current_summary_text(working_session),
            compacted_runs=compactable_runs,
            history_settings=history_settings,
            max_input_tokens=summary_input_budget,
        )
        if not included_runs:
            logger.warning(
                "Compaction skipped because no run fit the single-pass summary budget",
                session_id=session_id,
                scope=scope.key,
                candidate_runs=len(compactable_runs),
                summary_input_budget=summary_input_budget,
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
            summary_input_budget=summary_input_budget,
            session_id=session_id,
            scope=scope,
            history_settings=history_settings,
            summary_prompt=summary_prompt,
            timing_scope=timing_scope,
        )
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
            compacted_messages.extend(_messages_for_runs(included_runs, history_settings))
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
            summary_model_name=summary_model_name,
            before_tokens=before_tokens,
            available_history_budget=available_history_budget,
            runs_before=runs_before,
            threshold_tokens=threshold_tokens,
            total_compacted_run_count=total_compacted_run_count,
            selected_runs_remaining=len(pending_selected_run_ids),
        )

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
    selected_runs_remaining: int,
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
    summary_prompt: str,
    timing_scope: str | None = None,
) -> _GeneratedSummaryChunk:
    """Generate one summary chunk, shrinking the input per the retry policy when safe."""
    summary_input = initial_summary_input
    included_runs = initial_included_runs
    budget = summary_input_budget
    retry_policy = DEFAULT_SUMMARY_RETRY_POLICY
    attempt = 1
    while True:
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
            summary = await generate_compaction_summary(
                model=model,
                summary_input=summary_input,
                summary_prompt=summary_prompt,
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
            retry_budget = retry_policy.retry_budget(attempt=attempt, budget=budget, error=exc)
            if retry_budget is not None:
                rebuilt_input, rebuilt_runs = _build_summary_input(
                    previous_summary=previous_summary,
                    compacted_runs=compactable_runs,
                    history_settings=history_settings,
                    max_input_tokens=retry_budget,
                )
                # The policy decides whether a retry is allowed; rebuilt_runs is the
                # feasibility gate. An empty rebuild means the shrunken budget fits no
                # run at all, so a retry would resend the same failing input — fall
                # through to raise instead.
                if rebuilt_runs:
                    summary_input = rebuilt_input
                    included_runs = rebuilt_runs
                    budget = retry_budget
                    attempt += 1
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
    skip_roles = set(_history_skip_roles(history_settings))
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
    history_messages = _history_messages_for_estimation(
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


def _select_compaction_candidates(
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


def _history_messages_for_estimation(
    *,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    history_settings: ResolvedHistorySettings,
) -> list[Message]:
    """Return the prompt-visible history messages for token estimation only.

    No deepcopy: filter_tool_calls copies any message it modifies and only the
    list itself is mutated. Stale Anthropic replay fields are left in place
    because the char estimate never counts them.
    """
    history_messages = list(
        _session_history_messages(
            session=session,
            scope=scope,
            history_settings=history_settings,
        ),
    )
    if history_settings.max_tool_calls_from_history is not None and history_messages:
        filter_tool_calls(history_messages, history_settings.max_tool_calls_from_history)
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


def _history_skip_roles(history_settings: ResolvedHistorySettings) -> list[str]:
    """Return prompt roles that should never be materialized as persisted history."""
    return sorted(prompt_roles_for_history_storage(history_settings.system_message_role))


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
