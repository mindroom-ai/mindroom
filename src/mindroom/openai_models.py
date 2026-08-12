"""OpenAI and OpenAI-compatible models with cross-provider tool-call replay support."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, cast

from agno.models.deepseek import DeepSeek
from agno.models.llama_cpp import LlamaCpp
from agno.models.openai import OpenAIChat, OpenAIResponses
from agno.models.openai.like import OpenAILike
from agno.models.openrouter import OpenRouter
from openai.types.responses import ResponseOutputItemDoneEvent

from mindroom.openai_tool_search import (
    formatted_input_with_tool_search_items,
    model_deferred_tool_names,
    record_tool_search_items,
    request_params_with_deferred_tool_search,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agno.models.base import MessageData
    from agno.models.message import Message
    from agno.models.response import ModelResponse
    from agno.tools.function import Function
    from openai.types.responses import Response, ResponseStreamEvent
    from pydantic import BaseModel


def _openrouter_detail_values_match(left: object, right: object) -> bool:
    """Compare JSON-like metadata without equating booleans and numbers."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict) and isinstance(right, dict):
        left_dict = cast("dict[object, object]", left)
        right_dict = cast("dict[object, object]", right)
        return left_dict.keys() == right_dict.keys() and all(
            _openrouter_detail_values_match(left_dict[key], right_dict[key]) for key in left_dict
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _openrouter_detail_values_match(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    return left == right


def _openrouter_reasoning_text_detail_parts(detail: object) -> tuple[dict[str, object], str] | None:
    """Return exact metadata and text for a fragment that can be grouped."""
    if not isinstance(detail, dict) or not all(isinstance(key, str) for key in detail):
        return None
    detail_dict = cast("dict[str, object]", detail)
    if detail_dict.get("type") != "reasoning.text":
        return None
    text = detail_dict.get("text")
    if not isinstance(text, str):
        return None

    index = detail_dict.get("index")
    detail_id = detail_dict.get("id")
    has_stable_discriminator = type(index) in (int, float) or (isinstance(detail_id, str) and bool(detail_id))
    if not has_stable_discriminator:
        return None

    return ({key: value for key, value in detail_dict.items() if key != "text"}, text)


@dataclass
class _OpenRouterReasoningTextBlock:
    """One metadata template plus linearly accumulated adjacent text chunks."""

    metadata: dict[str, object]
    text_chunks: list[str]


def _append_openrouter_reasoning_detail_block(blocks: list[object], detail: object) -> None:
    """Append one detail to compact streaming blocks without concatenating text."""
    detail_parts = _openrouter_reasoning_text_detail_parts(detail)
    if detail_parts is None:
        blocks.append(detail)
        return

    metadata, text = detail_parts
    if blocks and isinstance(blocks[-1], _OpenRouterReasoningTextBlock):
        previous_block = blocks[-1]
        if _openrouter_detail_values_match(previous_block.metadata, metadata):
            previous_block.text_chunks.append(text)
            return
    blocks.append(_OpenRouterReasoningTextBlock(metadata=metadata, text_chunks=[text]))


def _materialized_openrouter_reasoning_detail_blocks(blocks: list[object]) -> list[object]:
    """Convert private streaming blocks into persistable provider dictionaries."""
    return [
        {**block.metadata, "text": "".join(block.text_chunks)}
        if isinstance(block, _OpenRouterReasoningTextBlock)
        else block
        for block in blocks
    ]


def _coalesced_openrouter_reasoning_details(details: object) -> object:
    """Join only adjacent, unambiguously matching OpenRouter text fragments."""
    if not isinstance(details, list):
        return details

    blocks: list[object] = []
    for detail in details:
        _append_openrouter_reasoning_detail_block(blocks, detail)
    return _materialized_openrouter_reasoning_detail_blocks(blocks)


_OPENROUTER_REASONING_DETAILS_BUFFER = "_mindroom_openrouter_reasoning_details_buffer"


# Agno 2.6.12 omits arguments for empty Anthropic tool inputs; agno-agi/agno#8970 proposes the source fix.
# Remove this repair only after upgrading to a release with that fix and migrating or dropping older histories.
def _messages_with_openai_tool_arguments(messages: list[Message]) -> list[Message]:
    """Repair function calls and remove sparse-stream placeholders from replay."""
    normalized_messages: list[Message] = []
    removed_tool_call_ids: set[str] = set()
    for message in messages:
        if message.role == "tool" and message.tool_call_id in removed_tool_call_ids:
            continue
        if message.role != "assistant" or not message.tool_calls:
            normalized_messages.append(message)
            continue

        changed = False
        normalized_tool_calls: list[dict[str, Any]] = []
        for tool_call in message.tool_calls:
            function = tool_call.get("function")
            if not isinstance(function, dict):
                tool_call_id = tool_call.get("id")
                if isinstance(tool_call_id, str):
                    removed_tool_call_ids.add(tool_call_id)
                changed = True
                continue
            if "arguments" in function:
                normalized_tool_calls.append(tool_call)
                continue
            normalized_tool_calls.append(
                {
                    **tool_call,
                    "function": {**function, "arguments": "{}"},
                },
            )
            changed = True

        normalized_messages.append(
            message.model_copy(update={"tool_calls": normalized_tool_calls}) if changed else message,
        )
    return normalized_messages


class ChatToolArgumentsCompat:
    """Repair replayed tool calls before OpenAI Chat Completions formatting.

    Mix in ahead of an ``OpenAIChat`` subclass; ``_format_all_messages`` is the
    single choke point for all four request paths.  Deliberately not a
    dataclass and not an ``OpenAIChat`` subclass: either would re-apply
    ``OpenAIChat`` field defaults over provider-specific ones (base URL, name)
    during dataclass field collection.
    """

    def parse_tool_calls(self, tool_calls_data: list[Any]) -> list[dict[str, Any]]:
        """Drop empty slots created when a streamed tool-call index starts above zero."""
        parsed = super().parse_tool_calls(tool_calls_data)  # ty: ignore[unresolved-attribute]
        return [tool_call for tool_call in parsed if isinstance(tool_call.get("function"), dict)]

    def _format_all_messages(
        self,
        messages: list[Message],
        compress_tool_results: bool = False,
    ) -> list[dict[str, Any]]:
        """Supply the arguments string required by OpenAI for every tool call."""
        return super()._format_all_messages(  # ty: ignore[unresolved-attribute]  # resolved by the OpenAIChat sibling base
            _messages_with_openai_tool_arguments(messages),
            compress_tool_results,
        )


@dataclass
class MindRoomOpenAIChat(ChatToolArgumentsCompat, OpenAIChat):
    """OpenAI Chat model that can replay tool calls from other providers."""


@dataclass
class MindRoomOpenAILike(ChatToolArgumentsCompat, OpenAILike):
    """OpenAI-compatible endpoint model that can replay tool calls from other providers."""


@dataclass
class MindRoomOpenRouter(ChatToolArgumentsCompat, OpenRouter):
    """OpenRouter model that can replay tool calls from other providers."""

    def _populate_stream_data(
        self,
        stream_data: MessageData,
        model_response_delta: ModelResponse,
    ) -> Iterator[ModelResponse]:
        """Accumulate reasoning details without retaining each text token separately."""
        provider_data = model_response_delta.provider_data
        if (
            provider_data
            and "reasoning_details" in provider_data
            and not isinstance(provider_data["reasoning_details"], list)
            and hasattr(stream_data, _OPENROUTER_REASONING_DETAILS_BUFFER)
        ):
            delattr(stream_data, _OPENROUTER_REASONING_DETAILS_BUFFER)
        if provider_data and isinstance(provider_data.get("reasoning_details"), list):
            buffered_details = getattr(stream_data, _OPENROUTER_REASONING_DETAILS_BUFFER, None)
            if not isinstance(buffered_details, list):
                buffered_details = []
                if stream_data.response_provider_data is not None:
                    existing_details = stream_data.response_provider_data.pop("reasoning_details", None)
                    if isinstance(existing_details, list):
                        for detail in existing_details:
                            _append_openrouter_reasoning_detail_block(buffered_details, detail)
                setattr(stream_data, _OPENROUTER_REASONING_DETAILS_BUFFER, buffered_details)
            for detail in provider_data["reasoning_details"]:
                _append_openrouter_reasoning_detail_block(buffered_details, detail)

            remaining_provider_data = {key: value for key, value in provider_data.items() if key != "reasoning_details"}
            sanitized_delta = replace(model_response_delta, provider_data=remaining_provider_data)

            for _ in super()._populate_stream_data(stream_data, sanitized_delta):
                yield model_response_delta
            return

        yield from super()._populate_stream_data(stream_data, model_response_delta)

    def _populate_assistant_message_from_stream_data(
        self,
        assistant_message: Message,
        stream_data: MessageData,
    ) -> None:
        """Materialize buffered reasoning fragments once before persistence."""
        buffered_details = getattr(stream_data, _OPENROUTER_REASONING_DETAILS_BUFFER, None)
        if isinstance(buffered_details, list):
            if stream_data.response_provider_data is None:
                stream_data.response_provider_data = {}
            stream_data.response_provider_data["reasoning_details"] = _materialized_openrouter_reasoning_detail_blocks(
                buffered_details,
            )
            delattr(stream_data, _OPENROUTER_REASONING_DETAILS_BUFFER)
        super()._populate_assistant_message_from_stream_data(assistant_message, stream_data)

    def _format_message(self, message: Message, compress_tool_results: bool = False) -> dict[str, Any]:
        """Compact persisted reasoning details on replay without mutating history."""
        if message.role == "assistant" and message.provider_data:
            details = message.provider_data.get("reasoning_details")
            normalized_details = _coalesced_openrouter_reasoning_details(details)
            if normalized_details != details:
                message = message.model_copy(
                    update={
                        "provider_data": {
                            **message.provider_data,
                            "reasoning_details": normalized_details,
                        },
                    },
                )
        return super()._format_message(message, compress_tool_results)


@dataclass
class MindRoomDeepSeek(ChatToolArgumentsCompat, DeepSeek):
    """DeepSeek model that can replay tool calls from other providers."""


@dataclass
class MindRoomLlamaCpp(ChatToolArgumentsCompat, LlamaCpp):
    """llama.cpp server model that can replay tool calls from other providers."""


@dataclass
class MindRoomOpenAIResponses(OpenAIResponses):
    """OpenAI Responses model that preserves native tool-search state."""

    def get_request_params(
        self,
        messages: list[Message] | None = None,
        response_format: dict[Any, Any] | type[BaseModel] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Tag deferred functions and add hosted tool search."""
        request_params = super().get_request_params(
            messages=messages,
            response_format=response_format,
            tools=tools,
            tool_choice=tool_choice,
        )
        return request_params_with_deferred_tool_search(request_params, model_deferred_tool_names(self))

    def _format_messages(
        self,
        messages: list[Message],
        compress_tool_results: bool = False,
        tools: list[Function | dict[str, Any]] | None = None,
    ) -> list[Any]:
        """Reinsert captured tool-search items that Agno drops from history."""
        messages = _messages_with_openai_tool_arguments(messages)
        formatted_input = super()._format_messages(messages, compress_tool_results, tools=tools)
        return formatted_input_with_tool_search_items(messages, formatted_input)

    def _parse_provider_response(self, response: Response, **kwargs: object) -> ModelResponse:
        """Capture tool-search output items that Agno's parser drops."""
        model_response = super()._parse_provider_response(response, **kwargs)
        record_tool_search_items(model_response, response.output)
        return model_response

    def _parse_provider_response_delta(
        self,
        stream_event: ResponseStreamEvent,
        assistant_message: Message,
        tool_use: dict[str, Any],
    ) -> tuple[ModelResponse, dict[str, Any]]:
        """Capture streamed tool-search output items that Agno drops."""
        model_response, tool_use = super()._parse_provider_response_delta(stream_event, assistant_message, tool_use)
        if isinstance(stream_event, ResponseOutputItemDoneEvent):
            record_tool_search_items(model_response, [stream_event.item])
        return model_response, tool_use
