"""Map OpenAI SDK, OpenAI-compatible server, and transport failures to the owned provider errors.

``retryable`` carries the transient-failure judgment the stream retry wrapper acts on; statuses and
codes are preserved so user-facing messages and compaction can still tell failures apart.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

import httpx

from mindroom.llm.errors import MISSING_API_KEY, AuthenticationFailed, ProviderError
from mindroom.providers.errors import classify_provider_error, provider_failure

__all__ = [
    "missing_api_key_error",
    "provider_error",
    "sdk_failures",
]


def sdk_failures() -> tuple[type[Exception], ...]:
    """Return the exception types adapters map with ``provider_error``; resolved late to keep the SDK unloaded."""
    import openai  # noqa: PLC0415 - keep the SDK out of module import time

    return (openai.OpenAIError, httpx.TransportError)


def _error_fields(body: object) -> tuple[str | None, str | None, str | None]:
    """Return the message, code, and type of an error body the SDK already unwrapped from its ``error`` envelope."""
    if not isinstance(body, Mapping):
        return (body if isinstance(body, str) and body else None), None, None
    fields = cast("Mapping[str, Any]", body)
    message, code, error_type = fields.get("message"), fields.get("code"), fields.get("type")
    return (
        message if isinstance(message, str) else None,
        str(code) if code is not None else None,
        error_type if isinstance(error_type, str) else None,
    )


def provider_error(error: Exception, *, provider: str) -> ProviderError:
    """Return the owned error for one SDK or transport failure; ``provider`` is the metrics provider name."""
    import openai  # noqa: PLC0415 - keep the SDK out of module import time

    if (failure := provider_failure(error, provider=provider)) is not None:
        return failure
    if isinstance(error, openai.APIStatusError):
        message, code, _ = _error_fields(error.body)
        if message is None:
            message = error.response.text or str(error)
        return classify_provider_error(message, provider=provider, status_code=error.status_code, code=code)
    if isinstance(error, openai.APIError):
        message, code, error_type = _error_fields(error.body)
        return classify_provider_error(message or error.message, provider=provider, code=code, error_type=error_type)
    return ProviderError(str(error) or type(error).__name__, provider=provider)


def missing_api_key_error(env_var: str, *, provider: str) -> AuthenticationFailed:
    """Return the error for a route that has no API key, which the reply turns into how to connect one."""
    message = f"{env_var} not set. Please set the {env_var} environment variable."
    return AuthenticationFailed(message, provider=provider, status_code=401, code=MISSING_API_KEY)
