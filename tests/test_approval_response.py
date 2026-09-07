"""Focused tests for response-side native approval coordination."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from agno.models.response import ToolExecution
from agno.run.agent import RunCompletedEvent, RunOutput, ToolCallCompletedEvent, ToolCallStartedEvent
from agno.run.base import RunStatus
from agno.run.requirement import RunRequirement

from mindroom.approval_execution import _collect_agent_continuation
from mindroom.approval_response import identify_approval_tools, require_ordered_pause_presentation
from mindroom.response_turn import PausedAttempt
from mindroom.tool_system.events import CollectedStreamPresentation, ToolTraceEntry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def test_identify_approval_tools_keeps_team_member_owner() -> None:
    """Approval policy uses the raw config identity behind Agno's provider member ID."""
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
            response_presentation_state={
                "kind": "team_stream",
                "version": 2,
                "members": [
                    {
                        "id": "researcher-a",
                        "config_name": "Researcher_A",
                        "display_name": "Researcher",
                        "content": "",
                    },
                ],
                "consensus": "",
            },
        ),
        default_agent_name="research-team",
    )

    assert identified == ((tool, "call-1", "dangerous", "Researcher_A"),)


def test_ordered_team_pause_rejects_a_coordinator_tool_in_a_member_scope() -> None:
    """A team-level approval requirement must be anchored in the coordinator slot."""
    tool = ToolExecution(
        tool_call_id="call-1",
        tool_name="dangerous",
        requires_confirmation=True,
    )
    paused = PausedAttempt(
        session_id="session-1",
        run_id="run-1",
        tools=(tool,),
        requirements=(RunRequirement(tool_execution=tool),),
        response_text="🔧 `dangerous` [1] ⏳",
        tool_trace=(
            ToolTraceEntry(
                type="tool_call_started",
                tool_name="dangerous",
                tool_call_id="call-1",
                scope_key="agent:wrong-member",
            ),
        ),
        response_presentation_state={
            "kind": "team_stream",
            "version": 2,
            "members": [],
            "consensus": "🔧 `dangerous` [1] ⏳",
        },
    )

    with pytest.raises(RuntimeError, match="ordered presentation"):
        require_ordered_pause_presentation(paused, show_tool_calls=True)


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


@pytest.mark.asyncio
async def test_agent_continuation_reuses_an_existing_visible_tool_separator() -> None:
    """A restored marker suffix must not become two blank paragraphs."""
    tool = ToolExecution(tool_call_id="call-1", tool_name="inspect", result="done")
    presentation = CollectedStreamPresentation(
        show_tool_calls=True,
        response_text="Before approval.\n\n🔧 `inspect` [1] ⏳\n\n",
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


@pytest.mark.asyncio
async def test_hidden_agent_continuation_separates_text_across_the_tool_boundary() -> None:
    """Hidden approval tools must not concatenate pre- and post-approval prose."""
    tool = ToolExecution(tool_call_id="call-1", tool_name="inspect", result="done")
    presentation = CollectedStreamPresentation(
        show_tool_calls=False,
        response_text="Before approval.",
        tool_trace=[
            ToolTraceEntry(
                type="tool_call_started",
                tool_name="inspect",
                tool_call_id="call-1",
            ),
        ],
        track_hidden_tools=True,
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

    assert presentation.final_text() == "Before approval.\n\nAfter approval."


@pytest.mark.asyncio
async def test_hidden_agent_continuation_separates_text_across_a_new_tool_boundary() -> None:
    """A hidden tool started after restoration must separate later prose."""
    tool = ToolExecution(tool_call_id="call-2", tool_name="inspect", result="done")
    presentation = CollectedStreamPresentation(
        show_tool_calls=False,
        response_text="Before tool.",
        track_hidden_tools=True,
    )
    terminal = RunOutput(
        run_id="run-1",
        session_id="session-1",
        status=RunStatus.completed,
        tools=[tool],
    )

    async def events() -> AsyncIterator[object]:
        yield ToolCallStartedEvent(
            tool=ToolExecution(tool_call_id="call-2", tool_name="inspect", tool_args={}),
        )
        yield ToolCallCompletedEvent(tool=tool)
        yield RunCompletedEvent(content="After tool.")
        yield terminal

    await _collect_agent_continuation(events(), presentation)

    assert presentation.final_text() == "Before tool.\n\nAfter tool."
