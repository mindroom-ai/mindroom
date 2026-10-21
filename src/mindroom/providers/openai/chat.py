"""The Chat Completions adapter and the routes that use it.

One adapter serves every OpenAI-compatible Chat Completions route; a route fixes the endpoint, identity
strings, wire dialect, and capabilities, and the per-model settings come from ``extra_kwargs``.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from mindroom.llm.messages import Message
from mindroom.llm.provider import ReasoningDelta, TextDelta, ToolCallReady, TurnCompleted, UsageReported
from mindroom.providers.openai.chat_wire import (
    ChatStream,
    ChatTurnParts,
    ChatWire,
    chat_request_kwargs,
    chat_stop_reason,
    parse_chat_completion,
)
from mindroom.providers.openai.errors import provider_error, sdk_failures
from mindroom.providers.openrouter_prompt_cache import openrouter_prompt_cache_control, with_prompt_cache_ladder

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Sequence

    from openai import AsyncOpenAI

    from mindroom.llm.provider import Capabilities, ModelIdentity, ModelRequest, StreamEvent
    from mindroom.providers.openai.settings import ChatSettings

__all__ = ["ChatAdapter", "ClientFactory"]

type ClientFactory = Callable[[], AsyncOpenAI]


def _assistant_message(
    parts: ChatTurnParts,
    identity: ModelIdentity,
    *,
    started: float,
    first_token: float | None,
) -> Message:
    message = Message(
        role="assistant",
        content=parts.content,
        reasoning_content=parts.reasoning,
        tool_calls=parts.tool_calls or None,
        provider_data={
            **parts.provider_data,
            "mindroom_model": {"id": identity.id, "provider": identity.provider_name},
        },
    )
    if parts.usage is not None:
        message.metrics = parts.usage
    message.metrics.duration = time.monotonic() - started
    if first_token is not None:
        message.metrics.time_to_first_token = first_token - started
    return message


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatAdapter:
    """One model behind a Chat Completions route.

    ``client_factory`` builds the SDK client; routes whose credentials can expire between requests (Kimi)
    build a fresh client per request, the others cache the first one.
    """

    identity: ModelIdentity
    capabilities: Capabilities
    wire: ChatWire
    settings: ChatSettings
    client_factory: ClientFactory
    fresh_client_per_request: bool = False
    _client_cache: list[AsyncOpenAI] = field(default_factory=list, repr=False, compare=False)

    def format_tool_results(self, messages: Sequence[Message], results: Sequence[Message]) -> list[Message]:  # noqa: ARG002
        """Chat Completions takes one tool message per result, in order."""
        return list(results)

    async def _client(self, request: ModelRequest) -> AsyncOpenAI:
        if self.fresh_client_per_request:
            # Building the client may refresh a CLI OAuth token under a file lock.
            client = await asyncio.to_thread(self.client_factory)
        elif self._client_cache:
            client = self._client_cache[0]
        else:
            client = self.client_factory()
            self._client_cache.append(client)
        if request.overrides.sdk_max_retries is not None:
            client = client.with_options(max_retries=request.overrides.sdk_max_retries)
        if self.wire.route == "openrouter":
            control = openrouter_prompt_cache_control(
                self.identity.id,
                cache_system_prompt=self.settings.cache_system_prompt and request.overrides.prompt_cache is not False,
                extended_cache_time=self.settings.extended_cache_time,
            )
            client = cast("AsyncOpenAI", with_prompt_cache_ladder(client, lambda: control))
        return client

    def _request_kwargs(self, request: ModelRequest) -> dict[str, Any]:
        settings = self.settings
        overrides = request.overrides
        kwargs = chat_request_kwargs(
            model_id=self.identity.id,
            messages=request.messages,
            tools=request.tools,
            native_tools=request.native_tools,
            tool_choice=request.tool_choice,
            response_format=request.response_format,
            stream=request.stream,
            settings=settings,
            wire=self.wire,
        )
        body = dict(kwargs.get("extra_body") or {})
        if overrides.temperature is not None and not self.capabilities.provider_default_sampling:
            body["temperature"] = overrides.temperature
        if overrides.max_output_tokens is not None:
            # OpenAI's own endpoints take the newer name; OpenAI-compatible servers kept ``max_tokens``.
            key = "max_completion_tokens" if self.wire.route in {"openai", "azure"} else "max_tokens"
            body[key] = overrides.max_output_tokens
        if body:
            kwargs["extra_body"] = body
        if overrides.timeout_seconds is not None:
            kwargs["timeout"] = overrides.timeout_seconds
        return kwargs

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
                        for event in accumulator.add(chunk):
                            if first_token is None:
                                first_token = time.monotonic()
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
        message = _assistant_message(parts, self.identity, started=started, first_token=first_token)
        for call in parts.tool_calls:
            yield ToolCallReady(call)
        if parts.usage is not None:
            yield UsageReported(message.metrics)
        yield TurnCompleted(message=message, stop_reason=chat_stop_reason(parts.finish_reason))
