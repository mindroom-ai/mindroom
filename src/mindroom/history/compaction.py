"""Scoped compaction persistence, hook emission, and lifecycle orchestration."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from mindroom.background_tasks import run_blocking_until_complete, run_coroutine_until_complete
from mindroom.history.conversation_text import utterances
from mindroom.history.replay import (
    current_summary_text,
    estimate_prompt_visible_history_tokens,
    scope_visible_runs,
)
from mindroom.history.storage import (
    ChunkBasis,
    FoldBasis,
    append_compaction_chunk,
    append_fold,
    read_chunk_basis,
    record_summary_usage,
)
from mindroom.history.summarizer import (
    ChunkState,
    Summarizer,
    SummaryChunk,
    check_summary_budget,
    summarize_chunk,
    summarize_runs,
)
from mindroom.history.types import (
    CompactionLifecycleProgress,
    CompactionOutcome,
    HistoryScope,
    ScopeSummary,
    owner_scope,
    replayed_summary_text,
)
from mindroom.hooks import EVENT_COMPACTION_AFTER, EVENT_COMPACTION_BEFORE, CompactionHookContext, TranscriptLine, emit
from mindroom.llm.runs import RunRecord
from mindroom.logging_config import get_logger
from mindroom.session_storage.lanes import run_session_storage_operation
from mindroom.timing import timed
from mindroom.tool_system.runtime_context import (
    current_requester_id,
    get_tool_runtime_context,
    scope_hook_context_kwargs,
    scope_workspace_root,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Sequence
    from pathlib import Path

    from mindroom.history.conversation_text import Utterance
    from mindroom.history.summary_fork import SummaryFork
    from mindroom.history.types import CompactionMode
    from mindroom.llm.messages import Message
    from mindroom.llm.provider import ModelIdentity, ModelProvider
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.llm.usage import MessageMetrics
    from mindroom.session_storage.store import ConversationStore
    from mindroom.session_storage.summary_rows import Fold
    from mindroom.session_storage.types import ConversationSession

logger = get_logger(__name__)


def scope_usage_recorder(
    storage: ConversationStore | Callable[[], ConversationStore],
    session_id: str,
    *,
    initial_session: ConversationSession | None = None,
) -> Callable[[ModelIdentity, MessageMetrics], Awaitable[None]]:
    """Record each summary response's usage under the current request's requester, creating ``initial_session`` if needed."""
    return partial(
        record_summary_usage,
        storage=storage,
        session_id=session_id,
        requester_id=current_requester_id(),
        initial_session=initial_session,
    )


type CompactionKind = Literal["pre_reply", "mid_turn", "run_local", "native"]


class _UnsetWorkspaceRoot:
    """Sentinel indicating ``emit_compaction_hook`` should resolve ``workspace_root`` lazily."""


_UNSET_WORKSPACE_ROOT = _UnsetWorkspaceRoot()


def _utterance_line(said: Utterance) -> TranscriptLine:
    return TranscriptLine(
        role="user" if said.kind == "said" else "assistant",
        sender=said.sender,
        display_name=said.display_name,
        text=said.unredacted,
    )


def _compaction_transcript(runs: Sequence[AnyRunRecord]) -> tuple[TranscriptLine, ...]:
    """Return what was said in ``runs``, oldest first, as compaction hooks read it."""
    return tuple(_utterance_line(said) for run in runs for said in utterances(run))


def projected_messages_transcript(
    history_messages: Sequence[Message],
    current_messages: Sequence[Message],
    *,
    record: AnyRunRecord,
    session_runs: Sequence[AnyRunRecord] = (),
) -> tuple[TranscriptLine, ...]:
    """Project replayed ``history_messages`` and ``current_messages`` into compaction transcript lines.

    History messages matching a stored run in ``session_runs`` reuse that run's ``utterances`` projection so run-level
    attribution and exclusions (automation turns, job follow-up prompts, stopped turns) are preserved.
    """
    lines: list[TranscriptLine] = []
    if history_messages:
        known_stored_ids = {msg.id for run in session_runs for msg in run.messages if msg.id}
        stored_lines_by_msg_id: dict[str, list[TranscriptLine]] = {}
        for run in session_runs:
            for said in utterances(run):
                if said.message.id:
                    stored_lines_by_msg_id.setdefault(said.message.id, []).append(_utterance_line(said))
        seen_stored_ids: set[str] = set()
        for msg in history_messages:
            if msg.id and msg.id in known_stored_ids:
                if msg.id not in seen_stored_ids:
                    seen_stored_ids.add(msg.id)
                    lines.extend(stored_lines_by_msg_id.get(msg.id, ()))
            else:
                lines.extend(_compaction_transcript([RunRecord(run_id="", messages=[msg])]))
    if current_messages:
        lines.extend(
            _compaction_transcript(
                [
                    RunRecord(
                        run_id="",
                        user_id=record.user_id,
                        messages=list(current_messages),
                        metadata=record.metadata,
                    ),
                ],
            ),
        )
    return tuple(lines)


async def emit_compaction_hook(
    *,
    event_name: str,
    scope: HistoryScope,
    transcript: Sequence[TranscriptLine],
    session_id: str,
    token_count_before: int,
    token_count_after: int | None = None,
    compaction_summary: str | None = None,
    kind: CompactionKind | None = None,
    mode: CompactionMode | None = None,
    summary_model: str | None = None,
    compacted_at: str | None = None,
    compacted_run_count: int | None = None,
    workspace_root: Path | None | _UnsetWorkspaceRoot = _UNSET_WORKSPACE_ROOT,
) -> Path | None | _UnsetWorkspaceRoot:
    """Emit a compaction observer hook when registered, resolving and returning ``workspace_root`` lazily."""
    runtime_context = get_tool_runtime_context()
    if runtime_context is None or not runtime_context.hooks.registry.has_hooks(event_name):
        return workspace_root
    effective_session_id = session_id or runtime_context.session_id
    resolved_workspace = (
        await asyncio.to_thread(scope_workspace_root, runtime_context, scope)
        if isinstance(workspace_root, _UnsetWorkspaceRoot)
        else workspace_root
    )
    hook_kwargs = scope_hook_context_kwargs(
        runtime_context,
        event_name=event_name,
        correlation_id=runtime_context.correlation_id or f"{event_name}:{effective_session_id}:{uuid4().hex}",
        scope=scope,
        session_id=effective_session_id,
    )
    # Compaction hooks always attribute to the compacted scope's own id (e.g. a team member).
    hook_kwargs["agent_name"] = scope.scope_id
    context = CompactionHookContext(
        **hook_kwargs,
        transcript=tuple(transcript),
        token_count_before=token_count_before,
        token_count_after=token_count_after,
        compaction_summary=compaction_summary,
        workspace_root=resolved_workspace,
        kind=kind,
        mode=mode,
        summary_model=summary_model,
        compacted_at=compacted_at,
        compacted_run_count=compacted_run_count,
        requester_id=runtime_context.requester_id,
    )
    await emit(runtime_context.hooks.registry, event_name, context)
    return resolved_workspace


def compaction_hooks_registered(*event_names: str) -> bool:
    """Return whether any compaction hook for ``event_names`` (default: before or after) is registered."""
    runtime_context = get_tool_runtime_context()
    if runtime_context is None:
        return False
    events = event_names or (EVENT_COMPACTION_BEFORE, EVENT_COMPACTION_AFTER)
    return any(runtime_context.hooks.registry.has_hooks(event_name) for event_name in events)


@timed("system_prompt_assembly.history_prepare.compaction")
async def compact_scope_history(
    *,
    storage: ConversationStore,
    session: ConversationSession,
    mode: CompactionMode,
    summarizer: Summarizer,
    available_history_budget: int | None,
    max_summary_tokens: int | None,
    before_tokens: int,
    replay_window_tokens: int | None,
    threshold_tokens: int | None,
    progress: Callable[[CompactionLifecycleProgress], Awaitable[None]] | None,
    complete: Callable[[CompactionOutcome], Awaitable[CompactionOutcome]],
    model: ModelProvider,
    fork: SummaryFork | None = None,
) -> CompactionOutcome | None:
    """Compact the scope owning ``session`` by appending summary rows that fold its oldest runs, chunk by chunk.

    ``max_summary_tokens`` is the most history the reply can replay; no chunk whose summary exceeds it, as ``model``'s
    route sizes it, is stored. ``progress`` receives progress after each stored chunk that leaves runs to replay, and
    ``complete`` receives the outcome after the last stored chunk.
    """
    scope = owner_scope(session)
    history_settings = summarizer.history_settings
    visible_runs = scope_visible_runs(session)
    if not visible_runs:
        return None

    hooks_registered = compaction_hooks_registered()
    compacted_transcript: list[TranscriptLine] = []
    resolved_workspace: Path | None | _UnsetWorkspaceRoot = _UNSET_WORKSPACE_ROOT
    estimate_tokens = partial(estimate_prompt_visible_history_tokens, history_settings=history_settings, model=model)

    async def before_persist(included_runs: Sequence[AnyRunRecord]) -> None:
        nonlocal resolved_workspace
        resolved_workspace = await emit_compaction_hook(
            event_name=EVENT_COMPACTION_BEFORE,
            scope=scope,
            transcript=_compaction_transcript(included_runs) if hooks_registered else (),
            session_id=session.session_id,
            token_count_before=before_tokens,
            kind="pre_reply",
            mode=mode,
            workspace_root=resolved_workspace,
        )

    async def after_persist(
        chunk: SummaryChunk,
        compacted_run_count: int,
        remaining_runs: Sequence[AnyRunRecord],
    ) -> None:
        if hooks_registered:
            compacted_transcript.extend(_compaction_transcript(chunk.included_runs))
        if progress is None or not remaining_runs:
            return
        snapshot = replace(
            session,
            summary=ScopeSummary(text=chunk.summary, updated_at=datetime.now(UTC)),
            runs=list(remaining_runs),
        )
        after_tokens = await asyncio.to_thread(estimate_tokens, session=snapshot)
        await progress(
            CompactionLifecycleProgress(
                mode=mode,
                session_id=session.session_id,
                scope=scope.key,
                summary_model=chunk.model.name,
                before_tokens=before_tokens,
                after_tokens=after_tokens,
                history_budget_tokens=available_history_budget,
                runs_before=len(visible_runs),
                compacted_run_count=compacted_run_count,
                runs_remaining=len(remaining_runs),
                threshold_tokens=threshold_tokens,
            ),
        )

    outcome = None

    async def report(chunk: SummaryChunk, compacted_run_count: int) -> None:
        nonlocal outcome
        summary_model = chunk.model
        logger.info(
            "Compaction summary generated",
            session_id=session.session_id,
            scope=scope.key,
            compacted_runs=compacted_run_count,
            model=summary_model.model.identity.id,
        )
        after_tokens = await asyncio.to_thread(estimate_tokens, session=session)
        outcome = await _report_committed(
            CompactionOutcome(
                mode=mode,
                session_id=session.session_id,
                scope=scope.key,
                summary=chunk.summary,
                summary_model=summary_model.name,
                before_tokens=before_tokens,
                after_tokens=after_tokens,
                window_tokens=replay_window_tokens or 0,
                threshold_tokens=threshold_tokens or 0,
                runs_before=len(visible_runs),
                runs_after=len(scope_visible_runs(session)),
                compacted_run_count=compacted_run_count,
                compacted_at=iso_utc_now(),
                history_budget_tokens=available_history_budget,
            ),
            scope=scope,
            transcript=compacted_transcript,
            complete=complete,
            kind="pre_reply",
            workspace_root=resolved_workspace,
        )

    try:
        await _rewrite_working_session_for_compaction(
            storage=storage,
            persisted_session=session,
            summarizer=summarizer,
            runs=visible_runs,
            max_summary_tokens=max_summary_tokens,
            model=model,
            before_persist=before_persist,
            after_persist=after_persist,
            report=report,
            fork=fork,
        )
    except ConversationChangedError:
        await run_session_storage_operation(lambda: storage, lambda opened: opened.reload_log(session))
        raise
    return outcome


@timed("system_prompt_assembly.history_prepare.compaction.rewrite_working_session")
async def _rewrite_working_session_for_compaction(
    *,
    storage: ConversationStore,
    persisted_session: ConversationSession,
    summarizer: Summarizer,
    runs: Sequence[AnyRunRecord],
    max_summary_tokens: int | None,
    model: ModelProvider,
    before_persist: Callable[[Sequence[AnyRunRecord]], Awaitable[None]],
    after_persist: Callable[[SummaryChunk, int, Sequence[AnyRunRecord]], Awaitable[None]],
    report: Callable[[SummaryChunk, int], Coroutine[object, object, None]],
    fork: SummaryFork | None = None,
) -> None:
    """Fold ``runs``, oldest first, into one appended summary row per chunk.

    Tries ``fork`` once on the first pass (already budget-validated on success); otherwise summarizes XML chunks.
    """
    scope = owner_scope(persisted_session)
    state = ChunkState(summarizer.model, summarizer.fallback, fork=fork)
    last_chunk: SummaryChunk | None = None
    previous_summary = current_summary_text(persisted_session)
    remaining = list(runs)

    async def _write(chunk: SummaryChunk, basis: ChunkBasis, remaining_after: Sequence[AnyRunRecord]) -> bool:
        appended = await run_session_storage_operation(
            lambda: storage,
            partial(
                append_compaction_chunk,
                session=persisted_session,
                basis=basis,
                included_run_ids=tuple(run.run_id for run in chunk.included_runs),
                summary=chunk.summary,
            ),
        )
        if not appended:
            return False
        await after_persist(chunk, len(runs) - len(remaining_after), remaining_after)
        if not remaining_after:
            await report(chunk, len(runs))
        return True

    while remaining:
        compacted_run_count = len(runs) - len(remaining)
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
        chunk = await summarize_chunk(
            summarizer,
            previous_summary,
            remaining,
            state=state,
            max_summary_tokens=max_summary_tokens,
            model=model,
        )
        if chunk is None:
            logger.warning(
                "Compaction skipped because no run fit the single-pass summary budget",
                session_id=persisted_session.session_id,
                scope=scope.key,
                candidate_runs=len(remaining),
                summary_input_budget_tokens=state.current_model.input_budget_tokens,
            )
            break
        if (error := await check_summary_budget(chunk, max_summary_tokens, model)) is not None:
            _stop_compaction(error, persisted_session.session_id, scope, compacted_run_count)
            break
        await before_persist(chunk.included_runs)
        remaining_after = remaining[len(chunk.included_runs) :]
        commit = _write(chunk, basis, remaining_after)
        if not await (commit if remaining_after else run_coroutine_until_complete(commit)):
            _stop_compaction(ConversationChangedError(), persisted_session.session_id, scope, compacted_run_count)
            break
        last_chunk, previous_summary, remaining = chunk, replayed_summary_text(chunk.summary), remaining_after
    if remaining and last_chunk is not None:
        await run_coroutine_until_complete(report(last_chunk, len(runs) - len(remaining)))


class ConversationChangedError(RuntimeError):
    """Another writer changed a scope's history after compaction read it, so compaction stored nothing."""

    def __init__(self) -> None:
        super().__init__("The conversation changed during compaction.")


