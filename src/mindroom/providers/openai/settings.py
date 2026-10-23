"""Typed ``extra_kwargs`` for the OpenAI wire family, under the field names Agno's model classes used.

Authored configs keep working unchanged: every key a route accepted as an Agno dataclass field that maps to
the wire or the SDK client is a field here. Keys a route does not accept fail with the list of valid keys.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mindroom.providers.extra_kwargs import SECRET

__all__ = [
    "CEREBRAS_CHAT_REQUEST",
    "CEREBRAS_FIELDS",
    "CHAT_FIELDS",
    "CHAT_REQUEST_FIELDS",
    "CLIENT_FIELDS",
    "CODEX_FIELDS",
    "CODEX_REJECTED_FIELDS",
    "GROQ_CHAT_REQUEST",
    "GROQ_FIELDS",
    "OPENAI_CHAT_REQUEST",
    "RESPONSES_FIELDS",
    "ChatSettings",
    "ResponsesSettings",
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
# The settings each Chat Completions dialect sends as request fields, in the order Agno's model class sent them;
# ``response_format`` marks where the request's prepared output format goes. Agno's OpenAIChat ``modalities`` and
# ``audio`` (audio output) are retired; Groq had no ``organization`` or ``extra_body``.
OPENAI_CHAT_REQUEST = (
    "store",
    "reasoning_effort",
    "verbosity",
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
    "extra_headers",
    "extra_query",
    "extra_body",
    "metadata",
    "service_tier",
    "response_format",
)
GROQ_CHAT_REQUEST = (
    "frequency_penalty",
    "logit_bias",
    "logprobs",
    "max_tokens",
    "presence_penalty",
    "response_format",
    "seed",
    "stop",
    "temperature",
    "top_logprobs",
    "top_p",
    "user",
    "extra_headers",
    "extra_query",
)
CEREBRAS_CHAT_REQUEST = (
    "max_completion_tokens",
    "repetition_penalty",
    "temperature",
    "top_p",
    "top_k",
    "extra_headers",
    "extra_query",
    "extra_body",
    "response_format",
)


def _accepted(request: tuple[str, ...], *shaping: str) -> frozenset[str]:
    """Return the settings a dialect accepts: those it sends, and the ``shaping`` ones it reads to build a request."""
    return frozenset(request) - {"response_format"} | frozenset(shaping)


CHAT_REQUEST_FIELDS = _accepted(OPENAI_CHAT_REQUEST, "strict_output", "request_params", "role_map")
CHAT_FIELDS = CHAT_REQUEST_FIELDS | CLIENT_FIELDS
GROQ_FIELDS = CLIENT_FIELDS - {"organization"} | _accepted(GROQ_CHAT_REQUEST, "request_params")
CEREBRAS_FIELDS = CLIENT_FIELDS - {"organization"} | _accepted(
    CEREBRAS_CHAT_REQUEST,
    "parallel_tool_calls",
    "strict_output",
    "request_params",
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
# The Responses fields the Codex endpoint rejects; Agno's Codex model took them and dropped them from every request.
CODEX_REJECTED_FIELDS = frozenset({"max_output_tokens", "temperature"})
CODEX_FIELDS = RESPONSES_FIELDS - CODEX_REJECTED_FIELDS | {
    "codex_home",
    "prompt_cache_key",
    "session_id",
    "default_instructions",
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
    extra_headers: dict[str, Any] | None = field(default=None, metadata=SECRET)
    extra_query: dict[str, Any] | None = field(default=None, metadata=SECRET)
    extra_body: dict[str, Any] | None = None
    request_params: dict[str, Any] | None = None
    role_map: dict[str, str] | None = None
    # Client parameters.
    api_key: str | None = field(default=None, metadata=SECRET)
    organization: str | None = None
    base_url: str | None = None
    timeout: float | None = None
    max_retries: int | None = None
    default_headers: dict[str, str] | None = field(default=None, metadata=SECRET)
    default_query: dict[str, Any] | None = field(default=None, metadata=SECRET)
    client_params: dict[str, Any] | None = field(default=None, metadata=SECRET)
    # Cerebras.
    top_k: int | None = None
    repetition_penalty: float | None = None
    parallel_tool_calls: bool | None = None
    # Azure.
    api_version: str | None = None
    azure_endpoint: str | None = None
    azure_deployment: str | None = None
    azure_ad_token: str | None = field(default=None, metadata=SECRET)
    azure_ad_token_provider: Any = field(default=None, metadata=SECRET)
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
    extra_headers: dict[str, Any] | None = field(default=None, metadata=SECRET)
    extra_query: dict[str, Any] | None = field(default=None, metadata=SECRET)
    extra_body: dict[str, Any] | None = None
    request_params: dict[str, Any] | None = None
    # Split the shared system prefix from session context so the prefix caches across threads.
    cache_system_prompt: bool = True
    # Client parameters.
    api_key: str | None = field(default=None, metadata=SECRET)
    organization: str | None = None
    base_url: str | None = None
    timeout: float | None = None
    max_retries: int | None = None
    default_headers: dict[str, str] | None = field(default=None, metadata=SECRET)
    default_query: dict[str, Any] | None = field(default=None, metadata=SECRET)
    client_params: dict[str, Any] | None = field(default=None, metadata=SECRET)
    # Codex CLI login.
    codex_home: str | None = None
    prompt_cache_key: str | None = None
    session_id: str | None = None
    default_instructions: str | None = None
