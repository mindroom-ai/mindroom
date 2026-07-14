"""OpenAI Responses model with native deferred-tool search replay."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agno.models.openai import OpenAIResponses
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
