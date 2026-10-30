"""Credential-safe errors shared by semantic embedding consumers."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Literal

from mindroom.llm.errors import AuthenticationFailed, ContextWindowExceeded, ProviderUnavailable
from mindroom.providers.openai.errors import provider_error

if TYPE_CHECKING:
    from mindroom.llm.errors import ProviderError

# A credential rejection names the status it came with: a 401, or another client error whose wording names the
# credentials, such as Gemini's 400 "API key not valid" on an OpenAI-compatible endpoint.
_EMBEDDER_AUTH_FAILED_PATTERN = re.compile(r"embedder authentication failed \(HTTP \d{3}\)")
_EMBEDDER_PERMISSION_DENIED_DETAIL = "embedder permission denied (HTTP 403)"
EMBEDDER_UNREACHABLE_DETAIL = "embedder endpoint unreachable"
EMBEDDER_EMPTY_VECTOR_DETAIL = "embedder returned an empty vector"

#: What an embedding failure proves, set where it is raised: ``credentials`` a rejected credential or permission, which
#: every later request repeats; ``too_large`` a request over the model's input or tokens-per-minute limit, which a
#: smaller one may pass; ``cardinality`` a multi-input response with a different number of vectors than inputs.
_EmbedderFailureKind = Literal["credentials", "too_large", "cardinality", "other"]


class EmbedderRequestError(RuntimeError):
    """Embedding failure carrying only a classified, credential-safe detail.

    The embedder boundary raises this instead of the provider exception so
    upstream loggers cannot render a raw response body that may echo a secret.
    ``kind`` says what the failure proves, ``retryable`` whether the same
    request may succeed later, and ``retry_after_seconds`` carries the
    provider's ``Retry-After`` hint; all three would otherwise be lost with the
    original exception.
    """

    def __init__(
        self,
        detail: str,
        *,
        kind: _EmbedderFailureKind = "other",
        retryable: bool = False,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(detail)
        self.kind = kind
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


def embedder_credentials_rejected(exc: BaseException) -> bool:
    """Return whether an embedding failure is a rejected credential or permission."""
    return isinstance(exc, EmbedderRequestError) and exc.kind == "credentials"


def is_embedder_auth_failure_detail(detail: str | None) -> bool:
    """Return whether a stored failure detail describes a credential rejection; live failures carry ``kind``."""
    if detail is None:
        return False
    return detail == _EMBEDDER_PERMISSION_DENIED_DETAIL or _EMBEDDER_AUTH_FAILED_PATTERN.fullmatch(detail) is not None


# Fully-fixed classified forms only: the type-name fallback is excluded because
# identifier-shaped text extracted from operator free text could be a secret.
_CLASSIFIED_DETAIL_PATTERN = re.compile(
    r"embedder authentication failed \(HTTP \d{3}\)"
    r"|embedder permission denied \(HTTP 403\)"
    r"|embedder request failed \(HTTP \d{3}\)"
    r"|embedder endpoint unreachable"
    r"|embedder returned an empty vector"
    r"|embedder returned \d+ embeddings for \d+ inputs",
)


def extract_classified_embedder_detail(text: str | None) -> str | None:
    """Extract a classified embedding failure from persisted free text."""
    if text is None:
        return None
    match = _CLASSIFIED_DETAIL_PATTERN.search(text)
    return match.group(0) if match else None


def classified_embedder_error(exc: BaseException) -> str | None:
    """Return a safe detail only for a classified embedder failure, which an embedder raises at its own boundary."""
    return str(exc) if isinstance(exc, EmbedderRequestError) else None


_FORBIDDEN_STATUS = 403


def embedder_request_error(error: BaseException, failure: ProviderError | None) -> EmbedderRequestError:
    """Return the credential-safe error of one failed embedding request.

    ``failure`` is the typed provider error the embedder's SDK mapping made of
    ``error``, or None when the mapping did not recognize it, which makes the
    failure final. The detail names only the HTTP status, an unreachable
    endpoint, or ``error``'s type, never provider text, and ``failure``'s type
    alone decides whether the request is retried, by the rule model requests
    follow, after the ``Retry-After`` hint it carries.
    """
    status_code = failure.status_code if failure is not None else None
    kind: _EmbedderFailureKind = "too_large" if isinstance(failure, ContextWindowExceeded) else "other"
    if status_code == _FORBIDDEN_STATUS:
        detail, kind = _EMBEDDER_PERMISSION_DENIED_DETAIL, "credentials"
    elif isinstance(failure, AuthenticationFailed) and status_code is not None:
        detail, kind = f"embedder authentication failed (HTTP {status_code})", "credentials"
    elif status_code is not None:
        detail = f"embedder request failed (HTTP {status_code})"
    elif isinstance(failure, ProviderUnavailable):
        detail = EMBEDDER_UNREACHABLE_DETAIL
    else:
        detail = f"embedder request failed ({type(error).__name__})"
    if failure is None:
        return EmbedderRequestError(detail)
    return EmbedderRequestError(
        detail,
        kind=kind,
        retryable=failure.retryable,
        retry_after_seconds=failure.retry_after_seconds,
    )


def openai_embedder_error(error: Exception) -> EmbedderRequestError:
    """Return the classified error of one OpenAI SDK or transport failure."""
    return embedder_request_error(error, provider_error(error, provider="openai"))


def embedder_failure_is_transient(exc: BaseException) -> bool:
    """Return whether an embedding failure is worth retrying.

    A failure is retried when its typed provider error says so, by the rule
    model requests follow. Credential rejections, invalid models, dimension
    mismatches and malformed payloads are not: retrying them repeats the same
    rejected request, burns the retry budget, and buries the real cause.
    """
    if isinstance(exc, EmbedderRequestError):
        return exc.retryable
    return isinstance(exc, (TimeoutError, ConnectionError))


def embedder_batch_cardinality_mismatch(exc: BaseException) -> bool:
    """Return whether a multi-input response came back with the wrong vector count.

    This is the one failure that proves a backend does not really implement
    batch input: it accepted the array and answered with a different number of
    vectors. Credential rejections, bad models and ordinary request errors are
    deliberately excluded, because they fail identically one input at a time
    and retrying them per item would only multiply the damage.
    """
    return isinstance(exc, EmbedderRequestError) and exc.kind == "cardinality"


def embedder_retry_after_seconds(exc: BaseException) -> float | None:
    """Return the provider's ``Retry-After`` hint in seconds, when it gave one."""
    return exc.retry_after_seconds if isinstance(exc, EmbedderRequestError) else None


def describe_embedder_error(exc: BaseException) -> str:
    """Return a compact failure description that never includes provider text."""
    return str(exc if isinstance(exc, EmbedderRequestError) else embedder_request_error(exc, None))
