"""Authored settings of one Claude route, parsed from ``ModelConfig.extra_kwargs`` under Agno's field names.

``extra_kwargs`` was documented as the fields of the Agno model class, so the accepted keys keep those
names; a key the route does not accept fails at model construction with the accepted list.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from mindroom.providers.extra_kwargs import SECRET, settings_from_extra_kwargs

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from boto3.session import Session

type ClaudeRouteKind = Literal["anthropic", "vertexai_claude", "bedrock_claude"]

# The runtime env variable that sets a route's endpoint when its config sets none; the anthropic SDK alone reads
# ``ANTHROPIC_BASE_URL``.
ENDPOINT_VARIABLES: dict[ClaudeRouteKind, str] = {"vertexai_claude": "ANTHROPIC_VERTEX_BASE_URL"}

# Agno's Claude default; a reply that reaches it is answered as cut off instead of run (the loop's policy).
_DEFAULT_MAX_TOKENS = 8192
# The anthropic SDK rejects non-streaming requests whose max_tokens project past ten minutes unless the
# client has an explicit timeout; 3600 s is the SDK's own ceiling for non-streaming operations.
_DEFAULT_TIMEOUT_SECONDS = 3600.0

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
ACCEPTED_KEYS: dict[ClaudeRouteKind, frozenset[str]] = {
    "anthropic": _COMMON_KEYS | {"auth_token"},
    "vertexai_claude": _COMMON_KEYS | {"base_url", "project_id", "region"},
    "bedrock_claude": _COMMON_KEYS
    | {"aws_access_key", "aws_profile", "aws_region", "aws_secret_key", "aws_session_token", "session"},
}


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaudeSettings:
    """What one Claude model's config authored, with MindRoom's defaults where it authored nothing.

    ``request_params`` are raw request overrides merged last, as Agno merged them, so a ``thinking`` or
    ``context_management`` value there (or inside its ``extra_body``) wins over the top-level field.
    """

    max_tokens: int | None = _DEFAULT_MAX_TOKENS
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
    timeout: float | None = _DEFAULT_TIMEOUT_SECONDS
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
    aws_profile: str | None = None
    session: Session | None = field(default=None, metadata=SECRET)

    def effective_thinking(self) -> Mapping[str, Any] | None:
        """Return the thinking setting after the raw request overrides."""
        return self._after_request_params("thinking", self.thinking)

    def thinking_enabled(self) -> bool:
        """Return whether requests run with thinking on, so prefill is rejected and a trailing user turn is needed."""
        thinking = self.effective_thinking()
        return isinstance(thinking, dict) and thinking.get("type") != "disabled"

    def effective_context_management(self) -> Mapping[str, Any] | None:
        """Return the context-management setting after the raw request overrides."""
        return self._after_request_params("context_management", self.context_management)

    def _after_request_params(self, name: str, authored: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
        """Return one body field as the SDK merges the body: ``extra_body`` over ``request_params`` over the field."""
        request_params = self.request_params or {}
        return (request_params.get("extra_body") or {}).get(name, request_params.get(name, authored))


def parse_claude_settings(route: ClaudeRouteKind, extra_kwargs: Mapping[str, Any] | None) -> ClaudeSettings:
    """Parse authored ``extra_kwargs`` for one route, rejecting keys the route does not accept."""
    return settings_from_extra_kwargs(ClaudeSettings, extra_kwargs, provider=route, accepted=ACCEPTED_KEYS[route])


__all__ = [
    "ACCEPTED_KEYS",
    "ENDPOINT_VARIABLES",
    "ClaudeRouteKind",
    "ClaudeSettings",
    "parse_claude_settings",
]
