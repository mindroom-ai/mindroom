"""Mindroom compatibility helpers for Vertex Claude models."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from agno.exceptions import ModelProviderError
from agno.models.vertexai.claude import Claude as VertexAIClaude
from agno.utils.models.claude import format_messages, format_tools_for_model
from agno.utils.tokens import count_schema_tokens

from mindroom.claude_prompt_cache import prepare_claude_request_kwargs
from mindroom.logging_config import get_logger
from mindroom.token_budget import estimate_compaction_input_tokens, stable_serialize

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from agno.models.message import Message
    from agno.models.response import ModelResponse
    from agno.run.agent import RunOutput

logger = get_logger(__name__)

_EXACT_COUNT_THRESHOLD_RATIO = 0.5
_EXACT_COUNT_BLOCK_TYPES = frozenset({"document", "image"})
_VERTEX_TOOL_SEARCH_TYPE = "tool_search_tool_regex_20251119"
_VERTEX_TOOL_SEARCH_HISTORY_BLOCK_TYPES = frozenset({"server_tool_use", "tool_search_tool_result"})
# Vertex generation reports 213 input tokens for the native regex search tool
# on both Claude Haiku 4.5 and Sonnet 4.6. Keep a small margin because the
# count-tokens endpoint cannot count that server-side prefix itself.
_VERTEX_TOOL_SEARCH_TOKEN_RESERVE = 256


def _strip_vertex_claude_tool_strict(
    tools: list[dict[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    """Return Vertex-compatible tool definitions without mutating the caller's list.

    Agno 2.5.13 can emit OpenAI-style ``strict`` flags on tool definitions.
    Anthropic-on-Vertex rejects those provider-level fields with a 400 error
    (``tools.0.custom.strict``), while schema properties named ``strict`` are
    valid user data and must be preserved. Strip only the provider metadata here
    until Agno normalizes Vertex Claude tool payloads itself.
    """
    if not tools:
        return tools

    changed = False
    sanitized: list[dict[str, Any]] = []
    for tool in tools:
        next_tool = tool
        if "strict" in next_tool:
            next_tool = dict(next_tool)
            next_tool.pop("strict", None)
            changed = True

        function = next_tool.get("function")
        if isinstance(function, dict) and "strict" in function:
            if next_tool is tool:
                next_tool = dict(next_tool)
            next_function = dict(function)
            next_function.pop("strict", None)
            next_tool["function"] = next_function
            changed = True

        sanitized.append(next_tool)

    return sanitized if changed else tools


def _blocks_require_exact_count(blocks: list[Any]) -> bool:
    """Return whether content includes media that cannot be estimated safely."""
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") in _EXACT_COUNT_BLOCK_TYPES:
            return True
        source = block.get("source")
        data = source.get("data") if isinstance(source, dict) else None
        if data:
            return True
        nested = block.get("content")
        if isinstance(nested, list) and _blocks_require_exact_count(nested):
            return True
    return False


def _request_requires_exact_count(request_kwargs: dict[str, Any]) -> bool:
    """Return whether any provider-formatted message contains media."""
    messages = request_kwargs.get("messages")
    if not isinstance(messages, list):
        return False
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list) and _blocks_require_exact_count(content):
            return True
    return False


def _request_for_vertex_token_count(request_kwargs: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Build a countable equivalent of a native tool-search request.

    Vertex generation accepts Anthropic's native tool-search schema, but its
    count-tokens endpoint rejects the search tool, ``defer_loading``, and the
    two server-search history block types. Deferred definitions are absent
    from the model context until selected, so omit them from the count payload.
    Preserve prior search traces as text and reserve the fixed server-side
    search prefix separately.
    """
    tools = request_kwargs.get("tools")
    if not isinstance(tools, list) or not any(
        isinstance(tool, dict) and tool.get("type") == _VERTEX_TOOL_SEARCH_TYPE for tool in tools
    ):
        return request_kwargs, 0

    count_kwargs = dict(request_kwargs)
    count_tools = [
        tool
        for tool in tools
        if not (
            isinstance(tool, dict)
            and (tool.get("type") == _VERTEX_TOOL_SEARCH_TYPE or tool.get("defer_loading") is True)
        )
    ]
    if count_tools:
        count_kwargs["tools"] = count_tools
    else:
        count_kwargs.pop("tools", None)

    messages = request_kwargs.get("messages")
    if isinstance(messages, list):
        count_messages = list(messages)
        for message_index, message in enumerate(messages):
            if not isinstance(message, dict):
                continue
            message_dict = cast("dict[str, Any]", message)
            content = message_dict.get("content")
            if not isinstance(content, list):
                continue
            count_content = [
                {"type": "text", "text": stable_serialize(block)}
                if isinstance(block, dict) and block.get("type") in _VERTEX_TOOL_SEARCH_HISTORY_BLOCK_TYPES
                else block
                for block in content
            ]
            if count_content != content:
                count_message = dict(message_dict)
                count_message["content"] = count_content
                count_messages[message_index] = count_message
        count_kwargs["messages"] = count_messages

    return count_kwargs, _VERTEX_TOOL_SEARCH_TOKEN_RESERVE


