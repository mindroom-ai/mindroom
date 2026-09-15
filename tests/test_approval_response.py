"""Focused tests for response-side native approval coordination."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from typing import TYPE_CHECKING

import pytest
from agno.agent import Agent
from agno.models.response import ToolExecution
from agno.run import RunContext
from agno.run.agent import RunCompletedEvent, RunOutput, ToolCallCompletedEvent, ToolCallStartedEvent
from agno.run.base import RunStatus
from agno.run.requirement import RunRequirement
from agno.session.agent import AgentSession

from mindroom.approval_execution import _collect_agent_continuation
from mindroom.approval_response import identify_approval_tools, require_ordered_pause_presentation
from mindroom.approval_tools import approval_denial_context
from mindroom.event_journal import ApprovalCall
from mindroom.response_turn import PausedAttempt
from mindroom.synthetic_model import SyntheticModel
from mindroom.tool_system.events import CollectedStreamPresentation, ToolTraceEntry
from tests.conftest import unwrap_extracted_collaborator
from tests.response_runner_helpers import _bot

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


@pytest.mark.parametrize("member_agent_id", ["researcher-a", "Researcher_A"])
def test_identify_approval_tools_keeps_team_member_owner(member_agent_id: str) -> None:
    """Approval policy uses the raw config identity behind Agno's provider member ID."""
    tool = ToolExecution(
        tool_call_id="call-1",
        tool_name="dangerous",
        requires_confirmation=True,
    )
    requirement = RunRequirement(tool_execution=tool)
    requirement.member_agent_id = member_agent_id
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
            toolkit_owners={("general", "dangerous"): "test_toolkit"},
        ),
        default_agent_name="research-team",
    )

    assert identified == ((tool, "call-1", "dangerous", "Researcher_A"),)


@pytest.fixture
def member_pause() -> PausedAttempt:
    """Create a visible member approval with frozen config and presentation identities."""
    tool = ToolExecution(tool_call_id="call-1", tool_name="dangerous", requires_confirmation=True)
    requirement = RunRequirement(tool_execution=tool)
    requirement.member_agent_id = "test_agent"
    requirement.member_agent_name = "Test Agent"
    return PausedAttempt(
        session_id="session-1",
        run_id="run-1",
        tools=(tool,),
        requirements=(requirement,),
        response_text="🔧 `dangerous` [1] ⏳",
        tool_trace=(
            ToolTraceEntry(
                type="tool_call_started",
                tool_name="dangerous",
                tool_call_id="call-1",
                scope_key="agent:test-agent",
            ),
        ),
        response_presentation_state={
            "kind": "team_stream",
            "version": 2,
            "members": [
                {
                    "id": "test-agent",
                    "config_name": "test_agent",
                    "display_name": "Test Agent",
                    "content": "🔧 `dangerous` [1] ⏳",
                },
                {"id": "other", "config_name": "other", "display_name": "Other", "content": ""},
            ],
            "consensus": "",
        },
        toolkit_owners={("general", "dangerous"): "test_toolkit"},
    )


@pytest.mark.parametrize("member_agent_id", ["test_agent", "test-agent"])
def test_ordered_team_pause_accepts_frozen_member_aliases(member_pause: PausedAttempt, member_agent_id: str) -> None:
    """Raw config and frozen IDs must select the same visible tool anchor."""
    member_pause.requirements[0].member_agent_id = member_agent_id

    require_ordered_pause_presentation(member_pause, show_tool_calls=True)


@pytest.mark.parametrize("member_agent_id", ["unknown", "Test Agent", "TEST_AGENT", "other"])
def test_ordered_team_pause_rejects_wrong_member_owner(member_pause: PausedAttempt, member_agent_id: str) -> None:
    """Similar names, missing members, and another valid member cannot own this anchor."""
    member_pause.requirements[0].member_agent_id = member_agent_id

    with pytest.raises(RuntimeError):
        require_ordered_pause_presentation(member_pause, show_tool_calls=True)