def _stop_compaction(error: Exception, session_id: str, scope: HistoryScope, compacted_run_count: int) -> None:
    if compacted_run_count == 0:
        raise error
    logger.warning(
        "Compaction stopped early",
        reason=str(error),
        session_id=session_id,
        scope=scope.key,
        compacted_runs=compacted_run_count,
    )


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
    max_summary_tokens: int | None,
    model: ModelProvider,
    on_commit: Callable[[str], None],
    rewritten_request_tokens: Callable[[str], int],
    complete: Callable[[CompactionOutcome], Awaitable[CompactionOutcome]],
    fork: SummaryFork | None = None,
) -> CompactionOutcome:
    """Summarize ``basis.runs`` and ``snapshots``, validate and size pre-commit, then append one row storing ``fold``."""
    scope = owner_scope(session)
    summary, summary_model = await summarize_runs(
        summarizer,
        basis.summary,
        [*basis.runs, *snapshots],
        fork=fork,
        max_summary_tokens=max_summary_tokens,
        model=model,
        enforce_xml_budget=True,
    )
    after_tokens = await asyncio.to_thread(rewritten_request_tokens, summary)
    folded_run = RunRecord(
        run_id="",
        user_id=fold.run.user_id,
        messages=list(fold.messages),
        metadata=fold.run.metadata,
    )
    transcript = _compaction_transcript([*basis.runs, folded_run]) if compaction_hooks_registered() else ()
    resolved_workspace = await emit_compaction_hook(
        event_name=EVENT_COMPACTION_BEFORE,
        scope=scope,
        transcript=transcript,
        session_id=session.session_id,
        token_count_before=before_tokens,
        kind="mid_turn",
        mode="auto",
    )

    async def commit_and_report() -> CompactionOutcome:
        appended = await run_session_storage_operation(
            lambda: storage,
            partial(append_fold, session=session, basis=basis, summary=summary, fold=fold),
        )
        if not appended:
            raise ConversationChangedError
        on_commit(summary)
        outcome = CompactionOutcome(
            mode="auto",
            session_id=session.session_id,
            scope=scope.key,
            summary=summary,
            summary_model=summary_model.name,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            window_tokens=replay_window_tokens or 0,
            threshold_tokens=threshold_tokens or 0,
            runs_before=len(basis.runs) + 1,
            runs_after=1,
            compacted_run_count=len(basis.runs) + 1,
            compacted_at=iso_utc_now(),
            history_budget_tokens=available_history_budget,
        )
        return await _report_committed(
            outcome,
            scope=scope,
            transcript=transcript,
            complete=complete,
            kind="mid_turn",
            workspace_root=resolved_workspace,
        )

    return await run_coroutine_until_complete(commit_and_report())


