"""Shared conversation compaction engine.

NOTE: Agno's Agent upserts the full in-memory session after each run.
If a concurrent request loads the session before compaction commits,
its subsequent save may overwrite the compaction. This is an inherent
Agno architecture limitation, not specific to this module. For
single-agent-per-session deployments (MindRoom default), the per-session
lock mitigates this. Full fix requires Agno-level transactional sessions.
"""

from __future__ import annotations

import asyncio
import json
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from agno.db.base import SessionType
from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.summary import SessionSummary
from agno.utils.message import filter_tool_calls

from mindroom.agents import _get_agent_session, create_session_storage
from mindroom.constants import MINDROOM_COMPACTION_METADATA_KEY
from mindroom.logging_config import get_logger
from mindroom.token_budget import _stable_serialize, compute_compaction_input_budget, estimate_text_tokens

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agno.agent import Agent
    from agno.db.sqlite import SqliteDb
    from agno.models.base import Model

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)

_COMPACTION_METADATA_VERSION = 1
_COMPACTION_NOTICE_VERSION = 1
_WRAPPER_OVERHEAD_TOKENS = 200
_SUMMARY_TRUNCATION_RATIO = 0.5
_DEFAULT_MAX_COMPACTION_PASSES = 10

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
- If a section has no content, write `None.`.

