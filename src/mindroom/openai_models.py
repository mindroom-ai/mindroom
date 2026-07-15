"""OpenAI and OpenAI-compatible models with cross-provider tool-call replay support."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

# The concrete module, not agno.models.azure: the package init wraps
# this import in a try/except stub whose fallback is not a Model.
from agno.models.azure.openai_chat import AzureOpenAI
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
    from agno.models.message import Message
    from agno.models.response import ModelResponse
    from agno.tools.function import Function
    from openai.types.responses import Response, ResponseStreamEvent
    from pydantic import BaseModel


def _messages_with_openai_tool_arguments(messages: list[Message]) -> list[Message]:
    """Fill arguments omitted by providers that represent empty tool input as an object."""
    normalized_messages: list[Message] = []
    for message in messages:
        if not message.tool_calls:
            normalized_messages.append(message)
            continue

        changed = False
        normalized_tool_calls: list[dict[str, Any]] = []
        for tool_call in message.tool_calls:
            function = tool_call["function"]
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


class _ChatToolArgumentsCompat:
    """Repair replayed tool calls before OpenAI Chat Completions formatting.

    Mix in ahead of an ``OpenAIChat`` subclass; ``_format_all_messages`` is the
    single choke point for all four request paths.  Deliberately not a
    dataclass and not an ``OpenAIChat`` subclass: either would re-apply
    ``OpenAIChat`` field defaults over provider-specific ones (base URL, name)
    during dataclass field collection.
    """

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
class MindRoomOpenAIChat(_ChatToolArgumentsCompat, OpenAIChat):
    """OpenAI Chat model that can replay tool calls from other providers."""


@dataclass
class MindRoomOpenAILike(_ChatToolArgumentsCompat, OpenAILike):
    """OpenAI-compatible endpoint model that can replay tool calls from other providers."""


@dataclass
class MindRoomAzureOpenAI(_ChatToolArgumentsCompat, AzureOpenAI):
    """Azure OpenAI model that can replay tool calls from other providers."""


@dataclass
class MindRoomOpenRouter(_ChatToolArgumentsCompat, OpenRouter):
    """OpenRouter model that can replay tool calls from other providers."""


@dataclass
class MindRoomDeepSeek(_ChatToolArgumentsCompat, DeepSeek):
    """DeepSeek model that can replay tool calls from other providers."""


@dataclass
class MindRoomLlamaCpp(_ChatToolArgumentsCompat, LlamaCpp):
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
