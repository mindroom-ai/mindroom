"""The team reply document: member slots, consensus, tool markers, the durable snapshot, member ids, finished runs."""
# ruff: noqa: D103

from __future__ import annotations

import pytest
from agno.utils.string import url_safe_string

from mindroom.approval.response import require_ordered_pause_presentation
from mindroom.llm.events import ContentDelta, ToolCompleted, ToolStarted
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunRequirement, TeamRunRecord, ToolExecution
from mindroom.response_turn import PausedAttempt
from mindroom.teams.presentation import (
    TeamStreamPresentation,
    apply_team_event,
    finished_run_text,
    format_team_header,
    format_team_response,
    render_team_member_slots,
    strip_team_display,
    url_safe_member_id,
)
from mindroom.teams.runtime import MemberEvent


def _presentation(*, show_tool_calls: bool = False) -> TeamStreamPresentation:
    return TeamStreamPresentation.new(["ledger_agent", "scout"], ["Ledger", "Scout"], show_tool_calls=show_tool_calls)


@pytest.mark.parametrize(
    "config_name",
    ["ledger_agent", "CamelCase", "Two Words", "dots.kept", "trailing__under", "mixed-Case_name 2"],
)
def test_member_ids_are_agno_s_url_safe_strings(config_name: str) -> None:
    """Stored reply snapshots keyed members by Agno's ``url_safe_string``; the copy must produce the same ids."""
    assert url_safe_member_id(config_name) == url_safe_string(config_name)


def test_member_and_leader_events_land_in_their_slots() -> None:
    presentation = _presentation()
    apply_team_event(presentation, MemberEvent("ledger_agent", "Ledger", "run-1", ContentDelta("Three ")))
    apply_team_event(presentation, MemberEvent("ledger_agent", "Ledger", "run-1", ContentDelta("vendors.")))
    apply_team_event(presentation, MemberEvent("scout", "Scout", "run-2", ContentDelta("Two more.")))
    apply_team_event(presentation, ContentDelta("Five in all."))

    assert presentation.per_member == {"ledger-agent": "Three vendors.", "scout": "Two more."}
    assert presentation.consensus == "Five in all."
    assert presentation.render_body() == (
        "🤝 **Team Response** (Ledger, Scout):\n\n"
        "**Ledger**: Three vendors.\n\n**Scout**: Two more.\n\n\n**Team Consensus**:\n\nFive in all."
    )
    assert strip_team_display(presentation.render_body()) == (
        "**Ledger**: Three vendors.\n\n**Scout**: Two more.\n\n\n**Team Consensus**:\n\nFive in all."
    )


def test_an_event_for_an_unknown_member_is_refused() -> None:
    with pytest.raises(RuntimeError, match="no frozen member slot"):
        apply_team_event(_presentation(), MemberEvent("stranger", "Stranger", "run-9", ContentDelta("hi")))


@pytest.mark.parametrize(
    "content",
    [
        "| Model | Revenue |\n|---|---|\n| Per seat | predictable |",
        "Model | Revenue\n---|---\nPer seat | predictable",
        "## Findings\nThree vendors price per seat.",
        "Findings\n========\nThree vendors price per seat.",
        "---\nThree vendors price per seat.",
        "- Per seat\n- Usage",
        "1. Per seat\n2. Usage",
        "> Quoted from the brief",
        "```python\nprint(1)\n```",
    ],
)
def test_member_content_that_opens_a_block_starts_its_own_paragraph(content: str) -> None:
    """A table or other block right after the member's name would not render, so it gets its own paragraph."""
    [part] = render_team_member_slots(
        ["ledger"],
        display_names_by_id={"ledger": "ledger"},
        per_member={"ledger": content},
        consensus="x",
    )[:1]
    assert part == f"**ledger**:\n\n{content}"


@pytest.mark.parametrize("content", ["2026 pricing: seat | usage, three vendors in four.", "[1]: https://example.com"])
def test_member_prose_stays_on_the_name_line(content: str) -> None:
    """Prose with a pipe or a number, and a link definition that renders nothing, stay right after the name."""
    [part] = render_team_member_slots(
        ["scout"],
        display_names_by_id={"scout": "scout"},
        per_member={"scout": content},
        consensus="x",
    )[:1]
    assert part == f"**scout**: {content}"


