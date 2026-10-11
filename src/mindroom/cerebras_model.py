"""Cerebras request policy for participation decisions, and Cerebras usage counters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agno.models.cerebras import Cerebras

from mindroom.provider_tool_policy import disable_tool_selection

if TYPE_CHECKING:
    from agno.metrics import MessageMetrics
    from cerebras.cloud.sdk.types.chat.chat_completion import ChatChunkResponseUsage, ChatCompletionResponseUsage
    from pydantic import BaseModel


@dataclass
class MindRoomCerebras(Cerebras):
    """Cerebras model that prevents tool selection during participation checks."""

    def get_request_params(
        self,
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | type[BaseModel] | None = None,
        **kwargs: object,
    ) -> dict[str, Any]:
        """Enforce the policy after Agno merges authored request overrides."""
        return disable_tool_selection(
            super().get_request_params(tools=tools, response_format=response_format, **kwargs),
        )

    # AGNO_COMPAT: Cerebras usage drops cached input tokens.
    # Reason: Agno copies only prompt and completion totals, although Cerebras reports
    # prompt_tokens_details.cached_tokens inside the prompt total.
    # Cerebras also reports completion_tokens_details.reasoning_tokens, but the pinned SDK does not declare
    # that field, so Cerebras reasoning stays unmapped.
    # Upstream issue: Tracking gap; no issue identified.
    # Upstream PR: None identified.
    # Remove when: Agno's Cerebras metrics report cached input as cache reads.
    # Coverage: tests/test_provider_usage_metrics.py::test_cerebras_reports_cached_input.
    def _get_metrics(self, response_usage: ChatCompletionResponseUsage | ChatChunkResponseUsage) -> MessageMetrics:
        metrics = super()._get_metrics(response_usage)
        if response_usage.prompt_tokens_details is not None:
            metrics.cache_read_tokens = response_usage.prompt_tokens_details.cached_tokens or 0
        return metrics
