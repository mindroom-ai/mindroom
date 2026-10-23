"""The Chat Completions adapter shared by every OpenAI-compatible route.

A route fixes the endpoint, identity strings, wire dialect, and capabilities (see ``build``); the per-model
settings come from ``extra_kwargs``.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from mindroom.llm.provider import (
    StreamProgress,
    ToolCallReady,
    TurnCompleted,
    UsageReported,
)
from mindroom.providers.assistant_turn import finished_assistant_message, output_started
from mindroom.providers.extra_kwargs import settings_params
from mindroom.providers.openai.adapter_common import SDKClients, with_overrides
from mindroom.providers.openai.chat_wire import (
    ChatStream,
    ChatWire,
    chat_request_kwargs,
    chat_stop_reason,
    parse_chat_completion,
)
from mindroom.providers.openai.errors import provider_error, sdk_failures
from mindroom.providers.openai.openrouter_cache import openrouter_prompt_cache_control, request_kwargs_with_prompt_cache

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mindroom.llm.provider import Capabilities, ModelIdentity, ModelRequest, StreamEvent
    from mindroom.providers.openai.settings import ChatSettings

__all__ = ["ChatAdapter"]


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
    replay_estimator: ClassVar[None] = None

    @property
    def model_params(self) -> dict[str, Any]:
        """The route's JSON-safe, non-secret settings."""
        return settings_params(self.settings)

    def _request_kwargs(self, request: ModelRequest) -> dict[str, Any]:
        kwargs = chat_request_kwargs(
            model_id=self.identity.id,
            messages=request.messages,
            tools=request.tools,
            tool_choice=request.tool_choice,
            response_format=request.response_format,
            stream=request.stream,
            settings=self.settings,
            wire=self.wire,
        )
        kwargs = with_overrides(kwargs, request.overrides, temperature=not self.capabilities.provider_default_sampling)
        if self.wire.route != "openrouter":
            return kwargs
        # OpenRouter forwards Claude cache breakpoints to Anthropic upstreams; the ladder runs on the final body.
        cache_control = openrouter_prompt_cache_control(
            self.identity.id,
            cache_system_prompt=self.settings.cache_system_prompt and request.overrides.prompt_cache is not False,
            extended_cache_time=self.settings.extended_cache_time,
        )
        return kwargs if cache_control is None else request_kwargs_with_prompt_cache(kwargs, cache_control)

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        """Send one Chat Completions request and yield its deltas, usage, and finished turn."""
        started = time.monotonic()
        first_token: float | None = None
        # Reading and encoding attachments can be slow, so the body is built off the event loop.
        kwargs = await asyncio.to_thread(self._request_kwargs, request)
        try:
            client = await self.clients.client(request.overrides)
            if request.stream:
                accumulator = ChatStream(self.wire)
                sdk_stream = await client.chat.completions.create(**kwargs)
                try:
                    async for chunk in sdk_stream:
                        # A chunk without deltas or usage, such as a tool-call argument piece, is still activity.
                        for event in accumulator.add(chunk) or [StreamProgress()]:
                            first_token = output_started(first_token, event)
                            yield event
                finally:
                    await sdk_stream.close()
                parts = accumulator.finish()
                # A stream assembles its tool calls from their pieces, so each is complete only now.
                for call in parts.tool_calls:
                    yield ToolCallReady(call)
            else:
                completion = await client.chat.completions.create(**kwargs)
                parts = parse_chat_completion(completion, self.wire, provider=self.identity.provider_name)
                first_token = time.monotonic()
        except sdk_failures() as error:
            raise provider_error(error, provider=self.identity.provider_name) from error
        message = finished_assistant_message(
            self.identity,
            content=parts.content,
            reasoning=parts.reasoning,
            tool_calls=parts.tool_calls,
            provider_data=parts.provider_data,
            usage=parts.usage,
            started=started,
            first_output_at=first_token,
        )
        if parts.usage is not None:
            yield UsageReported(message.metrics)
        yield TurnCompleted(message=message, stop_reason=chat_stop_reason(parts.finish_reason))