Write a plain-text summary in exactly this markdown structure:
## Goal
## Constraints
## Progress
## Decisions
## Next Steps
## Critical Context
"""

_SESSION_COMPACTION_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}


@dataclass(frozen=True)
class CompactionOutcome:
    """Notice-time telemetry and persisted compaction result."""

    mode: str
    summary: str
    topics: list[str]
    summary_model: str
    before_tokens: int
    after_tokens: int
    window_tokens: int
    threshold_tokens: int
    reserve_tokens: int
    keep_recent_tokens: int
    runs_before: int
    runs_after: int
    compacted_run_count: int
    compacted_at: str
    notify: bool

    def to_notice_metadata(self) -> dict[str, Any]:
        """Return the machine-readable Matrix payload for compaction notices.

        The full summary text is intentionally excluded to avoid leaking
        internal context into Matrix room events visible to all participants.
        """
        return {
            "version": _COMPACTION_NOTICE_VERSION,
            "mode": self.mode,
            "topics": list(self.topics),
            "summary_model": self.summary_model,
            "before_tokens": self.before_tokens,
            "after_tokens": self.after_tokens,
            "window_tokens": self.window_tokens,
            "runs_before": self.runs_before,
            "runs_after": self.runs_after,
            "compacted_run_count": self.compacted_run_count,
            "compacted_at": self.compacted_at,
        }

    def to_session_metadata(self, seen_event_ids: set[str]) -> dict[str, Any]:
        """Return the persisted session bookkeeping payload."""
        return {
            "version": _COMPACTION_METADATA_VERSION,
            "seen_event_ids": sorted(seen_event_ids),
            "compacted_at": self.compacted_at,
            "before_tokens": self.before_tokens,
            "after_tokens": self.after_tokens,
            "mode": self.mode,
            "summary_model": self.summary_model,
        }


@dataclass(frozen=True)
class PendingCompaction:
    """Deferred compaction state queued during the current tool call."""

    session_id: str
    agent_name: str
    config: Config
    runtime_paths: RuntimePaths
    execution_identity: ToolExecutionIdentity | None
    compacted_count: int
    summary: SessionSummary
    mode: str
    summary_model: str
    window_tokens: int
    threshold_tokens: int
    reserve_tokens: int
    keep_recent_tokens: int
    notify: bool


@dataclass(frozen=True)
class HistoryScrubStats:
    """Aggregate results for one history-message scrub run."""

    sessions_scanned: int
    sessions_changed: int
    messages_removed: int
    size_before_bytes: int
    size_after_bytes: int


@dataclass(frozen=True)
class _CompactionProgress:
    """In-memory multi-pass compaction state."""

    summary: SessionSummary | None
    remaining_compacted_runs: list[RunOutput | TeamRunOutput]
    compacted_count: int
    budget_exhausted_on_first_pass: bool


_PENDING_COMPACTION: ContextVar[PendingCompaction | None] = ContextVar(
    "pending_compaction",
    default=None,
)


def clear_pending_compaction() -> None:
    """Discard any queued pending compaction without applying it."""
    _PENDING_COMPACTION.set(None)


def estimate_message_media_chars(message: Message) -> int:
    """Estimate serialized media payload size for one message."""
    media_values = (
        message.images,
        message.audio,
        message.videos,
        message.files,
        message.audio_output,
        message.image_output,
        message.video_output,
        message.file_output,
    )
    media_chars = 0
    for media_value in media_values:
        if media_value:
            media_chars += len(str(media_value))
    return media_chars


def estimate_messages_tokens(messages: Sequence[Message] | None) -> int:
    """Estimate token count for messages using the shared chars / 4 heuristic."""
    if not messages:
        return 0
    total_chars = 0
    for msg in messages:
        total_chars += len(_render_message_content(msg))
        if msg.tool_calls:
            total_chars += len(_stable_serialize(msg.tool_calls))
        total_chars += estimate_message_media_chars(msg)
    return total_chars // 4


def estimate_static_tokens(agent: Agent, full_prompt: str) -> int:
    """Estimate tokens for system prompt plus current prompt body."""
    static_chars = len(agent.role or "")
    instructions = agent.instructions
    if isinstance(instructions, str):
        static_chars += len(instructions)
    elif isinstance(instructions, list):
        for instruction in instructions:
            static_chars += len(str(instruction))
    static_chars += len(full_prompt)
    return static_chars // 4


def get_history_skip_roles(agent: Agent) -> list[str] | None:
    """Return history roles skipped by Agno for this agent."""
    system_role = agent.system_message_role
    if isinstance(system_role, str) and system_role not in {"user", "assistant", "tool"}:
        return [system_role]
    return None


def get_team_scope(agent: Agent) -> tuple[str | None, str | None]:
    """Return the active (team_id, agent_id) scope for this agent."""
    team_id = agent.team_id
    if not isinstance(team_id, str) or not team_id:
        return None, None
    agent_id = agent.id
    return team_id, agent_id if isinstance(agent_id, str) and agent_id else None


def get_replayable_runs(session: AgentSession, agent: Agent) -> list[RunOutput | TeamRunOutput]:
    """Return runs that Agno can replay for prompt history."""
    runs = [run for run in session.runs or [] if isinstance(run, (RunOutput, TeamRunOutput))]
    team_id, agent_id = get_team_scope(agent)
    if team_id is not None:
        runs = [run for run in runs if isinstance(run, TeamRunOutput) and run.team_id == team_id]
    elif agent_id:
        runs = [run for run in runs if isinstance(run, RunOutput) and run.agent_id == agent_id]

    skip_statuses = {RunStatus.paused, RunStatus.cancelled, RunStatus.error}
    return [run for run in runs if run.parent_run_id is None and run.status not in skip_statuses]


def estimate_history_tokens(
    session: AgentSession,
    agent: Agent,
    run_limit: int | None,
    *,
    message_limit: int | None = None,
) -> int:
    """Estimate replayed history tokens using Agno's message construction path."""
    team_id, agent_id = get_team_scope(agent)
    messages = session.get_messages(
        agent_id=agent_id,
        team_id=team_id,
        last_n_runs=run_limit,
        limit=message_limit,
        skip_roles=get_history_skip_roles(agent),
    )
    max_tool_calls_from_history = agent.max_tool_calls_from_history
    if max_tool_calls_from_history is None:
        return estimate_messages_tokens(messages)
    history_copy = [deepcopy(msg) for msg in messages]
    filter_tool_calls(history_copy, max_tool_calls_from_history)
    return estimate_messages_tokens(history_copy)


def find_fitting_run_limit(session: AgentSession, agent: Agent, max_runs: int, budget: int) -> int:
    """Return the largest replayed run count that fits in the remaining budget."""
    low = 0
    high = max_runs
    best = 0
    while low <= high:
        mid = (low + high) // 2
        tokens = 0 if mid == 0 else estimate_history_tokens(session, agent, mid)
        if tokens <= budget:
            best = mid
            low = mid + 1
        else:
            high = mid - 1
    return best


def estimate_runs_tokens(
    runs: Sequence[RunOutput | TeamRunOutput] | None,
    *,
    max_tool_calls_from_history: int | None = None,
) -> int:
    """Estimate token count for serialized runs."""
    if not runs:
        return 0
    total = 0
    for run in runs:
        messages = run.messages
        if not messages:
            continue
        if max_tool_calls_from_history is None:
            total += estimate_messages_tokens(messages)
            continue
        filtered_messages = [deepcopy(message) for message in messages]
        filter_tool_calls(filtered_messages, max_tool_calls_from_history)
        total += estimate_messages_tokens(filtered_messages)
    return total


