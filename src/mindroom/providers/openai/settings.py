"""Typed ``extra_kwargs`` for the OpenAI wire family, under the field names Agno's model classes used.

Authored configs keep working unchanged: every key a route accepted as an Agno dataclass field that maps to
the wire or the SDK client is a field here. Keys a route does not accept fail with the list of valid keys.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "CHAT_REQUEST_FIELDS",
    "CLIENT_FIELDS",
    "RESPONSES_FIELDS",
    "ChatSettings",
    "ResponsesSettings",
    "parse_settings",
]

CLIENT_FIELDS = frozenset(
    {
        "api_key",
        "organization",
        "base_url",
        "timeout",
        "max_retries",
        "default_headers",
        "default_query",
        "client_params",
    },
)
# Agno OpenAIChat request fields; ``modalities`` and ``audio`` (audio output) are retired.
CHAT_REQUEST_FIELDS = frozenset(
    {
        "store",
        "reasoning_effort",
        "verbosity",
        "metadata",
        "frequency_penalty",
        "logit_bias",
        "logprobs",
        "top_logprobs",
        "max_tokens",
        "max_completion_tokens",
        "presence_penalty",
        "seed",
        "stop",
        "temperature",
        "user",
        "top_p",
        "service_tier",
        "strict_output",
        "extra_headers",
        "extra_query",
        "extra_body",
        "request_params",
        "role_map",
    },
)
RESPONSES_FIELDS = CLIENT_FIELDS | {
    "include",
    "max_output_tokens",
    "max_tool_calls",
    "metadata",
    "parallel_tool_calls",
    "reasoning",
    "verbosity",
    "reasoning_effort",
    "reasoning_summary",
    "store",
    "temperature",
    "top_p",
    "truncation",
    "user",
    "service_tier",
    "strict_output",
    "extra_headers",
    "extra_query",
    "extra_body",
    "request_params",
    "cache_system_prompt",
}


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatSettings:
    """Settings for every Chat Completions route; each route accepts its own subset of these keys."""

    store: bool | None = None
    reasoning_effort: str | None = None
    verbosity: str | None = None
    metadata: dict[str, Any] | None = None
    frequency_penalty: float | None = None
    logit_bias: Any = None
    logprobs: bool | None = None
    top_logprobs: int | None = None
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    presence_penalty: float | None = None
    seed: int | None = None
    stop: str | list[str] | None = None
    temperature: float | None = None
    user: str | None = None
    top_p: float | None = None
    service_tier: str | None = None
    strict_output: bool = True
    extra_headers: dict[str, Any] | None = None
    extra_query: dict[str, Any] | None = None
    extra_body: dict[str, Any] | None = None
    request_params: dict[str, Any] | None = None
    role_map: dict[str, str] | None = None
    # Client parameters.
    api_key: str | None = None
    organization: str | None = None
    base_url: str | None = None
    timeout: float | None = None
    max_retries: int | None = None
    default_headers: dict[str, str] | None = None
    default_query: dict[str, Any] | None = None
    client_params: dict[str, Any] | None = None
    # Cerebras.
    top_k: int | None = None
    repetition_penalty: float | None = None
    parallel_tool_calls: bool | None = None
    # Azure.
    api_version: str | None = None
    azure_endpoint: str | None = None
    azure_deployment: str | None = None
    azure_ad_token: str | None = None
    azure_ad_token_provider: Any = None
    # OpenRouter: model routing and the Claude prompt-cache ladder for Anthropic-routed ids.
    models: list[str] | None = None
    cache_system_prompt: bool = True
    extended_cache_time: bool = True
    # DeepSeek thinking mode; None keeps the model default.
    use_thinking: bool | None = None
    # Kimi Code login.
    kimi_home: str | None = None
    prompt_cache_key: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ResponsesSettings:
    """Settings for the Responses routes; Codex adds its CLI login and session keys."""

    include: list[str] | None = None
    max_output_tokens: int | None = None
    max_tool_calls: int | None = None
    metadata: dict[str, Any] | None = None
    parallel_tool_calls: bool | None = None
    reasoning: dict[str, Any] | None = None
    verbosity: str | None = None
    reasoning_effort: str | None = None
    reasoning_summary: str | None = None
    store: bool | None = None
    temperature: float | None = None
    top_p: float | None = None
    truncation: str | None = None
    user: str | None = None
    service_tier: str | None = None
    strict_output: bool = True
    extra_headers: dict[str, Any] | None = None
    extra_query: dict[str, Any] | None = None
    extra_body: dict[str, Any] | None = None
    request_params: dict[str, Any] | None = None
    # Split the shared system prefix from session context so the prefix caches across threads.
    cache_system_prompt: bool = True
    # Client parameters.
    api_key: str | None = None
    organization: str | None = None
    base_url: str | None = None
    timeout: float | None = None
    max_retries: int | None = None
    default_headers: dict[str, str] | None = None
    default_query: dict[str, Any] | None = None
    client_params: dict[str, Any] | None = None
    # Codex CLI login.
    codex_home: str | None = None
    prompt_cache_key: str | None = None
    session_id: str | None = None
    default_instructions: str | None = None


def parse_settings[SettingsT: (ChatSettings, ResponsesSettings)](
    settings_type: type[SettingsT],
    extra_kwargs: Mapping[str, Any],
    *,
    accepted: frozenset[str],
    provider: str,
) -> SettingsT:
    """Build one route's settings from authored ``extra_kwargs``, rejecting keys the route does not accept."""
    known = {item.name for item in fields(settings_type)}
    unknown = sorted(key for key in extra_kwargs if key not in accepted or key not in known)
    if unknown:
        msg = (
            f"Unsupported extra_kwargs for provider {provider!r}: {', '.join(unknown)}. "
            f"Valid keys: {', '.join(sorted(accepted & known))}"
        )
        raise ValueError(msg)
    # Authored nested values are copied so request building never writes into the config.
    return settings_type(**{key: deepcopy(value) for key, value in extra_kwargs.items()})
