"""Shared inline-media fallback detection and model capability helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from agno.models.anthropic import Claude
from agno.models.azure.openai_chat import AzureOpenAI
from agno.models.base import Model
from agno.models.cerebras import Cerebras
from agno.models.deepseek import DeepSeek
from agno.models.google import Gemini
from agno.models.groq import Groq
from agno.models.ollama import Ollama
from agno.models.openai import OpenAIChat, OpenAIResponses
from agno.models.openrouter import OpenRouter
from agno.models.vertexai.claude import Claude as VertexAIClaude

from mindroom.media_inputs import MediaInputs

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "ModelMediaRoute",
    "append_inline_media_fallback_prompt",
    "build_model_media_route",
    "filter_media_inputs_for_route",
    "reset_model_media_capability_cache",
    "retry_media_inputs_after_failure",
]

_INLINE_MEDIA_FALLBACK_MARKER = "[Inline media unavailable for this model]"
_INLINE_MEDIA_FIELD_PATTERN = re.compile(
    r"(?P<kind>document|image|audio|video)\.source\.base64(?:\.media_type)?",
)
_INLINE_MEDIA_MIME_MISMATCH_PATTERN = re.compile(r"image was specified using the .* media type")
_INLINE_MEDIA_GENERIC_UNSUPPORTED_PATTERN = re.compile(r"(?:inline media|media input) is not supported")
_MEDIA_KIND_PATTERN = r"audio|image|video|file|document"
_INLINE_MEDIA_UNSUPPORTED_PATTERNS = (
    re.compile(rf"(?P<kind>{_MEDIA_KIND_PATTERN}) input is not supported"),
    re.compile(rf"(?P<kind>{_MEDIA_KIND_PATTERN}) inputs are not supported"),
    re.compile(rf"does not support (?P<kind>{_MEDIA_KIND_PATTERN}) input"),
    re.compile(rf"support input (?P<kind>{_MEDIA_KIND_PATTERN})"),
    re.compile(rf"at most 0 (?P<kind>{_MEDIA_KIND_PATTERN})\(s\) may be provided"),
)

type _MediaKind = Literal["audio", "image", "file", "video"]


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
    removed_kinds: frozenset[_MediaKind]


@dataclass(frozen=True, slots=True)
class _MediaRetryDecision:
    """Retry policy after one provider media failure."""

    should_retry: bool
    media_inputs: MediaInputs
    removed_kinds: frozenset[_MediaKind]


# Intentional process-lifetime pessimism: learned negative capability state is cleared by restart.
_UNSUPPORTED_MEDIA_KINDS_BY_ROUTE: dict[ModelMediaRoute, set[_MediaKind]] = {}


def build_model_media_route(model: Model | str | None) -> ModelMediaRoute | None:
    """Return a process-cache key for one effective model route."""
    if model is None:
        return None
    if isinstance(model, str):
        provider, separator, model_id = model.partition(":")
        return ModelMediaRoute(
            provider=provider.lower() if separator else "unknown",
            model_id=model_id or provider,
            base_url=None,
        )
    if not isinstance(model, Model):
        return None

    provider = _route_text(model.provider) or model.__class__.__name__
    model_id = _route_text(model.id) or model.__class__.__name__
    return ModelMediaRoute(
        provider=provider.lower(),
        model_id=model_id,
        base_url=_route_endpoint(model),
    )


def filter_media_inputs_for_route(
    route: ModelMediaRoute | None,
    media_inputs: MediaInputs,
) -> _MediaFilterResult:
    """Omit learned-unsupported media kinds before a model request."""
    unsupported_kinds = (
        frozenset(_UNSUPPORTED_MEDIA_KINDS_BY_ROUTE.get(route, set())) if route is not None else frozenset()
    )
    removed_kinds = unsupported_kinds & _media_kinds_present(media_inputs)
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
    learn_route_capability: bool = False,
) -> _MediaRetryDecision:
    """Decide whether and how one media-bearing request should retry.

    Only callers that just sent inline media to a model should set
    ``learn_route_capability``; otherwise explicit media-kind errors retry once
    without poisoning the process-local route cache.
    """
    if not media_inputs.has_any():
        return _no_media_retry_decision(media_inputs)

    error_text = str(error)
    unsupported_kinds = _unsupported_media_kinds_from_error(error_text)
    if unsupported_kinds:
        return _media_retry_decision_for_kinds(
            media_inputs,
            unsupported_kinds,
            cache_route=route if learn_route_capability else None,
        )

    validation_kinds = _media_validation_kinds_from_error(error_text)
    if validation_kinds:
        return _media_retry_decision_for_kinds(media_inputs, validation_kinds)

    if _is_ambiguous_media_error(error_text):
        removed_kinds = _media_kinds_present(media_inputs)
        return _MediaRetryDecision(
            should_retry=True,
            media_inputs=_without_media_kinds(media_inputs, removed_kinds),
            removed_kinds=removed_kinds,
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
    kinds: frozenset[_MediaKind],
    *,
    cache_route: ModelMediaRoute | None = None,
) -> _MediaRetryDecision:
    removed_kinds = kinds & _media_kinds_present(media_inputs)
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


def _unsupported_media_kinds_from_error(error_text: str) -> frozenset[_MediaKind]:
    lowered_error_text = error_text.lower()
    kinds: set[_MediaKind] = set()
    for pattern in _INLINE_MEDIA_UNSUPPORTED_PATTERNS:
        for match in pattern.finditer(lowered_error_text):
            kind = _canonical_media_kind(match.group("kind"))
            if kind is not None:
                kinds.add(kind)
    return frozenset(kinds)


def _media_validation_kinds_from_error(error_text: str) -> frozenset[_MediaKind]:
    lowered_error_text = error_text.lower()
    kinds = {
        kind
        for match in _INLINE_MEDIA_FIELD_PATTERN.finditer(lowered_error_text)
        if (kind := _canonical_media_kind(match.group("kind"))) is not None
    }
    if _INLINE_MEDIA_MIME_MISMATCH_PATTERN.search(lowered_error_text):
        kinds.add("image")
    return frozenset(kinds)


def _is_ambiguous_media_error(error_text: str) -> bool:
    return bool(_INLINE_MEDIA_GENERIC_UNSUPPORTED_PATTERN.search(error_text.lower()))


def _canonical_media_kind(provider_kind: str) -> _MediaKind | None:
    if provider_kind == "document":
        return "file"
    if provider_kind == "audio":
        return "audio"
    if provider_kind == "image":
        return "image"
    if provider_kind == "file":
        return "file"
    if provider_kind == "video":
        return "video"
    return None


def _media_kinds_present(media_inputs: MediaInputs) -> frozenset[_MediaKind]:
    kinds: set[_MediaKind] = set()
    if media_inputs.audio:
        kinds.add("audio")
    if media_inputs.images:
        kinds.add("image")
    if media_inputs.files:
        kinds.add("file")
    if media_inputs.videos:
        kinds.add("video")
    return frozenset(kinds)


def _without_media_kinds(media_inputs: MediaInputs, kinds: frozenset[_MediaKind]) -> MediaInputs:
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
    if isinstance(model, VertexAIClaude):
        return _route_endpoint_text(
            str(model.base_url) if model.base_url is not None else None,
            _client_params_endpoint(model.client_params),
        )
    if isinstance(
        model,
        (
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
