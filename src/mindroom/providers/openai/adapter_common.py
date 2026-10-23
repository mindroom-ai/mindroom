"""What the Chat Completions and Responses adapters share: SDK client reuse and request overrides."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from openai import AsyncOpenAI

    from mindroom.llm.provider import RequestOverrides
    from mindroom.providers.openai.settings import ChatSettings, ResponsesSettings

__all__ = ["SDKClients", "request_settings"]


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


def request_settings[S: (ChatSettings, ResponsesSettings)](
    settings: S,
    overrides: RequestOverrides,
    *,
    sampling: bool,
) -> S:
    """Return the settings one request is built from: its temperature override replaces the model's temperature.

    Setting a model's temperature did the same under Agno, so the route's own rules still apply to it, such as
    Codex dropping every temperature. ``sampling=False`` leaves sampling to the provider.
    """
    if not sampling or overrides.temperature is None:
        return settings
    return replace(settings, temperature=overrides.temperature)
