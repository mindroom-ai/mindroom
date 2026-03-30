"""Single-pass scoped compaction."""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from html import escape
from typing import TYPE_CHECKING, cast

from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.summary import SessionSummary
from pydantic import BaseModel

from mindroom.history.replay import estimate_history_messages_tokens
from mindroom.history.storage import write_scope_state
from mindroom.history.types import CompactionOutcome, CompactionState, HistoryScope
from mindroom.logging_config import get_logger
from mindroom.token_budget import compute_compaction_input_budget, estimate_text_tokens, stable_serialize

if TYPE_CHECKING:
    from agno.agent import Agent
    from agno.db.sqlite import SqliteDb
    from agno.models.base import Model

    from mindroom.config.main import Config
    from mindroom.config.models import CompactionConfig
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

_WRAPPER_OVERHEAD_TOKENS = 200
_SUMMARY_TRUNCATION_RATIO = 0.5
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


async def compact_scope_history(
    *,
    storage: SqliteDb,
    session: AgentSession,
    scope: HistoryScope,
    state: CompactionState,
    visible_runs: list[RunOutput | TeamRunOutput],
    agent: Agent,
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> tuple[CompactionState, CompactionOutcome | None]:
    """Compact the oldest visible prefix for one scope in a single summary pass."""
    compactable_runs = visible_runs[:-2]
    cleared_state = replace(state, force_compact_before_next_run=False)
    if not compactable_runs:
        return cleared_state, None

    compaction_config = config.get_agent_compaction_config(agent_name)
    summary_model, compaction_model_context_window = resolve_compaction_model(
        config=config,
        runtime_paths=runtime_paths,
        agent_name=agent_name,
        compaction_config=compaction_config,
    )
    active_context_window = _active_context_window(config, agent_name)
    effective_window = compaction_model_context_window or active_context_window or 0
    reserve_tokens = normalize_compaction_budget_tokens(
        compaction_config.reserve_tokens,
        effective_window or None,
    )
    summary_input_budget = compute_compaction_input_budget(
        effective_window,
        reserve_tokens=reserve_tokens,
    )
    if summary_input_budget <= 0:
        logger.warning(
            "Compaction budget is non-positive; skipping compaction",
            session_id=session.session_id,
            scope=scope.key,
            effective_window=effective_window,
            reserve_tokens=reserve_tokens,
        )
        return cleared_state, None

    summary_input, included_runs = _build_summary_input(
        previous_summary=state.summary,
        compacted_runs=compactable_runs,
        max_input_tokens=summary_input_budget,
    )
    if not included_runs:
        logger.warning(
            "Compaction skipped because no run fit the single-pass summary budget",
            session_id=session.session_id,
            scope=scope.key,
            candidate_runs=len(compactable_runs),
            summary_input_budget=summary_input_budget,
        )
        return cleared_state, None

    new_summary = await _generate_compaction_summary(model=summary_model, summary_input=summary_input)
    compacted_at = _iso_utc_now()
    last_compacted_run_id = _require_last_compacted_run_id(included_runs)
    if last_compacted_run_id is None:
        return cleared_state, None

    new_state = CompactionState(
        summary=new_summary.summary,
        last_compacted_run_id=last_compacted_run_id,
        compacted_at=compacted_at,
        summary_model=_model_identifier(summary_model),
        force_compact_before_next_run=False,
    )
    write_scope_state(session, scope, new_state)
    storage.upsert_session(session)

    active_window = active_context_window or 0
    threshold_tokens = resolve_effective_compaction_threshold(compaction_config, active_window) if active_window else 0
    outcome = _build_compaction_outcome(
        before_visible_runs=visible_runs,
        before_summary=state.summary,
        after_visible_runs=_runs_after_run_id(visible_runs, last_compacted_run_id),
        new_summary=new_summary.summary,
        mode="manual" if state.force_compact_before_next_run else "auto",
        summary_model=_model_identifier(summary_model),
        window_tokens=active_window,
        threshold_tokens=threshold_tokens,
        reserve_tokens=compaction_config.reserve_tokens,
        last_compacted_run_id=last_compacted_run_id,
        notify=compaction_config.notify,
    )
    return new_state, outcome


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
    agent_name: str,
    compaction_config: CompactionConfig,
) -> tuple[Model, int | None]:
    """Resolve the summary model used for single-pass compaction."""
    from mindroom.ai import get_model_instance  # noqa: PLC0415

    model_name = compaction_config.model or config.get_entity_model_name(agent_name)
    model = get_model_instance(config, runtime_paths, model_name)
    model_config = config.models.get(model_name)
    context_window = model_config.context_window if model_config and model_config.context_window else None
    return model, context_window


