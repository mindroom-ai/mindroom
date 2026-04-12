"""Vertex Claude prompt-cache helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from agno.models.vertexai.claude import Claude as VertexAIClaude
from agno.utils.models.claude import _format_file_for_message, _format_image_for_message

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from agno.models.message import Message
    from agno.models.response import ModelResponse
    from agno.run.agent import RunOutput
    from pydantic import BaseModel

_VERTEX_CLAUDE_PROMPT_CACHE_HOOK_ATTR = "_mindroom_vertex_claude_prompt_cache_hook_installed"


def _vertex_claude_cache_control(model: VertexAIClaude) -> dict[str, str]:
    cache_control: dict[str, str] = {"type": "ephemeral"}
    if model.extended_cache_time is True:
        cache_control["ttl"] = "1h"
    return cache_control


def _normalized_message_content(content: object) -> list[object] | None:
    """Normalize supported message content shapes into one mutable block list."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if isinstance(content, list):
        return list(content)
    if content is None:
        return []
    return None


def _append_formatted_media_blocks(message: Message, blocks: list[object]) -> bool:
    """Append Claude-formatted image and file blocks and report whether any were added."""
    media_embedded = False
    if message.images is not None:
        for image in message.images:
            image_block = _format_image_for_message(image)
            if image_block is None:
                continue
            blocks.append(image_block)
            media_embedded = True

    if message.files is not None:
        for file in message.files:
            file_block = _format_file_for_message(file)
            if file_block is None:
                continue
            blocks.append(file_block)
            media_embedded = True

    return media_embedded


def _collect_user_message_blocks(message: Message) -> tuple[list[object] | None, bool]:
    """Return the effective Claude blocks for one user turn, including appended media."""
    if message.role != "user":
        return None, False

    blocks = _normalized_message_content(message.content)
    if blocks is None:
        return None, False

    media_embedded = _append_formatted_media_blocks(message, blocks)
    return blocks, media_embedded


def _mark_last_cacheable_block(
    content: list[object],
    *,
    cache_control: dict[str, str],
) -> list[object] | None:
    """Return a copied content list with cache control attached to the final cacheable block."""
    cacheable_block_types = {"document", "image", "text", "tool_result"}

    for block_index in range(len(content) - 1, -1, -1):
        block = content[block_index]
        if isinstance(block, str):
            content_copy = list(content)
            content_copy[block_index] = {
                "type": "text",
                "text": block,
                "cache_control": dict(cache_control),
            }
            return content_copy
        if not isinstance(block, dict):
            continue
        typed_block = cast("dict[str, object]", block)
        block_type = typed_block.get("type")
        if block_type not in cacheable_block_types:
            continue
        if block_type == "text" and not str(typed_block.get("text", "")):
            continue
        block_copy = dict(typed_block)
        block_copy["cache_control"] = dict(cache_control)
        content_copy = list(content)
        content_copy[block_index] = block_copy
        return content_copy
    return None


def copy_messages_with_vertex_prompt_cache_breakpoint(
    messages: list[Message],
    model: VertexAIClaude,
) -> list[Message]:
    """Copy request messages and mark the last cacheable block for Vertex Claude."""
    prepared_messages = [message.model_copy(deep=True) for message in messages]
    cache_control = _vertex_claude_cache_control(model)

    for message_index in range(len(prepared_messages) - 1, -1, -1):
        message = prepared_messages[message_index]
        content, media_embedded = _collect_user_message_blocks(message)
        if content is None:
            raw_content = message.content
            if isinstance(raw_content, str):
                if not raw_content:
                    continue
                message.content = [{"type": "text", "text": raw_content, "cache_control": dict(cache_control)}]
                return prepared_messages
            if not isinstance(raw_content, list):
                continue
            content = raw_content

        marked_content = _mark_last_cacheable_block(content, cache_control=cache_control)
        if marked_content is None:
            continue
        message.content = marked_content
        if media_embedded:
            message.images = None
            message.files = None
        return prepared_messages
    return prepared_messages


