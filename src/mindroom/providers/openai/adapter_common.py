"""What the Chat Completions and Responses adapters share: SDK client reuse and request overrides."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from openai import AsyncOpenAI

    from mindroom.llm.provider import RequestOverrides

__all__ = ["SDKClients", "with_overrides"]


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


# A request carries its output limit under one of these names; an override replaces whichever the config set.
_OUTPUT_LIMIT_KEYS = ("max_tokens", "max_completion_tokens", "max_output_tokens")


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
        for key in _OUTPUT_LIMIT_KEYS:
            body.pop(key, None)
        body[output_limit_key] = overrides.max_output_tokens
    if body:
        kwargs["extra_body"] = body
    if overrides.timeout_seconds is not None:
        kwargs["timeout"] = overrides.timeout_seconds
    return kwargs