@dataclass
class MindroomVertexAIClaude(VertexAIClaude):
    """Vertex Claude model with Mindroom-specific provider compatibility fixes."""

    context_window: int | None = None

    def _request_input_kwargs(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None,
        response_format: dict[str, Any] | type[Any] | None,
        compress_tool_results: bool,
    ) -> dict[str, Any]:
        """Build the provider-shaped payload used for input token counting."""
        anthropic_messages, system_prompt = format_messages(
            messages,
            compress_tool_results=compress_tool_results,
            append_trailing_user_message=self.append_trailing_user_message,
            trailing_user_message_content=self.trailing_user_message_content,
            enable_citations=self.citations and not self._output_format_enabled(response_format),
        )
        request_kwargs: dict[str, Any] = {
            "messages": anthropic_messages,
            "model": self.id,
        }
        system = self._build_system(system_prompt)
        if system:
            request_kwargs["system"] = system
        sanitized_tools = _strip_vertex_claude_tool_strict(tools)
        if sanitized_tools:
            request_kwargs["tools"] = format_tools_for_model(sanitized_tools)
        if self.thinking:
            request_kwargs["thinking"] = self.thinking
        return prepare_claude_request_kwargs(self, request_kwargs)

    def _estimate_request_input_tokens(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None,
        response_format: dict[str, Any] | type[Any] | None,
        compress_tool_results: bool,
    ) -> int | None:
        """Estimate text payloads; return ``None`` when exact counting is required.

        CPU-bound (message formatting plus a tokenizer encode); async callers
        must off-load it to a worker thread.
        """
        request_kwargs = self._request_input_kwargs(
            messages,
            tools=tools,
            response_format=response_format,
            compress_tool_results=compress_tool_results,
        )
        if _request_requires_exact_count(request_kwargs):
            return None
        return estimate_compaction_input_tokens(stable_serialize(request_kwargs)) + count_schema_tokens(
            response_format,
            self.id,
        )

    async def _count_request_input_tokens(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None,
        response_format: dict[str, Any] | type[Any] | None,
        compress_tool_results: bool,
    ) -> int:
        """Count the provider-shaped payload using Vertex's exact tokenizer."""
        request_kwargs = await asyncio.to_thread(
            self._request_input_kwargs,
            messages,
            tools=tools,
            response_format=response_format,
            compress_tool_results=compress_tool_results,
        )
        count_kwargs, tool_search_reserve = _request_for_vertex_token_count(request_kwargs)
        response = await self.get_async_client().messages.count_tokens(**count_kwargs)
        return response.input_tokens + tool_search_reserve + count_schema_tokens(response_format, self.id)

    @staticmethod
    def _replay_trim_candidates(messages: list[Message]) -> list[int]:
        """Return safe history-user cuts plus the drop-all-history cut."""
        first_history_index = next(
            (index for index, message in enumerate(messages) if message.from_history),
            None,
        )
        if first_history_index is None:
            return []
        history_user_starts = [
            index for index, message in enumerate(messages) if message.from_history and message.role == "user"
        ]
        return [cut for cut in [*history_user_starts, len(messages)] if cut > first_history_index]

    @staticmethod
    def _messages_after_history_cut(messages: list[Message], cut: int) -> list[Message]:
        """Drop only replay messages older than one safe history boundary."""
        return [message for index, message in enumerate(messages) if not message.from_history or index >= cut]

    async def _fit_request_messages(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None,
        response_format: dict[str, Any] | type[Any] | None,
        compress_tool_results: bool,
    ) -> list[Message]:
        """Drop the oldest replay turns until the exact request fits."""
        if self.context_window is None:
            return messages
        output_reserve = self.max_tokens or 0
        input_budget = self.context_window - output_reserve
        if input_budget <= 0:
            msg = "Vertex Claude context window leaves no room for input after the configured output reserve."
            raise ModelProviderError(message=msg, model_name=self.name, model_id=self.id)

        estimated_tokens = await asyncio.to_thread(
            self._estimate_request_input_tokens,
            messages,
            tools=tools,
            response_format=response_format,
            compress_tool_results=compress_tool_results,
        )
        if estimated_tokens is not None and estimated_tokens < input_budget * _EXACT_COUNT_THRESHOLD_RATIO:
            return messages

        async def _count(candidate: list[Message]) -> int:
            return await self._count_request_input_tokens(
                candidate,
                tools=tools,
                response_format=response_format,
                compress_tool_results=compress_tool_results,
            )

        original_tokens = await _count(messages)
        if original_tokens <= input_budget:
            return messages

        replay_cuts = self._replay_trim_candidates(messages)
        if not replay_cuts:
            msg = f"Vertex Claude request uses {original_tokens} input tokens; limit is {input_budget}."
            raise ModelProviderError(message=msg, model_name=self.name, model_id=self.id)

        best_messages: list[Message] | None = None
        best_tokens: int | None = None
        low = 0
        high = len(replay_cuts) - 1
        while low <= high:
            midpoint = (low + high) // 2
            candidate = self._messages_after_history_cut(messages, replay_cuts[midpoint])
            candidate_tokens = await _count(candidate)
            if candidate_tokens <= input_budget:
                best_messages = candidate
                best_tokens = candidate_tokens
                high = midpoint - 1
            else:
                low = midpoint + 1

        if best_messages is None or best_tokens is None:
            msg = f"Vertex Claude current turn uses more than the {input_budget}-token input limit."
            raise ModelProviderError(message=msg, model_name=self.name, model_id=self.id)

        logger.warning(
            "vertex_claude_request_history_trimmed",
            input_tokens=original_tokens,
            fitted_input_tokens=best_tokens,
            input_budget=input_budget,
            dropped_message_count=len(messages) - len(best_messages),
        )
        return best_messages

    async def ainvoke(
        self,
        messages: list[Message],
        assistant_message: Message,
        response_format: dict[str, Any] | type[Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        run_response: RunOutput | None = None,
        compress_tool_results: bool = False,
    ) -> ModelResponse:
        """Fit every async request, including requests after tool results."""
        fitted_messages = await self._fit_request_messages(
            messages,
            tools=tools,
            response_format=response_format,
            compress_tool_results=compress_tool_results,
        )
        return await super().ainvoke(
            fitted_messages,
            assistant_message,
            response_format=response_format,
            tools=tools,
            tool_choice=tool_choice,
            run_response=run_response,
            compress_tool_results=compress_tool_results,
        )

    async def ainvoke_stream(
        self,
        messages: list[Message],
        assistant_message: Message,
        response_format: dict[str, Any] | type[Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        run_response: RunOutput | None = None,
        compress_tool_results: bool = False,
    ) -> AsyncIterator[ModelResponse]:
        """Fit every async streaming request, including tool-loop requests."""
        fitted_messages = await self._fit_request_messages(
            messages,
            tools=tools,
            response_format=response_format,
            compress_tool_results=compress_tool_results,
        )
        async for response in super().ainvoke_stream(
            fitted_messages,
            assistant_message,
            response_format=response_format,
            tools=tools,
            tool_choice=tool_choice,
            run_response=run_response,
            compress_tool_results=compress_tool_results,
        ):
            yield response

    def _prepare_request_kwargs(
        self,
        system_message: str,
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | type[Any] | None = None,
        messages: list[Any] | None = None,
    ) -> dict[str, Any]:
        return super()._prepare_request_kwargs(
            system_message=system_message,
            tools=_strip_vertex_claude_tool_strict(tools),
            response_format=response_format,
            messages=messages,
        )

    def _has_beta_features(
        self,
        response_format: dict[str, Any] | type[Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> bool:
        return super()._has_beta_features(
            response_format=response_format,
            tools=_strip_vertex_claude_tool_strict(tools),
        )
