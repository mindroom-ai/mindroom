"""Read-only model guidance for requester-controlled Desktop pairing."""

from __future__ import annotations

from agno.tools import Toolkit

from mindroom.commands.desktop_commands import DesktopCommandScope, handle_desktop_command
from mindroom.tool_system.runtime_context import get_tool_runtime_context


class DesktopSetupTools(Toolkit):
    """Explain Desktop setup without granting the model registration authority."""

    def __init__(self) -> None:
        super().__init__(name="desktop_setup", tools=[self.desktop_setup])

    def desktop_setup(self, action: str = "instructions") -> str:
        """Return Desktop setup instructions or current requester-scoped status.

        Use action="status" to check readiness.
        Use action="instructions" to tell the requester how to begin setup.
        This tool cannot start, confirm, rotate, or disconnect a Desktop pairing.
        """
        context = get_tool_runtime_context()
        if context is None:
            return "Desktop setup requires a live Matrix chat."
        if action == "instructions":
            return "Ask the requester to send `!desktop setup` directly in this chat."
        if action != "status":
            return "Supported actions: instructions, status."
        return handle_desktop_command(
            "status",
            scope=DesktopCommandScope(
                config=context.config,
                runtime_paths=context.runtime_paths,
                agent_name=context.agent_name,
                requester_id=context.requester_id,
                room_id=context.room_id,
                thread_id=context.thread_id,
            ),
        )


__all__ = ["DesktopSetupTools"]