async def compact_session_now(
    *,
    storage: SqliteDb,
    session_id: str,
    agent: Agent,
    model: Model,
    mode: str,
    window_tokens: int,
    threshold_tokens: int,
    reserve_tokens: int,
    keep_recent_tokens: int,
    notify: bool,
    compaction_model_context_window: int | None = None,
    max_passes: int = _DEFAULT_MAX_COMPACTION_PASSES,
) -> tuple[AgentSession, CompactionOutcome] | None:
    """Compact one stored session immediately and persist the mutated state.

    Uses selective pruning: only runs that fit the compaction model's input
    budget are included in the summary and removed from the session.
    Oversized runs that don't fit are left in place.

    With multi-pass compaction enabled, the engine summarizes one contiguous
    prefix slice per pass, feeds that merged summary back in as
    ``<previous_summary>``, and continues from the next oldest remaining run
    until all eligible runs are compacted or ``max_passes`` is exhausted.
    """
    lock = _get_session_lock(storage, session_id)
    async with lock:
        session = _get_agent_session(storage, session_id)
        if session is None or not session.runs:
            return None

        cut_index = _find_auto_cut_index(session, agent, keep_recent_tokens)
        if cut_index is None:
            return None

        before_runs = list(session.runs)
        previous_summary = session.summary
        compacted_runs = before_runs[:cut_index]
        recent_runs = before_runs[cut_index:]

        # Compute budget from the compaction model's context window
        effective_window = compaction_model_context_window or window_tokens
        summary_input_budget = compute_compaction_input_budget(
            effective_window,
            reserve_tokens=reserve_tokens,
        )
        if summary_input_budget <= 0:
            logger.warning(
                "Compaction budget non-positive, skipping",
                session_id=session_id,
                window_tokens=effective_window,
                reserve_tokens=reserve_tokens,
            )
            return None

        progress = await _execute_compaction_passes(
            model=model,
            session_id=session_id,
            previous_summary=previous_summary,
            compacted_runs=compacted_runs,
            summary_input_budget=summary_input_budget,
            max_passes=max_passes,
        )
        if progress.compacted_count == 0 or progress.summary is None:
            summary_tokens = estimate_text_tokens(previous_summary.summary) if previous_summary else 0
            logger.warning(
                "Compaction skipped: no runs fit the input budget",
                session_id=session_id,
                candidate_runs=len(compacted_runs),
                budget=summary_input_budget,
                summary_tokens=summary_tokens,
                has_prior_summary=previous_summary is not None,
                budget_exhausted=progress.budget_exhausted_on_first_pass,
            )
            return None

        removed_runs = compacted_runs[: progress.compacted_count]
        # Contiguous prefix: included_runs is always a prefix of compacted_runs.
        # In the multi-pass flow, each pass advances by the next prefix slice,
        # so the total removed runs are still one contiguous prefix.
        # Remove the compacted prefix, keep remaining uncompacted runs + recent runs.
        remaining_runs = list(progress.remaining_compacted_runs) + list(recent_runs)
        outcome = _build_compaction_outcome(
            before_runs=before_runs,
            before_summary=previous_summary,
            after_runs=remaining_runs,
            new_summary=progress.summary,
            mode=mode,
            summary_model=_model_identifier(model),
            window_tokens=window_tokens,
            threshold_tokens=threshold_tokens,
            reserve_tokens=reserve_tokens,
            keep_recent_tokens=keep_recent_tokens,
            notify=notify,
            count_pending_run=True,
        )
        seen_event_ids = _merge_seen_event_ids(session_metadata=session.metadata, removed_runs=removed_runs)
        session.summary = progress.summary
        session.runs = remaining_runs
        session.metadata = {
            **(session.metadata or {}),
            MINDROOM_COMPACTION_METADATA_KEY: outcome.to_session_metadata(seen_event_ids),
        }
        storage.upsert_session(session)
        return session, outcome


