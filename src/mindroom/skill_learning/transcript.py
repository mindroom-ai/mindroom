"""Bounded conversation evidence for one skill review, following Hermes' digest replay.

Hermes replays a routed review as older turns collapsed into one-line digests plus the newest messages verbatim.
MindRoom reviews later from the persisted session, so the transcript is text rather than provider tool messages,
which keeps it valid for every provider regardless of which tools the reviewer itself declares. Like the messages
a Hermes review replays after context compression, it opens with the compaction summary of removed turns.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from mindroom.history_run_visibility import is_model_history_visible_run
from mindroom.redaction import REDACTION_FAILED, redact_private_keys, redact_sensitive_text

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from agno.models.message import Message
    from agno.run.agent import RunOutput
    from agno.session.agent import AgentSession

_CONVERSATION_ROLES = frozenset({"user", "assistant", "tool"})
_CLOSING_TAG = re.compile(r"</\s*conversation\s*>", re.IGNORECASE)
_TAIL_MESSAGES = 24
_DIGEST_USER_CHARS = 300
_DIGEST_ASSISTANT_CHARS = 200
# One rendered message, a few tool-call steps' worth of evidence.
_MAX_MESSAGE_CHARS = 60_000
_MIN_MESSAGE_CHARS = 2_000
# Room for the omission note inside a clipped message.
_CLIP_NOTE_CHARS = 64
# Redaction handles 64 KiB per call, so longer text is redacted in line chunks with a little preceding context.
_REDACTION_CHUNK_CHARS = 60_000
_REDACTION_CONTEXT_CHARS = 256


def conversation_messages(session: AgentSession) -> list[Message]:
    """Return the user, assistant, and tool messages of this session's model-visible runs, in order."""
    return [
        message
        for run in session.runs or []
        if is_model_history_visible_run(run)
        for message in run.messages or []
        if message.role in _CONVERSATION_ROLES and not message.from_history
    ]


def count_model_replies(runs: Iterable[RunOutput]) -> int:
    """Count assistant messages, one per model request including tool-calling steps, in model-visible runs."""
    return sum(
        1
        for run in runs
        if is_model_history_visible_run(run)
        for message in run.messages or []
        if message.role == "assistant" and not message.from_history
    )


def render_transcript(messages: Sequence[Message], *, summary: str | None = None, budget_chars: int) -> str:
    """Render the compaction summary, digest lines for older turns, and the newest messages within ``budget_chars``.

    The summary is the only record of compacted turns, so it is shortened rather than dropped.
    """
    tail = _TAIL_MESSAGES
    while len(messages) > tail and messages[-tail].role == "tool":
        # A kept run never starts on a tool result whose call was digested away.
        tail += 1
    older, recent = messages[: max(0, len(messages) - tail)], messages[-tail:]
    digest = [line for message in older if (line := _digest_line(message))]
    message_chars = min(_MAX_MESSAGE_CHARS, max(_MIN_MESSAGE_CHARS, budget_chars // 8))
    verbatim = [_render_message(message, message_chars) for message in recent]
    compacted = (
        [_clip(_redacted(f"[Summary of earlier turns removed by compaction.]\n{text}"), message_chars)]
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
    # Conversation content must not close the reviewer's <conversation> evidence block.
    return _CLOSING_TAG.sub("<\\/conversation>", "\n\n".join(sections))


def _digest_line(message: Message) -> str | None:
    if message.role not in {"user", "assistant"}:
        return None
    # Only the leading lines a digest keeps are redacted, whole, before they are cut to length.
    text = " ".join(_redacted(_leading_lines(message.get_content_string(), 4 * _DIGEST_USER_CHARS)).split())
    if message.role == "user":
        return f"USER: {text[:_DIGEST_USER_CHARS]}" if text else None
    parts = []
    if names := _tool_call_names(message):
        parts.append(f"ASSISTANT[tools: {_redacted(', '.join(names))[:_DIGEST_ASSISTANT_CHARS]}]")
    if text:
        parts.append(f"ASSISTANT: {text[:_DIGEST_ASSISTANT_CHARS]}")
    return "\n".join(parts) if parts else None


def _render_message(message: Message, limit: int) -> str:
    if message.role == "tool":
        heading = f"TOOL RESULT ({message.tool_name or 'unknown tool'}):"
    else:
        heading = f"{message.role.upper()}:"
    lines = [heading, message.get_content_string()]
    for call in message.tool_calls or []:
        function = call.get("function") or {}
        arguments = function.get("arguments")
        rendered = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
        lines.append(f"-> calls {function.get('name', 'unknown tool')}({rendered})")
    return _clip(_redacted("\n".join(lines)), limit)


def _tool_call_names(message: Message) -> list[str]:
    return [str((call.get("function") or {}).get("name", "?")) for call in message.tool_calls or []]


def _redacted(text: str) -> str:
    """Redact text of any length before anything cuts it, so no cut can hide a secret's recognizable prefix.

    Private keys span lines and go first over the whole text. The other patterns stay within a line, except a
    prefix such as "Bearer" followed by a line break, so longer text is redacted in line-aligned chunks that each see
    the preceding non-blank text as context. Redaction fails closed on a secret-named key that ends a line, whose
    value may follow it, so such a chunk is redone line by line and only the lines that fail are replaced.
    """
    lines = redact_private_keys(text).split("\n")
    redacted: list[str] = []
    context = ""
    start = 0
    while start < len(lines):
        end, size = start + 1, len(lines[start])
        while end < len(lines) and size + len(lines[end]) + 1 <= _REDACTION_CHUNK_CHARS:
            size += len(lines[end]) + 1
            end += 1
        chunk = lines[start:end]
        result = _redact_after(context, "\n".join(chunk))
        if result is not None:
            redacted.extend(result.split("\n"))
            context = _context_after(context, chunk)
        else:
            for line in chunk:
                line_result = _redact_after(context, line)
                redacted.append(REDACTION_FAILED if line_result is None else line_result)
                context = _context_after(context, [line])
        start = end
    return "\n".join(redacted)


def _redact_after(context: str, text: str) -> str | None:
    """Redact ``text`` as if it followed ``context``, or return None when redaction fails closed."""
    result = redact_sensitive_text(f"{context}\n{text}" if context else text)
    if result == REDACTION_FAILED:
        return None
    return result.split("\n", 1)[1] if context else result


def _context_after(context: str, lines: Sequence[str]) -> str:
    """Return the last non-blank text before the next line, whitespace collapsed onto one line."""
    tail: list[str] = []
    size = 0
    for line in reversed(lines):
        if words := line.split():
            tail.append(" ".join(words))
            size += len(tail[-1]) + 1
            if size >= _REDACTION_CONTEXT_CHARS:
                break
    if size < _REDACTION_CONTEXT_CHARS and context:
        tail.append(context)
    return " ".join(reversed(tail))[-_REDACTION_CONTEXT_CHARS:]


def _leading_lines(text: str, chars: int) -> str:
    """Return whole lines covering at least ``chars`` characters of ``text``."""
    end = text.find("\n", chars)
    return text if end == -1 else text[:end]


def _clip(text: str, limit: int) -> str:
    """Keep at most ``limit`` characters of already redacted text from its start and end.

    The start and end are where a command's error or result usually is.
    """
    if len(text) <= limit:
        return text
    half = (limit - _CLIP_NOTE_CHARS) // 2
    return f"{text[:half]}\n[... {len(text) - 2 * half} characters omitted ...]\n{text[-half:]}"
