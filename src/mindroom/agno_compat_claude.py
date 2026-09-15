"""Temporary request and response compatibility for Agno Claude adapters."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mindroom.model_defaults import CLAUDE_PROVIDER_DEFAULT_SAMPLING_MODEL_SUFFIXES

if TYPE_CHECKING:
    from agno.models.response import ModelResponse
    from anthropic.types import Message as AnthropicMessage
    from anthropic.types.beta import BetaMessage

_SAMPLING_CONTROL_NAMES = ("temperature", "top_p", "top_k")

# Reason: Agno 3.0.9 moves sampling controls into extra_body even for current
# Claude generations that reject those controls in supported request modes.
# Upstream issue: https://github.com/agno-agi/agno/issues/9931
# Upstream PR: https://github.com/agno-agi/agno/pull/9933
# Remove when: The pinned Agno release removes model fields and raw request
# overrides from both top-level params and extra_body for the same generations.
# Coverage: tests/test_claude_compat.py::test_default_sampling_models_lose_sampling_controls_everywhere;
# tests/test_claude_compat.py::test_other_claude_models_keep_sampling_controls_in_extra_body.

# Reason: Agno 3.0.9 does not expose Claude's terminal stop_reason in parsed
# provider_data, which prevents consumers from detecting provider-capped output.
# Upstream issue: No matching issue identified; this metadata extension point is untracked.
# Upstream PR: None identified.
# Remove when: The pinned Agno parser exposes stop_reason through provider_data or
# another stable terminal-metadata interface.
# Coverage: tests/test_compaction_summary_provider_compat.py::test_summary_uses_stop_reason_and_raw_body_precedence.


class ClaudeProviderSDKCompat:
    """Sanitize Agno-built requests and preserve terminal metadata."""

    id: str

    def get_request_params(
        self,
        response_format: dict[str, Any] | type[Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Remove unsupported sampling controls after Agno merges request parameters."""
        request_params = super().get_request_params(  # ty: ignore[unresolved-attribute]
            response_format=response_format,
            tools=tools,
        )
        if self.id.casefold().endswith(CLAUDE_PROVIDER_DEFAULT_SAMPLING_MODEL_SUFFIXES):
            extra_body = request_params.get("extra_body")
            for parameter_name in _SAMPLING_CONTROL_NAMES:
                request_params.pop(parameter_name, None)
                if isinstance(extra_body, dict):
                    extra_body.pop(parameter_name, None)
            if isinstance(extra_body, dict) and not extra_body:
                del request_params["extra_body"]
        return request_params

    def _parse_provider_response(
        self,
        response: AnthropicMessage | BetaMessage,
        response_format: dict[str, Any] | type[Any] | None = None,
        **kwargs: object,
    ) -> ModelResponse:
        parsed = super()._parse_provider_response(  # ty: ignore[unresolved-attribute]
            response,
            response_format=response_format,
            **kwargs,
        )
        parsed.provider_data = {**(parsed.provider_data or {}), "stop_reason": response.stop_reason}
        return parsed
