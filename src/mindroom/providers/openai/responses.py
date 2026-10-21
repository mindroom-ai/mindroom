"""The Responses API adapter for the OpenAI and Codex routes.

Codex only streams, so a non-streaming request on that route still streams and returns the drained turn.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.llm.errors import ProviderError
from mindroom.llm.provider import ReasoningDelta, TextDelta, ToolCallReady, TurnCompleted, UsageReported
from mindroom.providers.openai.adapter_common import SDKClients, assistant_message, with_overrides
from mindroom.providers.openai.errors import provider_error, sdk_failures
from mindroom.providers.openai.responses_wire import (
    ResponsesStream,
    ResponsesTurnParts,
    ResponsesWire,
    parse_response,
    request_store,
    responses_request_kwargs,
    responses_stop_reason,
)
from mindroom.tool_system.context_bound_streams import closing_async_stream

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from openai import AsyncOpenAI

    from mindroom.llm.messages import Message
    from mindroom.llm.provider import Capabilities, ModelIdentity, ModelRequest, StreamEvent
    from mindroom.providers.openai.settings import ResponsesSettings

__all__ = ["ResponsesAdapter"]


def _turn_deltas(parts: ResponsesTurnParts) -> list[StreamEvent]:
    """Return the deltas of a turn that arrived whole."""
    events: list[StreamEvent] = []
    if parts.reasoning:
        events.append(ReasoningDelta(parts.reasoning))
    if parts.content:
        events.append(TextDelta(parts.content))
    events.extend(ToolCallReady(call) for call in parts.tool_calls)
    return events


@dataclass(frozen=True, slots=True, kw_only=True)
class ResponsesAdapter:
    """One model behind the OpenAI Responses API or the Codex endpoint."""

    identity: ModelIdentity
    capabilities: Capabilities
    wire: ResponsesWire
    settings: ResponsesSettings
    clients: SDKClients

    def format_tool_results(self, messages: Sequence[Message], results: Sequence[Message]) -> list[Message]:  # noqa: ARG002
        """Responses takes one ``function_call_output`` per result, in order."""
        return list(results)

    def _request_kwargs(self, request: ModelRequest) -> dict[str, Any]:
        kwargs = responses_request_kwargs(
            model_id=self.identity.id,
            request=request,
            settings=self.settings,
            wire=self.wire,
        )
        # Codex rejects both sampling and output-limit fields.
        codex = self.wire.route == "codex"
        return with_overrides(
            kwargs,
            request.overrides,
            temperature=not codex and not self.capabilities.provider_default_sampling,
            output_limit_key=None if codex else "max_output_tokens",
        )

    async def _streamed(
        self,
        client: AsyncOpenAI,
        kwargs: dict[str, Any],
        accumulator: ResponsesStream,
    ) -> AsyncIterator[StreamEvent]:
        try:
            sdk_stream = await client.responses.create(**kwargs, stream=True)
        except sdk_failures() as error:
            raise provider_error(error, provider=self.identity.provider_name) from error
        try:
            async for event in sdk_stream:
                for delta in accumulator.add(event):
                    yield delta
        except sdk_failures() as error:
            raise provider_error(error, provider=self.identity.provider_name) from error
        finally:
            await sdk_stream.close()
        if not accumulator.completed:
            raise accumulator.incomplete_error()

    async def _completed(
        self,
        client: AsyncOpenAI,
        kwargs: dict[str, Any],
        request: ModelRequest,
    ) -> ResponsesTurnParts:
        try:
            response = await client.responses.create(**kwargs)
        except sdk_failures() as error:
            raise provider_error(error, provider=self.identity.provider_name) from error
        return parse_response(
            response,
            wire=self.wire,
            store=request_store(self.wire, request),
            portable_replay=request.portable_replay,
            native_compaction=request.native_compaction,
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        """Send one Responses request and yield its deltas, usage, and finished turn."""
        started = time.monotonic()
        first_token: float | None = None
        kwargs = await asyncio.to_thread(self._request_kwargs, request)
        client = await self.clients.client(request.overrides)
        if request.stream or self.wire.route == "codex":
            accumulator = ResponsesStream(
                wire=self.wire,
                store=request_store(self.wire, request),
                portable_replay=request.portable_replay,
                native_compaction=request.native_compaction,
            )
            try:
                events = self._streamed(client, kwargs, accumulator)
                async with closing_async_stream(events):
                    async for event in events:
                        first_token = first_token or time.monotonic()
                        yield event
            except ProviderError:
                # Usage the provider reported for a failed attempt is still billed.
                if accumulator.terminal_usage is not None:
                    yield UsageReported(accumulator.terminal_usage)
                raise
            parts = accumulator.finish()
        else:
            parts = await self._completed(client, kwargs, request)
            first_token = time.monotonic()
            for event in _turn_deltas(parts):
                yield event
        message = assistant_message(
            self.identity,
            content=parts.content,
            reasoning=parts.reasoning,
            tool_calls=parts.tool_calls,
            provider_data=parts.provider_data,
            usage=parts.usage,
            started=started,
            first_token=first_token,
        )
        if parts.usage is not None:
            yield UsageReported(message.metrics)
        stop = responses_stop_reason(parts.status, parts.incomplete_reason, tool_calls=bool(parts.tool_calls))
        yield TurnCompleted(message=message, stop_reason=stop)
