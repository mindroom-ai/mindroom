"""Scoped compaction."""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from html import escape
from typing import TYPE_CHECKING, cast

from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.team import TeamRunOutput
from agno.session.summary import SessionSummary
from agno.session.team import TeamSession
from agno.utils.message import filter_tool_calls
from pydantic import BaseModel

from mindroom.history.storage import write_scope_state
from mindroom.history.types import CompactionOutcome, HistoryScope, HistoryScopeState
from mindroom.logging_config import get_logger
from mindroom.token_budget import compute_compaction_input_budget, estimate_text_tokens, stable_serialize

if TYPE_CHECKING:
    from agno.agent import Agent
    from agno.db.sqlite import SqliteDb
    from agno.models.base import Model
    from agno.session.agent import AgentSession
    from agno.team import Team

    from mindroom.config.main import Config
    from mindroom.config.models import CompactionConfig
    from mindroom.constants import RuntimePaths
    from mindroom.history.types import ResolvedHistorySettings

logger = get_logger(__name__)

_WRAPPER_OVERHEAD_TOKENS = 200
_SUMMARY_TRUNCATION_RATIO = 0.5
_OVERSIZED_RUN_NOTE = "Run truncated to fit compaction budget."
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
class ResolvedCompactionRuntime:
    """Resolved model/window inputs needed for one compaction attempt."""

    model_name: str
    context_window: int | None


@dataclass(frozen=True)
class _CompactionRewriteResult:
    summary_text: str
    compacted_run_count: int


async def compact_scope_history(
    *,
    storage: SqliteDb,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    state: HistoryScopeState,
    config: Config,
    runtime_paths: RuntimePaths,
    compaction_config: CompactionConfig,
    history_settings: ResolvedHistorySettings,
    available_history_budget: int | None,
    active_model_name: str,
    active_context_window: int | None,
) -> tuple[HistoryScopeState, CompactionOutcome | None]:
    """Compact one scope by rewriting session.summary and session.runs."""
    visible_runs = _runs_for_scope(_completed_top_level_runs(session), scope)
    compactable_runs = _select_runs_to_compact(
        visible_runs=visible_runs,
        session=session,
        scope=scope,
        state=state,
        history_settings=history_settings,
        available_history_budget=available_history_budget,
    )
    if not compactable_runs:
        cleared_state = replace(state, force_compact_before_next_run=False)
        if cleared_state != state:
            write_scope_state(session, scope, cleared_state)
            storage.upsert_session(session)
        return cleared_state, None

    summary_model, effective_window = resolve_compaction_model(
        config=config,
        runtime_paths=runtime_paths,
        compaction_config=compaction_config,
        active_model_name=active_model_name,
        active_context_window=active_context_window,
    )
    window_tokens = effective_window or 0
    reserve_tokens = normalize_compaction_budget_tokens(
        compaction_config.reserve_tokens,
        window_tokens or None,
    )
    summary_input_budget = compute_compaction_input_budget(
        window_tokens,
        reserve_tokens=reserve_tokens,
    )
    if summary_input_budget <= 0:
        logger.warning(
            "Compaction budget is non-positive; skipping compaction",
            session_id=session.session_id,
            scope=scope.key,
            effective_window=window_tokens,
            reserve_tokens=reserve_tokens,
        )
        return _clear_force_flag(storage=storage, session=session, scope=scope, state=state), None

    before_tokens = estimate_prompt_visible_history_tokens(
        session=session,
        scope=scope,
        history_settings=history_settings,
    )
    before_run_count = len(visible_runs)
    working_session = deepcopy(session)
    rewrite_result = await _rewrite_working_session_for_compaction(
        working_session=working_session,
        summary_model=summary_model,
        session_id=session.session_id,
        scope=scope,
        state=state,
        history_settings=history_settings,
        available_history_budget=available_history_budget,
        summary_input_budget=summary_input_budget,
    )
    if rewrite_result is None:
        return _clear_force_flag(storage=storage, session=session, scope=scope, state=state), None

    compacted_at = _iso_utc_now()
    new_state = HistoryScopeState(
        last_compacted_at=compacted_at,
        last_summary_model=_model_identifier(summary_model),
        last_compacted_run_count=rewrite_result.compacted_run_count,
        force_compact_before_next_run=False,
    )
    session.summary = working_session.summary
    session.runs = working_session.runs
    write_scope_state(session, scope, new_state)
    storage.upsert_session(session)

    after_visible_runs = _runs_for_scope(_completed_top_level_runs(session), scope)
    after_tokens = estimate_prompt_visible_history_tokens(
        session=session,
        scope=scope,
        history_settings=history_settings,
    )
    active_window = active_context_window or 0
    threshold_tokens = resolve_effective_compaction_threshold(compaction_config, active_window) if active_window else 0
    outcome = CompactionOutcome(
        mode="manual" if state.force_compact_before_next_run else "auto",
        summary=rewrite_result.summary_text,
        summary_model=_model_identifier(summary_model),
        before_tokens=before_tokens,
        after_tokens=after_tokens,
        window_tokens=active_window,
        threshold_tokens=threshold_tokens,
        reserve_tokens=compaction_config.reserve_tokens,
        runs_before=before_run_count,
        runs_after=len(after_visible_runs),
        compacted_run_count=rewrite_result.compacted_run_count,
        compacted_at=compacted_at,
        notify=compaction_config.notify,
    )
    return new_state, outcome


