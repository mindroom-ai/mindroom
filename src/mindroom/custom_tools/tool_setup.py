"""Read-only guidance for tools requiring trusted requester setup."""

from __future__ import annotations

import json

from agno.tools import Toolkit

from mindroom.tool_system.runtime_availability import tool_setup_guidance


class ToolSetupTools(Toolkit):
    """Explain setup-gated tools without granting the model configuration authority."""

    def __init__(self, setup_required_tool_names: frozenset[str]) -> None:
        self._setup_required_tool_names = setup_required_tool_names
        super().__init__(name="tool_setup", tools=[self.tool_setup])

    def tool_setup(self, tool_name: str) -> str:
        """Return trusted setup guidance for one currently unavailable configured tool."""
        if tool_name not in self._setup_required_tool_names:
            return json.dumps(
                {
                    "status": "unknown",
                    "tool": "tool_setup",
                    "tool_name": tool_name,
                    "setup_required_tools": sorted(self._setup_required_tool_names),
                },
                sort_keys=True,
            )
        return json.dumps(
            {
                "status": "setup_required",
                "tool": "tool_setup",
                "tool_name": tool_name,
                "message": tool_setup_guidance(tool_name),
            },
            sort_keys=True,
        )


__all__ = ["ToolSetupTools"]
