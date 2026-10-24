"""Bounded portable-summary serialization of conversation runs and prior summaries."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from html import escape
from typing import TYPE_CHECKING

from mindroom.constants import (
    AI_RUN_METADATA_KEY,
    MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY,
    MINDROOM_COMPACTION_METADATA_KEY,
    MINDROOM_MATRIX_HISTORY_METADATA_KEY,
)
from mindroom.history.claude_replay_compat import strip_stale_anthropic_replay_fields
from mindroom.history.message_content import media_payload_snapshot, message_media_entries, render_message_content
from mindroom.history.request_messages import filter_tool_calls, replayable_messages
from mindroom.timing import timed
from mindroom.token_budget import estimate_compaction_input_tokens, stable_serialize
from mindroom.usage.storage import COMPACTED_REQUESTS_METADATA_KEY

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from mindroom.history.types import ResolvedHistorySettings
    from mindroom.llm.messages import Message
    from mindroom.llm.runs import AnyRunRecord


_WRAPPER_OVERHEAD_TOKENS = 200

_OVERSIZED_RUN_NOTE = "Run truncated to fit compaction budget."
# A tool result names the arguments of its call so it reads correctly apart from the call message; large argument
# values, such as a written file's content, are cut short, since the call message holds them in full.
_TOOL_RESULT_ARG_CHARS = 200

_SUMMARY_METADATA_OMIT_KEYS = frozenset(
    {
        AI_RUN_METADATA_KEY,
        MINDROOM_COMPACTION_METADATA_KEY,
        MINDROOM_MATRIX_HISTORY_METADATA_KEY,
        # The run's own messages already carry its prompts.
        MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY,
        COMPACTED_REQUESTS_METADATA_KEY,
        "model_params",
        # TODO(simplify): run decoding drops ``tools_schema``, so only a test's in-memory run still holds it here.
        # Drop the key; nobody but the tests/test_history_summary_call.py cases that set it would notice.
        "tools_schema",
    },
)


@dataclass(frozen=True)
class _ExcerptBlock:
    open_tag: str
    content: str
    close_tag: str

    def render(self, max_chars: int) -> str:
        snippet = _escape_xml_content(_truncate_excerpt(self.content, max_chars))
        return f"{self.open_tag}\n{snippet}\n{self.close_tag}"


@timed("system_prompt_assembly.history_prepare.compaction.summary_input_build")
def build_summary_input(
    *,
    previous_summary: str | None,
    compacted_runs: Sequence[AnyRunRecord],
    max_input_tokens: int,
    history_settings: ResolvedHistorySettings,
    token_estimator: Callable[[str], int] = estimate_compaction_input_tokens,
) -> tuple[str, list[AnyRunRecord]]:
    """Fit prior summary and ordered runs into one serialized summary request."""
    summary_block = _previous_summary_block(previous_summary)
    envelope_tokens = token_estimator(_compose_summary_input(summary_block, ""))
    remaining = max_input_tokens - envelope_tokens - _WRAPPER_OVERHEAD_TOKENS

    included_runs: list[AnyRunRecord] = []
    serialized_runs: list[str] = []
    if remaining > 0:
        for index, run in enumerate(compacted_runs):
            serialized_run = _serialize_run(run, index, history_settings)
            separator = "\n\n" if serialized_runs else ""
            run_tokens = token_estimator(f"{separator}{serialized_run}")
            if run_tokens > remaining:
                break
            included_runs.append(run)
            serialized_runs.append(serialized_run)
            remaining -= run_tokens
    if included_runs:
        return _compose_summary_input(summary_block, "\n\n".join(serialized_runs)), included_runs

    # Not even the first run fits whole, so it is excerpted.
    oversized_excerpt = (
        _serialize_oversized_run_excerpt(
            compacted_runs[0],
            history_settings=history_settings,
            max_tokens=max_input_tokens - envelope_tokens,
            token_estimator=token_estimator,
        )
        if compacted_runs
        else None
    )
    if oversized_excerpt is None:
        return summary_block, []
    return _compose_summary_input(summary_block, oversized_excerpt), [compacted_runs[0]]


def minimum_summary_input_tokens(
    *,
    previous_summary: str | None,
    first_run: AnyRunRecord,
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
    summary_block = _previous_summary_block(previous_summary)
    minimal_excerpt = _serialize_run_excerpt(first_run, blocks=(), content_budget_chars=0)
    return token_estimator(_compose_summary_input(summary_block, minimal_excerpt)) + _WRAPPER_OVERHEAD_TOKENS


def whole_run_input_tokens(
    *,
    previous_summary: str | None,
    first_run: AnyRunRecord,
    history_settings: ResolvedHistorySettings,
    token_estimator: Callable[[str], int],
) -> int:
    """Return the smallest budget at which ``build_summary_input`` holds ``first_run`` whole after the prior summary."""
    empty_input = _compose_summary_input(_previous_summary_block(previous_summary), "")
    return (
        token_estimator(empty_input)
        + _WRAPPER_OVERHEAD_TOKENS
        + token_estimator(_serialize_run(first_run, 0, history_settings))
    )


def _serialize_oversized_run_excerpt(
    run: AnyRunRecord,
    *,
    history_settings: ResolvedHistorySettings,
    max_tokens: int,
    token_estimator: Callable[[str], int],
) -> str | None:
    if max_tokens <= 0:
        return None

    full_run = _serialize_run(run, 0, history_settings)
    if token_estimator(full_run) <= max_tokens:
        return full_run

    blocks = _excerpt_blocks(run, history_settings)
    budget_chars = max_tokens * 4
    while budget_chars > 0:
        excerpt = _serialize_run_excerpt(run, blocks=blocks, content_budget_chars=budget_chars)
        if token_estimator(excerpt) <= max_tokens:
            return excerpt
        budget_chars //= 2

    minimal_excerpt = _serialize_run_excerpt(run, blocks=blocks, content_budget_chars=0)
    if token_estimator(minimal_excerpt) <= max_tokens:
        return minimal_excerpt
    return None


def _serialize_run_excerpt(
    run: AnyRunRecord,
    *,
    blocks: Sequence[_ExcerptBlock],
    content_budget_chars: int,
) -> str:
    """Render the only run of an oversized summary input, ``blocks`` cut to ``content_budget_chars`` characters."""
    lines = [_run_open_tag(run, 0), f"<note>{_OVERSIZED_RUN_NOTE}</note>"]
    remaining_chars = content_budget_chars
    for block in blocks:
        if remaining_chars <= 0:
            break
        lines.append(block.render(remaining_chars))
        if len(block.content) > remaining_chars:
            break
        remaining_chars -= len(block.content)

    lines.append("</run>")
    return "\n".join(lines)


def _compaction_replay_messages(run: AnyRunRecord, history_settings: ResolvedHistorySettings) -> list[Message]:
    """Return the run's messages a summary reads, not copied: the input renders no thinking, so only hooks strip it."""
    messages = replayable_messages([run])
    if history_settings.max_tool_calls_from_history is not None:
        messages = filter_tool_calls(messages, history_settings.max_tool_calls_from_history)
    return messages


