"""Focused tests for response-side native approval coordination, on owned records and events."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from mindroom.approval.execution import _collect_agent_continuation
from mindroom.approval.response import (
    continuation_response_kind,
    identify_approval_tools,
    plan_approval_calls,
    require_ordered_pause_presentation,
)
from mindroom.llm.events import ReportedCall, RunFinished, TextDelta, ToolCompleted, ToolProgress, ToolStarted
from mindroom.llm.runs import RunRecord, RunRequirement, RunStatus, ToolExecution
from mindroom.response_turn import PausedAttempt
from mindroom.tool_system.events import CollectedStreamPresentation, StructuredStreamChunk, ToolTraceEntry
from tests.approval_continuation_helpers import approval_continuation
from tests.conftest import unwrap_extracted_collaborator
from tests.response_runner_helpers import _bot

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.llm.events import RunEvent


def test_identify_approval_tools_keeps_team_member_owner() -> None:
    """Approval policy uses the member's config name, the id its slot is keyed by."""
    tool = ToolExecution(
        tool_call_id="call-1",
        tool_name="dangerous",
        requires_confirmation=True,
    )
    requirement = RunRequirement(tool_execution=tool, member_agent_id="Researcher_A", member_display_name="Researcher")

    identified = identify_approval_tools(
        PausedAttempt(
            session_id="session-1",
            run_id="run-1",
            requirements=(requirement,),
            response_presentation_state={
                "kind": "team_stream",
                "version": 3,
                "members": [{"id": "Researcher_A", "display_name": "Researcher", "content": ""}],
                "consensus": "",
            },
            toolkit_owners={("general", "dangerous"): "test_toolkit"},
        ),
        default_agent_name="research-team",
    )

    assert identified == ((tool, "Researcher_A"),)


@pytest.fixture
def member_pause() -> PausedAttempt:
    """Create a visible member approval with frozen config and presentation identities."""
    tool = ToolExecution(tool_call_id="call-1", tool_name="dangerous", requires_confirmation=True)
    requirement = RunRequirement(tool_execution=tool, member_agent_id="test_agent", member_display_name="Test Agent")
    return PausedAttempt(
        session_id="session-1",
        run_id="run-1",
        requirements=(requirement,),
        response_text="🔧 `dangerous` [1] ⏳",
        tool_trace=(
            ToolTraceEntry(
                type="tool_call_started",
                tool_name="dangerous",
                tool_call_id="call-1",
                member_id="test_agent",
            ),
        ),
        response_presentation_state={
            "kind": "team_stream",
            "version": 3,
            "members": [
                {"id": "test_agent", "display_name": "Test Agent", "content": "🔧 `dangerous` [1] ⏳"},
                {"id": "other", "display_name": "Other", "content": ""},
            ],
            "consensus": "",
        },
        toolkit_owners={("general", "dangerous"): "test_toolkit"},
    )


def test_ordered_team_pause_accepts_its_member_s_config_name(member_pause: PausedAttempt) -> None:
    """The member's config name selects the visible tool anchor in its slot."""
    require_ordered_pause_presentation(member_pause, show_tool_calls=True)


# "test-agent" is the url-safe id a version-2 snapshot keyed the member by; no member is named by it now.
@pytest.mark.parametrize("member_agent_id", ["unknown", "Test Agent", "TEST_AGENT", "test-agent", "other"])
def test_ordered_team_pause_rejects_wrong_member_owner(member_pause: PausedAttempt, member_agent_id: str) -> None:
    """Similar names, missing members, and another valid member cannot own this anchor."""
    member_pause = replace(
        member_pause,
        requirements=(replace(member_pause.requirements[0], member_agent_id=member_agent_id),),
    )

    with pytest.raises(RuntimeError):
        require_ordered_pause_presentation(member_pause, show_tool_calls=True)


