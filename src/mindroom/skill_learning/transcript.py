"""Reply counting and bounded conversation evidence for a replayed skill review, following Hermes' digest replay.

Hermes replays a routed review as older turns collapsed into one-line digests plus the newest messages verbatim.
MindRoom replays the persisted session this way when the review uses another model or cannot fork the response's
request, as text rather than provider tool messages, which keeps it valid for every provider regardless of which tools
the reviewer declares. Like the messages a Hermes review replays after context compression, it opens with the
compaction summary of removed turns.
"""

from __future__ import annotations

import json
from itertools import groupby
from typing import TYPE_CHECKING

from mindroom.history.conversation_text import Utterance, one_line, utterances
from mindroom.history.run_visibility import is_model_history_visible_run
from mindroom.redaction import redact_sensitive_text

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agno.models.message import Message
    from agno.run.agent import RunOutput
    from agno.session.agent import AgentSession

_TAIL_MESSAGES = 24
_DIGEST_USER_CHARS = 300
_DIGEST_ASSISTANT_CHARS = 200
# Redaction handles at most 64 KiB per call, so one rendered message stays below it.
_MAX_MESSAGE_CHARS = 60_000
_MIN_MESSAGE_CHARS = 2_000
# Room for the omission note inside a clipped message.
_CLIP_NOTE_CHARS = 64


def conversation_messages(session: AgentSession) -> list[list[Utterance]]:
    """Return each stored message of this session's turns, in order, as what was said in it, with tool calls and results.

    A prompt holding several people's messages stays one entry, so it is redacted as a whole like any stored message.
    """
    return [
        list(parts)
        for run in session.runs or []
        for _, parts in groupby(utterances(run, agent_work=True), key=lambda utterance: id(utterance.message))
    ]


def count_model_replies(run: RunOutput | None) -> tuple[int, bool]:
    """Count a model-visible run's assistant messages, one per model request including tool-calling steps.

    Like Hermes resetting its counter when ``skill_manage`` runs, only replies after the last reply that called it
    count; the second value says whether one did.
    """
    replies, restarted = 0, False
    if run is None or not is_model_history_visible_run(run):
        return replies, restarted
    for message in run.messages or []:
        if message.role != "assistant" or message.from_history:
            continue
        if any((call.get("function") or {}).get("name") == "skill_manage" for call in message.tool_calls or []):
            replies, restarted = 0, True
        else:
            replies += 1
    return replies, restarted


def render_transcript(
    messages: Sequence[Sequence[Utterance]],
    *,
    summary: str | None = None,
    budget_chars: int,
) -> str:
    """Render the compaction summary, digest lines for older turns, and the newest messages within ``budget_chars``.

    The summary is the only record of compacted turns, so it is shortened rather than dropped.
    """
    tail = _TAIL_MESSAGES
    while len(messages) > tail and messages[-tail][0].kind == "tool_result":
        # A kept run never starts on a tool result whose call was digested away.
        tail += 1
    older, recent = messages[: max(0, len(messages) - tail)], messages[-tail:]
    digest = [line for message in older if (line := _digest_line(message))]
    message_chars = min(_MAX_MESSAGE_CHARS, max(_MIN_MESSAGE_CHARS, budget_chars // 8))
    verbatim = [_render_message(message, message_chars) for message in recent]
    compacted = (
        [_render_text(f"[Summary of earlier turns removed by compaction.]\n{text}", message_chars)]
        if summary and (text := summary.strip())
        else []
    )

    size = sum(len(block) + 2 for block in compacted)
    size += sum(len(line) + 1 for line in digest) + sum(len(block) + 2 for block in verbatim)
    omitted_digest = 0
    while digest and size > budget_chars:
        size -= len(digest.pop(0)) + 1
        omitted_digest += 1
    omitted_messages = 0
    while len(verbatim) > 1 and size > budget_chars:
        size -= len(verbatim.pop(0)) + 2
        omitted_messages += 1

    sections = list(compacted)
    if omitted_digest or digest:
        header = "[Earlier conversation digest; older turns are shortened, recent messages follow verbatim.]"
        if omitted_digest:
            header += f"\n[{omitted_digest} earlier turns omitted to fit the review budget.]"
        sections.append("\n".join([header, *digest]))
    if omitted_messages:
        sections.append(f"[{omitted_messages} further messages omitted to fit the review budget.]")
    sections.extend(verbatim)
    return "\n\n".join(sections)


def _digest_line(message: Sequence[Utterance]) -> str | None:
    first = message[0]
    if first.kind == "tool_result":
        return None
    if first.kind != "reply":
        # A digest line keeps a few hundred characters, so only that much of a long message is normalized.
        said = one_line(" ".join(f"{_heading(part)} {part.unredacted}" for part in message)[: 4 * _DIGEST_USER_CHARS])
        return redact_sensitive_text(said[:_DIGEST_USER_CHARS])
    text = one_line(first.unredacted[: 4 * _DIGEST_USER_CHARS])
    parts = []
    if names := _tool_call_names(first.message):
        parts.append(f"ASSISTANT[tools: {', '.join(names)[:_DIGEST_ASSISTANT_CHARS]}]")
    if text:
        parts.append(f"ASSISTANT: {text[:_DIGEST_ASSISTANT_CHARS]}")
    return redact_sensitive_text("\n".join(parts)) if parts else None


def _heading(message: Utterance) -> str:
    if message.kind == "said":
        return f"USER ({message.sender}):"
    if message.kind == "runtime":
        return "RUNTIME:"
    if message.kind == "tool_result":
        return f"TOOL RESULT ({message.message.tool_name or 'unknown tool'}):"
    return "ASSISTANT:"


def _render_message(message: Sequence[Utterance], limit: int) -> str:
    lines = [line for part in message for line in (_heading(part), part.unredacted)]
    for call in message[0].message.tool_calls or []:
        function = call.get("function") or {}
        arguments = function.get("arguments")
        rendered = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
        lines.append(f"-> calls {function.get('name', 'unknown tool')}({rendered})")
    return _render_text("\n".join(lines), limit)


def _render_text(text: str, limit: int) -> str:
    """Keep the text's start and end, and redact what remains.

    The evidence is a conversation the agent's model already processed, so redaction is best-effort; learned files
    are checked again for credentials when they are written.
    """
    return redact_sensitive_text(_clip(text, limit))


def _tool_call_names(message: Message) -> list[str]:
    return [str((call.get("function") or {}).get("name", "?")) for call in message.tool_calls or []]


def _clip(text: str, limit: int) -> str:
    """Keep at most ``limit`` characters from the start and the end, where a command's error or result usually is."""
    if len(text) <= limit:
        return text
    half = (limit - _CLIP_NOTE_CHARS) // 2
    return f"{text[:half]}\n[... {len(text) - 2 * half} characters omitted ...]\n{text[-half:]}"
