"""Focused tests for response-side native approval coordination."""

from __future__ import annotations

from agno.models.response import ToolExecution
from agno.run.requirement import RunRequirement

from mindroom.approval_response import identify_approval_tools
from mindroom.response_turn import PausedAttempt


def test_identify_approval_tools_keeps_team_member_owner() -> None:
    """Approval policy and cards must use the member that invoked the exact call."""
    tool = ToolExecution(
        tool_call_id="call-1",
        tool_name="dangerous",
        requires_confirmation=True,
    )
    requirement = RunRequirement(tool_execution=tool)
    requirement.member_agent_name = "researcher"

    identified = identify_approval_tools(
        PausedAttempt(
            session_id="session-1",
            run_id="run-1",
            tools=(tool,),
            requirements=(requirement,),
        ),
        default_agent_name="research-team",
    )

    assert identified == ((tool, "call-1", "dangerous", "researcher"),)
