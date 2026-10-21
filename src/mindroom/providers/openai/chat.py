"""The Chat Completions adapter shared by every OpenAI-compatible route.

A route fixes the endpoint, identity strings, wire dialect, and capabilities (see ``build``); the per-model
settings come from ``extra_kwargs``.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from mindroom.llm.provider import (
    ReasoningDelta,
    StreamProgress,
    TextDelta,
    ToolCallReady,
    TurnCompleted,
    UsageReported,
)
from mindroom.providers.openai.adapter_common import SDKClients, assistant_message, with_overrides
from mindroom.providers.openai.chat_wire import (
    ChatStream,
    ChatWire,
    chat_request_kwargs,
    chat_stop_reason,
    parse_chat_completion,
)
from mindroom.providers.openai.errors import provider_error, sdk_failures
from mindroom.providers.openrouter_prompt_cache import openrouter_prompt_cache_control, with_prompt_cache_ladder

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from openai import AsyncOpenAI

    from mindroom.llm.messages import Message
    from mindroom.llm.provider import Capabilities, ModelIdentity, ModelRequest, StreamEvent
    from mindroom.providers.openai.settings import ChatSettings

__all__ = ["ChatAdapter"]

# OpenAI's own endpoints take the newer output-limit name; OpenAI-compatible servers kept ``max_tokens``.
_MAX_COMPLETION_TOKENS_ROUTES = frozenset({"openai", "azure"})


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatAdapter:
    """One model behind a Chat Completions route."""

    identity: ModelIdentity
    capabilities: Capabilities
    wire: ChatWire
    settings: ChatSettings
    clients: SDKClients

    def format_tool_results(self, messages: Sequence[Message], results: Sequence[Message]) -> list[Message]:  # noqa: ARG002
        """Chat Completions takes one tool message per result, in order."""
        return list(results)

    async def _client(self, request: ModelRequest) -> AsyncOpenAI:
        client = await self.clients.client(request.overrides)
        if self.wire.route != "openrouter":
            return client
        control = openrouter_prompt_cache_control(
            self.identity.id,
            cache_system_prompt=self.settings.cache_system_prompt and request.overrides.prompt_cache is not False,
            extended_cache_time=self.settings.extended_cache_time,
        )
        return cast("AsyncOpenAI", with_prompt_cache_ladder(client, lambda: control))

    def _request_kwargs(self, request: ModelRequest) -> dict[str, Any]:
        kwargs = chat_request_kwargs(
            model_id=self.identity.id,
            messages=request.messages,
            tools=request.tools,
            native_tools=request.native_tools,
            tool_choice=request.tool_choice,
            response_format=request.response_format,
            stream=request.stream,
            settings=self.settings,
            wire=self.wire,
        )
        return with_overrides(
            kwargs,
            request.overrides,
            temperature=not self.capabilities.provider_default_sampling,
            output_limit_key="max_completion_tokens"
            if self.wire.route in _MAX_COMPLETION_TOKENS_ROUTES
            else "max_tokens",
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        """Send one Chat Completions request and yield its deltas, usage, and finished turn."""
        started = time.monotonic()
        first_token: float | None = None
        # Reading and encoding attachments can be slow, so the body is built off the event loop.
        kwargs = await asyncio.to_thread(self._request_kwargs, request)
        try:
            client = await self._client(request)
            if request.stream:
                accumulator = ChatStream(self.wire)
                sdk_stream = await client.chat.completions.create(**kwargs)
                try:
                    async for chunk in sdk_stream:
                        events = accumulator.add(chunk)
                        if not events:
                            # A chunk without deltas, such as a tool-call argument piece, is still activity.
                            yield StreamProgress()
                            continue
                        first_token = first_token or time.monotonic()
                        for event in events:
                            yield event
                finally:
                    await sdk_stream.close()
                parts = accumulator.finish()
            else:
                parts = parse_chat_completion(await client.chat.completions.create(**kwargs), self.wire)
                first_token = time.monotonic()
                if parts.reasoning:
                    yield ReasoningDelta(parts.reasoning)
                if parts.content:
                    yield TextDelta(parts.content)
        except sdk_failures() as error:
            raise provider_error(error, provider=self.identity.provider_name) from error
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
        for call in parts.tool_calls:
            yield ToolCallReady(call)
        if parts.usage is not None:
            yield UsageReported(message.metrics)
        yield TurnCompleted(message=message, stop_reason=chat_stop_reason(parts.finish_reason))
