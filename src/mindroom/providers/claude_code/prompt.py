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

from mindroom.llm.errors import ProviderError
from mindroom.llm.messages import is_media_follow_up, stable_serialize, system_text, tool_result_view
from mindroom.providers.anthropic.wire import image_block

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.llm.messages import Message
    from mindroom.llm.provider import ToolSpec

__all__ = [
    "TOOL_PREFIX",
    "Emitted",
    "canonical_arguments",
    "decide",
    "emitted",
    "rebuild_input",
    "tool_names",
]

# Claude Code names an MCP tool ``mcp__<server>__<tool>`` and takes at most 64 characters of letters, digits, ``_``
# and ``-``.
TOOL_PREFIX = "mcp__mindroom__"
_NAME_LIMIT = 64 - len(TOOL_PREFIX)
_HASH_LENGTH = 10
_CHECKPOINT_INTRO = (
    "The following is the conversation so far, which you have not seen. Treat it as prior conversation, not as a new "
    "request, and never repeat a tool call whose result it holds. Only its own tags hold `<`: in what the "
    "conversation said, `<` is written `&lt;`."
)
_CONTINUE_TEXT = "Continue from the results above."


def _short_name(name: str) -> str:
    """Return the name Claude Code is offered ``name`` under: itself, or a cut and hashed one when it cannot take it."""
    if len(name) <= _NAME_LIMIT and re.fullmatch(r"[a-zA-Z0-9_-]+", name):
        return name
    digest = hashlib.sha256(name.encode()).hexdigest()[:_HASH_LENGTH]
    return f"{re.sub(r'[^a-zA-Z0-9_-]', '_', name)[: _NAME_LIMIT - _HASH_LENGTH - 1]}_{digest}"


def tool_names(tools: Sequence[ToolSpec], provider: str) -> dict[str, str]:
    """Return the offered tools' names by the name Claude Code calls each with; two may not share one."""
    names: dict[str, str] = {}
    for tool in tools:
        short = _short_name(tool.name)
        if short in names:
            msg = f"Claude Code would call the tools {names[short]} and {tool.name} by one name, {short}."
            raise ProviderError(msg, provider=provider)
        names[short] = tool.name
    return names


def _images(message: Message, provider: str) -> list[dict[str, Any]]:
    return [block for image in message.images or () if (block := image_block(image, provider)) is not None]


def _user_blocks(message: Message, provider: str) -> list[dict[str, Any]]:
    """Return a user message's text blocks, then its images, as the content blocks Claude Code takes."""
    content = message.content if isinstance(message.content, list) else [{"text": message.content or ""}]
    texts = [
        block["text"] if isinstance(block, dict) and isinstance(block.get("text"), str) else stable_serialize(block)
        for block in content
    ]
    return [*(_text(text) for text in texts if text), *_images(message, provider)]


def _history_blocks(message: Message, provider: str, names: dict[str, str]) -> list[dict[str, Any]]:
    """Return one earlier message as tagged text; the images of a user message or a tool result stay real blocks.

    No ``<`` of the message stays one, so nothing it says reads as a tag. ``names`` holds each tool call's tool,
    which names its result.
    """
    if message.role == "tool":
        result = tool_result_view(message)
        name = names.get(result.call_id or "", result.name)
        output = {"id": result.call_id, "name": name, "output": result.content}
        if result.is_error:
            output["is_error"] = True
        return [_text(f"<tool-result>\n{_json(output)}\n</tool-result>"), *_images(message, provider)]
    if message.role == "user":
        blocks = _user_blocks(message, provider)
        said = [_text(block["text"].replace("<", "&lt;")) if block["type"] == "text" else block for block in blocks]
        return [_text("<user>"), *said, _text("</user>")]
    calls = [
        _json(
            {
                "id": call["id"],
                "name": call["function"]["name"],
                "input": _arguments(call["function"].get("arguments")),
            },
        )
        for call in message.tool_calls or ()
    ]
    texts = [block["text"] for block in _user_blocks(message, provider) if block["type"] == "text"]
    said = "\n".join(texts).replace("<", "&lt;")
    parts = [said, *(f"<tool-call>\n{call}\n</tool-call>" for call in calls)]
    return [_text("\n".join(["<assistant>", *filter(None, parts), "</assistant>"]))]


