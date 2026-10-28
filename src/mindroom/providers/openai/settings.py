"""Typed ``extra_kwargs`` for the OpenAI wire family, under the field names Agno's model classes used.

Authored configs keep working unchanged: every key a route accepted as an Agno dataclass field that maps to
the wire or the SDK client is a field here. Keys a route does not accept fail with the list of valid keys.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mindroom.providers.extra_kwargs import SECRET

__all__ = [
    "CEREBRAS_FIELDS",
    "CHAT_FIELDS",
    "CODEX_FIELDS",
    "CODEX_REJECTED_FIELDS",
    "GROQ_FIELDS",
    "RESPONSES_FIELDS",
    "ChatSettings",
    "ResponsesSettings",
]

_CLIENT_FIELDS = frozenset(
    {
        "api_key",
        "organization",
        "base_url",
        "timeout",
        "default_headers",
        "default_query",
        "client_params",
    },
)


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
    # DeepSeek thinking mode.
    use_thinking: bool = True
    # Kimi Code login.
    kimi_home: str | None = None
    prompt_cache_key: str | None = None

    def request_fields(self) -> dict[str, Any]:
        """Return the request fields a Chat request sends from these settings, unset ones as None.

        Each route accepts only the settings its API takes, so the others stay unset.
        """
        return {
            "store": self.store,
            "reasoning_effort": self.reasoning_effort,
            "verbosity": self.verbosity,
            "frequency_penalty": self.frequency_penalty,
            "logit_bias": self.logit_bias,
            "logprobs": self.logprobs,
            "top_logprobs": self.top_logprobs,
            "max_tokens": self.max_tokens,
            "max_completion_tokens": self.max_completion_tokens,
            "presence_penalty": self.presence_penalty,
            "seed": self.seed,
            "stop": self.stop,
            "temperature": self.temperature,
            "user": self.user,
            "top_p": self.top_p,
            "extra_headers": self.extra_headers,
            "extra_query": self.extra_query,
            "extra_body": self.extra_body,
            "metadata": self.metadata,
            "service_tier": self.service_tier,
            "repetition_penalty": self.repetition_penalty,
            "top_k": self.top_k,
        }


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
    # Mark the stable system message, the first, with an explicit prompt-cache breakpoint where the route takes one.
    cache_system_prompt: bool = True
    # Client parameters.
    api_key: str | None = field(default=None, metadata=SECRET)
    organization: str | None = None
    base_url: str | None = None
    timeout: float | None = None
    default_headers: dict[str, str] | None = field(default=None, metadata=SECRET)
    default_query: dict[str, Any] | None = field(default=None, metadata=SECRET)
    client_params: dict[str, Any] | None = field(default=None, metadata=SECRET)
    # Codex CLI login.
    codex_home: str | None = None
    prompt_cache_key: str | None = None
    session_id: str | None = None
    default_instructions: str | None = None

    def request_fields(self) -> dict[str, Any]:
        """Return the request fields the Responses wire sends as configured, unset ones as None.

        ``store``, ``reasoning``, and the text settings are combined into the request by the wire itself.
        """
        return {
            "include": self.include,
            "max_output_tokens": self.max_output_tokens,
            "max_tool_calls": self.max_tool_calls,
            "metadata": self.metadata,
            "parallel_tool_calls": self.parallel_tool_calls,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "truncation": self.truncation,
            "user": self.user,
            "service_tier": self.service_tier,
            "extra_headers": self.extra_headers,
            "extra_query": self.extra_query,
            "extra_body": self.extra_body,
        }


# Each route accepts the request fields its API takes and the settings the wire or client reads otherwise.
CHAT_FIELDS = _CLIENT_FIELDS | {*ChatSettings().request_fields(), "strict_output", "request_params", "role_map"} - {
    "repetition_penalty",
    "top_k",
}
GROQ_FIELDS = _CLIENT_FIELDS - {"organization"} | {
    "frequency_penalty",
    "logit_bias",
    "logprobs",
    "max_tokens",
    "presence_penalty",
    "seed",
    "stop",
    "temperature",
    "top_logprobs",
    "top_p",
    "user",
    "extra_headers",
    "extra_query",
    "request_params",
}
CEREBRAS_FIELDS = _CLIENT_FIELDS - {"organization"} | {
    "max_completion_tokens",
    "repetition_penalty",
    "temperature",
    "top_p",
    "top_k",
    "extra_headers",
    "extra_query",
    "extra_body",
    "parallel_tool_calls",
    "strict_output",
    "request_params",
}
RESPONSES_FIELDS = _CLIENT_FIELDS | {
    *ResponsesSettings().request_fields(),
    "reasoning",
    "verbosity",
    "reasoning_effort",
    "reasoning_summary",
    "store",
    "strict_output",
    "request_params",
    "cache_system_prompt",
}
# The Responses fields the Codex endpoint rejects; Codex ``extra_kwargs`` do not accept them, and every Codex request
# drops them.
CODEX_REJECTED_FIELDS = frozenset({"max_output_tokens", "temperature"})
CODEX_FIELDS = RESPONSES_FIELDS - CODEX_REJECTED_FIELDS | {
    "codex_home",
    "prompt_cache_key",
    "session_id",
    "default_instructions",
}