def _excerpt_blocks(run: AnyRunRecord, history_settings: ResolvedHistorySettings) -> list[_ExcerptBlock]:
    metadata = _summary_metadata(run)
    blocks = [] if metadata is None else [_ExcerptBlock("<run_metadata>", metadata, "</run_metadata>")]
    for message in _compaction_replay_messages(run, history_settings):
        content = render_message_content(message)
        if content:
            tool_args = _tool_result_args(message)
            if tool_args is not None:
                blocks.append(_ExcerptBlock("<tool_args>", tool_args, "</tool_args>"))
            blocks.append(_ExcerptBlock(_message_open_tag(message), content, "</message>"))
        if message.tool_calls:
            # A result cut short still names the call and arguments that produced it.
            blocks.append(_ExcerptBlock("<tool_calls>", stable_serialize(message.tool_calls), "</tool_calls>"))
    return blocks


def _summary_metadata(run: AnyRunRecord) -> str | None:
    """Return the run's metadata without bulky request records, serialized, or None when nothing is left."""
    metadata = {key: value for key, value in (run.metadata or {}).items() if key not in _SUMMARY_METADATA_OMIT_KEYS}
    return stable_serialize(metadata) if metadata else None


def _truncate_excerpt(text: str, max_chars: int) -> str:
    """Cut ``text`` to ``max_chars`` characters, a positive count, ending a cut text with an ellipsis."""
    return text if len(text) <= max_chars else f"{text[: max_chars - 1].rstrip()}…"