def test_members_without_a_consensus_get_the_no_consensus_note_and_a_lone_consensus_renders_alone() -> None:
    assert render_team_member_slots(
        ["a"],
        display_names_by_id={"a": "agent1"},
        per_member={"a": "Response 1"},
        consensus="",
    ) == ["**agent1**: Response 1", "\n*No team consensus - showing individual responses only*"]
    assert render_team_member_slots(
        ["a"],
        display_names_by_id={"a": "agent1"},
        per_member={"a": ""},
        consensus="Team consensus only",
    ) == ["\n**Team Consensus**:", "Team consensus only"]
    assert format_team_header(["Ledger", "Scout"]) == "🤝 **Team Response** (Ledger, Scout):\n\n"


def test_a_finished_run_without_content_reads_as_its_assistant_messages() -> None:
    """A run whose content is empty shows the text of its own assistant messages, as Agno's blocking reply read it."""
    scout = RunRecord(
        run_id="scout-run",
        agent_id="scout",
        agent_name="Scout",
        messages=[
            Message(role="user", content="Find vendors."),
            Message(role="assistant", content="Three vendors."),
            Message(role="assistant", tool_calls=[{"id": "call-1"}]),
            Message(role="tool", content="two more", tool_call_id="call-1"),
            Message(role="assistant", content="Two more."),
        ],
    )

    assert finished_run_text(scout) == "Three vendors.\n\nTwo more."
    assert format_team_response(TeamRunRecord(run_id="team-run", content="Five in all."), [scout]) == (
        "**Scout**: Three vendors.\n\nTwo more.\n\n\n**Team Consensus**:\n\nFive in all."
    )


def test_hidden_tool_calls_separate_the_text_that_follows_them_and_survive_the_snapshot() -> None:
    """With tool calls hidden, text after a member's tool call starts a new paragraph; the snapshot restores that."""
    presentation = _presentation(show_tool_calls=False)
    tool = ToolExecution(tool_call_id="call-1", tool_name="lookup", tool_args={"q": "seats"}, result="4")
    apply_team_event(presentation, MemberEvent("scout", "Scout", "run-2", ContentDelta("Looking.")))
    apply_team_event(presentation, MemberEvent("scout", "Scout", "run-2", ToolStarted(tool)))
    state = presentation.to_state()
    assert state["separate_next_scopes"] == ["agent:scout"]

    restored = TeamStreamPresentation.restore(
        config_names=["ledger_agent", "scout"],
        show_tool_calls=False,
        state=state,
        tool_trace=presentation.tool_trace,
    )
    apply_team_event(restored, MemberEvent("scout", "Scout", "run-2", ToolCompleted(tool, parent_run_id="team")))
    apply_team_event(restored, MemberEvent("scout", "Scout", "run-2", ContentDelta("Four seats.")))

    assert restored.per_member["scout"] == "Looking.\n\nFour seats."
    assert [entry.type for entry in restored.tool_trace] == ["tool_call_completed"]
    assert restored.tool_tracker.pending_tools == []


def test_an_empty_delta_keeps_the_paragraph_break_a_hidden_call_left_for_the_next_text() -> None:
    """Providers stream empty text deltas; one must not use up the break before the text after a hidden call."""
    presentation = _presentation(show_tool_calls=False)
    member_tool = ToolExecution(tool_call_id="call-1", tool_name="lookup", tool_args={"q": "seats"})
    leader_tool = ToolExecution(tool_call_id="call-2", tool_name="lookup", tool_args={"q": "total"})
    apply_team_event(presentation, MemberEvent("scout", "Scout", "run-2", ContentDelta("Looking.")))
    apply_team_event(presentation, MemberEvent("scout", "Scout", "run-2", ToolStarted(member_tool)))
    apply_team_event(presentation, MemberEvent("scout", "Scout", "run-2", ContentDelta("")))
    apply_team_event(presentation, MemberEvent("scout", "Scout", "run-2", ContentDelta("Four seats.")))
    apply_team_event(presentation, ContentDelta("Summing up."))
    apply_team_event(presentation, ToolStarted(leader_tool))
    apply_team_event(presentation, ContentDelta(""))
    apply_team_event(presentation, ContentDelta("Four in all."))

    assert presentation.per_member["scout"] == "Looking.\n\nFour seats."
    assert presentation.consensus == "Summing up.\n\nFour in all."


