"""What the Chat Completions and Responses adapters share: SDK client reuse, request overrides, the finished message."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mindroom.llm.messages import Message

if TYPE_CHECKING:
    from collections.abc import Callable

    from openai import AsyncOpenAI

    from mindroom.llm.provider import ModelIdentity, RequestOverrides
    from mindroom.llm.usage import MessageMetrics

__all__ = ["SDKClients", "assistant_message", "with_overrides"]


@dataclass(frozen=True, slots=True)
class SDKClients:
    """Build a route's SDK client once, or once per request when its login token can expire in between."""

    factory: Callable[[], AsyncOpenAI]
    per_request: bool = False
    _cache: list[AsyncOpenAI] = field(default_factory=list, repr=False, compare=False)

    async def client(self, overrides: RequestOverrides) -> AsyncOpenAI:
        """Return the client for one request, with the request's SDK retry override applied."""
        if self.per_request:
            # Borrowing a CLI login token may refresh it under a file lock, so it stays off the event loop.
            client = await asyncio.to_thread(self.factory)
        elif self._cache:
            client = self._cache[0]
        else:
            client = self.factory()
            self._cache.append(client)
        if overrides.sdk_max_retries is not None:
            client = client.with_options(max_retries=overrides.sdk_max_retries)
        return client


def with_overrides(
    kwargs: dict[str, Any],
    overrides: RequestOverrides,
    *,
    temperature: bool,
    output_limit_key: str | None,
) -> dict[str, Any]:
    """Apply a request's overrides to SDK call arguments; ``temperature=False`` leaves sampling to the provider."""
    body = dict(kwargs.get("extra_body") or {})
    if temperature and overrides.temperature is not None:
        body["temperature"] = overrides.temperature
    if output_limit_key is not None and overrides.max_output_tokens is not None:
        body[output_limit_key] = overrides.max_output_tokens
    if body:
        kwargs["extra_body"] = body
    if overrides.timeout_seconds is not None:
        kwargs["timeout"] = overrides.timeout_seconds
    return kwargs


def assistant_message(
    identity: ModelIdentity,
    *,
    content: str | None,
    reasoning: str | None,
    tool_calls: list[dict[str, Any]],
    provider_data: dict[str, Any],
    usage: MessageMetrics | None,
    started: float,
    first_token: float | None,
) -> Message:
    """Return the finished assistant message, recording which model answered it and how long it took."""
    message = Message(
        role="assistant",
        content=content,
        reasoning_content=reasoning,
        tool_calls=tool_calls or None,
        provider_data={**provider_data, "mindroom_model": {"id": identity.id, "provider": identity.provider_name}},
    )
    if usage is not None:
        message.metrics = usage
    message.metrics.duration = time.monotonic() - started
    if first_token is not None:
        message.metrics.time_to_first_token = first_token - started
    return message
