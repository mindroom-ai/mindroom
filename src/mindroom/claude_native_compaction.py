"""Claude native compaction transport shared by direct Anthropic and Vertex."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from anthropic.lib.streaming import MessageStopEvent, ParsedBetaMessageStopEvent, ParsedMessageStopEvent
from anthropic.types.beta import BetaUsage

from mindroom.native_compaction import (
    NativeCompactionModel,
    checkpoint_items,
    native_replay_messages,
    record_native_checkpoint,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from agno.metrics import MessageMetrics
    from agno.models.message import Message
    from agno.models.response import ModelResponse
    from agno.run.agent import RunOutput
    from anthropic import Anthropic, AsyncAnthropic
    from anthropic.types import Message as AnthropicMessage
    from anthropic.types import MessageDeltaUsage, Usage
    from anthropic.types.beta import BetaMessage

_COMPACTION_BETA = "compact-2026-01-12"
_SUPPORTED_MODELS = (
    "claude-sonnet-4-6",
    "claude-sonnet-5",
    "claude-opus-4-6",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-opus-5",
    "claude-fable-5",
    "claude-mythos-5",
    "claude-mythos-preview",
)


class ClaudeNativeCompaction(NativeCompactionModel):
    """Adapt native checkpoints without changing the stored conversation."""

    context_management: dict[str, Any] | None
    request_params: dict[str, Any] | None
    client_params: dict[str, Any] | None
    client: Anthropic | None
    async_client: AsyncAnthropic | None

    def native_compaction_supported(self) -> bool:
        """Respect explicit context-management settings and supported Claude models."""
        return (
            self.provider in {"Anthropic", "VertexAI"}
            and self.id.startswith(_SUPPORTED_MODELS)
            and self.context_management is None
            and "context_management" not in (self.request_params or {})
            and (self.provider == "VertexAI" or self.native_compaction_endpoint() == "https://api.anthropic.com")
        )

    def native_compaction_endpoint(self) -> str:
        """Bind checkpoints to the concrete model's client route."""
        client = self.async_client or self.client
        if client is not None:
            return str(client.base_url).rstrip("/")
        return str(
            (self.client_params or {}).get("base_url")
            or os.getenv("ANTHROPIC_BASE_URL")
            or "https://api.anthropic.com",
        ).rstrip("/")

    def configure_native_compaction(self, *, threshold: int | None, history_generation: str = "") -> None:
        """Claude requires at least 50,000 input tokens before compaction."""
        if threshold is not None and threshold < 50000:
            threshold = None
        super().configure_native_compaction(threshold=threshold, history_generation=history_generation)

    def _has_beta_features(
        self,
        response_format: dict[str, Any] | type[Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> bool:
        return self.native_compaction is not None or super()._has_beta_features(  # ty: ignore[unresolved-attribute]
            response_format=response_format,
            tools=tools,
        )

    def get_request_params(
        self,
        response_format: dict[str, Any] | type[Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Supply the edit on Vertex as well as the direct Anthropic route."""
        params = super().get_request_params(response_format=response_format, tools=tools)  # ty: ignore[unresolved-attribute]
        if self.context_management is not None:
            params.setdefault("context_management", self.context_management)
        if self.native_compaction is not None:
            params["context_management"] = {
                "edits": [
                    {
                        "type": "compact_20260112",
                        "trigger": {"type": "input_tokens", "value": self.native_compaction.threshold},
                        "pause_after_compaction": False,
                        "instructions": (
                            "Summarize the conversation for continuation inside <summary></summary> tags. "
                            "Preserve user requests, decisions, constraints, completed work, and next steps. "
                            "Do not repeat system instructions. Do not call any tools; respond with text only."
                        ),
                    },
                ],
            }
            params["betas"] = list(dict.fromkeys([*(params.get("betas") or []), _COMPACTION_BETA]))
        context_management = params.get("context_management") or {}
        for edit in context_management.get("edits", []):
            if edit.get("type") == "compact_20260112" and edit.get("pause_after_compaction"):
                msg = "pause_after_compaction=True is unsupported; MindRoom requires automatic continuation."
                raise ValueError(msg)
        return params

    def native_replay_messages(self, messages: list[Message]) -> list[Message]:
        """Restore canonical blocks on foreign routes, or select checkpoint plus tail."""
        route = self.native_compaction.route if self.native_compaction is not None else None
        prepared = native_replay_messages(messages, route)
        result: list[Message] = []
        for message in prepared:
            data = message.provider_data
            if not data:
                result.append(message)
                continue
            items = checkpoint_items(message, route)
            next_data = dict(data)
            for key in ("content_blocks", "server_tool_blocks"):
                blocks = data.get(key)
                if isinstance(blocks, list):
                    next_data[key] = [block for block in blocks if block.get("type") != "compaction"]
            if items:
                next_data["content_blocks"] = items
            result.append(message.model_copy(update={"provider_data": next_data}))
        return result

    def _parse_provider_response(
        self,
        response: AnthropicMessage | BetaMessage,
        response_format: dict[str, Any] | type[Any] | None = None,
        **kwargs: object,
    ) -> ModelResponse:
        parsed = super()._parse_provider_response(  # ty: ignore[unresolved-attribute]
            response,
            response_format=response_format,
            **kwargs,
        )
        self._record_native_checkpoint(parsed)
        return parsed

    def _parse_provider_response_delta(
        self,
        response: object,
        response_format: dict[str, Any] | type[Any] | None = None,
    ) -> ModelResponse:
        parsed = super()._parse_provider_response_delta(  # ty: ignore[unresolved-attribute]
            response,
            response_format=response_format,
        )
        if isinstance(response, (MessageStopEvent, ParsedMessageStopEvent, ParsedBetaMessageStopEvent)):
            self._record_native_checkpoint(parsed)
        return parsed

    def _record_native_checkpoint(self, parsed: ModelResponse) -> None:
        blocks = (parsed.provider_data or {}).get("content_blocks", [])
        # Agno omits null fields during serialization; restore Claude's explicit
        # no-op marker before the shared selector examines an opaque payload.
        items = [
            {**block, "content": block.get("content")} if block.get("type") == "compaction" else block
            for block in blocks
        ]
        record_native_checkpoint(parsed, items, self.native_compaction)

    def _get_metrics(self, response_usage: Usage | MessageDeltaUsage | BetaUsage) -> MessageMetrics:
        """Keep billed iteration totals separate from the final active context."""
        metrics = super()._get_metrics(response_usage)  # ty: ignore[unresolved-attribute]
        context_usage = {
            "input_tokens": metrics.input_tokens,
            "cache_read_tokens": metrics.cache_read_tokens,
            "cache_write_tokens": metrics.cache_write_tokens,
        }
        if isinstance(response_usage, BetaUsage) and response_usage.iterations:
            iterations = response_usage.iterations
            metrics.input_tokens = sum(usage.input_tokens for usage in iterations)
            metrics.output_tokens = sum(usage.output_tokens for usage in iterations)
            metrics.cache_read_tokens = sum(usage.cache_read_input_tokens for usage in iterations)
            metrics.cache_write_tokens = sum(usage.cache_creation_input_tokens for usage in iterations)
            metrics.total_tokens = metrics.input_tokens + metrics.output_tokens
            last = iterations[-1]
            context_usage = {
                "input_tokens": last.input_tokens,
                "cache_read_tokens": last.cache_read_input_tokens,
                "cache_write_tokens": last.cache_creation_input_tokens,
            }
        metrics.provider_metrics = {**(metrics.provider_metrics or {}), "context_usage": context_usage}
        return metrics

    def invoke(
        self,
        messages: list[Message],
        assistant_message: Message,
        response_format: dict[str, Any] | type[Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        run_response: RunOutput | None = None,
        compress_tool_results: bool = False,
    ) -> ModelResponse:
        """Project a request-local history before synchronous invocation."""
        return super().invoke(  # ty: ignore[unresolved-attribute]
            self.native_replay_messages(messages),
            assistant_message,
            response_format=response_format,
            tools=tools,
            tool_choice=tool_choice,
            run_response=run_response,
            compress_tool_results=compress_tool_results,
        )

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
        """Project a request-local history before asynchronous invocation."""
        return await super().ainvoke(  # ty: ignore[unresolved-attribute]
            self.native_replay_messages(messages),
            assistant_message,
            response_format=response_format,
            tools=tools,
            tool_choice=tool_choice,
            run_response=run_response,
            compress_tool_results=compress_tool_results,
        )

    def invoke_stream(
        self,
        messages: list[Message],
        assistant_message: Message,
        response_format: dict[str, Any] | type[Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        run_response: RunOutput | None = None,
        compress_tool_results: bool = False,
    ) -> Iterator[ModelResponse]:
        """Stream a projected history without changing canonical messages."""
        yield from super().invoke_stream(  # ty: ignore[unresolved-attribute]
            self.native_replay_messages(messages),
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
        """Stream a projected history asynchronously."""
        async for chunk in super().ainvoke_stream(  # ty: ignore[unresolved-attribute]
            self.native_replay_messages(messages),
            assistant_message,
            response_format=response_format,
            tools=tools,
            tool_choice=tool_choice,
            run_response=run_response,
            compress_tool_results=compress_tool_results,
        ):
            yield chunk