def install_vertex_claude_prompt_cache_hook(model: object) -> None:
    """Mark the final cacheable message block so Vertex Claude can reuse the full prefix."""
    if not isinstance(model, VertexAIClaude):
        return
    model_dict = vars(model)
    if model_dict.get(_VERTEX_CLAUDE_PROMPT_CACHE_HOOK_ATTR) is True:
        return
    original_invoke = model.invoke
    original_ainvoke = model.ainvoke
    original_invoke_stream = model.invoke_stream
    original_ainvoke_stream = model.ainvoke_stream
    model_dict[_VERTEX_CLAUDE_PROMPT_CACHE_HOOK_ATTR] = True

    def _prepare_messages(messages: list[Message]) -> list[Message]:
        if not model.cache_system_prompt:
            return messages
        return copy_messages_with_vertex_prompt_cache_breakpoint(messages, model)

    def _invoke_with_prompt_cache(
        messages: list[Message],
        assistant_message: Message,
        response_format: dict[str, object] | type[BaseModel] | None = None,
        tools: list[dict[str, object]] | None = None,
        tool_choice: str | dict[str, object] | None = None,
        run_response: RunOutput | None = None,
        compress_tool_results: bool = False,
    ) -> ModelResponse:
        return original_invoke(
            messages=_prepare_messages(messages),
            assistant_message=assistant_message,
            response_format=response_format,
            tools=tools,
            tool_choice=tool_choice,
            run_response=run_response,
            compress_tool_results=compress_tool_results,
        )

    async def _ainvoke_with_prompt_cache(
        messages: list[Message],
        assistant_message: Message,
        response_format: dict[str, object] | type[BaseModel] | None = None,
        tools: list[dict[str, object]] | None = None,
        tool_choice: str | dict[str, object] | None = None,
        run_response: RunOutput | None = None,
        compress_tool_results: bool = False,
    ) -> ModelResponse:
        return await original_ainvoke(
            messages=_prepare_messages(messages),
            assistant_message=assistant_message,
            response_format=response_format,
            tools=tools,
            tool_choice=tool_choice,
            run_response=run_response,
            compress_tool_results=compress_tool_results,
        )

    def _invoke_stream_with_prompt_cache(
        messages: list[Message],
        assistant_message: Message,
        response_format: dict[str, object] | type[BaseModel] | None = None,
        tools: list[dict[str, object]] | None = None,
        tool_choice: str | dict[str, object] | None = None,
        run_response: RunOutput | None = None,
        compress_tool_results: bool = False,
    ) -> Iterator[ModelResponse]:
        yield from original_invoke_stream(
            messages=_prepare_messages(messages),
            assistant_message=assistant_message,
            response_format=response_format,
            tools=tools,
            tool_choice=tool_choice,
            run_response=run_response,
            compress_tool_results=compress_tool_results,
        )

    async def _ainvoke_stream_with_prompt_cache(
        messages: list[Message],
        assistant_message: Message,
        response_format: dict[str, object] | type[BaseModel] | None = None,
        tools: list[dict[str, object]] | None = None,
        tool_choice: str | dict[str, object] | None = None,
        run_response: RunOutput | None = None,
        compress_tool_results: bool = False,
    ) -> AsyncIterator[ModelResponse]:
        async for chunk in original_ainvoke_stream(
            messages=_prepare_messages(messages),
            assistant_message=assistant_message,
            response_format=response_format,
            tools=tools,
            tool_choice=tool_choice,
            run_response=run_response,
            compress_tool_results=compress_tool_results,
        ):
            yield chunk

    model_dict["invoke"] = _invoke_with_prompt_cache
    model_dict["ainvoke"] = _ainvoke_with_prompt_cache
    model_dict["invoke_stream"] = _invoke_stream_with_prompt_cache
    model_dict["ainvoke_stream"] = _ainvoke_stream_with_prompt_cache
