"""A team reply an earlier release paused, its snapshot of version 2, restores and resumes with config-name slots.

``v2026.10.674_paused_replies.json`` holds the reply records v2026.10.674 stored for one team reply paused on a
member's gated call, unchanged: the presentation its ``encode_presentation`` wrote for the pause its team runtime
produced, with tool calls shown and with them hidden. The team's members are ``Code_Review``, whose url-safe id
``code-review`` that release keyed its slot by, and ``general``.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.config.approval import ApprovalRuleConfig, ToolApprovalConfig
from mindroom.event_journal import ApprovalCall, approval_arguments_digest
from mindroom.llm.runs import ToolExecution
from mindroom.reply_presentation import Segment, decode_presentation
from mindroom.response_turn import CompletedApprovalRun, ResponsePausedForApproval
from mindroom.teams.presentation import TeamStreamPresentation
from mindroom.tool_system.events import ToolTraceEntry
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call
from tests.team_turn_helpers import (
    PROBE_TOOL,
    ProbeToolkit,
    TeamTurn,
    authored_team_config,
    build_team_turn,
    registered_probe,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from mindroom.tool_system.events import StructuredStreamChunk

_RECORDS: dict[str, str] = json.loads(
    (
        Path(__file__).parents[1] / "fixtures" / "legacy_team_presentation" / "v2026.10.674_paused_replies.json"
    ).read_text(),
)
_MEMBERS = ["Code_Review", "general"]


@pytest.fixture(autouse=True)
def probe() -> Iterator[type[ProbeToolkit]]:
    """Register the probe toolkit the members' configs name."""
    with registered_probe() as toolkit:
        yield toolkit


def _paused_segment(name: str) -> tuple[Segment, bool]:
    """Return the paused answer segment of a stored reply record and whether that reply shows its tool calls."""
    presentation = decode_presentation(_RECORDS[name])
    [segment] = presentation.segments
    assert segment.team_state is not None
    assert segment.team_state["version"] == 2
    return segment, presentation.show_tool_calls


def _restore(state: dict[str, Any], segment: Segment, *, show_tool_calls: bool) -> TeamStreamPresentation:
    return TeamStreamPresentation.restore(
        config_names=_MEMBERS,
        show_tool_calls=show_tool_calls,
        state=state,
        tool_trace=segment.tool_trace,
    )


@pytest.mark.parametrize("name", ["shown", "hidden"])
def test_a_v2_reply_record_restores_with_its_members_keyed_by_config_name(name: str) -> None:
    """The restored document shows what the pause showed, and every slot, call, and break is under a config name."""
    segment, show_tool_calls = _paused_segment(name)
    stored_state = dict(segment.team_state or {})

    restored = _restore(stored_state, segment, show_tool_calls=show_tool_calls)

    assert restored.member_ids == _MEMBERS
    assert restored.display_names_by_id == {"Code_Review": "Code Review", "general": "GeneralAgent"}
    assert restored.render_body(finished=False) == segment.text
    assert [(entry.tool_call_id, entry.scope_key, entry.type) for entry in restored.tool_trace] == [
        ("call-touch", "agent:Code_Review", "tool_call_started"),
        ("call-team", "team", "tool_call_completed"),
    ]
    state = restored.to_state()
    assert state["version"] == 3
    assert [member["id"] for member in state["members"]] == _MEMBERS
    assert state.get("separate_next_scopes") == (None if show_tool_calls else ["agent:Code_Review", "team"])

    restored.complete_member_tool(
        "Code_Review",
        ToolExecution(tool_call_id="call-touch", tool_name="touch", result="touched").call,
    )
    restored.append_member("Code_Review", "After approval.")
    restored.append_consensus("Done.")

    marker = "🔧 `touch` [1]\n\n" if show_tool_calls else ""
    assert restored.per_member["Code_Review"] == f"Before approval.\n\n{marker}After approval."
    assert restored.consensus.endswith("\n\nDone.")
    assert [entry.type for entry in restored.tool_trace] == ["tool_call_completed", "tool_call_completed"]


def _reviewed_team(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TeamTurn:
    """Build the team the stored replies paused: the leader delegates to ``Code_Review``, whose call is gated."""
    authored = authored_team_config(
        {
            "Code_Review": {
                "display_name": "Code Review",
                "role": "Reviews.",
                "model": "review-model",
                "tools": [PROBE_TOOL],
            },
            "general": {
                "display_name": "GeneralAgent",
                "role": "Answers.",
                "model": "general-model",
                "tools": [PROBE_TOOL],
            },
        },
        models=("default", "review-model", "general-model"),
    )
    team = build_team_turn(
        tmp_path,
        monkeypatch,
        authored,
        members=_MEMBERS,
        scripts={
            "default": [
                calls(
                    tool_call("delegate_task_to_member", "call-team", member_id="Code_Review", task="Review it"),
                    content="Consensus before approval.",
                ),
            ],
            "review-model": [calls(tool_call("touch", "call-touch", label="diff"), content="Before approval.")],
            "general-model": [],
        },
    )
    team.config.tool_approval = ToolApprovalConfig(rules=[ApprovalRuleConfig(match="touch", action="require_approval")])
    return team


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["shown", "hidden"])
async def test_a_team_reply_paused_by_v2026_10_674_resumes_in_place(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[ProbeToolkit],
    name: str,
) -> None:
    """An approval resumes the paused run from the reply's stored records, its document continuing the stored one."""
    segment, show_tool_calls = _paused_segment(name)
    team = _reviewed_team(tmp_path, monkeypatch)
    with pytest.raises(ResponsePausedForApproval) as raised:
        await team.respond(show_tool_calls=show_tool_calls)
    paused = raised.value.paused
    # The paused run's row is what this release stores; the reply's records are the ones the earlier release stored.
    assert paused.response_text == segment.text
    stored = replace(
        paused,
        tool_trace=segment.tool_trace,
        response_presentation_state=deepcopy(dict(segment.team_state or {})),
    )
    team.models["default"] = ScriptedModel([answer("Done.")])
    team.models["review-model"] = ScriptedModel([answer("After approval.")])
    published: list[str] = []
    trace: list[Any] = []

    async def progress(chunk: StructuredStreamChunk) -> None:
        published.append(chunk.content)

    result = await team.resume(
        stored,
        approved=True,
        approvals=(
            ApprovalCall(
                tool_call_id="call-touch",
                tool_name="touch",
                invoking_agent="Code_Review",
                toolkit_name=PROBE_TOOL,
                expires_at_ns=2**62,
                arguments_digest=approval_arguments_digest({"label": "diff"}),
            ),
        ),
        show_tool_calls=show_tool_calls,
        tool_trace_collector=trace,
        progress=progress,
    )

    assert isinstance(result, CompletedApprovalRun)
    assert probe.calls == ["diff"]
    assert published[0] == segment.text
    marker = "🔧 `touch` [1]\n\n" if show_tool_calls else ""
    assert f"**Code Review**: Before approval.\n\n{marker}After approval." in result.response_text
    assert result.response_text.endswith("Done.")
    # A reply that hides its tool calls collects no trace.
    assert [(entry.tool_call_id, entry.scope_key, entry.type) for entry in trace] == (
        [("call-touch", "agent:Code_Review", "tool_call_completed"), ("call-team", "team", "tool_call_completed")]
        if show_tool_calls
        else []
    )