@pytest.mark.parametrize("member_agent_id", ["unknown", "Test Agent", "TEST_AGENT", "test-agent"])
def test_identify_approval_tools_rejects_unknown_member_owner(
    member_pause: PausedAttempt,
    member_agent_id: str,
) -> None:
    """Ownership requires an exact frozen identity even when the display name matches."""
    member_pause = replace(
        member_pause,
        requirements=(replace(member_pause.requirements[0], member_agent_id=member_agent_id),),
    )

    with pytest.raises(RuntimeError, match=f"^Team has no frozen member slot for {member_agent_id!r}$"):
        identify_approval_tools(member_pause, default_agent_name="test-team")


def test_identify_approval_tools_keeps_simple_member_owner(member_pause: PausedAttempt) -> None:
    """A member whose raw and frozen IDs coincide keeps its own approval policy."""
    member_pause = replace(
        member_pause,
        requirements=(replace(member_pause.requirements[0], member_agent_id="other"),),
    )

    identified = identify_approval_tools(member_pause, default_agent_name="test-team")

    assert identified[0][1] == "other"


@pytest.mark.parametrize(
    ("card_id", "anchor_id", "member_id", "presented"),
    [
        # A member's own delegation card is shown by the member's call it asks about.
        ("test_agent:call-1", "call-1", "test_agent", True),
        # A marker of its own in the team's slot is not that call.
        ("test_agent:call-1", "test_agent:call-1", None, False),
        # Another member's slot does not show it.
        ("other:call-1", "call-1", "test_agent", False),
        # A child's projected call names its delegation, not a member, and keeps its marker in the team's slot.
        ("delegation-1:call-1", "delegation-1:call-1", None, True),
    ],
)
def test_a_team_delegation_card_is_presented_by_the_call_it_asks_about(
    card_id: str,
    anchor_id: str,
    member_id: str | None,
    presented: bool,
) -> None:
    """The card the delegation driver raises carries no member identity of its own, only its id."""
    tool = ToolExecution(tool_call_id=card_id, tool_name="run_subagent", requires_confirmation=True)
    paused = PausedAttempt(
        session_id="session-1",
        run_id="run-1",
        requirements=(RunRequirement(tool_execution=tool),),
        response_text="🔧 `run_subagent` [1] ⏳",
        tool_trace=(
            ToolTraceEntry(
                type="tool_call_started",
                tool_name="run_subagent",
                tool_call_id=anchor_id,
                member_id=member_id,
            ),
        ),
        response_presentation_state={
            "kind": "team_stream",
            "version": 3,
            "members": [
                {"id": "test_agent", "display_name": "Test Agent", "content": ""},
                {"id": "other", "display_name": "Other", "content": ""},
            ],
            "consensus": "",
        },
        toolkit_owners={("test_agent", "run_subagent"): "delegate"},
    )

    if presented:
        require_ordered_pause_presentation(paused, show_tool_calls=True)
    else:
        with pytest.raises(RuntimeError, match="ordered presentation"):
            require_ordered_pause_presentation(paused, show_tool_calls=True)


def test_two_members_pending_on_one_provider_call_id_are_each_presented_in_their_own_slot() -> None:
    """Providers number call ids per member, so two members' pending calls may share one; each slot shows its own."""
    card = ToolExecution(tool_call_id="test_agent:call-1", tool_name="run_subagent", requires_confirmation=True)
    gated = ToolExecution(tool_call_id="call-1", tool_name="dangerous", requires_confirmation=True)
    gated_requirement = RunRequirement(tool_execution=gated, member_agent_id="other")
    card_anchor = ToolTraceEntry(
        type="tool_call_started",
        tool_name="run_subagent",
        tool_call_id="call-1",
        member_id="test_agent",
    )
    paused = PausedAttempt(
        session_id="session-1",
        run_id="run-1",
        requirements=(RunRequirement(tool_execution=card), gated_requirement),
        response_text="🔧 `run_subagent` [1] ⏳\n\n🔧 `dangerous` [2] ⏳",
        tool_trace=(
            card_anchor,
            ToolTraceEntry(
                type="tool_call_started",
                tool_name="dangerous",
                tool_call_id="call-1",
                member_id="other",
            ),
        ),
        response_presentation_state={
            "kind": "team_stream",
            "version": 3,
            "members": [
                {"id": "test_agent", "display_name": "Test Agent", "content": ""},
                {"id": "other", "display_name": "Other", "content": ""},
            ],
            "consensus": "",
        },
        toolkit_owners={("test_agent", "run_subagent"): "delegate", ("other", "dangerous"): "test_toolkit"},
    )

    require_ordered_pause_presentation(paused, show_tool_calls=True)

    # A second pending marker of the call id in the card's own slot leaves the card without a unique anchor.
    doubled = replace(
        paused,
        response_text=f"{paused.response_text}\n\n🔧 `run_subagent` [3] ⏳",
        tool_trace=(*paused.tool_trace, replace(card_anchor)),
    )
    with pytest.raises(RuntimeError, match="ordered presentation"):
        require_ordered_pause_presentation(doubled, show_tool_calls=True)


