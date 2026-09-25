"""Bounded conversation evidence for one skill review, following Hermes' digest replay.

Hermes replays a routed review as older turns collapsed into one-line digests plus the newest messages verbatim.
MindRoom reviews later from the persisted session, so the transcript is text rather than provider tool messages,
which keeps it valid for every provider regardless of which tools the reviewer itself declares.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from mindroom.history_run_visibility import is_model_history_visible_run
from mindroom.redaction import redact_sensitive_text

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
# Redaction handles at most 64 KiB per call, so one rendered message stays below it.
_MAX_MESSAGE_CHARS = 60_000
_MIN_MESSAGE_CHARS = 2_000


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


def render_transcript(messages: Sequence[Message], *, budget_chars: int) -> str:
    """Render digest lines for older turns and the newest messages verbatim within ``budget_chars``."""
    tail = _TAIL_MESSAGES
    while len(messages) > tail and messages[-tail].role == "tool":
        # A kept run never starts on a tool result whose call was digested away.
        tail += 1
    older, recent = messages[: max(0, len(messages) - tail)], messages[-tail:]
    digest = [line for message in older if (line := _digest_line(message))]
    message_chars = min(_MAX_MESSAGE_CHARS, max(_MIN_MESSAGE_CHARS, budget_chars // 8))
    verbatim = [_render_message(message, message_chars) for message in recent]

    size = sum(len(line) + 1 for line in digest) + sum(len(block) + 2 for block in verbatim)
    omitted_digest = 0
    while digest and size > budget_chars:
        size -= len(digest.pop(0)) + 1
        omitted_digest += 1
    omitted_messages = 0
    while len(verbatim) > 1 and size > budget_chars:
        size -= len(verbatim.pop(0)) + 2
        omitted_messages += 1

    sections: list[str] = []
    if omitted_digest or digest:
        header = "[Earlier conversation digest; older turns are shortened to one line each, recent messages follow.]"
        if omitted_digest:
            header += f"\n[{omitted_digest} earlier turns omitted to fit the review budget.]"
        sections.append("\n".join([header, *digest]))
    if omitted_messages:
        sections.append(f"[{omitted_messages} further messages omitted to fit the review budget.]")
    sections.extend(verbatim)
    # Conversation content must not close the reviewer's <conversation> evidence block.
    return _CLOSING_TAG.sub("<\\/conversation>", "\n\n".join(sections))


def _digest_line(message: Message) -> str | None:
    text = " ".join(message.get_content_string().split())
    if message.role == "user" and text:
        return redact_sensitive_text(f"USER: {text[:_DIGEST_USER_CHARS]}")
    if message.role != "assistant":
        return None
    parts = []
    if names := _tool_call_names(message):
        parts.append(f"ASSISTANT[tools: {', '.join(names)}]")
    if text:
        parts.append(f"ASSISTANT: {text[:_DIGEST_ASSISTANT_CHARS]}")
    return redact_sensitive_text("\n".join(parts)) if parts else None


def _render_message(message: Message, limit: int) -> str:
    if message.role == "tool":
        heading = f"TOOL RESULT ({message.tool_name or 'unknown tool'}):"
    else:
        heading = f"{message.role.upper()}:"
    calls = []
    for call in message.tool_calls or []:
        function = call.get("function") or {}
        arguments = function.get("arguments")
        rendered = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
        calls.append(f"-> calls {function.get('name', 'unknown tool')}({_clip(rendered, limit // 4)})")
    content_limit = max(_MIN_MESSAGE_CHARS, limit - len(heading) - sum(len(line) + 1 for line in calls))
    lines = [heading, _clip(message.get_content_string(), content_limit), *calls]
    return redact_sensitive_text(_clip("\n".join(lines), limit))


def _tool_call_names(message: Message) -> list[str]:
    return [str((call.get("function") or {}).get("name", "?")) for call in message.tool_calls or []]


def _clip(text: str, limit: int) -> str:
    """Keep the start and the end, where a command and its error or result usually are."""
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n[... {len(text) - 2 * half} characters omitted ...]\n{text[-half:]}"