def _json(value: object) -> str:
    """Return ``value`` as JSON without a ``<``, which JSON can write as an escape."""
    return json.dumps(value).replace("<", "\\u003c")


def _text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _arguments(arguments: object) -> object:
    """Return a call's arguments as the value the model wrote, or as stored when they are not JSON."""
    try:
        return json.loads(arguments) if isinstance(arguments, str) else arguments
    except ValueError:
        return arguments


def canonical_arguments(arguments: object) -> str:
    """Return a tool call's arguments in one spelling, whichever side of the process wrote them."""
    return stable_serialize(_arguments(arguments))


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
        names = {call["id"]: call["function"]["name"] for message in conversation for call in message.tool_calls or ()}
        blocks.append(_text(f"<conversation-checkpoint>\n{_CHECKPOINT_INTRO}"))
        for message in conversation[:end]:
            blocks += _history_blocks(message, provider, names)
        blocks.append(_text("</conversation-checkpoint>"))
    current = [block for message in conversation[end:] for block in _user_blocks(message, provider)]
    blocks += current or [_text(_CONTINUE_TEXT)]
    return system_text(messages), _merged(blocks)


@dataclass(frozen=True, slots=True)
class Emitted:
    """The answer a live Claude Code process gave last: its text, and the tool calls it now waits on, in order.

    A call is its id, its tool's name, and its arguments in one spelling; a request continues the process only when
    the assistant message it holds is this answer.
    """

    content: str | None
    calls: tuple[tuple[str, str, str], ...]


def emitted(content: str | None, tool_calls: Sequence[dict[str, Any]]) -> Emitted:
    """Return the answer that an assistant message with ``content`` and ``tool_calls`` holds."""
    calls = (
        (call["id"], call["function"]["name"], canonical_arguments(call["function"].get("arguments")))
        for call in tool_calls
    )
    return Emitted(content=content or None, calls=tuple(calls))


# What continues a live process: each waiting call's MCP result, or the user blocks of its next turn.
type _Resume = tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]


def _mcp_images(message: Message, provider: str) -> list[dict[str, Any]]:
    """Return a message's images as MCP tool-result content."""
    blocks = _images(message, provider)
    return [{"type": "image", "data": b["source"]["data"], "mimeType": b["source"]["media_type"]} for b in blocks]


def _mcp_result(message: Message, provider: str) -> dict[str, Any]:
    """Return the MCP result of one ``tool`` message: its text, its images, and whether the call failed."""
    result = tool_result_view(message)
    text = result.content if isinstance(result.content, str) else json.dumps(result.content)
    return {"content": [_text(text), *_mcp_images(message, provider)], "isError": result.is_error}


def decide(answer: Emitted, tail: Sequence[Message], provider: str) -> _Resume | None:
    """Return how ``tail`` continues the process that answered with ``answer``, or None when it must be rebuilt.

    ``tail`` starts at the request's last assistant message, which must be the answer the process gave. A process
    parked on tool calls continues only when exactly one result per call follows, and then at most the images the
    agent loop moved off the results, which ride on the last one. An idle process continues only when user messages
    alone follow.
    """
    answered, rest = tail[0], tail[1:]
    # The answer of a process is plain text, so a message that holds blocks is not one as the process gave it.
    same = isinstance(answered.content, str | None) and emitted(answered.content, answered.tool_calls or ()) == answer
    if not same or any(message.files or message.audio or message.videos for message in rest):
        return None
    call_ids = [call[0] for call in answer.calls]
    if not call_ids:
        blocks = _merged([block for message in rest for block in _user_blocks(message, provider)])
        return ({}, blocks) if blocks and all(message.role == "user" for message in rest) else None
    media = rest[-1] if rest and is_media_follow_up(rest[-1]) else None
    results = [message for message in rest if message is not media]
    answered_ids = [str(message.tool_call_id) for message in results if message.role == "tool"]
    if len(results) != len(call_ids) or sorted(answered_ids) != sorted(call_ids):
        return None
    answers = {str(message.tool_call_id): _mcp_result(message, provider) for message in results}
    if media is not None:
        answers[call_ids[-1]]["content"] += _mcp_images(media, provider)
    return {call_id: answers[call_id] for call_id in call_ids}, []
