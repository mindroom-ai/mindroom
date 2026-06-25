"""Shared inline-media fallback detection and model capability helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

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
    "ModelMediaRoute",
    "append_inline_media_fallback_prompt",
    "build_model_media_route",
    "filter_media_inputs_for_route",
    "reset_model_media_capability_cache",
    "retry_media_inputs_after_failure",
    "unsupported_media_kinds_for_route",
]

_INLINE_MEDIA_FALLBACK_MARKER = "[Inline media unavailable for this model]"
_INLINE_MEDIA_FIELD_PATTERN = re.compile(
    r"(?P<kind>document|image|audio|video)\.source\.base64(?:\.media_type)?",
)
_INLINE_MEDIA_MIME_MISMATCH_PATTERN = re.compile(r"image was specified using the .* media type")
_INLINE_MEDIA_GENERIC_UNSUPPORTED_PATTERN = re.compile(r"(?:inline media|media input) is not supported")
_OPENAI_AUDIO_FORMAT_VALIDATION_PATTERN = re.compile(
    r"(?:input_audio\.format|invalid value: ['\"][^'\"]+['\"].*supported values are: ['\"]wav['\"] and ['\"]mp3['\"])",
)
_MEDIA_KIND_PATTERN = r"audio|image|video|file|document"
_INLINE_MEDIA_UNSUPPORTED_PATTERNS = (
    re.compile(rf"(?P<kind>{_MEDIA_KIND_PATTERN}) input is not supported"),
    re.compile(rf"(?P<kind>{_MEDIA_KIND_PATTERN}) inputs are not supported"),
    re.compile(rf"does not support (?P<kind>{_MEDIA_KIND_PATTERN}) input"),
    re.compile(rf"support input (?P<kind>{_MEDIA_KIND_PATTERN})"),
    re.compile(rf"at most 0 (?P<kind>{_MEDIA_KIND_PATTERN})\(s\) may be provided"),
)

# Provider error vocabulary -> our MediaInputs kind (providers say "document" for files).
_PROVIDER_MEDIA_KINDS: dict[str, MediaKind] = {
    "audio": "audio",
    "image": "image",
    "video": "video",
    "file": "file",
    "document": "file",
}


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
class _MediaRetryDecision:
    """Retry policy after one provider media failure."""

    should_retry: bool
    media_inputs: MediaInputs
    removed_kinds: frozenset[MediaKind]


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
) -> _MediaRetryDecision:
    """Decide whether and how one media-bearing request should retry.

    Explicit "kind is not supported" errors teach the route cache so later
    requests pre-drop that kind; validation and ambiguous media errors retry
    once without poisoning the cache. A kind can only be learned when it was
    actually present in ``media_inputs`` or ``extra_present_kinds`` (media
    pinned to thread-history messages in the run input).
    """
    present_kinds = media_inputs.kinds() | extra_present_kinds
    if not present_kinds:
        return _no_media_retry_decision(media_inputs)

    error_text = str(error)
    unsupported_kinds = _unsupported_media_kinds_from_error(error_text)
    if unsupported_kinds:
        return _media_retry_decision_for_kinds(
            media_inputs,
            unsupported_kinds,
            present_kinds=present_kinds,
            cache_route=route,
        )

    validation_kinds = _media_validation_kinds_from_error(error_text)
    if validation_kinds:
        return _media_retry_decision_for_kinds(media_inputs, validation_kinds, present_kinds=present_kinds)

    if _is_ambiguous_media_error(error_text):
        return _MediaRetryDecision(
            should_retry=True,
            media_inputs=_without_media_kinds(media_inputs, present_kinds),
            removed_kinds=present_kinds,
        )

    return _no_media_retry_decision(media_inputs)


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


def _media_retry_decision_for_kinds(
    media_inputs: MediaInputs,
    kinds: frozenset[MediaKind],
    *,
    present_kinds: frozenset[MediaKind],
    cache_route: ModelMediaRoute | None = None,
) -> _MediaRetryDecision:
    removed_kinds = kinds & present_kinds
    if not removed_kinds:
        return _no_media_retry_decision(media_inputs)
    if cache_route is not None:
        _UNSUPPORTED_MEDIA_KINDS_BY_ROUTE.setdefault(cache_route, set()).update(removed_kinds)
    return _MediaRetryDecision(
        should_retry=True,
        media_inputs=_without_media_kinds(media_inputs, removed_kinds),
        removed_kinds=removed_kinds,
    )


def _no_media_retry_decision(media_inputs: MediaInputs) -> _MediaRetryDecision:
    return _MediaRetryDecision(
        should_retry=False,
        media_inputs=media_inputs,
        removed_kinds=frozenset(),
    )


def _unsupported_media_kinds_from_error(error_text: str) -> frozenset[MediaKind]:
    lowered_error_text = error_text.lower()
    kinds: set[MediaKind] = set()
    for pattern in _INLINE_MEDIA_UNSUPPORTED_PATTERNS:
        for match in pattern.finditer(lowered_error_text):
            kind = _canonical_media_kind(match.group("kind"))
            if kind is not None:
                kinds.add(kind)
    return frozenset(kinds)


def _media_validation_kinds_from_error(error_text: str) -> frozenset[MediaKind]:
    lowered_error_text = error_text.lower()
    kinds = {
        kind
        for match in _INLINE_MEDIA_FIELD_PATTERN.finditer(lowered_error_text)
        if (kind := _canonical_media_kind(match.group("kind"))) is not None
    }
    if _INLINE_MEDIA_MIME_MISMATCH_PATTERN.search(lowered_error_text):
        kinds.add("image")
    if _OPENAI_AUDIO_FORMAT_VALIDATION_PATTERN.search(lowered_error_text):
        kinds.add("audio")
    return frozenset(kinds)


def _is_ambiguous_media_error(error_text: str) -> bool:
    return bool(_INLINE_MEDIA_GENERIC_UNSUPPORTED_PATTERN.search(error_text.lower()))


def _canonical_media_kind(provider_kind: str) -> MediaKind | None:
    return _PROVIDER_MEDIA_KINDS.get(provider_kind)


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
