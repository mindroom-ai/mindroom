"""The ``extra_kwargs`` an Ollama model accepts, in a module that loads no ollama SDK, so config load can check them."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from mindroom.providers.extra_kwargs import SECRET

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True, slots=True, kw_only=True)
class OllamaSettings:
    """The ``extra_kwargs`` an Ollama model accepts."""

    api_key: str | None = field(default=None, metadata=SECRET)
    timeout: float | None = None
    client_params: Mapping[str, Any] | None = field(default=None, metadata=SECRET)
    format: str | Mapping[str, Any] | None = None
    options: Mapping[str, Any] | None = None
    keep_alive: float | str | None = None
    # Thinking on or off, or a thinking model's effort; unset leaves it to the model.
    think: bool | Literal["low", "medium", "high"] | None = None
    request_params: Mapping[str, Any] | None = None


__all__ = ["OllamaSettings"]