def _compose_summary_input(summary_block: str, serialized_runs: str) -> str:
    conversation = f"<new_conversation>\n{serialized_runs}\n</new_conversation>"
    return f"{summary_block}\n\n{conversation}" if summary_block else conversation


def _previous_summary_block(summary: str | None) -> str:
    if summary is None or not summary.strip():
        return ""
    return f"<previous_summary>\n{_escape_xml_content(summary)}\n</previous_summary>"


def messages_for_runs(
    runs: Sequence[AnyRunRecord],
    history_settings: ResolvedHistorySettings,
) -> list[Message]:
    """Copy and sanitize conversation messages for portable compaction hooks."""
    messages = [deepcopy(message) for run in runs for message in _compaction_replay_messages(run, history_settings)]
    strip_stale_anthropic_replay_fields(messages)
    return messages


def _serialize_run(run: AnyRunRecord, index: int, history_settings: ResolvedHistorySettings) -> str:
    lines = [_run_open_tag(run, index)]
    metadata = _summary_metadata(run)
    if metadata is not None:
        lines.extend(["<run_metadata>", _escape_xml_content(metadata), "</run_metadata>"])
    for message in _compaction_replay_messages(run, history_settings):
        lines.extend(_serialize_message(message))
    lines.append("</run>")
    return "\n".join(lines)


def _serialize_message(message: Message) -> list[str]:
    tool_args = _tool_result_args(message)
    lines = [] if tool_args is None else ["<tool_args>", _escape_xml_content(tool_args), "</tool_args>"]
    lines += [_message_open_tag(message), _escape_xml_content(render_message_content(message)), "</message>"]
    if message.tool_calls:
        lines.extend(["<tool_calls>", _escape_xml_content(stable_serialize(message.tool_calls)), "</tool_calls>"])
    for tag, media_value in message_media_entries(message):
        serialized = "" if media_value is None else stable_serialize(media_payload_snapshot(media_value))
        if serialized:
            lines.extend([f"<{tag}>", _escape_xml_content(serialized), f"</{tag}>"])
    return lines


def _run_open_tag(run: AnyRunRecord, index: int) -> str:
    attrs = [f'index="{index}"']
    if run.run_id:
        attrs.append(f'run_id="{escape(str(run.run_id), quote=True)}"')
    attrs.append(f'status="{escape(str(run.status), quote=True)}"')
    return f"<run {' '.join(attrs)}>"


def _message_open_tag(message: Message) -> str:
    attrs = [f'role="{escape(message.role, quote=True)}"']
    if message.name:
        attrs.append(f'name="{escape(message.name, quote=True)}"')
    if message.tool_call_id:
        attrs.append(f'tool_call_id="{escape(message.tool_call_id, quote=True)}"')
    if message.role == "tool" and message.tool_name:
        attrs.append(f'tool_name="{escape(message.tool_name, quote=True)}"')
    return f"<message {' '.join(attrs)}>"


def _tool_result_args(message: Message) -> str | None:
    """Return a tool result's own call arguments, bounded, so it reads correctly apart from its call message."""
    if message.role != "tool" or not message.tool_args:
        return None
    args = message.tool_args
    if not isinstance(args, dict):
        return _truncate_excerpt(stable_serialize(args), _TOOL_RESULT_ARG_CHARS)
    return stable_serialize({key: _bounded_arg(value) for key, value in args.items()})


def _bounded_arg(value: object) -> object:
    serialized = stable_serialize(value)
    return value if len(serialized) <= _TOOL_RESULT_ARG_CHARS else _truncate_excerpt(serialized, _TOOL_RESULT_ARG_CHARS)


def _escape_xml_content(text: str) -> str:
    """Escape ``text`` for an XML body, unescaping it first so already-escaped text is not escaped twice."""
    return escape(text.replace("&gt;", ">").replace("&lt;", "<").replace("&amp;", "&"), quote=False)