def test_a_member_s_pending_call_is_presented_in_its_slot_when_the_leader_used_its_id_too(
    member_pause: PausedAttempt,
) -> None:
    """The leader's provider numbers its calls apart from the members', so a leader's call may share a member call's id."""
    leader_call = ToolTraceEntry(type="tool_call_started", tool_name="delegate_task_to_member", tool_call_id="call-1")
    paused = replace(
        member_pause,
        response_text=f"{member_pause.response_text}\n\n🔧 `delegate_task_to_member` [2] ⏳",
        tool_trace=(*member_pause.tool_trace, leader_call),
    )

    require_ordered_pause_presentation(paused, show_tool_calls=True)


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
        requirements=(RunRequirement(tool_execution=tool),),
        response_text="🔧 `dangerous` [1] ⏳",
        tool_trace=(
            ToolTraceEntry(
                type="tool_call_started",
                tool_name="dangerous",
                tool_call_id="call-1",
                member_id="wrong-member",
            ),
        ),
        response_presentation_state={
            "kind": "team_stream",
            "version": 3,
            "members": [],
            "consensus": "🔧 `dangerous` [1] ⏳",
        },
        toolkit_owners={("general", "dangerous"): "test_toolkit"},
    )

    with pytest.raises(RuntimeError, match="ordered presentation"):
        require_ordered_pause_presentation(paused, show_tool_calls=True)


def _finished(
    *,
    run_id: str = "run-1",
    status: RunStatus = RunStatus.completed,
    content: str | None = None,
    tools: list[ToolExecution] | None = None,
) -> RunFinished:
    return RunFinished(RunRecord(run_id=run_id, session_id="session-1", status=status, content=content, tools=tools))


async def _events(*items: RunEvent) -> AsyncIterator[RunEvent]:
    for item in items:
        yield item


@pytest.mark.asyncio
async def test_agent_continuation_defers_terminal_only_content() -> None:
    """A completed record's text is the continuation delta when no content deltas were streamed."""
    presentation = CollectedStreamPresentation(show_tool_calls=True, response_text="Before approval. ")
    terminal = _finished(content="After approval.")

    response = await _collect_agent_continuation(_events(terminal), presentation, run_id="run", progress=None)

    assert response.response is terminal.record
    assert response.terminal_content == "After approval."
    assert presentation.final_text() == "Before approval. "


@pytest.mark.asyncio
async def test_a_continued_calls_output_reaches_the_live_reply_until_it_completes() -> None:
    """The output a call resumed after an approval reports is published with the reply it continues."""
    presentation = CollectedStreamPresentation(show_tool_calls=True, response_text="Approved. ")
    published: list[StructuredStreamChunk] = []

    async def progress(chunk: StructuredStreamChunk) -> None:
        published.append(chunk)

    call = ReportedCall(tool_call_id="call-1", tool_name="run_shell_command", tool_args={})
    done = ReportedCall(tool_call_id="call-1", tool_name="run_shell_command", tool_args={}, result="ok")
    await _collect_agent_continuation(
        _events(ToolStarted(call), ToolProgress("call-1", "step 1"), ToolCompleted(done), _finished()),
        presentation,
        run_id="run-1",
        progress=progress,
    )

    assert {"call-1": "step 1"} in [chunk.tool_progress for chunk in published]
    assert published[-1].tool_progress == {}


