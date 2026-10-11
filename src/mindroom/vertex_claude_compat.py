"""Mindroom compatibility helpers for Vertex Claude models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agno.models.vertexai.claude import Claude as VertexAIClaude

from mindroom.agno_compat_vertex_claude_tools import (
    strip_vertex_claude_tool_strict,
)
from mindroom.claude_compat import ClaudeProviderCompat
from mindroom.native_compaction import common_native_endpoint

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from agno.models.message import Message
    from agno.models.response import ModelResponse
    from agno.run.agent import RunOutput
    from anthropic import AnthropicVertex, AsyncAnthropicVertex


def _messages_with_replay_safe_reasoning(messages: list[Message]) -> list[Message]:
    """Omit reasoning that cannot be replayed as an Anthropic thinking block."""
    sanitized_messages: list[Message] | None = None
    for index, message in enumerate(messages):
        if message.reasoning_content is None or message.provider_data is None:
            continue
        signature = message.provider_data.get("signature")
        if isinstance(signature, str) and signature:
            continue
        if sanitized_messages is None:
            sanitized_messages = list(messages)
        sanitized_message = message.model_copy(deep=True)
        sanitized_message.reasoning_content = None
        sanitized_messages[index] = sanitized_message
    return sanitized_messages if sanitized_messages is not None else messages


@dataclass
class MindroomVertexAIClaude(ClaudeProviderCompat, VertexAIClaude):
    """Vertex Claude model with Mindroom-specific provider compatibility fixes."""

    client: AnthropicVertex | None = None
    async_client: AsyncAnthropicVertex | None = None

    def native_compaction_endpoint(self) -> str:
        """Keep Vertex checkpoint replay inside its project and endpoint."""
        clients = [client for client in (self.async_client, self.client) if client is not None]
        if clients:
            return common_native_endpoint(
                [f"{str(client.base_url).rstrip('/')}|{client.project_id}|{client.region}" for client in clients],
            )
        params = self._get_client_params()
        project, region = params["project_id"], params["region"]
        default_endpoint = {
            "global": "https://aiplatform.googleapis.com/v1",
            "us": "https://aiplatform.us.rep.googleapis.com/v1",
            "eu": "https://aiplatform.eu.rep.googleapis.com/v1",
        }.get(region, f"https://{region}-aiplatform.googleapis.com/v1")
        endpoint = str(params["base_url"] or default_endpoint)
        return f"{endpoint.rstrip('/')}|{project}|{region}"

    def _request_messages(self, messages: list[Message]) -> list[Message]:
        """Project the route's replay and drop reasoning that cannot replay as a signed thinking block."""
        return _messages_with_replay_safe_reasoning(self.native_replay_messages(messages))

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
        """Replay only reasoning Vertex can verify, on every request including tool-loop requests."""
        return await super().ainvoke(
            self._request_messages(messages),
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
        """Replay only reasoning Vertex can verify, on every streaming request including tool-loop requests."""
        async for response in super().ainvoke_stream(
            self._request_messages(messages),
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
            tools=strip_vertex_claude_tool_strict(tools),
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
            tools=strip_vertex_claude_tool_strict(tools),
        )