async def queue_pending_compaction(
    *,
    storage: SqliteDb,
    session_id: str,
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
    model: Model,
    keep_recent_runs: int,
    window_tokens: int,
    threshold_tokens: int,
    reserve_tokens: int,
    notify: bool,
    compaction_model_context_window: int | None = None,
    max_passes: int = _DEFAULT_MAX_COMPACTION_PASSES,
) -> CompactionOutcome | None:
    """Queue a deferred post-run compaction for the current session."""
    lock = _get_session_lock(storage, session_id)
    async with lock:
        session = _get_agent_session(storage, session_id)
        if session is None or not session.runs:
            return None
        if keep_recent_runs < 0:
            msg = "keep_recent_runs must be >= 0"
            raise ValueError(msg)
        cut_index = len(session.runs) - keep_recent_runs
        if cut_index <= 0:
            return None

        before_runs = list(session.runs)
        previous_summary = session.summary
        compacted_runs = before_runs[:cut_index]
        recent_runs = before_runs[cut_index:]

        # Compute budget from the compaction model's context window
        effective_window = compaction_model_context_window or window_tokens
        summary_input_budget = compute_compaction_input_budget(
            effective_window,
            reserve_tokens=reserve_tokens,
        )
        if summary_input_budget <= 0:
            logger.warning(
                "Manual compaction budget non-positive, skipping",
                session_id=session_id,
                window_tokens=effective_window,
                reserve_tokens=reserve_tokens,
            )
            return None

        progress = await _execute_compaction_passes(
            model=model,
            session_id=session_id,
            previous_summary=previous_summary,
            compacted_runs=compacted_runs,
            summary_input_budget=summary_input_budget,
            max_passes=max_passes,
        )
        if progress.compacted_count == 0 or progress.summary is None:
            summary_tokens = estimate_text_tokens(previous_summary.summary) if previous_summary else 0
            logger.warning(
                "Manual compaction skipped: no runs fit the input budget",
                session_id=session_id,
                candidate_runs=len(compacted_runs),
                budget=summary_input_budget,
                summary_tokens=summary_tokens,
                has_prior_summary=previous_summary is not None,
                budget_exhausted=progress.budget_exhausted_on_first_pass,
            )
            return None

        # Contiguous prefix: included_runs is always a prefix of compacted_runs.
        # In the multi-pass flow, each pass advances by the next prefix slice,
        # so the queued removal is still one contiguous prefix.
        remaining_runs = list(progress.remaining_compacted_runs) + list(recent_runs)
        queued_outcome = _build_compaction_outcome(
            before_runs=before_runs,
            before_summary=previous_summary,
            after_runs=remaining_runs,
            new_summary=progress.summary,
            mode="manual",
            summary_model=_model_identifier(model),
            window_tokens=window_tokens,
            threshold_tokens=threshold_tokens,
            reserve_tokens=reserve_tokens,
            keep_recent_tokens=estimate_runs_tokens(recent_runs),
            notify=notify,
            count_pending_run=True,
        )
        _PENDING_COMPACTION.set(
            PendingCompaction(
                session_id=session_id,
                agent_name=agent_name,
                config=config,
                runtime_paths=runtime_paths,
                execution_identity=execution_identity,
                compacted_count=progress.compacted_count,
                summary=progress.summary,
                mode="manual",
                summary_model=_model_identifier(model),
                window_tokens=window_tokens,
                threshold_tokens=threshold_tokens,
                reserve_tokens=reserve_tokens,
                keep_recent_tokens=estimate_runs_tokens(recent_runs),
                notify=notify,
            ),
        )
        return queued_outcome


async def apply_pending_compaction() -> CompactionOutcome | None:
    """Apply a queued post-run compaction after Agno saves the current run.

    Uses positional prefix removal: the first ``compacted_count`` runs are
    removed from the session.  Agno appends new runs at the end, so the
    compacted prefix stays stable between queue and apply.

    In the multi-pass flow, ``compacted_count`` may represent several passes
    worth of prefix slices, but they still collapse to one stable prefix that
    can be removed after the run save.
    """
    pending = _PENDING_COMPACTION.get(None)
    _PENDING_COMPACTION.set(None)
    if pending is None:
        return None

    storage = create_session_storage(
        pending.agent_name,
        pending.config,
        pending.runtime_paths,
        execution_identity=pending.execution_identity,
    )
    lock = _get_session_lock(storage, pending.session_id)
    async with lock:
        session = _get_agent_session(storage, pending.session_id)
        if session is None or not session.runs:
            logger.warning(
                "Pending compaction skipped: session not found post-run",
                agent=pending.agent_name,
                session_id=pending.session_id,
            )
            return None

        if len(session.runs) < pending.compacted_count:
            logger.warning(
                "Pending compaction skipped: session has fewer runs than expected",
                agent=pending.agent_name,
                session_id=pending.session_id,
                expected_prefix=pending.compacted_count,
                current_runs=len(session.runs),
            )
            return None

        before_runs = list(session.runs)
        previous_summary = session.summary
        removed_runs = before_runs[: pending.compacted_count]
        remaining_runs = before_runs[pending.compacted_count :]

        outcome = _build_compaction_outcome(
            before_runs=before_runs,
            before_summary=previous_summary,
            after_runs=remaining_runs,
            new_summary=pending.summary,
            mode=pending.mode,
            summary_model=pending.summary_model,
            window_tokens=pending.window_tokens,
            threshold_tokens=pending.threshold_tokens,
            reserve_tokens=pending.reserve_tokens,
            keep_recent_tokens=pending.keep_recent_tokens,
            notify=pending.notify,
        )
        seen_event_ids = _merge_seen_event_ids(session_metadata=session.metadata, removed_runs=removed_runs)
        session.summary = pending.summary
        session.runs = remaining_runs
        session.metadata = {
            **(session.metadata or {}),
            MINDROOM_COMPACTION_METADATA_KEY: outcome.to_session_metadata(seen_event_ids),
        }
        storage.upsert_session(session)
        return outcome


