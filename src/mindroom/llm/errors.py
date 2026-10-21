"""Errors the provider adapters raise, normalized across SDKs."""

from __future__ import annotations

# The ``ProviderError.code`` for a tool call the provider could not parse from the model's output, such as
# Gemini's MALFORMED_FUNCTION_CALL; the request pipeline answers it with one guidance retry, and the media fallback
# never strips media for it.
MALFORMED_TOOL_CALL = "malformed_tool_call"
# The ``ProviderError.code`` of a stream that failed after the attempt exposed output or a hosted tool ran: a retry
# could duplicate output or rerun the tool, so the error is final and the media fallback never resends the request.
INCOMPLETE_STREAM = "incomplete_stream"
# The ``ProviderError.code`` of a response the model's safeguards refused: neither a retry nor a resend without the
# request's media can change the answer.
SAFEGUARD_REFUSAL = "refusal"
# The text of a safeguard refusal; error runs store it, and the runtime recognizes stored refusals by it.
MODEL_SAFEGUARD_REFUSAL_MESSAGE = "Claude returned stop_reason=refusal"


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


class ModelOutputLimitError(RuntimeError):
    """A run's model responses kept stopping at the output limit before their tool calls were complete."""


class ModelSafeguardRefusal(ProviderError):  # noqa: N818
    """The model's safeguards stopped the response; final, so it is never retried or resent without media."""

    def __init__(self, *, provider: str) -> None:
        super().__init__(MODEL_SAFEGUARD_REFUSAL_MESSAGE, provider=provider, code=SAFEGUARD_REFUSAL)


__all__ = [
    "INCOMPLETE_STREAM",
    "MALFORMED_TOOL_CALL",
    "MODEL_SAFEGUARD_REFUSAL_MESSAGE",
    "SAFEGUARD_REFUSAL",
    "ContextWindowExceeded",
    "InvalidRequest",
    "ModelOutputLimitError",
    "ModelSafeguardRefusal",
    "ProviderError",
    "ProviderUnavailable",
    "RateLimited",
]