@pytest.mark.asyncio
async def test_streamed_text_is_the_continuation_delta_and_the_record_adds_nothing() -> None:
    """Text that streamed as deltas is not appended again from the completed record."""
    presentation = CollectedStreamPresentation(show_tool_calls=True, response_text="Before approval. ")

    response = await _collect_agent_continuation(
        _events(TextDelta("After "), TextDelta("approval."), _finished(content="After approval.")),
        presentation,
        run_id="run",
        progress=None,
    )

    assert (response.terminal_content, response.has_visible_content) == ("", True)
    assert presentation.final_text() == "Before approval. After approval."


@pytest.mark.asyncio
@pytest.mark.parametrize("completion_source", ["stream", "terminal", "snapshot"])
async def test_agent_approval_completion_preserves_recovered_same_name_trace(completion_source: str) -> None:
    """Reconciliation must never give a new call's result to a recovered public trace slot."""
    prefix = "Before restart.\n\n🔧 `inspect` [1] ⏳\n\n"
    already_completed = completion_source == "snapshot"
    presentation = CollectedStreamPresentation(
        show_tool_calls=True,
        response_text=prefix + "Before approval.\n\n🔧 `inspect` [2]" + ("" if already_completed else " ⏳"),
        tool_trace=[
            ToolTraceEntry(type="tool_call_started", tool_name="inspect"),
            ToolTraceEntry(
                type="tool_call_completed" if already_completed else "tool_call_started",
                tool_name="inspect",
                tool_call_id="current-call",
                result_preview="current result" if already_completed else None,
            ),
        ],
    )
    tool = ToolExecution(tool_call_id="current-call", tool_name="inspect", result="current result")
    events = [ToolCompleted(tool.call)] if completion_source == "stream" else []
    events.append(_finished(run_id="current-run", tools=[tool]))

    await _collect_agent_continuation(_events(*events), presentation, run_id="run", progress=None)

    assert presentation.final_text().startswith(prefix)
    assert presentation.tool_trace[0] == ToolTraceEntry(type="tool_call_started", tool_name="inspect")
    assert presentation.tool_trace[1].type == "tool_call_completed"
    assert presentation.tool_trace[1].result_preview == "current result"
    assert "🔧 `inspect` [2] ⏳" not in presentation.final_text()


@pytest.mark.asyncio
async def test_agent_chained_pause_anchors_a_terminal_only_pending_tool() -> None:
    """A paused record supplies the pending anchor when no tool-start event announced the call."""
    tool = ToolExecution(
        tool_call_id="call-2",
        tool_name="publish_report",
        tool_args={},
        requires_confirmation=True,
    )
    presentation = CollectedStreamPresentation(show_tool_calls=True, response_text="Before approval.")

    await _collect_agent_continuation(
        _events(_finished(run_id="run-2", status=RunStatus.paused, tools=[tool])),
        presentation,
        run_id="run",
        progress=None,
    )

    assert presentation.response_text.endswith("🔧 `publish_report` [1] ⏳\n\n")
    assert len(presentation.tool_trace) == 1
    assert presentation.tool_trace[0].type == "tool_call_started"
    assert presentation.tool_trace[0].tool_call_id == "call-2"


@pytest.mark.asyncio
async def test_agent_continuation_keeps_text_after_a_stripped_tool_marker() -> None:
    """Continuation content without leading whitespace must not join the marker line."""
    tool = ToolExecution(tool_call_id="call-1", tool_name="inspect", tool_args={}, result="done")
    presentation = CollectedStreamPresentation(
        show_tool_calls=True,
        response_text="Before approval.\n\n🔧 `inspect` [1] ⏳",
        tool_trace=[ToolTraceEntry(type="tool_call_started", tool_name="inspect", tool_call_id="call-1")],
    )

    collected = await _collect_agent_continuation(
        _events(_finished(content="After approval.", tools=[tool])),
        presentation,
        run_id="run",
        progress=None,
    )
    presentation.append_text(collected.terminal_content)

    assert presentation.final_text() == "Before approval.\n\n🔧 `inspect` [1]\n\nAfter approval."


