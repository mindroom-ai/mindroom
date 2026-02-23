"""Session orchestration toolkit implementation."""

from __future__ import annotations

from agno.tools import Toolkit

from mindroom.custom_tools.openclaw_compat import OpenClawCompatTools


class SessionOrchestrationTools(OpenClawCompatTools):
    """Session and subagent orchestration tools for any MindRoom agent."""

    def __init__(self) -> None:
        Toolkit.__init__(
            self,
            name="session_orchestration",
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