def test_a_snapshot_for_other_members_is_refused() -> None:
    presentation = _presentation()
    with pytest.raises(RuntimeError, match="snapshot is invalid"):
        TeamStreamPresentation.restore(
            config_names=["ledger_agent", "someone_else"],
            show_tool_calls=False,
            state=presentation.to_state(),
            tool_trace=(),
        )


def test_members_with_the_same_display_name_keep_their_own_slots() -> None:
    """Slots are keyed by member id, so equal display names share neither text nor tools."""
    presentation = TeamStreamPresentation.new(["member_a", "member_b"], ["Same", "Same"], show_tool_calls=True)

    presentation.append_member("member-a", "First answer.")
    presentation.append_member("member-b", "Second answer.")
    presentation.start_member_tool("member-b", ToolExecution(tool_call_id="call-1", tool_name="inspect", tool_args={}))

    assert presentation.per_member == {
        "member-a": "First answer.",
        "member-b": "Second answer.\n\n🔧 `inspect` [1] ⏳\n\n",
    }
    assert presentation.tool_trace[0].scope_key == "agent:member-b"
    assert presentation.render_body().count("**Same**:") == 2


def test_a_pause_renumbers_interleaved_member_calls_in_document_order() -> None:
    """The trace and the markers follow the slots' order, not the order the calls started in."""
    presentation = TeamStreamPresentation.new(["first", "second"], ["First", "Second"], show_tool_calls=True)
    first = ToolExecution(tool_call_id="call-first", tool_name="inspect_first", requires_confirmation=True)
    second = ToolExecution(tool_call_id="call-second", tool_name="inspect_second", requires_confirmation=True)
    presentation.start_member_tool("second", second)
    presentation.start_member_tool("first", first)
    paused = PausedAttempt(
        session_id="session-1",
        run_id="run-1",
        tools=(first, second),
        requirements=(
            RunRequirement(first, member_agent_id="first"),
            RunRequirement(second, member_agent_id="second"),
        ),
        response_text=presentation.render_body(),
        tool_trace=tuple(presentation.tool_trace),
        response_presentation_state=presentation.to_state(),
        toolkit_owners={("first", "inspect_first"): "test_toolkit", ("second", "inspect_second"): "test_toolkit"},
    )

    require_ordered_pause_presentation(paused, show_tool_calls=True)

    assert [entry.tool_call_id for entry in presentation.tool_trace] == ["call-first", "call-second"]
    assert "🔧 `inspect_first` [1] ⏳" in presentation.per_member["first"]
    assert "🔧 `inspect_second` [2] ⏳" in presentation.per_member["second"]
    presentation.complete_member_tool(
        "second",
        ToolExecution(tool_call_id="call-second", tool_name="inspect_second", result="done"),
    )
    assert [entry.type for entry in presentation.tool_trace] == ["tool_call_started", "tool_call_completed"]
    assert "🔧 `inspect_second` [2]\n\n" in presentation.per_member["second"]


def test_a_hidden_snapshot_keeps_the_call_identity_without_markers() -> None:
    """With tool calls hidden, the restored document completes its pending call by id and still shows no marker."""
    presentation = TeamStreamPresentation.new(["general"], ["GeneralAgent"], show_tool_calls=False)
    presentation.append_member("general", "Before.")
    presentation.start_member_tool(
        "general",
        ToolExecution(tool_call_id="call-1", tool_name="inspect", tool_args={"query": "value"}),
    )

    restored = TeamStreamPresentation.restore(
        config_names=["general"],
        show_tool_calls=False,
        state=presentation.to_state(),
        tool_trace=presentation.tool_trace,
    )
    restored.complete_member_tool("general", ToolExecution(tool_call_id="call-1", tool_name="inspect", result="done"))
    restored.append_member("general", " After.")

    assert "🔧" not in restored.render_body()
    assert (restored.tool_trace[0].tool_call_id, restored.tool_trace[0].type) == ("call-1", "tool_call_completed")


