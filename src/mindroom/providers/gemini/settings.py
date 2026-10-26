"""The ``extra_kwargs`` a Gemini model accepts, in a module that loads no Google SDK, so config load can check them."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mindroom.providers.extra_kwargs import SECRET

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass(frozen=True, slots=True, kw_only=True)
class GeminiSettings:
    """The ``extra_kwargs`` a Gemini model accepts."""

    api_key: str | None = field(default=None, metadata=SECRET)
    vertexai: bool = False
    project_id: str | None = None
    location: str | None = None
    # A google.auth credentials object; only programmatic callers can supply one.
    credentials: object | None = field(default=None, metadata=SECRET)
    client_params: Mapping[str, Any] | None = field(default=None, metadata=SECRET)
    timeout: float | None = None
    generation_config: Mapping[str, Any] | None = None
    generative_model_kwargs: Mapping[str, Any] | None = None
    safety_settings: Sequence[Any] | None = None
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    max_output_tokens: int | None = None
    stop_sequences: Sequence[str] | None = None
    logprobs: bool | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    seed: int | None = None
    cached_content: str | None = None
    thinking_budget: int | None = None
    include_thoughts: bool | None = None
    thinking_level: str | None = None
    # Google Search grounding, sent in place of the request's function tools, so a grounded model calls no
    # function; kept as main behaves.
    search: bool = False


def calls_vertexai(vertexai: bool) -> bool:
    """Return whether a Gemini model calls Vertex AI: its ``vertexai`` setting, else ``GOOGLE_GENAI_USE_VERTEXAI``."""
    return vertexai or os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "false").lower() == "true"


def configured_gemini_endpoint(extra_kwargs: Mapping[str, Any] | None) -> str | None:
    """Return the endpoint a Gemini model calls when its config or the SDK's variable sets one, else None.

    google-genai takes ``client_params.http_options.base_url``, else ``GOOGLE_VERTEX_BASE_URL`` on Vertex AI or
    ``GOOGLE_GEMINI_BASE_URL`` otherwise from the process environment.
    """
    authored = extra_kwargs or {}
    http_options = (authored.get("client_params") or {}).get("http_options")
    variable = "GOOGLE_VERTEX_BASE_URL" if calls_vertexai(bool(authored.get("vertexai"))) else "GOOGLE_GEMINI_BASE_URL"
    configured = http_options.get("base_url") if isinstance(http_options, Mapping) else None
    return configured or os.environ.get(variable) or None


__all__ = ["GeminiSettings", "calls_vertexai", "configured_gemini_endpoint"]