def _find_auto_cut_index(session: AgentSession, agent: Agent, keep_recent_tokens: int) -> int | None:
    replayable_runs = get_replayable_runs(session, agent)
    if not replayable_runs:
        return None
    if keep_recent_tokens <= 0:
        return len(session.runs or [])

    all_runs = session.runs or []
    kept_count = 0
    kept_tokens = 0
    for run in reversed(replayable_runs):
        kept_count += 1
        kept_tokens += estimate_runs_tokens(
            [run],
            max_tool_calls_from_history=agent.max_tool_calls_from_history,
        )
        if kept_tokens >= keep_recent_tokens:
            break

    if kept_count == len(replayable_runs):
        return None
    oldest_kept_run = replayable_runs[len(replayable_runs) - kept_count]
    # Find the index of this run in the full session.runs list by identity
    for index, run in enumerate(all_runs):
        if run is oldest_kept_run:
            return index
    return None


async def _execute_compaction_passes(
    *,
    model: Model,
    session_id: str,
    previous_summary: SessionSummary | None,
    compacted_runs: Sequence[RunOutput | TeamRunOutput],
    summary_input_budget: int,
    max_passes: int,
) -> _CompactionProgress:
    working_summary = previous_summary
    working_compacted_runs = list(compacted_runs)
    compacted_count = 0
    budget_exhausted_on_first_pass = False

    for pass_index in range(max_passes):
        summary_input, included_runs, budget_exhausted = _build_summary_input(
            previous_summary=working_summary,
            compacted_runs=working_compacted_runs,
            max_input_tokens=summary_input_budget,
        )
        if pass_index == 0:
            budget_exhausted_on_first_pass = budget_exhausted
        if not included_runs:
            break

        try:
            working_summary = await _generate_compaction_summary(
                model=model,
                summary_input=summary_input,
            )
        except Exception:
            if compacted_count == 0:
                raise
            logger.exception(
                "Compaction pass failed after partial progress",
                session_id=session_id,
                completed_passes=pass_index,
                compacted_count=compacted_count,
            )
            break

        working_compacted_runs = working_compacted_runs[len(included_runs) :]
        compacted_count += len(included_runs)
        if not working_compacted_runs:
            break

    if working_compacted_runs and compacted_count > 0 and max_passes > 0:
        logger.info(
            "Compaction pass loop finished",
            session_id=session_id,
            compacted_count=compacted_count,
            remaining_runs=len(working_compacted_runs),
            max_passes=max_passes,
        )

    return _CompactionProgress(
        summary=working_summary,
        remaining_compacted_runs=working_compacted_runs,
        compacted_count=compacted_count,
        budget_exhausted_on_first_pass=budget_exhausted_on_first_pass,
    )


async def _generate_compaction_summary(
    *,
    model: Model,
    summary_input: str,
) -> SessionSummary:
    response = await model.aresponse(
        messages=[
            Message(role="system", content=_COMPACTION_SUMMARY_PROMPT),
            Message(role="user", content=summary_input),
        ],
    )
    raw_text = response.content if isinstance(response.content, str) else ""
    raw_text = _normalize_compaction_summary_text(raw_text)
    if not raw_text:
        msg = "summary generation returned no result"
        raise RuntimeError(msg)
    return SessionSummary(summary=raw_text, topics=[], updated_at=datetime.now(UTC))


