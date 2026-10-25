"""See through the request pipeline to the adapter the registry built, for tests that inspect adapter-only fields."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.providers.pipeline.wrapped import WrappedProvider

if TYPE_CHECKING:
    from mindroom.llm.provider import ModelProvider


def bare_adapter(provider: ModelProvider) -> ModelProvider:
    """Return the adapter inside the pipeline stages the registry put around it.

    Production reads everything it needs through the ``ModelProvider`` contract; only tests that assert a route's
    settings, wire, or adapter class unwrap the pipeline.
    """
    while isinstance(provider, WrappedProvider):
        provider = provider.inner
    return provider
