"""Groq request policy for decisions that cannot execute provider-managed tools, and Groq usage counters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agno.models.groq import Groq

from mindroom.provider_tool_policy import disable_tool_selection, provider_tools_disabled

if TYPE_CHECKING:
    from agno.metrics import MessageMetrics
    from groq.types import CompletionUsage
    from pydantic import BaseModel

_COMPOUND_MODELS = frozenset({"groq/compound", "groq/compound-mini", "compound-beta", "compound-beta-mini"})


@dataclass
class MindRoomGroq(Groq):
    """Groq model that keeps native execution outside participation decisions."""

    def get_request_params(
        self,
        response_format: dict[str, Any] | type[BaseModel] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Reject automatic Compound tools and disable all declared tool selection."""
        request_params = super().get_request_params(
            response_format=response_format,
            tools=tools,
            tool_choice=tool_choice,
        )
        if not provider_tools_disabled():
            return request_params
        extra_body = request_params.get("extra_body")
        sources = [request_params, extra_body] if isinstance(extra_body, dict) else [request_params]
        model_ids = [self.id, *(str(source.get("model", "")) for source in sources)]
        if any(model_id.casefold() in _COMPOUND_MODELS for model_id in model_ids) or any(
            source.get("compound_custom") is not None for source in sources
        ):
            # Compound enables hosted tools by default; tool_choice controls local calls.
            msg = "Participation decisions cannot disable native Groq Compound tools"
            raise ValueError(msg)
        return disable_tool_selection(request_params)

    # AGNO_COMPAT: Groq usage drops cached input tokens.
    # Reason: Agno copies only prompt and completion totals, although Groq reports
    # prompt_tokens_details.cached_tokens inside the prompt total.
    # Upstream issue: Tracking gap; no issue identified.
    # Upstream PR: None identified.
    # Remove when: Agno's Groq metrics report cached input as cache reads.
    # Coverage: tests/test_provider_usage_metrics.py::test_groq_reports_cached_input_and_reasoning.
    # AGNO_COMPAT: Groq usage drops reasoning tokens.
    # Reason: Agno ignores completion_tokens_details.reasoning_tokens, which Groq reports inside the
    # completion total.
    # Upstream issue: Tracking gap; no issue identified.
    # Upstream PR: None identified.
    # Remove when: Agno's Groq metrics report reasoning tokens.
    # Coverage: tests/test_provider_usage_metrics.py::test_groq_reports_cached_input_and_reasoning.
    def _get_metrics(self, response_usage: CompletionUsage) -> MessageMetrics:
        metrics = super()._get_metrics(response_usage)
        if response_usage.prompt_tokens_details is not None:
            metrics.cache_read_tokens = response_usage.prompt_tokens_details.cached_tokens
        if response_usage.completion_tokens_details is not None:
            metrics.reasoning_tokens = response_usage.completion_tokens_details.reasoning_tokens
        return metrics