def _normalize_compaction_summary_text(raw_text: str) -> str:
    """Accept plain text and tolerate legacy fenced-JSON summary wrappers."""
    normalized = raw_text.strip()
    if not normalized:
        return ""
    if normalized.startswith("```") and normalized.endswith("```"):
        first_newline = normalized.find("\n")
        if first_newline != -1:
            normalized = normalized[first_newline + 1 : -3].strip()
    if normalized.startswith("{"):
        try:
            payload = json.loads(normalized)
        except json.JSONDecodeError:
            return normalized
        summary = payload.get("summary") if isinstance(payload, dict) else None
        if isinstance(summary, str):
            return summary.strip()
    return normalized


def _estimate_serialized_run_tokens(run: RunOutput | TeamRunOutput) -> int:
    """Estimate token count for the XML serialization of a single run."""
    return estimate_text_tokens(_serialize_run(run, 0))


def _build_summary_input(
    *,
    previous_summary: SessionSummary | None,
    compacted_runs: Sequence[RunOutput | TeamRunOutput],
    max_input_tokens: int | None = None,
) -> tuple[str, list[RunOutput | TeamRunOutput], bool]:
    """Build the summary prompt payload, selecting a contiguous prefix of runs.

    When *max_input_tokens* is given, runs are walked oldest-to-newest and
    included as long as they fit within the remaining token budget.  When a run
    does not fit, selection **stops** — no later runs are included.  This
    guarantees the summary always covers a contiguous prefix of history.

    In the multi-pass flow, this helper is called once per pass. Each call
    selects only the next contiguous prefix from the still-uncompacted runs,
    while the prior pass's merged summary is carried forward in
    ``previous_summary``.

    If the previous summary alone exhausts the budget, it is truncated to
    leave room for at least one run.

    Returns ``(summary_input_string, included_runs, budget_exhausted)``.
    *budget_exhausted* is ``True`` when candidate runs exist but none could
    fit after accounting for the summary and overhead.
    """
    # Build previous summary block first and subtract from budget
    summary_block = ""
    if previous_summary is not None and previous_summary.summary.strip():
        escaped_summary = _escape_xml_content(previous_summary.summary)
        summary_block = f"<previous_summary>\n{escaped_summary}\n</previous_summary>"

    if max_input_tokens is not None:
        remaining = max_input_tokens - estimate_text_tokens(summary_block) - _WRAPPER_OVERHEAD_TOKENS
        if remaining <= 0 and summary_block:
            # Truncate the previous summary to free budget (account for overhead)
            max_summary_tokens = int((max_input_tokens - _WRAPPER_OVERHEAD_TOKENS) * _SUMMARY_TRUNCATION_RATIO)
            max_summary_chars = max(0, max_summary_tokens * 4)
            truncated_text = previous_summary.summary[:max_summary_chars]  # type: ignore[union-attr]
            escaped_truncated = _escape_xml_content(truncated_text)
            summary_block = f"<previous_summary>\n{escaped_truncated}\n</previous_summary>"
            remaining = max_input_tokens - estimate_text_tokens(summary_block) - _WRAPPER_OVERHEAD_TOKENS
            logger.warning(
                "Truncated previous summary to fit compaction budget",
                budget=max_input_tokens,
                original_summary_tokens=estimate_text_tokens(previous_summary.summary),  # type: ignore[union-attr]
                truncated_summary_tokens=estimate_text_tokens(summary_block),
                remaining_after_truncation=remaining,
            )

        if remaining <= 0:
            logger.warning(
                "Compaction budget exhausted: no room for runs after summary and overhead",
                budget=max_input_tokens,
                summary_tokens=estimate_text_tokens(summary_block),
                overhead=_WRAPPER_OVERHEAD_TOKENS,
                candidate_runs=len(compacted_runs),
            )
            return summary_block, [], True

        # Walk oldest-to-newest, stop at first run that doesn't fit
        included_runs: list[RunOutput | TeamRunOutput] = []
        stopped_count = 0
        for run in compacted_runs:
            run_tokens = _estimate_serialized_run_tokens(run)
            if run_tokens <= remaining:
                included_runs.append(run)
                remaining -= run_tokens
            else:
                stopped_count = len(compacted_runs) - len(included_runs)
                break

        if stopped_count > 0:
            logger.info(
                "Compaction input budget: stopped at oversized run",
                budget=max_input_tokens,
                included=len(included_runs),
                stopped_at_index=len(included_runs),
                remaining_candidates=stopped_count,
                remaining_tokens=remaining,
            )
    else:
        included_runs = list(compacted_runs)

    if not included_runs:
        logger.warning(
            "Compaction budget exhausted: first candidate run exceeds remaining budget",
            budget=max_input_tokens,
            candidate_runs=len(compacted_runs),
            first_run_tokens=_estimate_serialized_run_tokens(compacted_runs[0]) if compacted_runs else 0,
        )
        return summary_block, [], bool(compacted_runs)

    serialized_runs = "\n\n".join(_serialize_run(run, index) for index, run in enumerate(included_runs))
    parts: list[str] = []
    if summary_block:
        parts.append(summary_block)
    parts.append(f"<new_conversation>\n{serialized_runs}\n</new_conversation>")
    return "\n\n".join(parts), included_runs, False


