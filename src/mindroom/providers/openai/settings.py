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
    "CHAT_REQUEST_FIELDS",
    "CLIENT_FIELDS",
    "CODEX_FIELDS",
    "GROQ_FIELDS",
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

    def openai_request_fields(self) -> dict[str, Any]:
        """Return the request fields the ``openai`` Chat dialect sends from these settings, unset ones as None."""
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
        }

    def groq_request_fields(self) -> dict[str, Any]:
        """Return the request fields the ``groq`` dialect sends; Agno's Groq had no ``organization`` or ``extra_body``."""
        return {
            "frequency_penalty": self.frequency_penalty,
            "logit_bias": self.logit_bias,
            "logprobs": self.logprobs,
            "max_tokens": self.max_tokens,
            "presence_penalty": self.presence_penalty,
            "seed": self.seed,
            "stop": self.stop,
            "temperature": self.temperature,
            "top_logprobs": self.top_logprobs,
            "top_p": self.top_p,
            "user": self.user,
            "extra_headers": self.extra_headers,
            "extra_query": self.extra_query,
        }

    def cerebras_request_fields(self) -> dict[str, Any]:
        """Return the request fields the ``cerebras`` dialect sends from these settings, unset ones as None."""
        return {
            "max_completion_tokens": self.max_completion_tokens,
            "repetition_penalty": self.repetition_penalty,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "top_k": self.top_k,
            "extra_headers": self.extra_headers,
            "extra_query": self.extra_query,
            "extra_body": self.extra_body,
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


# Each route accepts the request fields its wire sends and the settings the wire or client reads otherwise. Agno
# OpenAIChat's ``modalities`` and ``audio`` (audio output) are retired.
CHAT_REQUEST_FIELDS = frozenset(
    {*ChatSettings().openai_request_fields(), "strict_output", "request_params", "role_map"},
)
CHAT_FIELDS = CHAT_REQUEST_FIELDS | CLIENT_FIELDS
GROQ_FIELDS = CLIENT_FIELDS - {"organization"} | {*ChatSettings().groq_request_fields(), "request_params"}
CEREBRAS_FIELDS = CLIENT_FIELDS - {"organization"} | {
    *ChatSettings().cerebras_request_fields(),
    "parallel_tool_calls",
    "strict_output",
    "request_params",
}
RESPONSES_FIELDS = CLIENT_FIELDS | {
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
CODEX_FIELDS = RESPONSES_FIELDS | {"codex_home", "prompt_cache_key", "session_id", "default_instructions"}
