"""Authored settings of one Claude route, parsed from ``ModelConfig.extra_kwargs``.

The accepted keys keep the names existing configs use; a key the route does not accept fails with the accepted
list at config load and again when the model is built.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from mindroom.providers.extra_kwargs import SECRET, reject_unsupported_extra_kwargs, settings_from_extra_kwargs

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from boto3.session import Session

type ClaudeRouteKind = Literal["anthropic", "vertexai_claude", "bedrock_claude"]

# The runtime env variable that sets a route's endpoint when its config sets none.
ENDPOINT_VARIABLES: dict[ClaudeRouteKind, str] = {
    "anthropic": "ANTHROPIC_BASE_URL",
    "vertexai_claude": "ANTHROPIC_VERTEX_BASE_URL",
}

# The output cap of a reply whose model config sets no ``max_tokens``. The direct API counts only the output a reply
# produces against its rate limit (https://platform.claude.com/docs/en/api/rate-limits), so a model there may write
# its maximum. Bedrock deducts the input plus ``max_tokens`` from its quota when a request starts
# (https://docs.aws.amazon.com/bedrock/latest/userguide/quotas-token-burndown.html), and Vertex AI estimates each
# request's tokens and refunds the unused ones after it
# (https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/partner-models/claude/quotas), so a large cap
# there throttles requests; those routes, and a model the direct route does not list, keep this cap.
_RESERVED_QUOTA_MAX_TOKENS = 8192
# Each Claude model's maximum output on the synchronous Messages API, by the start of its direct API id, from
# https://platform.claude.com/docs/en/about-claude/models/overview and each listed model's page (checked 2026-10-29).
_MAX_OUTPUT_TOKENS: dict[str, int] = {
    "claude-fable-5": 128_000,
    "claude-mythos-5": 128_000,
    "claude-opus-5": 128_000,
    "claude-sonnet-5": 128_000,
    "claude-haiku-5": 128_000,
    "claude-opus-4-8": 128_000,
    "claude-opus-4-7": 128_000,
    "claude-opus-4-6": 128_000,
    "claude-sonnet-4-6": 128_000,
    "claude-opus-4-5": 64_000,
    "claude-haiku-4-5": 64_000,
}

_COMMON_KEYS = frozenset(
    {
        "api_key",
        "betas",
        "cache_system_prompt",
        "client_params",
        "context_management",
        "default_headers",
        "extended_cache_time",
        "max_tokens",
        "output_config",
        "request_params",
        "stop_sequences",
        "temperature",
        "thinking",
        "timeout",
        "top_k",
        "top_p",
    },
)
# The ``extra_kwargs`` keys each route's config may set. Bedrock's ``aws_profile`` names the AWS profile its boto3
# ``session`` resolves, so it is no setting of its own.
ACCEPTED_KEYS: dict[ClaudeRouteKind, frozenset[str]] = {
    "anthropic": _COMMON_KEYS | {"auth_token"},
    "vertexai_claude": _COMMON_KEYS | {"base_url", "project_id", "region"},
    "bedrock_claude": _COMMON_KEYS
    | {"aws_access_key", "aws_profile", "aws_region", "aws_secret_key", "aws_session_token", "session"},
}


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaudeSettings:
    """What one Claude model's config authored, with MindRoom's defaults where it authored nothing.

    ``request_params`` are raw request overrides merged last, so a ``thinking`` or ``context_management``
    value there (or inside its ``extra_body``) wins over the top-level field.
    """

    # ``parse_claude_settings`` fills the route's default (``_default_max_tokens``) unless the config sets one.
    max_tokens: int | None
    thinking: Mapping[str, Any] | None = None
    output_config: Mapping[str, Any] | None = None
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    stop_sequences: Sequence[str] | None = None
    cache_system_prompt: bool = True
    extended_cache_time: bool = True
    betas: Sequence[str] | None = None
    context_management: Mapping[str, Any] | None = None
    request_params: Mapping[str, Any] | None = None
    timeout: float | None = None
    default_headers: Mapping[str, str] | None = field(default=None, metadata=SECRET)
    client_params: Mapping[str, Any] | None = field(default=None, metadata=SECRET)
    # Direct API credentials; Vertex and Bedrock authenticate with Google and AWS credentials and ignore a key.
    api_key: str | None = field(default=None, metadata=SECRET)
    auth_token: str | None = field(default=None, metadata=SECRET)
    # Vertex AI.
    project_id: str | None = None
    region: str | None = None
    base_url: str | None = None
    # Bedrock; ``session`` resolves rotating credentials before every request.
    aws_access_key: str | None = field(default=None, metadata=SECRET)
    aws_secret_key: str | None = field(default=None, metadata=SECRET)
    aws_session_token: str | None = field(default=None, metadata=SECRET)
    aws_region: str | None = None
    session: Session | None = field(default=None, metadata=SECRET)

    def effective_thinking(self) -> Mapping[str, Any] | None:
        """Return the thinking setting after the raw request overrides."""
        return self._after_request_params("thinking", self.thinking)

    def authors_thinking(self) -> bool:
        """Return whether the settings send a thinking setting other than ``disabled``, which rules out prefill.

        A model that thinks by default with no setting sent, such as every Claude 5 model, gets False here; none of
        those accepts prefill either.
        """
        thinking = self.effective_thinking()
        return isinstance(thinking, dict) and thinking.get("type") != "disabled"

    def effective_context_management(self) -> Mapping[str, Any] | None:
        """Return the context-management setting after the raw request overrides."""
        return self._after_request_params("context_management", self.context_management)

    def _after_request_params(self, name: str, authored: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
        """Return one body field as the SDK merges the body: ``extra_body`` over ``request_params`` over the field."""
        request_params = self.request_params or {}
        return (request_params.get("extra_body") or {}).get(name, request_params.get(name, authored))


def _default_max_tokens(route: ClaudeRouteKind, model_id: str) -> int:
    """Return the output cap of a reply whose model config sets no ``max_tokens``."""
    if route == "anthropic":
        for prefix, max_tokens in _MAX_OUTPUT_TOKENS.items():
            if model_id.startswith(prefix):
                return max_tokens
    return _RESERVED_QUOTA_MAX_TOKENS


def parse_claude_settings(
    route: ClaudeRouteKind,
    model_id: str,
    extra_kwargs: Mapping[str, Any] | None,
) -> ClaudeSettings:
    """Parse one route's resolved ``extra_kwargs``, rejecting keys the route does not accept.

    Bedrock resolves ``aws_profile`` into ``session`` first, so the profile reaches no settings field. An authored
    ``max_tokens`` replaces the route's default for ``model_id``.
    """
    reject_unsupported_extra_kwargs(extra_kwargs, ACCEPTED_KEYS[route], provider=route)
    authored = {"max_tokens": _default_max_tokens(route, model_id), **(extra_kwargs or {})}
    return settings_from_extra_kwargs(ClaudeSettings, authored, provider=route)


__all__ = [
    "ACCEPTED_KEYS",
    "ENDPOINT_VARIABLES",
    "ClaudeRouteKind",
    "ClaudeSettings",
    "parse_claude_settings",
]
