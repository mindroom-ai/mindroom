"""Credential-safe errors shared by semantic embedding consumers."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

_EMBEDDER_AUTH_FAILED_DETAIL = "embedder authentication failed (HTTP 401)"
_EMBEDDER_PERMISSION_DENIED_DETAIL = "embedder permission denied (HTTP 403)"
EMBEDDER_UNREACHABLE_DETAIL = "embedder endpoint unreachable"
EMBEDDER_EMPTY_VECTOR_DETAIL = "embedder returned an empty vector"


class EmbedderRequestError(RuntimeError):
    """Embedding failure carrying only a classified, credential-safe detail.

    The embedder boundary raises this instead of the provider exception so
    upstream loggers cannot render a raw response body that may echo a secret.
    ``retry_after_seconds`` carries the provider's ``Retry-After`` hint, which
    would otherwise be lost with the original exception.
    """

    def __init__(self, detail: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(detail)
        self.retry_after_seconds = retry_after_seconds


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


_TRANSIENT_STATUS_CODES = frozenset({408, 429})
_MIN_TRANSIENT_SERVER_STATUS = 500
_MAX_HTTP_STATUS = 599
_CLASSIFIED_HTTP_STATUS_PATTERN = re.compile(r"embedder request failed \(HTTP (\d{3})\)")


def _status_code_is_transient(status_code: int) -> bool:
    return status_code in _TRANSIENT_STATUS_CODES or (_MIN_TRANSIENT_SERVER_STATUS <= status_code <= _MAX_HTTP_STATUS)


def embedder_failure_is_transient(exc: BaseException) -> bool:
    """Return whether an embedding failure is worth retrying.

    Transport faults, timeouts, throttling and server-side errors are retried.
    Credential rejections, invalid models, dimension mismatches and malformed
    payloads are not: retrying them repeats the same rejected request, burns
    the retry budget, and buries the real cause.
    """
    if isinstance(exc, EmbedderRequestError):
        detail = str(exc)
        if detail == EMBEDDER_UNREACHABLE_DETAIL:
            return True
        status_match = _CLASSIFIED_HTTP_STATUS_PATTERN.fullmatch(detail)
        return status_match is not None and _status_code_is_transient(int(status_match.group(1)))
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    if not _is_embedder_provider_error(exc):
        return False

    # Deferred like the probe above: slim entry points must not pay the openai
    # SDK import, and reaching here means a provider call already loaded it.
    from openai import APIConnectionError, APIStatusError  # noqa: PLC0415

    if isinstance(exc, APIConnectionError):
        return True
    return isinstance(exc, APIStatusError) and _status_code_is_transient(exc.status_code)


_CARDINALITY_MISMATCH_PATTERN = re.compile(r"embedder returned \d+ embeddings for \d+ inputs")


def embedder_batch_cardinality_mismatch(exc: BaseException) -> bool:
    """Return whether a multi-input response came back with the wrong vector count.

    This is the one failure that proves a backend does not really implement
    batch input: it accepted the array and answered with a different number of
    vectors. Credential rejections, bad models and ordinary request errors are
    deliberately excluded, because they fail identically one input at a time
    and retrying them per item would only multiply the damage.
    """
    return isinstance(exc, EmbedderRequestError) and _CARDINALITY_MISMATCH_PATTERN.fullmatch(str(exc)) is not None


def embedder_retry_after_seconds(exc: BaseException) -> float | None:
    """Return the provider's ``Retry-After`` hint in seconds, when it gave one."""
    if isinstance(exc, EmbedderRequestError):
        return exc.retry_after_seconds
    if not _is_embedder_provider_error(exc):
        return None

    # Deferred for the same reason: keep the openai SDK out of import time.
    from openai import APIStatusError  # noqa: PLC0415

    if not isinstance(exc, APIStatusError):
        return None
    return _retry_after_seconds_from_headers(exc.response.headers)


def _retry_after_seconds_from_headers(headers: Mapping[str, str]) -> float | None:
    """Parse a numeric ``Retry-After`` header; HTTP-date forms are ignored."""
    raw_value = headers.get("retry-after") or headers.get("Retry-After")
    if raw_value is None:
        return None
    try:
        seconds = float(raw_value.strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


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