@pytest.mark.asyncio
async def test_agent_continuation_reuses_an_existing_visible_tool_separator() -> None:
    """A restored marker suffix must not become two blank paragraphs."""
    tool = ToolExecution(tool_call_id="call-1", tool_name="inspect", result="done")
    presentation = CollectedStreamPresentation(
        show_tool_calls=True,
        response_text="Before approval.\n\n🔧 `inspect` [1] ⏳\n\n",
        tool_trace=[ToolTraceEntry(type="tool_call_started", tool_name="inspect", tool_call_id="call-1")],
    )

    collected = await _collect_agent_continuation(
        _events(_finished(content="After approval.", tools=[tool])),
        presentation,
        run_id="run",
        progress=None,
    )
    presentation.append_text(collected.terminal_content)

    assert presentation.final_text() == "Before approval.\n\n🔧 `inspect` [1]\n\nAfter approval."


@pytest.mark.asyncio
async def test_hidden_agent_continuation_separates_text_across_the_tool_boundary() -> None:
    """Hidden approval tools must not concatenate pre- and post-approval prose."""
    tool = ToolExecution(tool_call_id="call-1", tool_name="inspect", result="done")
    presentation = CollectedStreamPresentation(
        show_tool_calls=False,
        response_text="Before approval.",
        tool_trace=[ToolTraceEntry(type="tool_call_started", tool_name="inspect", tool_call_id="call-1")],
        track_hidden_tools=True,
    )

    collected = await _collect_agent_continuation(
        _events(_finished(content="After approval.", tools=[tool])),
        presentation,
        run_id="run",
        progress=None,
    )
    presentation.append_text(collected.terminal_content)

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

    collected = await _collect_agent_continuation(
        _events(
            ToolStarted(ToolExecution(tool_call_id="call-2", tool_name="inspect", tool_args={}).call),
            ToolCompleted(tool.call),
            _finished(content="After tool.", tools=[tool]),
        ),
        presentation,
        run_id="run",
        progress=None,
    )
    presentation.append_text(collected.terminal_content)

    assert presentation.final_text() == "Before tool.\n\nAfter tool."


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [None, "calculator"])
async def test_pause_plan_requires_exact_live_toolkit_origin(tmp_path: Path, owner: str | None) -> None:
    """A new card cannot defer discovering its toolkit owner until approval time."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    tool = ToolExecution(tool_call_id="call-1", tool_name="add", requires_confirmation=True)
    origins = {("general", "add"): owner, ("other", "add"): "calculator"}
    if owner is None:
        with pytest.raises(RuntimeError, match="toolkit origin"):
            await plan_approval_calls(
                ((tool, "general"),),
                config=runner.deps.runtime.config,
                runtime_paths=runner.deps.runtime_paths,
                requester_id="@user:localhost",
                toolkit_owners=origins,
                room_id="!room:test",
                thread_id="$thread",
            )
    else:
        plan = await plan_approval_calls(
            ((tool, "general"),),
            config=runner.deps.runtime.config,
            runtime_paths=runner.deps.runtime_paths,
            requester_id="@user:localhost",
            toolkit_owners=origins,
            room_id="!room:test",
            thread_id="$thread",
        )
        assert plan.calls[0].toolkit_name == "calculator"


@pytest.mark.parametrize(("entity_kind", "response_kind"), [("agent", "ai"), ("team", "team")])
def test_a_resumed_reply_answers_as_the_kind_of_entity_that_paused(entity_kind: str, response_kind: str) -> None:
    """A team's approval resumes as a team reply, which its hooks and reply mirror tell apart from an agent's."""
    assert continuation_response_kind(approval_continuation(entity_kind=entity_kind)) == response_kind
