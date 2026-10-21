"""The team reply document: member slots, the consensus, tool markers, the durable snapshot, and Agno's member ids."""
# ruff: noqa: D103

from __future__ import annotations

import pytest
from agno.utils.string import url_safe_string

from mindroom.llm.events import ContentDelta, ToolCompleted, ToolStarted
from mindroom.llm.runs import ToolExecution
from mindroom.teams.presentation import (
    TeamStreamPresentation,
    apply_team_event,
    format_team_header,
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


def test_a_snapshot_for_other_members_is_refused() -> None:
    presentation = _presentation()
    with pytest.raises(RuntimeError, match="snapshot is invalid"):
        TeamStreamPresentation.restore(
            config_names=["ledger_agent", "someone_else"],
            show_tool_calls=False,
            state=presentation.to_state(),
            tool_trace=(),
        )
