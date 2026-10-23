"""Errors the provider adapters raise, normalized across SDKs."""

from __future__ import annotations

from typing import ClassVar

from mindroom.llm.usage import MessageMetrics

# The ``ProviderError.code`` for a tool call the provider could not parse from the model's output, such as
# Gemini's MALFORMED_FUNCTION_CALL; the request pipeline answers it with one guidance retry, and the media fallback
# never strips media for it.
MALFORMED_TOOL_CALL = "malformed_tool_call"
# The ``ProviderError.code`` of a stream that ended before its turn completed.
INCOMPLETE_STREAM = "incomplete_stream"
# The ``ProviderError.code`` of a transport failure that timed out, such as an HTTP connect or read timeout, so callers
# that report timeouts need not read the error's text; other transport failures carry ``connection_error``.
TRANSPORT_TIMEOUT = "timeout"
# The ``ProviderError.code`` of a response the model's safeguards refused: neither a retry nor a resend without the
# request's media can change the answer.
SAFEGUARD_REFUSAL = "refusal"
# The text of a safeguard refusal; error runs store it, and the runtime recognizes stored refusals by it.
MODEL_SAFEGUARD_REFUSAL_MESSAGE = "Claude returned stop_reason=refusal"


class ProviderError(Exception):
    """A model provider rejected or failed a request.

    ``retryable`` tells the loop whether the same request may succeed later, and each error class fixes it;
    ``provider`` is the metrics provider name of the adapter that raised.
    """

    retryable: ClassVar[bool] = False

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        status_code: int | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.code = code


class ContextWindowExceeded(ProviderError):  # noqa: N818
    """The request does not fit the model's context window; compaction, not retrying, resolves it."""


class InvalidRequest(ProviderError):  # noqa: N818
    """The provider rejected the request body; retrying the same body cannot succeed."""


class RateLimited(ProviderError):  # noqa: N818
    """The provider throttled the request; retryable."""

    retryable = True


class ProviderUnavailable(ProviderError):  # noqa: N818
    """The provider or the network failed before a response completed; retryable."""

    retryable = True


class ModelOutputLimitError(RuntimeError):
    """A run's model responses kept stopping at the output limit before their tool calls were complete."""


class ModelSafeguardRefusal(ProviderError):  # noqa: N818
    """The model's safeguards stopped the response; final, so it is never retried or resent without media.

    ``usage`` is the refused response's usage, for a caller that reads only the finished turn, such as ``complete``.
    """

    def __init__(self, *, provider: str, usage: MessageMetrics | None = None) -> None:
        super().__init__(MODEL_SAFEGUARD_REFUSAL_MESSAGE, provider=provider, code=SAFEGUARD_REFUSAL)
        self.usage = usage if usage is not None else MessageMetrics()


__all__ = [
    "INCOMPLETE_STREAM",
    "MALFORMED_TOOL_CALL",
    "MODEL_SAFEGUARD_REFUSAL_MESSAGE",
    "SAFEGUARD_REFUSAL",
    "TRANSPORT_TIMEOUT",
    "ContextWindowExceeded",
    "InvalidRequest",
    "ModelOutputLimitError",
    "ModelSafeguardRefusal",
    "ProviderError",
    "ProviderUnavailable",
    "RateLimited",
]