@pytest.mark.parametrize("member_agent_id", ["unknown", "Test Agent", "TEST_AGENT"])
def test_identify_approval_tools_rejects_unknown_member_owner(
    member_pause: PausedAttempt,
    member_agent_id: str,
) -> None:
    """Ownership requires an exact frozen identity even when the display name matches."""
    member_pause.requirements[0].member_agent_id = member_agent_id

    with pytest.raises(RuntimeError, match="frozen member"):
        identify_approval_tools(member_pause, default_agent_name="test-team")


def test_identify_approval_tools_keeps_simple_member_owner(member_pause: PausedAttempt) -> None:
    """A member whose raw and frozen IDs coincide keeps its own approval policy."""
    member_pause.requirements[0].member_agent_id = "other"

    identified = identify_approval_tools(member_pause, default_agent_name="test-team")

    assert identified[0][1:] == ("call-1", "dangerous", "other")


@pytest.mark.parametrize("member_agent_id", ["test_agent", "test-agent"])
def test_team_pause_rejects_ambiguous_frozen_member_identity(
    member_pause: PausedAttempt,
    member_agent_id: str,
) -> None:
    """Conflicting aliases must never silently select another member's approval policy."""
    member_pause.requirements[0].member_agent_id = member_agent_id
    member_pause.response_presentation_state["members"] = [
        {"id": "test-agent", "config_name": "test_agent"},
        {"id": "different", "config_name": "test-agent"},
    ]

    with pytest.raises(RuntimeError):
        identify_approval_tools(member_pause, default_agent_name="test-team")
    with pytest.raises(RuntimeError):
        require_ordered_pause_presentation(member_pause, show_tool_calls=True)


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
        toolkit_owners={("general", "dangerous"): "test_toolkit"},
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


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [None, "calculator"])
async def test_pause_plan_requires_exact_live_toolkit_origin(tmp_path: Path, owner: str | None) -> None:
    """A new card cannot defer discovering its toolkit owner until approval time."""
    coordinator = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)._approval_responses
    tool = ToolExecution(tool_call_id="call-1", tool_name="add", requires_confirmation=True)
    origins = {("general", "add"): owner, ("other", "add"): "calculator"}
    if owner is None:
        with pytest.raises(RuntimeError, match="toolkit origin"):
            await coordinator.plan_pause(
                ((tool, "call-1", "add", "general"),),
                requester_id="@user:localhost",
                toolkit_owners=origins,
            )
    else:
        plan = await coordinator.plan_pause(
            ((tool, "call-1", "add", "general"),),
            requester_id="@user:localhost",
            toolkit_owners=origins,
        )
        assert plan.calls[0].toolkit_name == "calculator"


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_error", [None, RuntimeError, asyncio.CancelledError])
async def test_denial_context_matches_run_identity_and_restores_lookup(exit_error: type[BaseException] | None) -> None:
    """Exact denials survive retries but cannot affect other runs or outlive the continuation."""
    actor = Agent(id="general", model=SyntheticModel(id="synthetic"), tools=[], telemetry=False)
    call = ApprovalCall(tool_call_id="reused-id", tool_name="missing", invoking_agent="general", expires_at_ns=2**62)

    async def lookup_twice(run_id: str) -> tuple[RunOutput, ToolExecution]:
        tool = ToolExecution(
            tool_call_id="reused-id",
            tool_name="missing",
            confirmed=False,
            requires_confirmation=True,
            confirmation_note="Declined by requester",
        )
        run = RunOutput(run_id=run_id, session_id="session", messages=[], tools=[tool])
        for _ in range(2):
            await actor.aget_tools(
                run_response=run,
                run_context=RunContext(run_id=run_id, session_id="session"),
                session=AgentSession(session_id="session"),
            )
        return run, tool

    with (
        pytest.raises(exit_error) if exit_error is not None else nullcontext(),
        approval_denial_context(actor, {"continued-run": (call,)}),
    ):
        for run_id in ("earlier-run", "continued-run", "later-run", "continued-run"):
            run, tool = await lookup_twice(run_id)
            if run_id == "continued-run":
                assert len(run.messages or []) == 1
                assert run.messages[0].tool_call_error is True
                assert tool.requires_confirmation is False
            else:
                assert run.messages == []
                assert tool.requires_confirmation is True
        if exit_error is not None:
            raise exit_error

    run, tool = await lookup_twice("continued-run")
    assert run.messages == []
    assert tool.requires_confirmation is True
