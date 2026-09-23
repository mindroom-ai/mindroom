"""Ollama request policy for participation decisions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agno.models.ollama import Ollama

from mindroom.provider_tool_policy import provider_tools_disabled


@dataclass
class MindRoomOllama(Ollama):
    """Ollama model that keeps function calls outside participation checks."""

    def get_request_params(self, tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Omit tool declarations because Ollama has no tool_choice parameter."""
        request_params = super().get_request_params(tools=tools)
        if provider_tools_disabled():
            request_params.pop("tools", None)
        return request_params
