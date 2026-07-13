"""Credential-safe errors shared by semantic embedding consumers."""

from __future__ import annotations

import re

_EMBEDDER_AUTH_FAILED_DETAIL = "embedder authentication failed (HTTP 401)"
_EMBEDDER_PERMISSION_DENIED_DETAIL = "embedder permission denied (HTTP 403)"
EMBEDDER_UNREACHABLE_DETAIL = "embedder endpoint unreachable"
EMBEDDER_EMPTY_VECTOR_DETAIL = "embedder returned an empty vector"


class EmbedderRequestError(RuntimeError):
    """Embedding failure carrying only a classified, credential-safe detail.

    The embedder boundary raises this instead of the provider exception so
    upstream loggers cannot render a raw response body that may echo a secret.
    """


def is_embedder_auth_failure_detail(detail: str | None) -> bool:
    """Return whether a failure detail describes a credential rejection."""
    return detail in {_EMBEDDER_AUTH_FAILED_DETAIL, _EMBEDDER_PERMISSION_DENIED_DETAIL}


# Fully-fixed classified forms only: the type-name fallback is excluded because
# identifier-shaped text extracted from operator free text could be a secret.
_CLASSIFIED_DETAIL_PATTERN = re.compile(
    r"embedder authentication failed \(HTTP 401\)"
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


def _is_embedder_provider_error(exc: BaseException) -> bool:
    """Return whether an exception came from the embedding provider SDK."""
    # Deferred so slim entry points never pay the openai SDK import; when a
    # provider call raised, the SDK is already loaded.
    from openai import OpenAIError  # noqa: PLC0415

    return isinstance(exc, OpenAIError)


def classified_embedder_error(exc: BaseException) -> str | None:
    """Return a safe detail only for a known embedding-provider failure."""
    if isinstance(exc, EmbedderRequestError) or _is_embedder_provider_error(exc):
        return describe_embedder_error(exc)
    return None


def describe_embedder_error(exc: BaseException) -> str:
    """Return a compact failure description that never includes provider text."""
    if isinstance(exc, EmbedderRequestError):
        return str(exc)

    from openai import APIConnectionError, APIStatusError, AuthenticationError, PermissionDeniedError  # noqa: PLC0415

    if isinstance(exc, AuthenticationError):
        return _EMBEDDER_AUTH_FAILED_DETAIL
    if isinstance(exc, PermissionDeniedError):
        return _EMBEDDER_PERMISSION_DENIED_DETAIL
    if isinstance(exc, APIStatusError):
        return f"embedder request failed (HTTP {exc.status_code})"
    if isinstance(exc, APIConnectionError):
        return EMBEDDER_UNREACHABLE_DETAIL
    return f"embedder request failed ({type(exc).__name__})"
