"""Compose the request pipeline around one provider adapter.

Outermost first: the malformed tool-call guidance retry, portable deferred tool search, media fallback, stream
retry with the idle limit, the safeguard refusal, and request logging. Each attempt is therefore logged, a refused
one included; retries never resend media a route rejected; and the guidance message passes through the same
projections as the conversation it follows.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Any, cast

from mindroom.providers.names import canonical_provider
from mindroom.providers.pipeline.deferred_tools import portable_tool_search_stream
from mindroom.providers.pipeline.guidance_retry import guidance_retry_stream
from mindroom.providers.pipeline.media import media_fallback_stream
from mindroom.providers.pipeline.refusal import refusal_stream
from mindroom.providers.pipeline.request_log import RequestLog, logged_stream
from mindroom.providers.pipeline.retry import retrying_stream
from mindroom.providers.pipeline.wrapped import WrappedProvider

if TYPE_CHECKING:
    from mindroom.config.models import ModelConfig
    from mindroom.constants import RuntimePaths
    from mindroom.llm.provider import ModelProvider

__all__ = ["stream_idle_timeout_seconds", "wrap_provider"]

# A hosted API that sends no stream event for this long has stalled.
_DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS = 300.0
# Local servers can stay silent for minutes while a request queues or a model loads.
_LOCAL_STREAM_PROVIDERS = frozenset({"ollama", "llama_cpp"})
# The runtime env variables that configure a provider's endpoint when its config sets none.
_ENDPOINT_VARIABLES = {"openai": "OPENAI_BASE_URL", "vertexai_claude": "ANTHROPIC_VERTEX_BASE_URL"}


def _sets_base_url(value: object) -> bool:
    """Return whether settings, at any depth, point the client at an explicit endpoint."""
    if not isinstance(value, dict):
        return False
    settings = cast("dict[str, Any]", value)
    return any((key == "base_url" and bool(item)) or _sets_base_url(item) for key, item in settings.items())


def stream_idle_timeout_seconds(model_config: ModelConfig, runtime_paths: RuntimePaths) -> float | None:
    """Return the silence limit for one model's streamed requests, or None for no limit.

    Unset, hosted providers on their built-in endpoint get 300 s; local servers and configured
    endpoints, including ``OPENAI_BASE_URL`` and ``ANTHROPIC_VERTEX_BASE_URL``, get no limit; ``0`` disables it.
    """
    if model_config.stream_idle_timeout_seconds is not None:
        return model_config.stream_idle_timeout_seconds or None
    provider = canonical_provider(model_config.provider)
    if provider in _LOCAL_STREAM_PROVIDERS or _sets_base_url(model_config.extra_kwargs):
        return None
    if (variable := _ENDPOINT_VARIABLES.get(provider)) and runtime_paths.env_value(variable):
        return None
    return _DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS


def wrap_provider(
    adapter: ModelProvider,
    *,
    idle_timeout_seconds: float | None,
    media_fallback_prompt: str,
    request_log: RequestLog | None,
) -> ModelProvider:
    """Return ``adapter`` with the full request pipeline around its ``stream``."""
    provider = adapter
    if request_log is not None:
        provider = WrappedProvider(provider, partial(logged_stream, log=request_log))
    provider = WrappedProvider(provider, refusal_stream)
    provider = WrappedProvider(provider, partial(retrying_stream, idle_timeout_seconds=idle_timeout_seconds))
    provider = WrappedProvider(provider, partial(media_fallback_stream, fallback_prompt=media_fallback_prompt))
    provider = WrappedProvider(provider, portable_tool_search_stream)
    return WrappedProvider(provider, guidance_retry_stream)
