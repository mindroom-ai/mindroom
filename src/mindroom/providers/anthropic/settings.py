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

# TODO(simplify): a Claude reply whose model config sets no ``max_tokens`` stops at this fixed cap, and a reply that
# reaches it is answered as cut off instead of run (the loop's policy). Default to the model's maximum or a documented
# per-model default (#3113). Users would notice longer Claude replies.
_DEFAULT_MAX_TOKENS = 8192
# The anthropic SDK rejects non-streaming requests whose max_tokens project past ten minutes unless the
# client has an explicit timeout; 3600 s is the SDK's own ceiling for non-streaming operations.
# TODO(simplify): every request streams, so the SDK guard this timeout satisfies no longer applies. Drop it for the
# SDK's default timeout. Operators would notice the shorter default (#3119).
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


def parse_claude_settings(route: ClaudeRouteKind, extra_kwargs: Mapping[str, Any] | None) -> ClaudeSettings:
    """Parse one route's resolved ``extra_kwargs``, rejecting keys the route does not accept.

    Bedrock resolves ``aws_profile`` into ``session`` first, so the profile reaches no settings field.
    """
    reject_unsupported_extra_kwargs(extra_kwargs, ACCEPTED_KEYS[route], provider=route)
    return settings_from_extra_kwargs(ClaudeSettings, extra_kwargs, provider=route)


__all__ = [
    "ACCEPTED_KEYS",
    "ENDPOINT_VARIABLES",
    "ClaudeRouteKind",
    "ClaudeSettings",
    "parse_claude_settings",
]
