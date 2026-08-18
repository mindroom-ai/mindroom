"""Focused tests for response-side native approval coordination."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from agno.models.response import ToolExecution
from agno.run.agent import RunCompletedEvent, RunOutput
from agno.run.base import RunStatus
from agno.run.requirement import RunRequirement

from mindroom.approval_execution import _collect_agent_continuation
from mindroom.approval_response import identify_approval_tools
from mindroom.response_turn import PausedAttempt
from mindroom.tool_system.events import CollectedStreamPresentation, ToolTraceEntry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def test_identify_approval_tools_keeps_team_member_owner() -> None:
    """Approval policy and cards must use stable identity, not a duplicate display label."""
    tool = ToolExecution(
        tool_call_id="call-1",
        tool_name="dangerous",
        requires_confirmation=True,
    )
    requirement = RunRequirement(tool_execution=tool)
    requirement.member_agent_id = "researcher-a"
    requirement.member_agent_name = "Researcher"

    identified = identify_approval_tools(
        PausedAttempt(
            session_id="session-1",
            run_id="run-1",
            tools=(tool,),
            requirements=(requirement,),
        ),
        default_agent_name="research-team",
    )

    assert identified == ((tool, "call-1", "dangerous", "researcher-a"),)


@pytest.mark.asyncio
async def test_agent_continuation_appends_terminal_only_content() -> None:
    """A provider completion event is the continuation delta when no content events were emitted."""
    presentation = CollectedStreamPresentation(show_tool_calls=True, response_text="Before approval. ")
    terminal = RunOutput(run_id="run-1", session_id="session-1", status=RunStatus.completed)

    async def events() -> AsyncIterator[object]:
        yield RunCompletedEvent(content="After approval.")
        yield terminal

    response = await _collect_agent_continuation(events(), presentation)

    assert response is terminal
    assert presentation.final_text() == "Before approval. After approval."


@pytest.mark.asyncio
async def test_agent_chained_pause_anchors_a_terminal_only_pending_tool() -> None:
    """A paused final output supplies the pending anchor when Agno emitted no tool-start event."""
    tool = ToolExecution(
        tool_call_id="call-2",
        tool_name="publish_report",
        tool_args={},
        requires_confirmation=True,
    )
    presentation = CollectedStreamPresentation(show_tool_calls=True, response_text="Before approval.")
    terminal = RunOutput(
        run_id="run-2",
        session_id="session-1",
        status=RunStatus.paused,
        tools=[tool],
    )

    async def events() -> AsyncIterator[object]:
        yield terminal

    await _collect_agent_continuation(events(), presentation)

    assert presentation.response_text.endswith("🔧 `publish_report` [1] ⏳\n\n")
    assert len(presentation.tool_trace) == 1
    assert presentation.tool_trace[0].type == "tool_call_started"
    assert presentation.tool_trace[0].tool_call_id == "call-2"


@pytest.mark.asyncio
async def test_agent_continuation_keeps_text_after_a_stripped_tool_marker() -> None:
    """Continuation content without leading whitespace must not join the marker line."""
    tool = ToolExecution(
        tool_call_id="call-1",
        tool_name="inspect",
        tool_args={},
        result="done",
    )
    presentation = CollectedStreamPresentation(
        show_tool_calls=True,
        response_text="Before approval.\n\n🔧 `inspect` [1] ⏳",
        tool_trace=[
            ToolTraceEntry(
                type="tool_call_started",
                tool_name="inspect",
                tool_call_id="call-1",
            ),
        ],
    )
    terminal = RunOutput(
        run_id="run-1",
        session_id="session-1",
        status=RunStatus.completed,
        tools=[tool],
    )

    async def events() -> AsyncIterator[object]:
        yield RunCompletedEvent(content="After approval.")
        yield terminal

    await _collect_agent_continuation(events(), presentation)

    assert presentation.final_text() == "Before approval.\n\n🔧 `inspect` [1]\n\nAfter approval."
