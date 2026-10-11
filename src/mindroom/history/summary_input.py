"""Bounded portable-summary serialization of conversation runs and prior summaries."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from html import escape
from typing import TYPE_CHECKING

from agno.utils.message import filter_tool_calls

from mindroom.constants import (
    AI_RUN_METADATA_KEY,
    MINDROOM_COMPACTION_METADATA_KEY,
    MINDROOM_MATRIX_HISTORY_METADATA_KEY,
)
from mindroom.history.claude_replay_compat import strip_stale_anthropic_replay_fields
from mindroom.history.message_content import media_payload_snapshot, message_media_entries, render_message_content
from mindroom.history.replay import history_skip_roles
from mindroom.timing import timed
from mindroom.token_budget import estimate_compaction_input_tokens, stable_serialize

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from agno.models.message import Message
    from agno.run.agent import RunOutput
    from agno.run.team import TeamRunOutput

    from mindroom.history.types import ResolvedHistorySettings


_WRAPPER_OVERHEAD_TOKENS = 200

_OVERSIZED_RUN_NOTE = "Run truncated to fit compaction budget."

_SUMMARY_METADATA_OMIT_KEYS = frozenset(
    {
        AI_RUN_METADATA_KEY,
        MINDROOM_COMPACTION_METADATA_KEY,
        MINDROOM_MATRIX_HISTORY_METADATA_KEY,
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


@timed("system_prompt_assembly.history_prepare.compaction.summary_input_build")
def build_summary_input(
    *,
    previous_summary: str | None,
    compacted_runs: Sequence[RunOutput | TeamRunOutput],
    max_input_tokens: int,
    history_settings: ResolvedHistorySettings,
    token_estimator: Callable[[str], int] = estimate_compaction_input_tokens,
) -> tuple[str, list[RunOutput | TeamRunOutput]]:
    """Fit prior summary and ordered runs into one serialized summary request."""
    summary_block = ""
    if previous_summary is not None and previous_summary.strip():
        summary_block = _previous_summary_block(previous_summary)

    empty_input = _compose_summary_input(summary_block, "")
    remaining = max_input_tokens - token_estimator(empty_input) - _WRAPPER_OVERHEAD_TOKENS

    if remaining <= 0:
        return _build_oversized_summary_input(
            previous_summary=previous_summary,
            compacted_runs=compacted_runs[:1],
            history_settings=history_settings,
            max_input_tokens=max_input_tokens,
            token_estimator=token_estimator,
        )

    included_runs: list[RunOutput | TeamRunOutput] = []
    serialized_runs: list[str] = []
    for index, run in enumerate(compacted_runs):
        serialized_run = _serialize_run(run, index, history_settings)
        separator = "\n\n" if serialized_runs else ""
        run_tokens = token_estimator(f"{separator}{serialized_run}")
        if run_tokens > remaining:
            if not included_runs:
                return _build_oversized_summary_input(
                    previous_summary=previous_summary,
                    compacted_runs=[run],
                    history_settings=history_settings,
                    max_input_tokens=max_input_tokens,
                    token_estimator=token_estimator,
                )
            break
        included_runs.append(run)
        serialized_runs.append(serialized_run)
        remaining -= run_tokens

    if not included_runs:
        return summary_block, []

    return _compose_summary_input(summary_block, "\n\n".join(serialized_runs)), included_runs


def _build_oversized_summary_input(
    *,
    previous_summary: str | None,
    compacted_runs: Sequence[RunOutput | TeamRunOutput],
    history_settings: ResolvedHistorySettings,
    max_input_tokens: int,
    token_estimator: Callable[[str], int],
) -> tuple[str, list[RunOutput | TeamRunOutput]]:
    summary_block = (
        _previous_summary_block(previous_summary) if previous_summary is not None and previous_summary.strip() else ""
    )
    if not compacted_runs:
        return summary_block, []
    first_run = compacted_runs[0]
    oversized_excerpt = _serialize_oversized_run_excerpt(
        first_run,
        index=0,
        history_settings=history_settings,
        max_tokens=_remaining_excerpt_budget(max_input_tokens, summary_block, token_estimator),
        token_estimator=token_estimator,
    )
    if oversized_excerpt is None:
        return summary_block, []
    return _compose_summary_input(summary_block, oversized_excerpt), [first_run]


def minimum_summary_input_tokens(
    *,
    previous_summary: str | None,
    first_run: RunOutput | TeamRunOutput,
    token_estimator: Callable[[str], int],
) -> int:
    """Return the smallest shrink budget preserving the prior summary and one run envelope.

    Below this size ``build_summary_input`` rebuilds to a run-less input
    because the previous-summary block alone swallows the envelope, so
    ``SummaryRetryPolicy`` clamps shrink targets here. A zero content budget
    renders the run as its open tag, truncation note, and close tag; the
    wrapper overhead covers the builder's own envelope accounting and
    tokenizer boundary effects.
    """
    summary_block = (
        _previous_summary_block(previous_summary) if previous_summary is not None and previous_summary.strip() else ""
    )
    minimal_excerpt = _serialize_run_excerpt(first_run, index=0, blocks=(), content_budget_chars=0)
    return token_estimator(_compose_summary_input(summary_block, minimal_excerpt)) + _WRAPPER_OVERHEAD_TOKENS


def _serialize_oversized_run_excerpt(
    run: RunOutput | TeamRunOutput,
    *,
    index: int,
    history_settings: ResolvedHistorySettings,
    max_tokens: int,
    token_estimator: Callable[[str], int],
) -> str | None:
    if max_tokens <= 0:
        return None

    full_run = _serialize_run(run, index, history_settings)
    if token_estimator(full_run) <= max_tokens:
        return full_run

    blocks = _excerpt_blocks(run, history_settings)
    budget_chars = max_tokens * 4
    while budget_chars > 0:
        excerpt = _serialize_run_excerpt(run, index=index, blocks=blocks, content_budget_chars=budget_chars)
        if token_estimator(excerpt) <= max_tokens:
            return excerpt
        budget_chars //= 2

    minimal_excerpt = _serialize_run_excerpt(run, index=index, blocks=blocks, content_budget_chars=0)
    if token_estimator(minimal_excerpt) <= max_tokens:
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


def _compaction_replay_messages(
    run: RunOutput | TeamRunOutput,
    history_settings: ResolvedHistorySettings,
) -> list[Message]:
    skip_roles = set(history_skip_roles(history_settings))
    messages = [deepcopy(message) for message in run.messages or [] if message.role not in skip_roles]
    if history_settings.max_tool_calls_from_history is not None and messages:
        filter_tool_calls(messages, history_settings.max_tool_calls_from_history)
    strip_stale_anthropic_replay_fields(messages)
    return messages


def _excerpt_blocks(run: RunOutput | TeamRunOutput, history_settings: ResolvedHistorySettings) -> list[_ExcerptBlock]:
    blocks: list[_ExcerptBlock] = []
    if run.metadata:
        metadata = _metadata_for_summary(run.metadata)
        if metadata:
            blocks.append(_ExcerptBlock("<run_metadata>", stable_serialize(metadata), "</run_metadata>"))
    for message in _compaction_replay_messages(run, history_settings):
        content = render_message_content(message)
        if not content:
            continue
        blocks.append(_ExcerptBlock(_message_open_tag(message), content, "</message>"))
    return blocks


def _metadata_for_summary(metadata: dict[str, object]) -> dict[str, object]:
    """Omit bulky request metadata from compaction summary inputs."""
    return {key: value for key, value in metadata.items() if key not in _SUMMARY_METADATA_OMIT_KEYS}


def _truncate_excerpt(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    if max_chars == 1:
        return "…"
    return f"{text[: max_chars - 1].rstrip()}…"


def _remaining_excerpt_budget(
    max_input_tokens: int,
    summary_block: str,
    token_estimator: Callable[[str], int],
) -> int:
    return max_input_tokens - token_estimator(_compose_summary_input(summary_block, ""))


def _compose_summary_input(summary_block: str, serialized_runs: str) -> str:
    parts: list[str] = []
    if summary_block:
        parts.append(summary_block)
    parts.append(f"<new_conversation>\n{serialized_runs}\n</new_conversation>")
    return "\n\n".join(parts)


def _previous_summary_block(summary: str) -> str:
    return f"<previous_summary>\n{_escape_xml_content(summary)}\n</previous_summary>"


def messages_for_runs(
    runs: Sequence[RunOutput | TeamRunOutput],
    history_settings: ResolvedHistorySettings,
) -> list[Message]:
    """Copy and sanitize conversation messages for portable compaction hooks."""
    messages: list[Message] = []
    for run in runs:
        messages.extend(_compaction_replay_messages(run, history_settings))
    strip_stale_anthropic_replay_fields(messages)
    return messages


def _serialize_run(run: RunOutput | TeamRunOutput, index: int, history_settings: ResolvedHistorySettings) -> str:
    lines = [_run_open_tag(run, index)]
    if run.metadata:
        metadata = _metadata_for_summary(run.metadata)
        if metadata:
            lines.extend(["<run_metadata>", _escape_xml_content(stable_serialize(metadata)), "</run_metadata>"])
    for message in _compaction_replay_messages(run, history_settings):
        lines.extend(_serialize_message(message))
    lines.append("</run>")
    return "\n".join(lines)


def _serialize_message(message: Message) -> list[str]:
    lines = [_message_open_tag(message), _escape_xml_content(render_message_content(message)), "</message>"]
    if message.tool_calls:
        lines.extend(["<tool_calls>", _escape_xml_content(stable_serialize(message.tool_calls)), "</tool_calls>"])
    for tag, media_value in message_media_entries(message):
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


def _serialize_media_payload(media_value: object | None) -> str:
    if media_value is None:
        return ""
    return stable_serialize(media_payload_snapshot(media_value))


def _unescape_xml_content(text: str) -> str:
    return text.replace("&gt;", ">").replace("&lt;", "<").replace("&amp;", "&")


def _escape_xml_content(text: str) -> str:
    return escape(_unescape_xml_content(text), quote=False)