async def _rewrite_working_session_for_compaction(
    *,
    working_session: AgentSession | TeamSession,
    summary_model: Model,
    session_id: str,
    scope: HistoryScope,
    state: HistoryScopeState,
    history_settings: ResolvedHistorySettings,
    available_history_budget: int | None,
    summary_input_budget: int,
) -> _CompactionRewriteResult | None:
    final_summary_text = _current_summary_text(working_session) or ""
    total_compacted_run_count = 0

    while True:
        working_visible_runs = _runs_for_scope(_completed_top_level_runs(working_session), scope)
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
        if not compactable_runs:
            break

        summary_input, included_runs = _build_summary_input(
            previous_summary=_current_summary_text(working_session),
            compacted_runs=compactable_runs,
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

        new_summary = await _generate_compaction_summary(model=summary_model, summary_input=summary_input)
        final_summary_text = new_summary.summary
        compacted_run_ids = {run.run_id for run in included_runs if isinstance(run.run_id, str) and run.run_id}
        working_session.summary = SessionSummary(summary=new_summary.summary, updated_at=datetime.now(UTC))
        working_session.runs = _remove_runs_by_id(working_session.runs or [], compacted_run_ids)
        total_compacted_run_count += len(included_runs)

        if available_history_budget is None:
            break

        after_tokens = estimate_prompt_visible_history_tokens(
            session=working_session,
            scope=scope,
            history_settings=history_settings,
        )
        if after_tokens <= available_history_budget:
            break
        if len(_runs_for_scope(_completed_top_level_runs(working_session), scope)) <= 1:
            break

    if total_compacted_run_count == 0:
        return None
    return _CompactionRewriteResult(
        summary_text=final_summary_text,
        compacted_run_count=total_compacted_run_count,
    )


def estimate_static_tokens(agent: Agent, full_prompt: str) -> int:
    """Estimate system and current-user prompt tokens outside persisted replay."""
    static_chars = len(agent.role or "")
    instructions = agent.instructions
    if isinstance(instructions, str):
        static_chars += len(instructions)
    elif isinstance(instructions, list):
        for instruction in instructions:
            static_chars += len(str(instruction))
    static_chars += len(full_prompt)
    return static_chars // 4


def estimate_team_static_tokens(team: Team, full_prompt: str) -> int:
    """Estimate the non-history team prompt using Agno's team system-message builder."""
    static_tokens = estimate_text_tokens(full_prompt)
    system_message = team.get_system_message(
        session=TeamSession(session_id="history-budget", team_id=team.id),
        add_session_state_to_context=False,
    )
    if system_message is None or system_message.content is None:
        return static_tokens
    return static_tokens + estimate_text_tokens(str(system_message.content))


def resolve_effective_compaction_threshold(compaction_config: CompactionConfig, context_window: int) -> int:
    """Resolve the absolute token threshold that should trigger auto-compaction."""
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


def resolve_compaction_model(
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    compaction_config: CompactionConfig,
    active_model_name: str,
    active_context_window: int | None,
) -> tuple[Model, int | None]:
    """Resolve the summary model used for single-pass compaction."""
    from mindroom.ai import get_model_instance  # noqa: PLC0415

    runtime = resolve_compaction_runtime_settings(
        config=config,
        compaction_config=compaction_config,
        active_model_name=active_model_name,
        active_context_window=active_context_window,
    )
    model = get_model_instance(config, runtime_paths, runtime.model_name)
    return model, runtime.context_window


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
    return ResolvedCompactionRuntime(
        model_name=model_name,
        context_window=model_context_window or active_context_window,
    )


async def _generate_compaction_summary(*, model: Model, summary_input: str) -> SessionSummary:
    response = await model.aresponse(
        messages=[
            Message(role="system", content=_COMPACTION_SUMMARY_PROMPT),
            Message(role="user", content=summary_input),
        ],
    )
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


def _build_summary_input(
    *,
    previous_summary: str | None,
    compacted_runs: Sequence[RunOutput | TeamRunOutput],
    max_input_tokens: int,
) -> tuple[str, list[RunOutput | TeamRunOutput]]:
    summary_block = ""
    if previous_summary is not None and previous_summary.strip():
        escaped_summary = _escape_xml_content(previous_summary)
        summary_block = f"<previous_summary>\n{escaped_summary}\n</previous_summary>"

    remaining = max_input_tokens - estimate_text_tokens(summary_block) - _WRAPPER_OVERHEAD_TOKENS
    if remaining <= 0 and summary_block:
        max_summary_tokens = int((max_input_tokens - _WRAPPER_OVERHEAD_TOKENS) * _SUMMARY_TRUNCATION_RATIO)
        max_summary_chars = max(0, max_summary_tokens * 4)
        truncated_summary = previous_summary[:max_summary_chars] if previous_summary is not None else ""
        escaped_summary = _escape_xml_content(truncated_summary)
        summary_block = f"<previous_summary>\n{escaped_summary}\n</previous_summary>"
        remaining = max_input_tokens - estimate_text_tokens(summary_block) - _WRAPPER_OVERHEAD_TOKENS

    if remaining <= 0:
        return _build_oversized_summary_input(
            summary_block=summary_block,
            compacted_runs=compacted_runs,
            max_input_tokens=max_input_tokens,
        )

    included_runs: list[RunOutput | TeamRunOutput] = []
    for run in compacted_runs:
        run_tokens = _estimate_serialized_run_tokens(run)
        if run_tokens > remaining:
            if not included_runs:
                return _build_oversized_summary_input(
                    summary_block=summary_block,
                    compacted_runs=[run],
                    max_input_tokens=max_input_tokens,
                )
            break
        included_runs.append(run)
        remaining -= run_tokens

    if not included_runs:
        return summary_block, []

    serialized_runs = "\n\n".join(_serialize_run(run, index) for index, run in enumerate(included_runs))
    return _compose_summary_input(summary_block, serialized_runs), included_runs


def _build_oversized_summary_input(
    *,
    summary_block: str,
    compacted_runs: Sequence[RunOutput | TeamRunOutput],
    max_input_tokens: int,
) -> tuple[str, list[RunOutput | TeamRunOutput]]:
    if not compacted_runs:
        return summary_block, []
    first_run = compacted_runs[0]
    oversized_excerpt = _serialize_oversized_run_excerpt(
        first_run,
        index=0,
        max_tokens=_remaining_excerpt_budget(max_input_tokens, summary_block),
    )
    if oversized_excerpt is None and summary_block:
        summary_block = ""
        oversized_excerpt = _serialize_oversized_run_excerpt(
            first_run,
            index=0,
            max_tokens=_remaining_excerpt_budget(max_input_tokens, summary_block),
        )
    if oversized_excerpt is None:
        return summary_block, []
    return _compose_summary_input(summary_block, oversized_excerpt), [first_run]


def _serialize_oversized_run_excerpt(
    run: RunOutput | TeamRunOutput,
    *,
    index: int,
    max_tokens: int,
) -> str | None:
    if max_tokens <= 0:
        return None

    full_run = _serialize_run(run, index)
    if estimate_text_tokens(full_run) <= max_tokens:
        return full_run

    blocks = _excerpt_blocks(run)
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


def _excerpt_blocks(run: RunOutput | TeamRunOutput) -> list[_ExcerptBlock]:
    blocks: list[_ExcerptBlock] = []
    if run.metadata:
        blocks.append(_ExcerptBlock("<run_metadata>", stable_serialize(run.metadata), "</run_metadata>"))
    for message in run.messages or []:
        content = _render_message_content(message)
        if not content:
            continue
        blocks.append(_ExcerptBlock(_message_open_tag(message), content, "</message>"))
    return blocks


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


def _estimate_serialized_run_tokens(run: RunOutput | TeamRunOutput) -> int:
    return estimate_text_tokens(_serialize_run(run, 0))


def _serialize_run(run: RunOutput | TeamRunOutput, index: int) -> str:
    lines = [_run_open_tag(run, index)]
    if run.metadata:
        lines.extend(["<run_metadata>", _escape_xml_content(stable_serialize(run.metadata)), "</run_metadata>"])
    for message in run.messages or []:
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
    """Estimate the persisted summary plus raw history Agno would replay for one run."""
    summary_tokens = _estimate_session_summary_tokens(session.summary.summary if session.summary is not None else None)
    history_messages = _history_messages_for_session(
        session=session,
        scope=scope,
        history_settings=history_settings,
    )
    return summary_tokens + estimate_history_messages_tokens(history_messages)


def estimate_history_messages_tokens(messages: list[Message]) -> int:
    """Estimate the token count of materialized history messages."""
    if not messages:
        return 0
    total_chars = 0
    for message in messages:
        total_chars += len(_render_message_content(message))
        if message.tool_calls:
            total_chars += len(stable_serialize(message.tool_calls))
        total_chars += _estimate_message_media_chars(message)
    return total_chars // 4


def _select_runs_to_compact(
    *,
    visible_runs: list[RunOutput | TeamRunOutput],
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    state: HistoryScopeState,
    history_settings: ResolvedHistorySettings,
    available_history_budget: int | None,
) -> list[RunOutput | TeamRunOutput]:
    if len(visible_runs) <= 1:
        return []

    if state.force_compact_before_next_run:
        return visible_runs[:-2] if len(visible_runs) > 2 else []

    if available_history_budget is None:
        return []

    current_tokens = estimate_prompt_visible_history_tokens(
        session=session,
        scope=scope,
        history_settings=history_settings,
    )
    if current_tokens <= available_history_budget:
        return []

    if len(visible_runs) > 2:
        keep_two = visible_runs[-2:]
        projected_two_tokens = _project_remaining_history_tokens(
            session=session,
            scope=scope,
            remaining_runs=keep_two,
            history_settings=history_settings,
        )
        if projected_two_tokens <= available_history_budget:
            return visible_runs[:-2]

    return visible_runs[:-1]


def _project_remaining_history_tokens(
    *,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    remaining_runs: Sequence[RunOutput | TeamRunOutput],
    history_settings: ResolvedHistorySettings,
) -> int:
    projected_session = replace(session, runs=list(remaining_runs))
    history_messages = _history_messages_for_session(
        session=projected_session,
        scope=scope,
        history_settings=history_settings,
    )
    return _estimate_session_summary_tokens(_current_summary_text(session)) + estimate_history_messages_tokens(
        history_messages,
    )


def _history_messages_for_session(
    *,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    history_settings: ResolvedHistorySettings,
) -> list[Message]:
    history_messages = [
        deepcopy(message)
        for message in _session_history_messages(
            session=session,
            scope=scope,
            history_settings=history_settings,
        )
    ]
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
    if history_settings.policy.mode == "runs":
        return session.get_messages(agent_id=scope_id, last_n_runs=limit)
    if history_settings.policy.mode == "messages":
        return session.get_messages(agent_id=scope_id, limit=limit)
    return session.get_messages(agent_id=scope_id)


def _team_session_history_messages(
    *,
    session: TeamSession,
    scope_id: str,
    history_settings: ResolvedHistorySettings,
    limit: int | None,
) -> list[Message]:
    if history_settings.policy.mode == "runs":
        return session.get_messages(team_id=scope_id, last_n_runs=limit)
    if history_settings.policy.mode == "messages":
        return session.get_messages(team_id=scope_id, limit=limit)
    return session.get_messages(team_id=scope_id)


def _estimate_session_summary_tokens(summary_text: str | None) -> int:
    if summary_text is None or summary_text.strip() == "":
        return 0
    wrapper = (
        "Here is a brief summary of your previous interactions:\n\n"
        "<summary_of_previous_interactions>\n"
        f"{summary_text.strip()}\n"
        "</summary_of_previous_interactions>\n\n"
        "You should ALWAYS prefer information from this conversation over the past summary.\n\n"
    )
    return estimate_text_tokens(wrapper)


def _completed_top_level_runs(session: AgentSession | TeamSession) -> list[RunOutput | TeamRunOutput]:
    skip_statuses = {RunStatus.paused, RunStatus.cancelled, RunStatus.error}
    return [
        run
        for run in session.runs or []
        if isinstance(run, (RunOutput, TeamRunOutput)) and run.parent_run_id is None and run.status not in skip_statuses
    ]


def _runs_for_scope(
    runs: Sequence[RunOutput | TeamRunOutput],
    scope: HistoryScope,
) -> list[RunOutput | TeamRunOutput]:
    if scope.kind == "team":
        return [run for run in runs if isinstance(run, TeamRunOutput) and run.team_id == scope.scope_id]
    return [run for run in runs if isinstance(run, RunOutput) and run.agent_id == scope.scope_id]


def _current_summary_text(session: AgentSession | TeamSession) -> str | None:
    if session.summary is None:
        return None
    summary = session.summary.summary.strip()
    return summary or None


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

    return [run for run in runs if not isinstance(run.run_id, str) or run.run_id not in remove_ids]


def _clear_force_flag(
    *,
    storage: SqliteDb,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    state: HistoryScopeState,
) -> HistoryScopeState:
    if not state.force_compact_before_next_run:
        return state
    cleared_state = replace(state, force_compact_before_next_run=False)
    write_scope_state(session, scope, cleared_state)
    storage.upsert_session(session)
    return cleared_state


def _estimate_message_media_chars(message: Message) -> int:
    media_chars = 0
    for media_value in (
        message.images,
        message.audio,
        message.videos,
        message.files,
        message.audio_output,
        message.image_output,
        message.video_output,
        message.file_output,
    ):
        if media_value is None:
            continue
        media_chars += len(stable_serialize(_media_payload_snapshot(media_value)))
    return media_chars


def _model_identifier(model: Model) -> str:
    return model.id or model.__class__.__name__


def _iso_utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
