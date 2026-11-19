"""What the Chat Completions and Responses adapters share: SDK client reuse and request overrides."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from mindroom.providers.extra_kwargs import sdk_request_body

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Mapping

    from openai import AsyncOpenAI

    from mindroom.llm.provider import Capabilities, RequestOverrides
    from mindroom.providers.openai.settings import ChatSettings, ResponsesSettings

__all__ = ["SDKClients", "request_settings", "untyped_sdk_call"]


def untyped_sdk_call(kwargs: Mapping[str, Any], option_keys: Collection[str], **required: Any) -> dict[str, Any]:  # noqa: ANN401
    """Return SDK call arguments that send the body of ``kwargs`` as ``extra_body``, which the SDK sends as given.

    The SDK checks each typed body field against its TypedDict annotations on the event loop, which for a long
    history and many tools blocks the loop for tens of milliseconds and changes nothing in the plain JSON body
    MindRoom builds. ``required`` are the typed fields the SDK itself reads, such as ``stream``.
    """
    options = {key: kwargs[key] for key in option_keys if key in kwargs and key != "extra_body"}
    return {**options, **required, "extra_body": sdk_request_body(kwargs, option_keys)}


@dataclass(frozen=True, slots=True)
class SDKClients:
    """Build a route's SDK client once, or once per request when its login token can expire in between."""

    factory: Callable[[], AsyncOpenAI]
    per_request: bool = False
    _cache: list[AsyncOpenAI] = field(default_factory=list, repr=False, compare=False)

    async def client(self) -> AsyncOpenAI:
        """Return the client for one request."""
        if self.per_request:
            # Borrowing a CLI login token may refresh it under a file lock, so it stays off the event loop.
            client = await asyncio.to_thread(self.factory)
        elif self._cache:
            client = self._cache[0]
        else:
            client = self.factory()
            self._cache.append(client)
        return client


def request_settings[S: (ChatSettings, ResponsesSettings)](
    settings: S,
    overrides: RequestOverrides,
    capabilities: Capabilities,
) -> S:
    """Return the settings one request is built from: its temperature override replaces the model's temperature.

    The route's own rules then apply to the override, such as Codex dropping every temperature; a model that leaves
    sampling to the provider (``provider_default_sampling``) ignores it.
    """
    if capabilities.provider_default_sampling or overrides.temperature is None:
        return settings
    return replace(settings, temperature=overrides.temperature)
