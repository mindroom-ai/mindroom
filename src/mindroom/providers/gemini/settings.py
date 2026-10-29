"""The ``extra_kwargs`` a Gemini model accepts, in a module that loads no Google SDK, so config load can check them."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mindroom.providers.extra_kwargs import SECRET

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


@dataclass(frozen=True, slots=True, kw_only=True)
class GeminiSettings:
    """The ``extra_kwargs`` a Gemini model accepts."""

    api_key: str | None = field(default=None, metadata=SECRET)
    # The model builder records here the route ``gemini_uses_vertexai`` (``providers/credentials.py``) resolved.
    vertexai: bool = False
    project_id: str | None = None
    location: str | None = None
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
    # Google Search grounding, beside the function tools on the Gemini API and in place of them on Vertex AI.
    search: bool = False


__all__ = ["GeminiSettings"]
