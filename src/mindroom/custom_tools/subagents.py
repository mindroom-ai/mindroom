"""Sub-agents toolkit implementation."""

from __future__ import annotations

from agno.tools import Toolkit

from mindroom.custom_tools.openclaw_compat import OpenClawCompatTools


class SubAgentsTools(OpenClawCompatTools):
    """Session and sub-agent orchestration tools for any MindRoom agent."""

    def __init__(self) -> None:
        Toolkit.__init__(
            self,
            name="subagents",
            tools=[
                self.agents_list,
                self.session_status,
                self.sessions_list,
                self.sessions_history,
                self.sessions_send,
                self.sessions_spawn,
                self.subagents,
            ],
        )
