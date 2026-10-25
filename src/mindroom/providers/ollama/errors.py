"""Map Ollama SDK and transport failures to the normalized provider errors.

The Ollama chat adapter and the Ollama embedder share this mapping, so a failure reads the same from either.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ollama import ResponseError

from mindroom.providers.errors import classify_provider_error, provider_failure

if TYPE_CHECKING:
    from mindroom.llm.errors import ProviderError

__all__ = ["provider_error"]


def provider_error(error: BaseException, *, provider: str) -> ProviderError | None:
    """Return the normalized error for one failed request, or None when ``error`` is not a provider failure."""
    if (failure := provider_failure(error, provider=provider)) is not None:
        return failure
    if isinstance(error, ResponseError):
        # The SDK reports an error the server streams after a 200 response with status -1.
        status_code = error.status_code if error.status_code > 0 else None
        return classify_provider_error(str(error), provider=provider, status_code=status_code)
    return None