def _serialize_run(run: RunOutput | TeamRunOutput, index: int) -> str:
    attrs = [f'index="{index}"']
    if run.run_id:
        attrs.append(f'run_id="{escape(str(run.run_id), quote=True)}"')
    if run.status is not None:
        attrs.append(f'status="{escape(str(run.status), quote=True)}"')
    lines = [f"<run {' '.join(attrs)}>"]
    if run.metadata:
        lines.extend(
            [
                "<run_metadata>",
                _escape_xml_content(_stable_serialize(run.metadata)),
                "</run_metadata>",
            ],
        )
    for message in run.messages or []:
        lines.extend(_serialize_message(message))
    lines.append("</run>")
    return "\n".join(lines)


def _serialize_message(message: Message) -> list[str]:
    attrs = [f'role="{escape(message.role, quote=True)}"']
    if message.name:
        attrs.append(f'name="{escape(message.name, quote=True)}"')
    if message.tool_call_id:
        attrs.append(f'tool_call_id="{escape(message.tool_call_id, quote=True)}"')
    content = _escape_xml_content(_render_message_content(message))
    lines = [
        f"<message {' '.join(attrs)}>",
        content,
        "</message>",
    ]
    if message.tool_calls:
        lines.extend(
            [
                "<tool_calls>",
                _escape_xml_content(_stable_serialize(message.tool_calls)),
                "</tool_calls>",
            ],
        )
    return lines


def _render_message_content(message: Message) -> str:
    content = message.compressed_content if message.compressed_content is not None else message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_stable_serialize(part) for part in content)
    if content is None:
        return ""
    return _stable_serialize(content)


def _unescape_xml_content(text: str) -> str:
    """Reverse XML entity escaping so re-escaping is idempotent."""
    return text.replace("&gt;", ">").replace("&lt;", "<").replace("&amp;", "&")


def _escape_xml_content(text: str) -> str:
    """Escape XML-like delimiters so untrusted content cannot inject fake tags.

    Unescapes first so that repeated calls are idempotent — prevents
    progressive ``&amp;amp;`` build-up across compaction cycles.
    """
    return escape(_unescape_xml_content(text), quote=False)


def _build_compaction_outcome(
    *,
    before_runs: Sequence[RunOutput | TeamRunOutput],
    before_summary: SessionSummary | None,
    after_runs: Sequence[RunOutput | TeamRunOutput],
    new_summary: SessionSummary,
    mode: str,
    summary_model: str,
    window_tokens: int,
    threshold_tokens: int,
    reserve_tokens: int,
    keep_recent_tokens: int,
    notify: bool,
    count_pending_run: bool = False,
) -> CompactionOutcome:
    before_tokens = estimate_runs_tokens(before_runs) + estimate_text_tokens(
        before_summary.summary if before_summary else "",
    )
    after_tokens = estimate_runs_tokens(after_runs) + estimate_text_tokens(new_summary.summary)
    compacted_at = _iso_utc_now()
    runs_after = len(after_runs) + (1 if count_pending_run else 0)
    return CompactionOutcome(
        mode=mode,
        summary=new_summary.summary,
        topics=list(new_summary.topics or []),
        summary_model=summary_model,
        before_tokens=before_tokens,
        after_tokens=after_tokens,
        window_tokens=window_tokens,
        threshold_tokens=threshold_tokens,
        reserve_tokens=reserve_tokens,
        keep_recent_tokens=keep_recent_tokens,
        runs_before=len(before_runs),
        runs_after=runs_after,
        compacted_run_count=len(before_runs) - len(after_runs),
        compacted_at=compacted_at,
        notify=notify,
    )


def _merge_seen_event_ids(
    *,
    session_metadata: dict[str, Any] | None,
    removed_runs: Sequence[RunOutput | TeamRunOutput],
) -> set[str]:
    seen_event_ids = _existing_compaction_seen_event_ids(session_metadata)
    for run in removed_runs:
        metadata = run.metadata
        if not isinstance(metadata, dict):
            continue
        seen_ids = metadata.get("matrix_seen_event_ids")
        if isinstance(seen_ids, list):
            seen_event_ids.update(str(event_id) for event_id in seen_ids if isinstance(event_id, str))
    return seen_event_ids


