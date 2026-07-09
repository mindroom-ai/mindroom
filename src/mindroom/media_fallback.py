"""Shared inline-media fallback and model capability helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agno.exceptions import ContextWindowExceededError, ModelProviderError
from agno.models.anthropic import Claude
from agno.models.azure.openai_chat import AzureOpenAI
from agno.models.cerebras import Cerebras
from agno.models.deepseek import DeepSeek
from agno.models.google import Gemini
from agno.models.groq import Groq
from agno.models.ollama import Ollama
from agno.models.openai import OpenAIChat, OpenAIResponses
from agno.models.openrouter import OpenRouter
from agno.models.vertexai.claude import Claude as VertexAIClaude

from mindroom.media_inputs import MediaInputs, MediaKind

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agno.models.base import Model

__all__ = [
    "MediaRetryDecision",
    "ModelMediaRoute",
    "append_inline_media_fallback_prompt",
    "build_model_media_route",
    "filter_media_inputs_for_route",
    "reset_model_media_capability_cache",
    "retry_media_inputs_after_failure",
    "unsupported_media_kinds_for_route",
]

_INLINE_MEDIA_FALLBACK_MARKER = "[Inline media unavailable for this model]"
_PAYLOAD_TOO_LARGE_STATUS = 413
_RATE_LIMIT_STATUS = 429
_SERVER_ERROR_STATUS = 500


@dataclass(frozen=True, slots=True)
class ModelMediaRoute:
    """Concrete model route used for process-local media capability learning."""

    provider: str
    model_id: str
    base_url: str | None = None


@dataclass(frozen=True, slots=True)
class _MediaFilterResult:
    """Media inputs after route capability filtering."""

    media_inputs: MediaInputs
    removed_kinds: frozenset[MediaKind]


@dataclass(frozen=True, slots=True)
class MediaRetryDecision:
    """Retry policy after one provider media failure.

    ``teach_route_on_success`` carries the route whose capability cache should
    learn ``removed_kinds`` once the without-media retry actually succeeds; the
    attempt loop reports that via :meth:`record_retry_success`.
    """

    should_retry: bool
    media_inputs: MediaInputs
    removed_kinds: frozenset[MediaKind]
    teach_route_on_success: ModelMediaRoute | None = None

    def record_retry_success(self) -> None:
        """Teach the route cache from the successful without-media experiment."""
        if self.teach_route_on_success is None or not self.removed_kinds:
            return
        _UNSUPPORTED_MEDIA_KINDS_BY_ROUTE.setdefault(self.teach_route_on_success, set()).update(self.removed_kinds)


# Intentional process-lifetime pessimism: learned negative capability state is cleared by restart.
_UNSUPPORTED_MEDIA_KINDS_BY_ROUTE: dict[ModelMediaRoute, set[MediaKind]] = {}


def build_model_media_route(model: Model | None) -> ModelMediaRoute | None:
    """Return a process-cache key for one effective model route."""
    if model is None:
        return None

    provider = _route_text(model.provider) or model.__class__.__name__
    model_id = _route_text(model.id) or model.__class__.__name__
    return ModelMediaRoute(
        provider=provider.lower(),
        model_id=model_id,
        base_url=_route_endpoint(model),
    )


def unsupported_media_kinds_for_route(route: ModelMediaRoute | None) -> frozenset[MediaKind]:
    """Return media kinds this route has been learned to reject."""
    if route is None:
        return frozenset()
    return frozenset(_UNSUPPORTED_MEDIA_KINDS_BY_ROUTE.get(route, set()))


def filter_media_inputs_for_route(
    route: ModelMediaRoute | None,
    media_inputs: MediaInputs,
) -> _MediaFilterResult:
    """Omit learned-unsupported media kinds before a model request."""
    removed_kinds = unsupported_media_kinds_for_route(route) & media_inputs.kinds()
    if not removed_kinds:
        return _MediaFilterResult(media_inputs=media_inputs, removed_kinds=frozenset())
    return _MediaFilterResult(
        media_inputs=_without_media_kinds(media_inputs, removed_kinds),
        removed_kinds=removed_kinds,
    )


def retry_media_inputs_after_failure(
    route: ModelMediaRoute | None,
    error: Exception | str,
    media_inputs: MediaInputs,
    *,
    extra_present_kinds: frozenset[MediaKind] = frozenset(),
) -> MediaRetryDecision:
    """Decide how one media-bearing request should retry after a failure.

    Every failure of a media-bearing request retries once without media —
    no error wording decides whether to retry, so unknown provider prose
    (and streamed run errors that lost their HTTP status) degrade
    gracefully instead of leaking a raw provider error to the user. The
    route capability cache learns the dropped kinds once the retry actually
    succeeds (via :meth:`MediaRetryDecision.record_retry_success`), except
    when the error names a payload-size or context-overflow cause, where
    dropping media can succeed for the wrong reason. A kind can only be
    learned when it was actually present in ``media_inputs`` or
    ``extra_present_kinds`` (media pinned to thread-history messages in the
    run input).
    """
    present_kinds = media_inputs.kinds() | extra_present_kinds
    if not present_kinds:
        return _no_media_retry_decision(media_inputs)

    teaching_blocked = _capability_teaching_blocked(error, str(error).lower())
    return MediaRetryDecision(
        should_retry=True,
        media_inputs=_without_media_kinds(media_inputs, present_kinds),
        removed_kinds=present_kinds,
        teach_route_on_success=None if teaching_blocked else route,
    )


def reset_model_media_capability_cache() -> None:
    """Clear process-local learned model media capabilities."""
    _UNSUPPORTED_MEDIA_KINDS_BY_ROUTE.clear()


def append_inline_media_fallback_prompt(
    full_prompt: str,
    *,
    fallback_prompt: str,
) -> str:
    """Append one-time guidance when inline media had to be dropped."""
    if _INLINE_MEDIA_FALLBACK_MARKER in full_prompt:
        return full_prompt

    return f"{full_prompt.rstrip()}\n\n{_INLINE_MEDIA_FALLBACK_MARKER}\n{fallback_prompt}"


def _no_media_retry_decision(media_inputs: MediaInputs) -> MediaRetryDecision:
    return MediaRetryDecision(
        should_retry=False,
        media_inputs=media_inputs,
        removed_kinds=frozenset(),
    )


def _capability_teaching_blocked(error: Exception | str, lowered_error_text: str) -> bool:
    """Report when a retry success would not prove the media kinds unsupported.

    Payload-size and context-overflow rejections shrink below the limit once
    media is dropped, and transient failures (5xx outages, 429 rate limits)
    can pass on the retry because the blip passed — in both cases a
    successful retry says nothing about media capability. Status codes come
    from the exception object, never from provider error prose; streamed run
    errors arrive as bare text without a status and stay eligible to teach.
    """
    if isinstance(error, ContextWindowExceededError):
        return True
    if isinstance(error, ModelProviderError) and (
        error.status_code in (_PAYLOAD_TOO_LARGE_STATUS, _RATE_LIMIT_STATUS)
        or error.status_code >= _SERVER_ERROR_STATUS
    ):
        return True
    if f"error code: {_PAYLOAD_TOO_LARGE_STATUS}" in lowered_error_text:
        return True
    return any(marker in lowered_error_text for marker in ModelProviderError.CONTEXT_WINDOW_PATTERNS)


def _without_media_kinds(media_inputs: MediaInputs, kinds: frozenset[MediaKind]) -> MediaInputs:
    return MediaInputs(
        audio=() if "audio" in kinds else media_inputs.audio,
        images=() if "image" in kinds else media_inputs.images,
        files=() if "file" in kinds else media_inputs.files,
        videos=() if "video" in kinds else media_inputs.videos,
    )


def _route_endpoint(model: Model) -> str | None:
    if isinstance(model, AzureOpenAI):
        return _route_endpoint_text(
            model.azure_endpoint,
            model.base_url,
            _client_params_endpoint(model.client_params),
        )
    if isinstance(model, Ollama):
        return _route_endpoint_text(
            model.host,
            _client_params_endpoint(model.client_params),
        )
    # VertexAIClaude subclasses the Anthropic Claude model but exposes a base_url,
    # so it must be matched here, before the Claude/Gemini branch below.
    if isinstance(
        model,
        (
            VertexAIClaude,
            Cerebras,
            DeepSeek,
            Groq,
            OpenAIChat,
            OpenAIResponses,
            OpenRouter,
        ),
    ):
        return _route_endpoint_text(
            str(model.base_url) if model.base_url is not None else None,
            _client_params_endpoint(model.client_params),
        )
    if isinstance(model, (Claude, Gemini)):
        return _client_params_endpoint(model.client_params)
    return None


def _client_params_endpoint(client_params: Mapping[str, object] | None) -> str | None:
    if client_params is None:
        return None
    for field_name in ("base_url", "host", "azure_endpoint"):
        candidate = client_params.get(field_name)
        endpoint = _route_text(candidate) if isinstance(candidate, str) else None
        if endpoint:
            return endpoint.rstrip("/")
    return None


def _route_endpoint_text(*values: str | None) -> str | None:
    for value in values:
        endpoint = _route_text(value)
        if endpoint:
            return endpoint.rstrip("/")
    return None


def _route_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None
