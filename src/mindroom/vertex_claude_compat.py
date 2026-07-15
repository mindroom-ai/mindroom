"""Mindroom compatibility helpers for Vertex Claude models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agno.exceptions import ModelProviderError
from agno.models.vertexai.claude import Claude as VertexAIClaude
from agno.utils.models.claude import format_messages, format_tools_for_model
from agno.utils.tokens import count_schema_tokens

from mindroom.claude_prompt_cache import prepare_claude_request_kwargs
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from agno.models.message import Message
    from agno.models.response import ModelResponse
    from agno.run.agent import RunOutput

logger = get_logger(__name__)

_PROMPT_ROLES = frozenset({"system", "developer", "instructions"})


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


@dataclass
class MindroomVertexAIClaude(VertexAIClaude):
    """Vertex Claude model with Mindroom-specific provider compatibility fixes."""

    context_window: int | None = None

    async def _count_request_input_tokens(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None,
        response_format: dict[str, Any] | type[Any] | None,
        compress_tool_results: bool,
    ) -> int:
        """Count the same provider-shaped payload used by the real request."""
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
        request_kwargs = prepare_claude_request_kwargs(self, request_kwargs)
        response = await self.get_async_client().messages.count_tokens(**request_kwargs)
        return response.input_tokens + count_schema_tokens(response_format, self.id)

    @staticmethod
    def _replay_trim_candidates(messages: list[Message]) -> tuple[list[Message], list[int]]:
        """Return the leading prompt messages and safe user-turn suffix starts."""
        prefix_end = 0
        while prefix_end < len(messages) and messages[prefix_end].role in _PROMPT_ROLES:
            prefix_end += 1
        prefix = messages[:prefix_end]
        user_starts = [index for index in range(prefix_end, len(messages)) if messages[index].role == "user"]
        return prefix, user_starts

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

        prefix, user_starts = self._replay_trim_candidates(messages)
        if not user_starts:
            msg = f"Vertex Claude request uses {original_tokens} input tokens; limit is {input_budget}."
            raise ModelProviderError(message=msg, model_name=self.name, model_id=self.id)

        best_messages: list[Message] | None = None
        best_tokens: int | None = None
        low = 0
        high = len(user_starts) - 1
        while low <= high:
            midpoint = (low + high) // 2
            candidate = [*prefix, *messages[user_starts[midpoint] :]]
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
