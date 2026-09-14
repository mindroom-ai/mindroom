"""Claude native compaction transport shared by direct Anthropic and Vertex."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from anthropic.lib.streaming import MessageStopEvent, ParsedBetaMessageStopEvent, ParsedMessageStopEvent
from anthropic.types.beta import BetaUsage

from mindroom.model_defaults import CLAUDE_NATIVE_COMPACTION_MODEL_PREFIXES
from mindroom.native_compaction import (
    NativeCompactionModel,
    checkpoint_items,
    common_native_endpoint,
    native_replay_messages,
    native_replay_route_matches,
    record_native_checkpoint,
    record_native_request_prefix,
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


def effective_context_management(params: dict[str, Any]) -> dict[str, Any]:
    """Resolve the SDK's raw-body override before its top-level policy."""
    return (params.get("extra_body") or {}).get("context_management", params.get("context_management")) or {}


class ClaudeNativeCompaction(NativeCompactionModel):
    """Adapt native checkpoints without changing the stored conversation."""

    context_management: dict[str, Any] | None
    request_params: dict[str, Any] | None
    client_params: dict[str, Any] | None
    client: Anthropic | None
    async_client: AsyncAnthropic | None

    def __post_init__(self) -> None:
        """Preserve authored compaction replay for standalone adapter callers."""
        super().__post_init__()  # ty: ignore[unresolved-attribute]
        self.configure_native_compaction(threshold=None, allow_authored=True)

    def authored_native_compaction_supported(self) -> bool:
        """Keep explicit Claude edits independent of MindRoom's automatic trigger."""
        if self.provider not in {"Anthropic", "VertexAI"}:
            return False
        params = {"context_management": self.context_management, **(self.request_params or {})}
        return any(
            edit.get("type") == "compact_20260112" for edit in effective_context_management(params).get("edits", [])
        )

    def native_compaction_supported(self) -> bool:
        """Respect explicit context-management settings and supported Claude models."""
        request_params = self.request_params or {}
        return (
            self.provider in {"Anthropic", "VertexAI"}
            and self.id.startswith(CLAUDE_NATIVE_COMPACTION_MODEL_PREFIXES)
            and self.context_management is None
            and "context_management" not in request_params
            and "context_management" not in (request_params.get("extra_body") or {})
            and (self.provider == "VertexAI" or self.native_compaction_endpoint() == "https://api.anthropic.com")
        )

    def native_compaction_endpoint(self) -> str:
        """Bind checkpoints to the concrete model's client route."""
        clients = [client for client in (self.async_client, self.client) if client is not None]
        if clients:
            return common_native_endpoint([str(client.base_url).rstrip("/") for client in clients])
        return str(
            (self.client_params or {}).get("base_url")
            or os.getenv("ANTHROPIC_BASE_URL")
            or "https://api.anthropic.com",
        ).rstrip("/")

    def configure_native_compaction(
        self,
        *,
        threshold: int | None,
        history_generation: str = "",
        allow_authored: bool = False,
    ) -> None:
        """Claude requires at least 50,000 input tokens before compaction."""
        if threshold is not None and threshold < 50000:
            threshold = None
        super().configure_native_compaction(
            threshold=threshold,
            history_generation=history_generation,
            allow_authored=allow_authored,
        )

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
        if self.native_compaction is not None and self.native_compaction.threshold is not None:
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
        if self.native_compaction is not None:
            params["betas"] = list(dict.fromkeys([*(params.get("betas") or []), _COMPACTION_BETA]))
        context_management = effective_context_management(params)
        for edit in context_management.get("edits", []):
            if edit.get("type") == "compact_20260112" and edit.get("pause_after_compaction"):
                msg = "pause_after_compaction=True is unsupported; MindRoom requires automatic continuation."
                raise ValueError(msg)
        return params

    def _uses_native_checkpoint(self, messages: list[Message]) -> bool:
        return self.native_compaction is not None and any(
            checkpoint_items(message, self.native_compaction.route) for message in messages
        )

    def native_replay_messages(self, messages: list[Message]) -> list[Message]:
        """Restore canonical blocks on foreign routes, or select checkpoint plus tail."""
        route = self.native_compaction.route if self.native_compaction is not None else None
        prepared = native_replay_messages(messages, route)
        thinking_route = route if self._uses_native_checkpoint(prepared) else None
        result: list[Message] = []
        stale_thinking = False
        for message in prepared:
            data = message.provider_data or {}
            if not data and not stale_thinking:
                result.append(message)
                continue
            items = checkpoint_items(message, route)
            block_lists = {
                key: data[key] for key in ("content_blocks", "server_tool_blocks") if isinstance(data.get(key), list)
            }
            if items:
                stale_thinking = False
            elif any(
                block.get("type") == "compaction" and block.get("content")
                for blocks in block_lists.values()
                for block in blocks
            ):
                # Thinking after a removed checkpoint is signed against a different
                # prefix. Its later thinking chain is invalid too, including fields
                # Agno can use to rebuild blocks when content_blocks is empty.
                stale_thinking = True
            elif (matches := native_replay_route_matches(message, thinking_route)) is not None:
                # A response produced after fallback starts a new valid chain on
                # that route. Keep it only while replaying the same prefix kind.
                stale_thinking = not matches
            dropped_types = {"compaction"}
            if stale_thinking:
                dropped_types.update({"thinking", "redacted_thinking", "redacted_reasoning_content"})
            next_data = dict(data)
            for key, blocks in block_lists.items():
                next_data[key] = [block for block in blocks if block.get("type") not in dropped_types]
            if items:
                next_data["content_blocks"] = items
            updates: dict[str, Any] = {"provider_data": next_data}
            if stale_thinking:
                next_data.pop("signature", None)
                updates.update(reasoning_content=None, redacted_reasoning_content=None)
            result.append(message.model_copy(update=updates))
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
        prepared = self.native_replay_messages(messages)
        checkpoint_prefix = self._uses_native_checkpoint(prepared)
        result = super().invoke(  # ty: ignore[unresolved-attribute]
            prepared,
            assistant_message,
            response_format=response_format,
            tools=tools,
            tool_choice=tool_choice,
            run_response=run_response,
            compress_tool_results=compress_tool_results,
        )
        record_native_request_prefix(result, checkpoint_prefix=checkpoint_prefix)
        return result

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
        prepared = self.native_replay_messages(messages)
        checkpoint_prefix = self._uses_native_checkpoint(prepared)
        result = await super().ainvoke(  # ty: ignore[unresolved-attribute]
            prepared,
            assistant_message,
            response_format=response_format,
            tools=tools,
            tool_choice=tool_choice,
            run_response=run_response,
            compress_tool_results=compress_tool_results,
        )
        record_native_request_prefix(result, checkpoint_prefix=checkpoint_prefix)
        return result

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
        prepared = self.native_replay_messages(messages)
        checkpoint_prefix = self._uses_native_checkpoint(prepared)
        for chunk in super().invoke_stream(  # ty: ignore[unresolved-attribute]
            prepared,
            assistant_message,
            response_format=response_format,
            tools=tools,
            tool_choice=tool_choice,
            run_response=run_response,
            compress_tool_results=compress_tool_results,
        ):
            record_native_request_prefix(chunk, checkpoint_prefix=checkpoint_prefix)
            yield chunk

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
        prepared = self.native_replay_messages(messages)
        checkpoint_prefix = self._uses_native_checkpoint(prepared)
        async for chunk in super().ainvoke_stream(  # ty: ignore[unresolved-attribute]
            prepared,
            assistant_message,
            response_format=response_format,
            tools=tools,
            tool_choice=tool_choice,
            run_response=run_response,
            compress_tool_results=compress_tool_results,
        ):
            record_native_request_prefix(chunk, checkpoint_prefix=checkpoint_prefix)
            yield chunk