@pytest.mark.parametrize(
    ("config_name", "member_id"),
    [
        ("ledger_agent", "ledger-agent"),
        ("CamelCase", "camel-case"),
        ("Two Words", "two-words"),
        ("dots.kept", "dots.kept"),
        ("trailing__under", "trailing-under"),
        ("mixed-Case_name 2", "mixed-case-name-2"),
        ("Q&A bot!", "qa-bot"),
    ],
)
def test_a_v2_snapshot_keys_each_member_by_the_url_safe_id_of_its_config_name(config_name: str, member_id: str) -> None:
    """A version-2 snapshot's member id is Agno's url-safe id of the config name, which restores as that name."""
    state = {
        "kind": "team_stream",
        "version": 2,
        "members": [{"id": member_id, "config_name": config_name, "display_name": "Member", "content": "Before."}],
        "consensus": "",
        "separate_next_scopes": [f"agent:{member_id}"],
    }
    pending = ToolTraceEntry(
        type="tool_call_started",
        tool_name="touch",
        tool_call_id="call-1",
        scope_key=f"agent:{member_id}",
    )

    restored = TeamStreamPresentation.restore(
        config_names=[config_name],
        show_tool_calls=False,
        state=state,
        tool_trace=[pending],
    )

    assert restored.member_ids == [config_name]
    assert restored.tool_trace[0].scope_key == f"agent:{config_name}"
    assert restored.to_state()["separate_next_scopes"] == [f"agent:{config_name}"]


def _stored_v2(**changes: object) -> dict[str, Any]:
    segment, _ = _paused_segment("hidden")
    state = deepcopy(dict(segment.team_state or {}))
    state.update(changes)
    return state


def _v2_member(member_id: str, config_name: str) -> dict[str, str]:
    return {"id": member_id, "config_name": config_name, "display_name": config_name, "content": ""}


@pytest.mark.parametrize(
    ("state", "scopes", "config_names"),
    [
        # Ids that are not the url-safe ids of their config names, though the separators and trace use them.
        (
            _stored_v2(
                members=[_v2_member("Code_Review", "Code_Review"), _v2_member("general", "general")],
                separate_next_scopes=["team"],
            ),
            ("agent:Code_Review", "team"),
            _MEMBERS,
        ),
        # Two members under one url-safe id, so a slot the trace names could be either's.
        (
            _stored_v2(
                members=[_v2_member("code-review", "Code_Review"), _v2_member("code-review", "code-review")],
                separate_next_scopes=["team"],
            ),
            ("agent:code-review", "team"),
            ["Code_Review", "code-review"],
        ),
        # A member slot without a config name.
        (
            _stored_v2(members=[{"id": "code-review", "display_name": "Code Review", "content": ""}]),
            ("team", "team"),
            [],
        ),
        # A separator for a slot the snapshot does not hold, one that is not a slot, and separators not in a list.
        (_stored_v2(separate_next_scopes=["agent:Code_Review"]), ("agent:code-review", "team"), _MEMBERS),
        (_stored_v2(separate_next_scopes=[["team"]]), ("agent:code-review", "team"), _MEMBERS),
        (_stored_v2(separate_next_scopes={"team": True}), ("agent:code-review", "team"), _MEMBERS),
        # A tool call in a slot the snapshot does not hold, or in none.
        (_stored_v2(), ("agent:Code_Review", "team"), _MEMBERS),
        (_stored_v2(), ("agent:code-review", None), _MEMBERS),
    ],
)
def test_a_malformed_v2_snapshot_is_refused(
    state: dict[str, Any],
    scopes: tuple[str | None, str | None],
    config_names: list[str],
) -> None:
    """Only a snapshot a version-2 writer could have stored restores; anything else is refused before it is read."""
    segment, _ = _paused_segment("hidden")
    trace = [replace(entry, scope_key=scope) for entry, scope in zip(segment.tool_trace, scopes, strict=True)]

    with pytest.raises(RuntimeError, match="snapshot is invalid"):
        TeamStreamPresentation.restore(config_names=config_names, show_tool_calls=False, state=state, tool_trace=trace)
