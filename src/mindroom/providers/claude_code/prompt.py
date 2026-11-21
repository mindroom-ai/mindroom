"""What the ``claude_code`` route gives Claude Code: tool names, a rebuilt conversation, and a parked turn's results.

Claude Code takes no earlier turns, so a new process gets MindRoom's history as tagged text in one user message,
with images as real blocks. A live process gets only what follows the answer it gave: the results of the tool calls
it waits on, or the next user messages.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.llm.messages import system_text, tool_result_view
from mindroom.providers.anthropic.wire import image_block

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.llm.messages import Message
    from mindroom.llm.provider import ToolSpec

__all__ = ["CONTINUE_TEXT", "TOOL_PREFIX", "Emitted", "Resume", "decide", "rebuild_input", "short_name", "tool_names"]

# Claude Code names an MCP tool ``mcp__<server>__<tool>`` and takes at most 64 characters of letters, digits, ``_``
# and ``-``.
TOOL_PREFIX = "mcp__mindroom__"
_NAME_LIMIT = 64 - len(TOOL_PREFIX)
_HASH_LENGTH = 10
_CHECKPOINT_INTRO = (
    "The following is the conversation so far, which you have not seen. Treat it as prior conversation, not as a new "
    "request, and never repeat a tool call whose result it holds."
)
CONTINUE_TEXT = "Continue from the results above."


def short_name(name: str) -> str:
    """Return the name Claude Code is offered ``name`` under: itself, or a cut and hashed one when it cannot take it."""
    if len(name) <= _NAME_LIMIT and re.fullmatch(r"[a-zA-Z0-9_-]+", name):
        return name
    digest = hashlib.sha256(name.encode()).hexdigest()[:_HASH_LENGTH]
    return f"{re.sub(r'[^a-zA-Z0-9_-]', '_', name)[: _NAME_LIMIT - _HASH_LENGTH - 1]}_{digest}"


def tool_names(tools: Sequence[ToolSpec]) -> dict[str, str]:
    """Return the offered tools' names by the name Claude Code calls each with."""
    return {short_name(tool.name): tool.name for tool in tools}


def _user_blocks(message: Message, provider: str) -> list[dict[str, Any]]:
    """Return a user message's text and images as the content blocks Claude Code takes."""
    text = message.get_content_string()
    blocks: list[dict[str, Any]] = [{"type": "text", "text": text}] if text else []
    return blocks + [block for image in message.images or () if (block := image_block(image, provider)) is not None]


def _history_blocks(message: Message, provider: str) -> list[dict[str, Any]]:
    """Return one earlier message as tagged text; a user message's images stay real blocks."""
    if message.role == "tool":
        result = tool_result_view(message)
        output = {"id": result.call_id, "name": result.name, "output": result.content}
        if result.is_error:
            output["is_error"] = True
        return [_text(f"<tool-result>\n{json.dumps(output)}\n</tool-result>")]
    if message.role == "user":
        return [_text("<user>"), *_user_blocks(message, provider), _text("</user>")]
    calls = [
        json.dumps({"id": call["id"], "name": call["function"]["name"], "input": _arguments(call)})
        for call in message.tool_calls or ()
    ]
    parts = [message.get_content_string(), *(f"<tool-call>\n{call}\n</tool-call>" for call in calls)]
    return [_text("\n".join(["<assistant>", *filter(None, parts), "</assistant>"]))]


def _text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _arguments(call: dict[str, Any]) -> object:
    """Return a stored call's arguments as the value the model wrote, or as stored when they are not JSON."""
    arguments = call["function"].get("arguments")
    try:
        return json.loads(arguments) if isinstance(arguments, str) else arguments
    except ValueError:
        return arguments


def _merged(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return ``blocks`` with each run of text blocks joined into one, a line each."""
    merged: list[dict[str, Any]] = []
    for block in blocks:
        if block["type"] == "text" and merged and merged[-1]["type"] == "text":
            merged[-1] = _text(f"{merged[-1]['text']}\n{block['text']}")
        else:
            merged.append(block)
    return merged


def rebuild_input(messages: Sequence[Message], provider: str) -> tuple[str, list[dict[str, Any]]]:
    """Return the system prompt and the one user message that give a new Claude Code process ``messages``.

    The messages through the last assistant or tool message become tagged history inside a checkpoint, and the user
    messages after it follow as they are; history that ends the request asks Claude to continue from it.
    """
    conversation = [message for message in messages if message.role != "system"]
    end = max((index + 1 for index, message in enumerate(conversation) if message.role != "user"), default=0)
    blocks: list[dict[str, Any]] = []
    if end:
        blocks.append(_text(f"<conversation-checkpoint>\n{_CHECKPOINT_INTRO}"))
        for message in conversation[:end]:
            blocks += _history_blocks(message, provider)
        blocks.append(_text("</conversation-checkpoint>"))
    current = [block for message in conversation[end:] for block in _user_blocks(message, provider)]
    blocks += current or [_text(CONTINUE_TEXT)]
    return system_text(messages), _merged(blocks)


@dataclass(frozen=True, slots=True)
class Emitted:
    """The answer a live Claude Code process gave last: its text, and the tool calls it now waits on, in order."""

    content: str | None
    tool_call_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Resume:
    """What continues a live process: each waiting call's MCP result, or the user blocks of its next turn."""

    results: dict[str, dict[str, Any]]
    blocks: list[dict[str, Any]]


def _mcp_content(blocks: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return Claude content blocks as MCP tool-result content."""
    return [
        block
        if block["type"] == "text"
        else {"type": "image", "data": block["source"]["data"], "mimeType": block["source"]["media_type"]}
        for block in blocks
    ]


def _mcp_result(message: Message, provider: str) -> dict[str, Any]:
    """Return the MCP result of one ``tool`` message: its text, its images, and whether the call failed."""
    result = tool_result_view(message)
    text = result.content if isinstance(result.content, str) else json.dumps(result.content)
    images = [block for image in message.images or () if (block := image_block(image, provider)) is not None]
    return {"content": [{"type": "text", "text": text}, *_mcp_content(images)], "isError": result.is_error}


def decide(emitted: Emitted, tail: Sequence[Message], provider: str) -> Resume | None:
    """Return how ``tail`` continues the process that answered with ``emitted``, or None when it must be rebuilt.

    ``tail`` starts at the request's last assistant message. A process parked on tool calls continues only when that
    message holds exactly those calls and exactly one result per call follows, then at most user messages, whose text
    and images ride on the last result. An idle process continues only when that message is its answer and only user
    messages follow.
    """
    answered, rest = tail[0], tail[1:]
    if tuple(call["id"] for call in answered.tool_calls or ()) != emitted.tool_call_ids:
        return None
    results = [message for message in rest if message.role == "tool"]
    users = rest[len(results) :]
    if any(message.role != "user" or message.files or message.audio or message.videos for message in users):
        return None
    blocks = [block for message in users for block in _user_blocks(message, provider)]
    if not emitted.tool_call_ids:
        same_answer = (answered.get_content_string() or None) == emitted.content
        return Resume({}, blocks) if same_answer and blocks and not results else None
    if sorted(message.tool_call_id or "" for message in results) != sorted(emitted.tool_call_ids):
        return None
    answers = {str(message.tool_call_id): _mcp_result(message, provider) for message in results}
    answers[emitted.tool_call_ids[-1]]["content"] += _mcp_content(blocks)
    return Resume({call_id: answers[call_id] for call_id in emitted.tool_call_ids}, [])
