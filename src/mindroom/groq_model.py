"""Groq request policy for decisions that cannot execute provider-managed tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agno.models.groq import Groq

from mindroom.provider_tool_policy import provider_tools_disabled

if TYPE_CHECKING:
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
        """Reject automatic Compound tools and disable explicitly listed native tools."""
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
        effective_tools = (
            extra_body.get("tools", request_params.get("tools"))
            if isinstance(extra_body, dict)
            else request_params.get("tools")
        )
        if any(not isinstance(tool, dict) or tool.get("type") != "function" for tool in effective_tools or []):
            request_params["tool_choice"] = "none"
            if isinstance(extra_body, dict) and "tool_choice" in extra_body:
                request_params["extra_body"] = {**extra_body, "tool_choice": "none"}
        return request_params
