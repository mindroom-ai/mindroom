"""Compose the request pipeline around one provider adapter.

Outermost first: the malformed tool-call guidance retry, portable deferred tool search, media fallback, stream
retry with the idle limit, the safeguard refusal, and request logging. Each attempt is therefore logged, a refused
one included; retries never resend media a route rejected; and the guidance message passes through the same
projections as the conversation it follows.
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
