"""Vertex Claude prompt-cache helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agno.models.vertexai.claude import Claude as VertexAIClaude

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


def copy_messages_with_vertex_prompt_cache_breakpoint(  # noqa: C901
    messages: list[Message],
    model: VertexAIClaude,
) -> list[Message]:
    """Copy request messages and mark the last cacheable block for Vertex Claude."""
    prepared_messages = [message.model_copy(deep=True) for message in messages]
    cache_control = _vertex_claude_cache_control(model)
    cacheable_block_types = {"document", "image", "text"}

    for message_index in range(len(prepared_messages) - 1, -1, -1):
        message = prepared_messages[message_index]
        if message.role != "user":
            continue
        content = message.content
        if isinstance(content, str):
            if not content:
                continue
            prepared_messages[message_index].content = [
                {"type": "text", "text": content, "cache_control": dict(cache_control)},
            ]
            return prepared_messages
        if not isinstance(content, list):
            continue
        for block_index in range(len(content) - 1, -1, -1):
            block = content[block_index]
            if isinstance(block, str):
                content_copy = list(content)
                content_copy[block_index] = {
                    "type": "text",
                    "text": block,
                    "cache_control": dict(cache_control),
                }
                prepared_messages[message_index].content = content_copy
                return prepared_messages
            if not isinstance(block, dict):
                continue
            if block.get("type") not in cacheable_block_types:
                continue
            if block.get("type") == "text" and not str(block.get("text", "")):
                continue
            block_copy = dict(block)
            block_copy["cache_control"] = dict(cache_control)
            content_copy = list(content)
            content_copy[block_index] = block_copy
            prepared_messages[message_index].content = content_copy
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
