"""Apply the Claude prompt-cache ladder to OpenRouter Chat Completions requests.

OpenRouter forwards ``cache_control`` markers on Chat Completions content parts
and function tool definitions to Anthropic-family upstreams (Anthropic, Vertex,
Bedrock, and Azure), which only cache up to explicit breakpoints. Other
OpenRouter model families cache implicitly or use different markers, so this
module only marks requests whose model ID routes to Anthropic.

The placement rules live in :mod:`mindroom.providers.openai.openrouter_cache`, which
the owned Chat Completions adapter applies to its request directly; this module
wraps the Agno path's SDK client so the same rules run on each request it sends.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mindroom.providers.openai.openrouter_cache import (
    openrouter_prompt_cache_control,
    request_kwargs_with_prompt_cache,
)

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["openrouter_prompt_cache_control", "with_prompt_cache_ladder"]


class _PromptCacheCompletionsProxy:
    """Chat completions namespace proxy that adds the cache ladder to each request."""

    def __init__(self, completions: object, cache_control: Callable[[], dict[str, str] | None]) -> None:
        self._completions: Any = completions
        self._cache_control = cache_control

    def create(self, **request_kwargs: Any) -> object:  # noqa: ANN401 - mirrors the SDK signature
        cache_control = self._cache_control()
        if cache_control is not None:
            request_kwargs = request_kwargs_with_prompt_cache(request_kwargs, cache_control)
        return self._completions.create(**request_kwargs)

    def __getattr__(self, name: str) -> object:
        return getattr(self._completions, name)


class _PromptCacheChatProxy:
    def __init__(self, chat: object, cache_control: Callable[[], dict[str, str] | None]) -> None:
        self._chat: Any = chat
        self._cache_control = cache_control

    @property
    def completions(self) -> _PromptCacheCompletionsProxy:
        return _PromptCacheCompletionsProxy(self._chat.completions, self._cache_control)

    def __getattr__(self, name: str) -> object:
        return getattr(self._chat, name)


class _PromptCacheClientProxy:
    """OpenAI SDK client proxy that routes chat completions through the cache ladder."""

    def __init__(self, client: object, cache_control: Callable[[], dict[str, str] | None]) -> None:
        self._client: Any = client
        self._cache_control = cache_control

    @property
    def chat(self) -> _PromptCacheChatProxy:
        return _PromptCacheChatProxy(self._client.chat, self._cache_control)

    def __getattr__(self, name: str) -> object:
        return getattr(self._client, name)


def with_prompt_cache_ladder(client: object, cache_control: Callable[[], dict[str, str] | None]) -> object:
    """Wrap an OpenAI SDK client so chat completions carry Claude ladder breakpoints.

    ``cache_control`` is read per request, so opt-outs set after the client is
    cached (for example on summary models) still apply.
    """
    return _PromptCacheClientProxy(client, cache_control)
