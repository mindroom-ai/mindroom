"""Errors the provider adapters raise, normalized across SDKs."""

from __future__ import annotations

# The ``ProviderError.code`` for a tool call the provider could not parse from the model's output, such as
# Gemini's MALFORMED_FUNCTION_CALL; the loop answers it with one guidance retry, and the media fallback
# never strips media for it.
MALFORMED_TOOL_CALL = "malformed_tool_call"


class ProviderError(Exception):
    """A model provider rejected or failed a request.

    ``retryable`` tells the loop whether the same request may succeed later;
    ``provider`` is the metrics provider name of the adapter that raised.
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        status_code: int | None = None,
        code: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.code = code
        self.retryable = retryable


class ContextWindowExceeded(ProviderError):  # noqa: N818
    """The request does not fit the model's context window; compaction, not retrying, resolves it."""


class InvalidRequest(ProviderError):  # noqa: N818
    """The provider rejected the request body; retrying the same body cannot succeed."""


class RateLimited(ProviderError):  # noqa: N818
    """The provider throttled the request; retryable unless the adapter says otherwise."""

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        status_code: int | None = None,
        code: str | None = None,
        retryable: bool = True,
    ) -> None:
        super().__init__(message, provider=provider, status_code=status_code, code=code, retryable=retryable)


class ProviderUnavailable(ProviderError):  # noqa: N818
    """The provider or the network failed before a response completed; retryable by default."""

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        status_code: int | None = None,
        code: str | None = None,
        retryable: bool = True,
    ) -> None:
        super().__init__(message, provider=provider, status_code=status_code, code=code, retryable=retryable)


__all__ = [
    "MALFORMED_TOOL_CALL",
    "ContextWindowExceeded",
    "InvalidRequest",
    "ProviderError",
    "ProviderUnavailable",
    "RateLimited",
]
