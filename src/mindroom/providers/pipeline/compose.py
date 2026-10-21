"""Compose the request pipeline around one provider adapter.

Outermost first: portable deferred tool search, media fallback, stream retry with the idle limit, and
request logging, so each retried attempt is logged and retries never resend media a route rejected.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Any, cast

from mindroom.providers.pipeline.deferred_tools import portable_tool_search_stream
from mindroom.providers.pipeline.media import media_fallback_stream
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


def _sets_base_url(value: object) -> bool:
    """Return whether settings, at any depth, point the client at an explicit endpoint."""
    if not isinstance(value, dict):
        return False
    settings = cast("dict[str, Any]", value)
    return any((key == "base_url" and bool(item)) or _sets_base_url(item) for key, item in settings.items())


def stream_idle_timeout_seconds(model_config: ModelConfig, runtime_paths: RuntimePaths) -> float | None:
    """Return the silence limit for one model's streamed requests, or None for no limit.

    Unset, hosted providers on their built-in endpoint get 300 s; local servers and configured
    endpoints, including ``OPENAI_BASE_URL``, get no limit; ``0`` disables it.
    """
    if model_config.stream_idle_timeout_seconds is not None:
        return model_config.stream_idle_timeout_seconds or None
    provider = model_config.provider.strip().lower().replace("-", "_")
    if provider in _LOCAL_STREAM_PROVIDERS or _sets_base_url(model_config.extra_kwargs):
        return None
    if provider == "openai" and runtime_paths.env_value("OPENAI_BASE_URL"):
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
    provider = WrappedProvider(provider, partial(retrying_stream, idle_timeout_seconds=idle_timeout_seconds))
    provider = WrappedProvider(provider, partial(media_fallback_stream, fallback_prompt=media_fallback_prompt))
    return WrappedProvider(provider, portable_tool_search_stream)
