"""Simple error handling for MindRoom agents."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from mindroom.failure_text import failure_text
from mindroom.llm.errors import (
    CONNECTION_ERROR,
    MISSING_API_KEY,
    QUOTA_EXHAUSTED,
    STREAM_STALLED,
    TOKENS_PER_MINUTE,
    TRANSPORT_TIMEOUT,
    AuthenticationFailed,
    ContextWindowExceeded,
    ModelSafeguardRefusal,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
)
from mindroom.logging_config import get_logger
from mindroom.origins import configured_public_url
from mindroom.providers.names import ROUTE_PROVIDERS, provider_route
from mindroom.redaction import REDACTED, redact_sensitive_text
from mindroom.tool_system.runtime_context import current_execution_identity

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

_STALL_DURATION_RE = re.compile(r"\bfor\s+(\d+(?:\.\d+)?)s\b")
# Bound exception-derived fields in ``io.mindroom.ai_run.error`` so oversized provider errors
# cannot push Matrix edit payloads (duplicated into top-level and ``m.new_content``) over the 64 KiB limit.
_MAX_ERROR_SUMMARY_CHARS = 1_000
_MAX_ERROR_DETAIL_CHARS = 2_000
_MAX_ERROR_CODE_CHARS = 200
_USER_REDACTED = "[redacted]"

type _ReplyErrorKind = Literal[
    "safeguard_refusal",
    "missing_api_key",
    "quota_exhausted",
    "authentication_failed",
    "tokens_per_minute",
    "context_window",
    "rate_limited",
    "stream_stalled",
    "provider_unavailable",
    "timeout",
    "runtime_error",
]
type _ReplyErrorAction = Literal["continue", "retry", "compact"]

_PLAIN_TEXT_ICONS: dict[_ReplyErrorKind, str] = {
    "quota_exhausted": "💳",
    "rate_limited": "⏱️",
    "timeout": "⏰",
}


@dataclass(frozen=True, slots=True)
class _ReplyErrorInfo:
    """Structured classification of a reply failure for ``io.mindroom.ai_run.error`` and plain-text fallback."""

    kind: _ReplyErrorKind
    title: str
    summary: str
    detail: str | None = None
    code: str | None = None
    status_code: int | None = None
    provider: str | None = None
    retryable: bool = False
    action: _ReplyErrorAction | None = None

    def to_wire(self, *, fallback: str | None = None) -> dict[str, Any]:
        """Return the wire object published at ``io.mindroom.ai_run["error"]``."""
        resolved_fallback = (self.plain_text if fallback is None else fallback).strip()
        optional = (
            ("detail", self.detail),
            ("code", self.code),
            ("status_code", self.status_code),
            ("provider", self.provider),
            ("action", self.action),
            (
                "fallback",
                resolved_fallback if resolved_fallback and len(resolved_fallback) <= _MAX_ERROR_DETAIL_CHARS else None,
            ),
        )
        return {
            "kind": self.kind,
            "title": self.title,
            "summary": self.summary,
            "retryable": self.retryable,
            **{key: value for key, value in optional if value is not None and value != ""},
        }

    @property
    def plain_text(self) -> str:
        """Return the plain-text fallback shown in Matrix clients that do not render structured error cards."""
        if self.kind == "missing_api_key":
            return f"🔑 {self.summary}"
        if self.kind == "authentication_failed":
            return f"❌ {self.title}: {self.summary}"
        if self.kind == "runtime_error":
            return f"⚠️ {self.summary}"
        icon = _PLAIN_TEXT_ICONS.get(self.kind, "⚠️")
        return f"{icon} {self.title} — {self.summary}"


class AvatarGenerationError(RuntimeError):
    """Raised when managed avatar generation cannot produce required assets."""


class AvatarSyncError(RuntimeError):
    """Raised when managed avatar sync cannot complete."""


class MinimalModeUnavailableError(RuntimeError):
    """Minimal mode cannot serve a turn; the message already names its recovery command."""


class StructuredReplyError(RuntimeError):
    """A reply failure that already carries structured ``io.mindroom.ai_run["error"]`` metadata."""

    def __init__(self, message: str, *, error_info: Mapping[str, Any]) -> None:
        super().__init__(message)
        self.error_info: dict[str, Any] = dict(error_info)


def minimal_mode_failure_message(reason: str, agent_name: str, *, subagent: bool) -> str:
    """Include how to recover from unavailable minimal mode: the mode command, or a standard subagent."""
    if subagent:
        return f"{reason.rstrip('.')}. Start a new subagent without minimal."
    return f"{reason.rstrip('.')}. Return to standard mode with `/mode {agent_name} standard`."


def model_error_labels(model: Mapping[str, str] | None) -> tuple[str | None, str | None]:
    """Return ``(model_label, provider)`` from a published ``io.mindroom.ai_run["model"]`` payload."""
    if not model:
        return None, None
    return model.get("display_name") or model.get("id") or None, model.get("provider") or None


def _display_provider_name(provider: str | None) -> str | None:
    if not provider or not provider.strip():
        return None
    stripped = provider.strip()
    return ROUTE_PROVIDERS.get(provider_route(stripped), stripped)


def _stall_duration_label(safe_error: str) -> str | None:
    match = _STALL_DURATION_RE.search(safe_error)
    if match is None:
        return None
    seconds = float(match.group(1))
    if seconds <= 0:
        return None
    if seconds >= 60:
        minutes, rem = divmod(round(seconds), 60)
        return f"{minutes} min" if rem == 0 else f"{minutes} min {rem}s"
    return f"{seconds:g}s"


def _redact_user_error_text(text: str, *, max_length: int) -> str:
    """Redact sensitive text and normalize ``***redacted***`` to Markdown-neutral ``[redacted]``."""
    return redact_sensitive_text(text, max_length=max_length).replace(REDACTED, _USER_REDACTED)


def _truncate_error_text(text: str, max_chars: int) -> str:
    """Bound exception-derived text after redaction so wire error metadata stays compact."""
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 1].rstrip()}…"


def with_error_fallback(
    error_info: Mapping[str, Any] | None,
    fallback: str | None,
) -> dict[str, Any] | None:
    """Return ``error_info`` with ``fallback`` set to the exact rendered text, or omitted when empty or oversized."""
    if error_info is None:
        return None
    updated = dict(error_info)
    stripped = fallback.strip() if fallback is not None else ""
    if stripped and len(stripped) <= _MAX_ERROR_DETAIL_CHARS:
        updated["fallback"] = stripped
    else:
        updated.pop("fallback", None)
    return updated


def empty_response_error_info(
    *,
    summary: str,
    model_label: str | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    """Return structured ``io.mindroom.ai_run["error"]`` metadata for an exhausted empty model response."""
    resolved_model = model_label.strip() if model_label and model_label.strip() else None
    resolved_provider = _display_provider_name(provider)
    return _ReplyErrorInfo(
        kind="provider_unavailable",
        title=f"{resolved_model} returned an empty response" if resolved_model else "Empty response",
        summary=summary.removeprefix("⚠️ ").strip(),
        provider=resolved_provider,
        retryable=True,
        action="retry",
    ).to_wire(fallback=summary)


def _missing_provider_key_message(runtime_paths: RuntimePaths | None, safe_error: str) -> str:
    identity = current_execution_identity()
    if runtime_paths is not None and identity is not None and identity.channel == "headless":
        # A headless run has no dashboard: its keys come from the process environment or the storage's `.env`.
        return (
            f"No AI provider key is set up yet, so I can't reply. {safe_error.rstrip('.')}. "
            f"A `.env` file at {runtime_paths.env_path} works too."
        )
    dashboard_url = configured_public_url(runtime_paths) if runtime_paths else None
    dashboard = f"the MindRoom dashboard ({dashboard_url})" if dashboard_url else "the MindRoom dashboard"
    return (
        "No AI provider key is set up yet, so I can't reply. "
        f"Open {dashboard}, choose Connect your AI provider, paste your key, then send your message again."
    )


def _classify_provider_error(  # noqa: PLR0911 - one return per provider error kind
    error: ProviderError,
    *,
    raw_error: str,
    safe_summary: str,
    resolved_model: str | None,
    resolved_provider: str | None,
) -> tuple[_ReplyErrorKind, str, str, bool, _ReplyErrorAction | None]:
    model_text = resolved_model or "The model"
    model_inline = resolved_model or "the model"
    provider_text = resolved_provider or "The model provider"
    provider_inline = resolved_provider or "the model provider"

    if error.code == QUOTA_EXHAUSTED:
        return (
            "quota_exhausted",
            "Account quota reached",
            (
                f"{provider_text} rejected this request because the account's quota or credits are exhausted. "
                "Add credit, renew the plan, or raise the spending limit, then try again."
            ),
            False,
            None,
        )
    if isinstance(error, AuthenticationFailed):
        title = f"Authentication failed ({resolved_provider})" if resolved_provider else "Authentication failed"
        return ("authentication_failed", title, safe_summary, False, None)
    if isinstance(error, ContextWindowExceeded) and error.code == TOKENS_PER_MINUTE:
        return (
            "tokens_per_minute",
            "Request too large for rate limit",
            "This request exceeded the account's tokens-per-minute limit. Compact the thread or start a new one.",
            False,
            "compact",
        )
    if isinstance(error, ContextWindowExceeded):
        return (
            "context_window",
            "Context window full",
            f"This conversation exceeded {model_inline}'s context window. Compact the thread or start a new one.",
            False,
            "compact",
        )
    if isinstance(error, RateLimited):
        return (
            "rate_limited",
            "Rate limit reached",
            f"{model_text} hit {provider_inline}'s rate limit. Try again shortly.",
            True,
            "retry",
        )
    if isinstance(error, ProviderUnavailable):
        if error.code == STREAM_STALLED:
            duration = _stall_duration_label(raw_error)
            summary = (
                f"{provider_text} sent no data for {duration}."
                if duration
                else f"{provider_text} stopped sending data."
            )
            return ("stream_stalled", f"{model_text} stopped responding", summary, True, "continue")
        if error.code in (TRANSPORT_TIMEOUT, CONNECTION_ERROR):
            return (
                "timeout",
                "Network error or timeout",
                (
                    "The model request failed with a network error or timeout. "
                    "If this keeps happening, check the MindRoom host's internet connection."
                ),
                True,
                "retry",
            )
        status = error.status_code
        status_hint = f" ({status})" if status is not None and 500 <= status < 600 else ""
        provider_title = resolved_provider or "Model provider"
        return (
            "provider_unavailable",
            f"{provider_title} unavailable{status_hint}",
            f"{model_text} could not complete the reply. Try again shortly.",
            True,
            "continue",
        )
    return ("runtime_error", "Reply failed", safe_summary, False, None)


def describe_reply_error(
    error: Exception,
    *,
    runtime_paths: RuntimePaths | None = None,
    model_label: str | None = None,
    provider: str | None = None,
) -> _ReplyErrorInfo:
    """Classify ``error`` into structured wire metadata and clean plain-text copy.

    An error's type alone decides its classification: a provider error was classified from the provider's status and
    wording where its adapter raised it, and any other error shows its own text, except that a timeout says so.
    """
    raw_error = failure_text(error)
    safe_error = _redact_user_error_text(raw_error, max_length=_MAX_ERROR_DETAIL_CHARS)
    safe_summary = _truncate_error_text(safe_error, _MAX_ERROR_SUMMARY_CHARS)
    provider_error = error if isinstance(error, ProviderError) else None
    resolved_model = model_label.strip() if model_label and model_label.strip() else None
    model_text = resolved_model or "The model"
    raw_provider = (provider_error.provider or provider) if provider_error is not None else None
    resolved_provider = _display_provider_name(raw_provider)
    status_code = provider_error.status_code if provider_error is not None else None
    code = (
        _redact_user_error_text(provider_error.code, max_length=_MAX_ERROR_CODE_CHARS)
        if provider_error is not None and provider_error.code
        else None
    )

    retryable = False
    action: _ReplyErrorAction | None = None
    if isinstance(error, ModelSafeguardRefusal):
        safe_provider_code = (
            _redact_user_error_text(error.provider_code, max_length=_MAX_ERROR_CODE_CHARS)
            if error.provider_code
            else None
        )
        reason_hint = f" ({safe_provider_code})" if safe_provider_code else ""
        kind: _ReplyErrorKind = "safeguard_refusal"
        title = "Blocked by model safeguards"
        summary = (
            f"{model_text}'s safeguards blocked this request{reason_hint}. "
            "Choose another model (/model list) or revise the prompt."
        )
        code = safe_provider_code
    elif needs_provider_key(error):
        kind = "missing_api_key"
        title = "No AI provider key configured"
        summary = _missing_provider_key_message(runtime_paths, safe_error)
        code = MISSING_API_KEY
    elif provider_error is not None:
        kind, title, summary, retryable, action = _classify_provider_error(
            provider_error,
            raw_error=raw_error,
            safe_summary=safe_summary,
            resolved_model=resolved_model,
            resolved_provider=resolved_provider,
        )
    elif isinstance(error, TimeoutError):
        kind = "timeout"
        title = "Request timed out"
        summary = f"{model_text} timed out waiting for a response. Try again shortly."
        retryable = True
        action = "retry"
    else:
        kind = "runtime_error"
        title = "Reply failed"
        summary = safe_summary

    return _ReplyErrorInfo(
        kind=kind,
        title=title,
        summary=summary,
        detail=None if kind == "quota_exhausted" or not safe_error or safe_error == summary else safe_error,
        code=code,
        status_code=status_code,
        provider=resolved_provider,
        retryable=retryable,
        action=action,
    )


def log_reply_error(
    error: Exception,
    agent_name: str | None = None,
    *,
    runtime_paths: RuntimePaths | None = None,
    model_label: str | None = None,
    provider: str | None = None,
) -> _ReplyErrorInfo:
    """Log ``error`` as ``agent_error`` and return its structured ``_ReplyErrorInfo`` classification."""
    provider_error = error if isinstance(error, ProviderError) else None

    logger.error(
        "agent_error",
        agent=agent_name or "agent",
        error_type=type(error).__name__,
        error=repr(error),
        error_code=provider_error.code if provider_error else None,
        status_code=provider_error.status_code if provider_error else None,
        provider=provider_error.provider if provider_error else None,
    )

    return describe_reply_error(
        error,
        runtime_paths=runtime_paths,
        model_label=model_label,
        provider=provider,
    )


def needs_provider_key(error: Exception) -> bool:
    """Return whether ``error`` means no AI provider key is set up, which its reply tells the user how to fix.

    That reply is setup guidance the user reads, unlike the other failure replies, which report an error. Only the
    typed missing-key error an adapter raises at its boundary means it.
    """
    return isinstance(error, AuthenticationFailed) and error.code == MISSING_API_KEY