def test_two_members_may_reuse_one_call_id() -> None:
    """Members' providers number calls independently; a call id is scoped to its member's slot."""
    presentation = TeamStreamPresentation.new(["first", "second"], ["First", "Second"], show_tool_calls=True)
    presentation.start_member_tool(
        "first",
        ToolExecution(tool_call_id="call-1", tool_name="inspect", tool_args={"item": "first"}),
    )
    presentation.start_member_tool(
        "second",
        ToolExecution(tool_call_id="call-1", tool_name="inspect", tool_args={"item": "second"}),
    )
    presentation.complete_member_tool(
        "first",
        ToolExecution(tool_call_id="call-1", tool_name="inspect", result="first done"),
    )

    assert [(entry.scope_key, entry.type) for entry in presentation.tool_trace] == [
        ("agent:first", "tool_call_completed"),
        ("agent:second", "tool_call_started"),
    ]
    assert "🔧 `inspect` [1]" in presentation.per_member["first"]
    assert "🔧 `inspect` [2] ⏳" in presentation.per_member["second"]


@pytest.mark.parametrize("restored", [False, True])
def test_a_hidden_leader_call_separates_the_consensus_that_follows_it(*, restored: bool) -> None:
    """The consensus after a hidden leader call starts a new paragraph, also when a resume restored the pause."""
    presentation = TeamStreamPresentation.new([], [], show_tool_calls=False)
    tool = ToolExecution(tool_call_id="call-1", tool_name="inspect", tool_args={})
    apply_team_event(presentation, ContentDelta("Before approval."))
    apply_team_event(presentation, ToolStarted(tool))
    if restored:
        presentation = TeamStreamPresentation.restore(
            config_names=[],
            show_tool_calls=False,
            state=presentation.to_state(),
            tool_trace=presentation.tool_trace,
        )
    apply_team_event(
        presentation,
        ToolCompleted(ToolExecution(tool_call_id="call-1", tool_name="inspect", result="ok")),
    )
    apply_team_event(presentation, ContentDelta("After approval."))

    assert presentation.consensus == "Before approval.\n\nAfter approval."


def test_a_chained_pause_keeps_the_hidden_separators_of_every_slot() -> None:
    """A pause in one member's slot keeps the pending separator another member's finished call left."""
    prior = TeamStreamPresentation.new(["first", "second"], ["First", "Second"], show_tool_calls=False)
    prior.append_member("first", "Before tool.")
    prior.start_member_tool("first", ToolExecution(tool_call_id="call-1", tool_name="inspect", tool_args={}))
    prior.complete_member_tool("first", ToolExecution(tool_call_id="call-1", tool_name="inspect", result="done"))
    prior.append_member("second", "Waiting.")
    prior.start_member_tool("second", ToolExecution(tool_call_id="call-2", tool_name="approve", tool_args={}))

    restored = TeamStreamPresentation.restore(
        config_names=["first", "second"],
        show_tool_calls=False,
        state=prior.to_state(),
        tool_trace=prior.tool_trace,
    )
    restored.append_member("first", "After tool.")

    assert restored.per_member["first"] == "Before tool.\n\nAfter tool."


def test_a_restored_visible_marker_keeps_one_paragraph_break_before_the_consensus() -> None:
    """Completing a restored leader marker and continuing the consensus adds no second blank paragraph."""
    prior = TeamStreamPresentation.new([], [], show_tool_calls=True)
    prior.append_consensus("Before approval.")
    prior.start_tool("team", ToolExecution(tool_call_id="call-1", tool_name="inspect", tool_args={}))
    presentation = TeamStreamPresentation.restore(
        config_names=[],
        show_tool_calls=True,
        state=prior.to_state(),
        tool_trace=prior.tool_trace,
    )

    apply_team_event(
        presentation,
        ToolCompleted(ToolExecution(tool_call_id="call-1", tool_name="inspect", result="ok")),
    )
    apply_team_event(presentation, ContentDelta("After approval."))

    assert presentation.consensus == "Before approval.\n\n🔧 `inspect` [1]\n\nAfter approval."
