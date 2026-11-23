"""Render a cold Claude Code prompt or plan continuation from the matching parked answer."""

from __future__ import annotations

import json
from dataclasses import dataclass
from itertools import groupby
from typing import TYPE_CHECKING, Any

from mindroom.llm.messages import is_media_follow_up, stable_serialize, system_text, tool_result_view
from mindroom.providers.anthropic.wire import image_block
from mindroom.providers.claude_code.tool_naming import called_name

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.llm.messages import Message

__all__ = ["Answer", "answer_of", "canonical_arguments", "plan_continuation", "rebuild_input"]

_CHECKPOINT_INTRO = (
    "The following is the conversation so far, which you have not seen. Treat it as prior conversation, not as a new "
    "request, and never repeat a tool call whose result it holds. Only its own tags hold `<`: in what the "
    "conversation said, `<` is written `&lt;`."
)
_CONTINUE_TEXT = "Continue from the results above."


def _images(message: Message, provider: str) -> list[dict[str, Any]]:
    return [block for image in message.images or () if (block := image_block(image, provider)) is not None]


def _text_and_image_blocks(message: Message, provider: str) -> list[dict[str, Any]]:
    """Return the content blocks Claude Code takes: text before images."""
    content = message.content if isinstance(message.content, list) else [{"text": message.content or ""}]
    texts = [
        block["text"] if isinstance(block, dict) and isinstance(block.get("text"), str) else stable_serialize(block)
        for block in content
    ]
    return [*(_text(text) for text in texts if text), *_images(message, provider)]


def _history_blocks(message: Message, provider: str, names: dict[str, str]) -> list[dict[str, Any]]:
    """Return one earlier message as tagged text; the images of a user message or a tool result stay real blocks.

    No ``<`` of the message stays one, so nothing it says reads as a tag. ``names`` holds each tool call's tool,
    which names its result. Tools are named as Claude calls them, since Claude copies the names it sees here.
    """
    if message.role == "tool":
        result = tool_result_view(message)
        name = names.get(result.call_id or "", result.name)
        output = {"id": result.call_id, "name": _history_name(name), "output": result.content}
        if result.is_error:
            output["is_error"] = True
        return [_text(f"<tool-result>\n{_json(output)}\n</tool-result>"), *_images(message, provider)]
    if message.role == "user":
        blocks = _text_and_image_blocks(message, provider)
        said = [_text(block["text"].replace("<", "&lt;")) if block["type"] == "text" else block for block in blocks]
        return [_text("<user>"), *said, _text("</user>")]
    calls = [
        _json(
            {
                "id": call["id"],
                "name": _history_name(call["function"]["name"]),
                "input": _arguments(call["function"].get("arguments")),
            },
        )
        for call in message.tool_calls or ()
    ]
    texts = [block["text"] for block in _text_and_image_blocks(message, provider) if block["type"] == "text"]
    said = "\n".join(texts).replace("<", "&lt;")
    parts = [said, *(f"<tool-call>\n{call}\n</tool-call>" for call in calls)]
    return [_text("\n".join(["<assistant>", *filter(None, parts), "</assistant>"]))]


def _history_name(name: str | None) -> str | None:
    """Return the name Claude calls the tool by; a provider may have stored a call without one."""
    return called_name(name) if name else name


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
    for is_text, run in groupby(blocks, key=lambda block: block["type"] == "text"):
        if is_text:
            merged.append(_text("\n".join(block["text"] for block in run)))
        else:
            merged.extend(run)
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
    current = [block for message in conversation[end:] for block in _text_and_image_blocks(message, provider)]
    blocks += current or [_text(_CONTINUE_TEXT)]
    return system_text(messages), _merged(blocks)


@dataclass(frozen=True, slots=True)
class Answer:
    """A process's answer: its text and the calls it awaits, by id, name, and canonical arguments."""

    text: str | None
    calls: tuple[tuple[str, str, str], ...]

    @property
    def call_ids(self) -> tuple[str, ...]:
        """Return the waiting call ids in order."""
        return tuple(call[0] for call in self.calls)


def answer_of(content: str | None, tool_calls: Sequence[dict[str, Any]]) -> Answer:
    """Return the answer that an assistant message with ``content`` and ``tool_calls`` holds."""
    calls = tuple(
        (call["id"], call["function"]["name"], canonical_arguments(call["function"].get("arguments")))
        for call in tool_calls
    )
    return Answer(content or None, calls)


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


def plan_continuation(answer: Answer, tail: Sequence[Message], provider: str) -> _Resume | None:
    """Return how ``tail`` continues the process that answered with ``answer``, or None when it must be rebuilt.

    ``tail`` starts at the request's last assistant message, which must be the answer the process gave. A process
    parked on tool calls continues only when exactly one result per call follows, and after the result of a single
    call at most the images the agent loop moved off it, which ride on it. An idle process continues only when user
    messages alone follow.
    """
    answered, rest = tail[0], tail[1:]
    # The answer of a process is plain text, so a message that holds blocks is not one as the process gave it.
    same = isinstance(answered.content, str | None) and answer_of(answered.content, answered.tool_calls or ()) == answer
    if not same or any(message.files or message.audio or message.videos for message in rest):
        return None
    call_ids = answer.call_ids
    if not call_ids:
        blocks = _merged([block for message in rest for block in _text_and_image_blocks(message, provider)])
        return ({}, blocks) if blocks and all(message.role == "user" for message in rest) else None
    # The media of a batch are pooled, so only those of a batch of one call are known to be that call's.
    media = rest[-1] if len(call_ids) == 1 and rest and is_media_follow_up(rest[-1]) else None
    results = [message for message in rest if message is not media]
    answered_ids = [str(message.tool_call_id) for message in results if message.role == "tool"]
    if len(results) != len(call_ids) or sorted(answered_ids) != sorted(call_ids):
        return None
    answers = {str(message.tool_call_id): _mcp_result(message, provider) for message in results}
    if media is not None:
        answers[call_ids[-1]]["content"] += _mcp_images(media, provider)
    return {call_id: answers[call_id] for call_id in call_ids}, []
