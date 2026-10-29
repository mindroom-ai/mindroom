"""Errors the provider adapters raise, normalized across SDKs."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from mindroom.llm.usage import MessageMetrics

# MindRoom's own ``ProviderError.code`` markers, which a code the provider sent never matches.
# The marker for a tool call the provider could not parse from the model's output, such as Gemini's
# MALFORMED_FUNCTION_CALL; the request pipeline answers it with one guidance retry, and the media fallback never strips
# media for it.
MALFORMED_TOOL_CALL = "malformed_tool_call"
# The marker of a transport failure that timed out, such as an HTTP connect or read timeout, so callers that report
# timeouts need not read the error's text; other transport failures carry ``connection_error``.
TRANSPORT_TIMEOUT = "timeout"
# The marker of a response the model's safeguards refused: neither a retry nor a resend without the request's media can
# change the answer.
SAFEGUARD_REFUSAL = "refusal"
# The marker of a request whose route has no credential at all, so the reply can say how to add one.
MISSING_API_KEY = "missing_api_key"
# The marker of a request that alone exceeds the account's tokens-per-minute limit: it fails as a context-window error,
# since only a smaller request passes, but its reply must not say the conversation outgrew the context window.
TOKENS_PER_MINUTE = "tokens_per_minute"
# The marker of a request the provider refused because the account has no credit left or reached its monthly spend or
# usage limit: no retry passes until the account's billing or limits change, and the request's media did not cause it.
QUOTA_EXHAUSTED = "quota_exhausted"
# The text of a safeguard refusal, which error runs store.
MODEL_SAFEGUARD_REFUSAL_MESSAGE = "Claude returned stop_reason=refusal"


class ProviderError(Exception):
    """A model provider rejected or failed a request.

    ``retryable`` tells the loop whether the same request may succeed later, and each error class fixes it;
    ``provider`` is the metrics provider name of the adapter that raised. ``code`` is a marker MindRoom sets for a failure
    its type alone does not tell apart, one of those above or the pipeline's stall marker, and ``provider_code`` is the
    code the provider sent, such as ``context_length_exceeded``, kept apart so it never reads as a marker.
    ``retry_after_seconds`` is how long the provider's ``Retry-After`` header asked to wait before a retry.
    """

    retryable: ClassVar[bool] = False

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        status_code: int | None = None,
        code: str | None = None,
        provider_code: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.code = code
        self.provider_code = provider_code
        self.retry_after_seconds = retry_after_seconds


class AuthenticationFailed(ProviderError):  # noqa: N818
    """The provider rejected the request's credentials, or the route has none; a retry cannot succeed."""


class ContextWindowExceeded(ProviderError):  # noqa: N818
    """The request does not fit the model's context window, or alone exceeds the account's tokens-per-minute limit.

    A smaller request, not a retry, resolves it.
    """


class InvalidRequest(ProviderError):  # noqa: N818
    """The provider rejected the request body; retrying the same body cannot succeed."""


class RateLimited(ProviderError):  # noqa: N818
    """The provider throttled the request; retryable."""

    retryable = True


class ProviderUnavailable(ProviderError):  # noqa: N818
    """The provider or the network failed before a response completed; retryable."""

    retryable = True


def incomplete_stream_error(*, provider: str) -> ProviderUnavailable:
    """Return the error of a stream that ended before the provider finished its turn; ``provider`` names the route.

    It is retryable like a dropped connection, since the next attempt may finish the turn.
    """
    msg = "The provider stream ended without completing the turn"
    return ProviderUnavailable(msg, provider=provider)


class ModelOutputLimitError(RuntimeError):
    """A run's model responses kept stopping at the output limit before their tool calls were complete."""


class ModelSafeguardRefusal(ProviderError):  # noqa: N818
    """The model's safeguards stopped the response; final, so it is never retried or resent without media.

    ``usage`` is the refused response's usage, for a caller that reads only the finished turn, such as ``complete``.
    """

    def __init__(self, *, provider: str, usage: MessageMetrics) -> None:
        super().__init__(MODEL_SAFEGUARD_REFUSAL_MESSAGE, provider=provider, code=SAFEGUARD_REFUSAL)
        self.usage = usage


__all__ = [
    "MALFORMED_TOOL_CALL",
    "MISSING_API_KEY",
    "MODEL_SAFEGUARD_REFUSAL_MESSAGE",
    "QUOTA_EXHAUSTED",
    "SAFEGUARD_REFUSAL",
    "TOKENS_PER_MINUTE",
    "TRANSPORT_TIMEOUT",
    "AuthenticationFailed",
    "ContextWindowExceeded",
    "InvalidRequest",
    "ModelOutputLimitError",
    "ModelSafeguardRefusal",
    "ProviderError",
    "ProviderUnavailable",
    "RateLimited",
    "incomplete_stream_error",
]
