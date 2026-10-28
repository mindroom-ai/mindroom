"""The ``extra_kwargs`` an Ollama model accepts, and the client options that reach an Ollama server.

The module loads no ollama SDK, so config load can check the settings and doctor can build the options.
"""

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


def ollama_client_options(host: str | None, api_key: str | None) -> dict[str, Any]:
    """Return the Ollama SDK client options that reach ``host``, sending ``api_key``, when set, as the bearer token.

    Without a key, the SDK sends ``OLLAMA_API_KEY`` from the process environment itself.
    """
    return {"host": host, "headers": {"authorization": f"Bearer {api_key}"} if api_key else None}


__all__ = ["OllamaSettings", "ollama_client_options"]
