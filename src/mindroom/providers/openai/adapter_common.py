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
        """Return the client for one request, without the SDK's retries when the caller retries the request itself."""
        if self.per_request:
            # Borrowing a CLI login token may refresh it under a file lock, so it stays off the event loop.
            client = await asyncio.to_thread(self.factory)
        elif self._cache:
            client = self._cache[0]
        else:
            client = self.factory()
            self._cache.append(client)
        if overrides.caller_retries:
            client = client.with_options(max_retries=0)
        return client


def with_overrides(kwargs: dict[str, Any], overrides: RequestOverrides, *, temperature: bool) -> dict[str, Any]:
    """Apply a request's temperature override to SDK call arguments; ``temperature=False`` leaves sampling to the provider."""
    if not temperature or overrides.temperature is None:
        return kwargs
    kwargs["extra_body"] = {**(kwargs.get("extra_body") or {}), "temperature": overrides.temperature}
    return kwargs
