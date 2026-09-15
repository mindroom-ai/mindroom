"""Cerebras request policy for participation decisions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agno.models.cerebras import Cerebras

from mindroom.provider_tool_policy import disable_tool_selection

if TYPE_CHECKING:
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
