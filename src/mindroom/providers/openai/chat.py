"""The Chat Completions adapter shared by every OpenAI-compatible route.

A route fixes the endpoint, identity strings, wire rules, and capabilities (see ``build``); the per-model
settings come from ``extra_kwargs``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, cast

import httpx2

from mindroom.llm.messages import system_texts
from mindroom.llm.provider import ToolCallReady
from mindroom.model_defaults import OPENAI_BASE_URL_DEFAULT
from mindroom.providers.assistant_turn import TurnTimer, finished_turn
from mindroom.providers.extra_kwargs import sdk_request_body, settings_params
from mindroom.providers.openai.adapter_common import SDKClients, request_settings
from mindroom.providers.openai.chat_wire import (
    SDK_OPTION_KEYS,
    ChatStream,
    ChatWire,
    chat_request_kwargs,
    chat_stop_reason,
)
from mindroom.providers.openai.errors import provider_error, rejected_media, sdk_failures
from mindroom.providers.openai.openrouter_cache import openrouter_prompt_cache_control, request_kwargs_with_prompt_cache
from mindroom.token_counting.families import counter_for_model

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mindroom.llm.provider import Capabilities, ModelIdentity, ModelRequest, StreamEvent, TokenCounter
    from mindroom.providers.openai.settings import ChatSettings

__all__ = ["ChatAdapter"]


@dataclass
class _WatchedBody(httpx2.AsyncByteStream):
    """A response body that records whether the SDK decoded it to its end.

    The SDK decodes the events through its first read of the body. After ``[DONE]`` it drains the rest of an HTTP/1.1
    body through a second read, so the connection returns to the pool, and that read's end says nothing.
    """

    body: httpx2.AsyncByteStream
    reads: int = 0
    decoded_to_end: bool = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """Pass the body through, then record whether the decoding read ended it."""
        self.reads += 1
        decoding = self.reads == 1
        async for chunk in self.body:
            yield chunk
        if decoding:
            self.decoded_to_end = True

    async def aclose(self) -> None:
        """Close the body."""
        await self.body.aclose()


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatAdapter:
    """One model behind a Chat Completions route."""

    identity: ModelIdentity
    capabilities: Capabilities
    wire: ChatWire
    settings: ChatSettings
    clients: SDKClients
    # Chat Completions keeps no history of its own, and its replayed history is sized canonically.
    native_history_route: ClassVar[None] = None

    @property
    def model_params(self) -> dict[str, Any]:
        """The route's JSON-safe, non-secret settings."""
        return settings_params(self.settings)

    @property
    def token_counter(self) -> TokenCounter:
        """OpenAI's models count locally on routes that serve only them.

        Any other route may serve another model under an OpenAI name, so the name only picks its estimate.
        """
        route = self.wire.route
        return counter_for_model(
            self.identity,
            local_encoder=self.identity.endpoint == OPENAI_BASE_URL_DEFAULT
            or route == "azure"
            or (route == "openrouter" and self.identity.id.startswith("openai/")),
        )

    def _request_kwargs(self, request: ModelRequest) -> dict[str, Any]:
        kwargs = chat_request_kwargs(
            model_id=self.identity.id,
            messages=request.messages,
            tools=request.tools,
            tool_choice=request.tool_choice,
            output_schema=request.output_schema,
            structured_output=self.capabilities.structured_output,
            settings=request_settings(self.settings, request.overrides, self.capabilities),
            wire=self.wire,
            provider=self.identity.provider,
        )
        if self.wire.route != "openrouter":
            return kwargs
        # OpenRouter forwards Claude cache breakpoints to Anthropic upstreams; the ladder runs on the final body.
        cache_control = openrouter_prompt_cache_control(
            self.identity.id,
            cache_system_prompt=self.settings.cache_system_prompt and request.overrides.prompt_cache,
            extended_cache_time=self.settings.extended_cache_time,
        )
        if cache_control is None:
            return kwargs
        return request_kwargs_with_prompt_cache(kwargs, cache_control, system_texts(request.messages))

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        """Send one Chat Completions request and yield its deltas, usage, and finished turn."""
        timer = TurnTimer()
        # Reading and encoding attachments can be slow, so the body is built off the event loop.
        kwargs = await asyncio.to_thread(self._request_kwargs, request)
        accumulator = ChatStream(self.wire)
        try:
            client = await self.clients.client()
            await request.on_send(sdk_request_body(kwargs, SDK_OPTION_KEYS))
            sdk_stream = await client.chat.completions.create(**kwargs)
            # The SDK's chunks end alike at the ``[DONE]`` sentinel and where the body just ends, but the SDK
            # stops decoding at the sentinel, so a body decoded to its end never sent one.
            body = _WatchedBody(cast("httpx2.AsyncByteStream", sdk_stream.response.stream))
            sdk_stream.response.stream = body
            try:
                async for chunk in sdk_stream:
                    for event in timer.chunk(accumulator.add(chunk)):
                        yield event
            finally:
                await sdk_stream.close()
            if body.decoded_to_end and accumulator.parts.finish_reason is None:
                # Servers may omit a finish reason or ``[DONE]``, but a stream with neither was cut off.
                return
        except sdk_failures() as error:
            failure = provider_error(error, provider=self.identity.provider)
            # OpenRouter's rejections index the body its upstream received, which it rewrote, so they name nothing here.
            if self.wire.route != "openrouter":
                failure.rejected_media = rejected_media(error, kwargs, request.messages, chat=True)
            raise failure from error
        for event in accumulator.finish():
            yield timer.output(event)
        parts = accumulator.parts
        # A stream assembles its tool calls from their pieces, so each is complete only now.
        for call in parts.tool_calls:
            yield timer.output(ToolCallReady(call))
        for event in finished_turn(
            self.identity,
            timer,
            content=parts.content,
            reasoning=parts.reasoning,
            tool_calls=parts.tool_calls,
            provider_data=parts.provider_data,
            usage=parts.usage,
            stop_reason=chat_stop_reason(parts.finish_reason),
        ):
            yield event