def _active_context_window(config: Config, agent_name: str) -> int | None:
    model_name = config.get_entity_model_name(agent_name)
    model_config = config.models.get(model_name)
    return model_config.context_window if model_config and model_config.context_window else None


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
        return summary_block, []

    included_runs: list[RunOutput | TeamRunOutput] = []
    for run in compacted_runs:
        run_tokens = _estimate_serialized_run_tokens(run)
        if run_tokens > remaining:
            break
        included_runs.append(run)
        remaining -= run_tokens

    if not included_runs:
        return summary_block, []

    serialized_runs = "\n\n".join(_serialize_run(run, index) for index, run in enumerate(included_runs))
    parts: list[str] = []
    if summary_block:
        parts.append(summary_block)
    parts.append(f"<new_conversation>\n{serialized_runs}\n</new_conversation>")
    return "\n\n".join(parts), included_runs


def _estimate_serialized_run_tokens(run: RunOutput | TeamRunOutput) -> int:
    return estimate_text_tokens(_serialize_run(run, 0))


def _serialize_run(run: RunOutput | TeamRunOutput, index: int) -> str:
    attrs = [f'index="{index}"']
    if run.run_id:
        attrs.append(f'run_id="{escape(str(run.run_id), quote=True)}"')
    if run.status is not None:
        attrs.append(f'status="{escape(str(run.status), quote=True)}"')
    lines = [f"<run {' '.join(attrs)}>"]
    if run.metadata:
        lines.extend(["<run_metadata>", _escape_xml_content(stable_serialize(run.metadata)), "</run_metadata>"])
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
    lines = [f"<message {' '.join(attrs)}>", _escape_xml_content(_render_message_content(message)), "</message>"]
    if message.tool_calls:
        lines.extend(["<tool_calls>", _escape_xml_content(stable_serialize(message.tool_calls)), "</tool_calls>"])
    for tag, media_value in _message_media_entries(message):
        serialized = _serialize_media_payload(media_value)
        if not serialized:
            continue
        lines.extend([f"<{tag}>", _escape_xml_content(serialized), f"</{tag}>"])
    return lines


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


def _require_last_compacted_run_id(compacted_runs: Sequence[RunOutput | TeamRunOutput]) -> str | None:
    if not compacted_runs:
        return None
    last_run_id = compacted_runs[-1].run_id
    if isinstance(last_run_id, str) and last_run_id:
        return last_run_id
    return None


def _runs_after_run_id(
    runs: Sequence[RunOutput | TeamRunOutput],
    after_run_id: str | None,
) -> list[RunOutput | TeamRunOutput]:
    if after_run_id is None:
        return list(runs)
    after_index = next((index for index, run in enumerate(runs) if run.run_id == after_run_id), None)
    if after_index is None:
        return list(runs)
    return list(runs[after_index + 1 :])


def _estimate_runs_tokens(runs: Sequence[RunOutput | TeamRunOutput]) -> int:
    total = 0
    for run in runs:
        messages = run.messages or []
        total += estimate_history_messages_tokens([deepcopy(message) for message in messages])
    return total


def _build_compaction_outcome(
    *,
    before_visible_runs: Sequence[RunOutput | TeamRunOutput],
    before_summary: str | None,
    after_visible_runs: Sequence[RunOutput | TeamRunOutput],
    new_summary: str,
    mode: str,
    summary_model: str,
    window_tokens: int,
    threshold_tokens: int,
    reserve_tokens: int,
    last_compacted_run_id: str | None,
    notify: bool,
) -> CompactionOutcome:
    before_tokens = _estimate_runs_tokens(before_visible_runs) + estimate_text_tokens(before_summary)
    after_tokens = _estimate_runs_tokens(after_visible_runs) + estimate_text_tokens(new_summary)
    return CompactionOutcome(
        mode=cast("str", mode),
        summary=new_summary,
        summary_model=summary_model,
        before_tokens=before_tokens,
        after_tokens=after_tokens,
        window_tokens=window_tokens,
        threshold_tokens=threshold_tokens,
        reserve_tokens=reserve_tokens,
        runs_before=len(before_visible_runs),
        runs_after=len(after_visible_runs),
        compacted_run_count=len(before_visible_runs) - len(after_visible_runs),
        last_compacted_run_id=last_compacted_run_id,
        compacted_at=_iso_utc_now(),
        notify=notify,
    )


def _model_identifier(model: Model) -> str:
    return model.id or model.__class__.__name__


def _iso_utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