def _existing_compaction_seen_event_ids(metadata: dict[str, Any] | None) -> set[str]:
    if not isinstance(metadata, dict):
        return set()
    compaction_metadata = metadata.get(MINDROOM_COMPACTION_METADATA_KEY)
    if not isinstance(compaction_metadata, dict):
        return set()
    seen_ids = compaction_metadata.get("seen_event_ids")
    if not isinstance(seen_ids, list):
        return set()
    return {str(event_id) for event_id in seen_ids if isinstance(event_id, str)}


def _model_identifier(model: Model) -> str:
    if model.id:
        return model.id
    return model.__class__.__name__


def _get_session_lock(storage: SqliteDb, session_id: str) -> asyncio.Lock:
    key = (_storage_identity(storage), session_id)
    lock = _SESSION_COMPACTION_LOCKS.get(key)
    if lock is None:
        if len(_SESSION_COMPACTION_LOCKS) > 100:
            # Evict unlocked entries only — never drop a lock held by an active compaction
            to_remove = []
            for existing_key, existing_lock in list(_SESSION_COMPACTION_LOCKS.items()):
                if not existing_lock.locked():
                    to_remove.append(existing_key)
                if len(_SESSION_COMPACTION_LOCKS) - len(to_remove) <= 100:
                    break
            for existing_key in to_remove:
                _SESSION_COMPACTION_LOCKS.pop(existing_key, None)
        lock = asyncio.Lock()
        _SESSION_COMPACTION_LOCKS[key] = lock
    return lock


def _storage_identity(storage: SqliteDb) -> str:
    if storage.db_file:
        return str(Path(storage.db_file).resolve())
    if storage.db_url:
        return storage.db_url
    return str(storage.id)


def _iso_utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def scrub_history_messages_from_sessions(storage: SqliteDb) -> HistoryScrubStats:
    """Remove redundant ``from_history`` messages from every stored session.

    .. warning::

        This function performs unsynchronized read-modify-write operations on
        each session.  It must only be called while the MindRoom service is
        **stopped** (offline) to avoid silently overwriting concurrent agent
        writes.
    """
    raw_sessions = storage.get_sessions(session_type=SessionType.AGENT)
    sessions = raw_sessions[0] if isinstance(raw_sessions, tuple) else raw_sessions

    sessions_scanned = 0
    sessions_changed = 0
    messages_removed = 0
    size_before_bytes = 0
    size_after_bytes = 0

    for raw_session in sessions:
        session_stub = _coerce_agent_session(raw_session)
        session_id = session_stub.session_id
        if not isinstance(session_id, str):
            continue
        session = _get_agent_session(storage, session_id)
        if session is None:
            continue

        sessions_scanned += 1
        size_before_bytes += _serialized_session_size(session)

        session_changed = False
        for run in session.runs or []:
            if not isinstance(run, (RunOutput, TeamRunOutput)) or not run.messages:
                continue
            kept_messages = [message for message in run.messages if not message.from_history]
            removed_count = len(run.messages) - len(kept_messages)
            if removed_count == 0:
                continue
            run.messages = kept_messages
            messages_removed += removed_count
            session_changed = True

        size_after_bytes += _serialized_session_size(session)
        if not session_changed:
            continue

        sessions_changed += 1
        storage.upsert_session(session)

    stats = HistoryScrubStats(
        sessions_scanned=sessions_scanned,
        sessions_changed=sessions_changed,
        messages_removed=messages_removed,
        size_before_bytes=size_before_bytes,
        size_after_bytes=size_after_bytes,
    )
    logger.info(
        "Scrubbed history messages from stored sessions",
        sessions=sessions_scanned,
        changed_sessions=sessions_changed,
        messages_removed=messages_removed,
        size_before_bytes=size_before_bytes,
        size_after_bytes=size_after_bytes,
    )
    return stats


def _coerce_agent_session(raw_session: object) -> AgentSession:
    if isinstance(raw_session, AgentSession):
        return raw_session
    if isinstance(raw_session, dict):
        session = AgentSession.from_dict(cast("dict[str, Any]", raw_session))
        if session is not None:
            return session
    msg = f"Unsupported session payload: {type(raw_session).__name__}"
    raise TypeError(msg)


def _serialized_session_size(session: AgentSession) -> int:
    return len(
        json.dumps(
            session.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8"),
    )