async def _report_committed(
    outcome: CompactionOutcome,
    *,
    scope: HistoryScope,
    transcript: Sequence[TranscriptLine],
    complete: Callable[[CompactionOutcome], Awaitable[CompactionOutcome]],
    kind: CompactionKind,
    workspace_root: Path | None | _UnsetWorkspaceRoot = _UNSET_WORKSPACE_ROOT,
) -> CompactionOutcome:
    await emit_compaction_hook(
        event_name=EVENT_COMPACTION_AFTER,
        scope=scope,
        transcript=transcript,
        session_id=outcome.session_id,
        token_count_before=outcome.before_tokens,
        token_count_after=outcome.after_tokens,
        compaction_summary=outcome.summary,
        kind=kind,
        mode=outcome.mode,
        summary_model=outcome.summary_model,
        compacted_at=outcome.compacted_at,
        compacted_run_count=outcome.compacted_run_count,
        workspace_root=workspace_root,
    )
    return await complete(outcome)


def iso_utc_now() -> str:
    """Return the current UTC timestamp as an ISO-8601 string with ``Z`` suffix."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = [
    "CompactionKind",
    "ConversationChangedError",
    "compact_running_turn",
    "compact_scope_history",
    "compaction_hooks_registered",
    "emit_compaction_hook",
    "iso_utc_now",
    "projected_messages_transcript",
    "scope_usage_recorder",
]
