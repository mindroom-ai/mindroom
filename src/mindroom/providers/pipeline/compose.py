"""Compose the request pipeline around one provider adapter.

Outermost first: portable deferred tool search, media fallback, the malformed tool-call guidance retry, stream
retry with the idle limit, the safeguard refusal, and request logging. Each attempt is therefore logged, a refused
one included; and retries never resend media a route rejected, since the guidance retry resends the request the media
fallback sent.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from mindroom.llm.tool_search import portable_tool_search_stream
from mindroom.providers.pipeline.guidance_retry import guidance_retry_stream
from mindroom.providers.pipeline.media import media_fallback_stream
from mindroom.providers.pipeline.refusal import refusal_stream
from mindroom.providers.pipeline.request_log import RequestLog, logged_stream
from mindroom.providers.pipeline.retry import retrying_stream
from mindroom.providers.pipeline.wrapped import WrappedProvider

if TYPE_CHECKING:
    from mindroom.llm.provider import ModelProvider

__all__ = ["wrap_provider"]


def wrap_provider(
    adapter: ModelProvider,
    *,
    idle_timeout_seconds: float | None,
    media_fallback_prompt: str,
    request_log: RequestLog,
) -> ModelProvider:
    """Return ``adapter`` with the full request pipeline around its ``stream``."""
    provider = WrappedProvider(adapter, partial(logged_stream, log=request_log))
    provider = WrappedProvider(provider, refusal_stream)
    provider = WrappedProvider(provider, partial(retrying_stream, idle_timeout_seconds=idle_timeout_seconds))
    provider = WrappedProvider(provider, guidance_retry_stream)
    provider = WrappedProvider(provider, partial(media_fallback_stream, fallback_prompt=media_fallback_prompt))
    return WrappedProvider(provider, portable_tool_search_stream)
