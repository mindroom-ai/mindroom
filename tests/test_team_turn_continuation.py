"""Same-turn continuation of a team reply after a member switches the thread's model.

A member's ``switch_thread_model`` call (the real ``thread_model`` tool) ends its run; the turn then runs again with a
continuation prompt, rebuilding the members, on the new model when the switch applies after the call. Providers are
scripted at the team runtime seam (``tests/team_turn_helpers.py``).
"""
# ruff: noqa: D103

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from mindroom.agents import create_agent
from mindroom.config.approval import ApprovalRuleConfig, ToolApprovalConfig
from mindroom.llm.provider import TextDelta
from mindroom.llm.request_context import current_llm_request_log_context
from mindroom.llm.runs import RunRecord, TeamRunRecord, ToolExecution
from mindroom.response_turn import TurnPartialSnapshot
from mindroom.room_model_overrides import set_room_model_override
from mindroom.teams.attempt import TeamAttempt
from mindroom.teams.envelope import _TeamTurnHolder
from mindroom.teams.members import materialize_exact_team_members, materialize_team_members
from mindroom.teams.presentation import TeamStreamPresentation
from mindroom.tool_system.runtime_context import ToolRuntimeModelBinding
from mindroom.turn_continuation import TURN_CONTINUATION_LIMIT
from tests.fakes.scripted_model import Await, ScriptStep, answer, calls, tool_call
from tests.team_turn_helpers import (
    PROBE_TOOL,
    ROOM_ID,
    SESSION_ID,
    ProbeToolkit,
    TeamTurn,
    authored_team_config,
    build_team_turn,
    delegate,
    registered_probe,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

PROMPT = "Switch to the large model and finish the task."
CONTINUATION_MARK = "MODEL SWITCH TOOL CALL COMPLETED"


@pytest.fixture(autouse=True)
def probe() -> Iterator[type[ProbeToolkit]]:
    """Register the probe toolkit the member's config names."""
    with registered_probe() as toolkit:
        yield toolkit


def _team(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    default: Sequence[Sequence[ScriptStep]] = (),
    general: Sequence[Sequence[ScriptStep]] = (),
    large: Sequence[Sequence[ScriptStep]] = (),
) -> TeamTurn:
    """Build the ad hoc team of ``general``, which may switch the thread's model to ``large``.

    The leader runs on ``default`` and the member on ``general-model`` until a switch moves them to ``large``.
    """
    authored = authored_team_config(
        {
            "general": {
                "display_name": "GeneralAgent",
                "role": "Answers.",
                "model": "general-model",
                "tools": ["thread_model", PROBE_TOOL],
            },
        },
        models=("default", "general-model", "large"),
    )
    return build_team_turn(
        tmp_path,
        monkeypatch,
        authored,
        members=("general",),
        scripts={"default": default, "general-model": general, "large": large},
    )


def _switch(when: str, *, model_name: str = "large", call_id: str = "call-switch") -> dict[str, Any]:
    return tool_call("switch_thread_model", call_id, model_name=model_name, when=when)


def _completed_tool_names(team: TeamTurn) -> list[str | None]:
    return [entry.tool_name for entry in team.recorder.completed_tools]


@pytest.mark.asyncio
@pytest.mark.parametrize(("stream", "show_tool_calls"), [(False, True), (True, True), (True, False)])
async def test_a_member_s_next_turn_switch_runs_the_turn_again_with_rebuilt_members(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    stream: bool,
    show_tool_calls: bool,
) -> None:
    """The second attempt has its own run id and continuation prompt; only its answer is the reply."""
    team = _team(
        tmp_path,
        monkeypatch,
        default=[delegate("call-team", "general", "Switch"), answer("Switching."), answer("Finished the task.")],
        general=[calls(_switch("next-turn"))],
    )
    run_ids: list[str] = []

    with patch("mindroom.teams.members.create_agent", wraps=create_agent) as build:
        text = await team.reply(
            stream=stream,
            message=PROMPT,
            run_id_callback=run_ids.append,
            show_tool_calls=show_tool_calls,
        )

    assert text.endswith("Finished the task.")
    assert "Switching." not in text
    assert "No team response generated." not in text
    assert build.call_count == 2
    leader = team.models["default"]
    second_prompt = str(leader.requests[-1].messages[-1].content)
    assert CONTINUATION_MARK in second_prompt
    assert "Continue the same task" in second_prompt
    assert run_ids[0] == "run-1"
    assert len(run_ids) == 2
    assert run_ids[1] != "run-1"
    assert team.recorder.outcome == "completed"
    assert team.recorder.assistant_text.endswith("Finished the task.")
    # The turn's trace still carries the first attempt's model switch.
    assert "switch_thread_model" in _completed_tool_names(team)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_a_continuation_on_the_session_s_first_turn_replays_the_first_attempt_from_its_new_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    stream: bool,
) -> None:
    """The first attempt gives the scope its first session; the continuation stores into it and replays that attempt."""
    team = _team(
        tmp_path,
        monkeypatch,
        default=[delegate("call-team", "general", "Switch"), answer("Switching."), answer("Finished the task.")],
        general=[calls(_switch("next-turn"))],
    )
    assert team.storage.load_session(SESSION_ID, "team") is None
    run_ids: list[str] = []

    await team.reply(stream=stream, message=PROMPT, run_id_callback=run_ids.append)

    first, *_, continued = team.models["default"].requests
    contents = [str(message.content) for message in continued.messages]
    assert contents[1] == str(first.messages[1].content)
    assert "Switching." in contents
    assert CONTINUATION_MARK in contents[-1]
    session = team.storage.load_session(SESSION_ID, "team")
    assert session is not None
    assert [run.run_id for run in session.runs if isinstance(run, TeamRunRecord)] == run_ids


@pytest.mark.asyncio
async def test_model_changes_made_mid_turn_do_not_reach_the_rebuilt_members(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The member models a turn starts with are pinned for its continuation."""

    async def change_the_room_default() -> None:
        set_room_model_override(team.runtime_paths, room_id=ROOM_ID, model_name="default")

    team = _team(
        tmp_path,
        monkeypatch,
        # The room default reaches the member, not the ad hoc team's leader, which runs on ``default``.
        default=[
            delegate("call-team", "general", "Switch"),
            answer("Switching."),
            delegate("call-again", "general", "Finish"),
            answer("Finished the task."),
        ],
        # The switch writes the thread override at once, and it outranks the room default, so it names a model
        # other than the pinned one: a rebuild that dropped the pin would run the member on it.
        large=[
            [Await(change_the_room_default), *calls(_switch("next-turn", model_name="general-model"))],
            answer("Done."),
        ],
    )
    set_room_model_override(team.runtime_paths, room_id=ROOM_ID, model_name="large")

    text = await team.respond(message=PROMPT)

    assert text.endswith("Finished the task.")
    assert len(team.models["large"].requests) == 2
    assert team.models["general-model"].requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_an_after_call_switch_rebuilds_the_leader_and_members_on_the_new_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: type[ProbeToolkit],
    *,
    stream: bool,
) -> None:
    """Keeping the first attempt's members would make a switch for the current reply wait until the next turn."""
    team = _team(
        tmp_path,
        monkeypatch,
        default=[delegate("call-team", "general", "Switch"), answer("Switching.")],
        general=[calls(tool_call("model", "call-model")), calls(_switch("after-toolcall"))],
        large=[
            delegate("call-again", "general", "Finish"),
            calls(tool_call("model", "call-model-2")),
            answer("Member finished."),
            answer("Finished with the new model."),
        ],
    )

    text = await team.reply(stream=stream, message=PROMPT, attempt_model_runtime=ToolRuntimeModelBinding())

    assert text.endswith("Finished with the new model.")
    # A member's tools run under the member's own model: its configured one first, the switched one after the rebuild.
    assert probe.active_models == ["general-model", "large"]
    assert len(team.models["large"].requests) == 4


@pytest.mark.asyncio
async def test_the_last_successful_switch_wins_over_a_later_rejected_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected sibling call cannot hide a successful switch in the same member batch."""
    team = _team(
        tmp_path,
        monkeypatch,
        default=[delegate("call-team", "general", "Switch"), answer("Switching.")],
        general=[
            calls(
                _switch("after-toolcall", call_id="call-switch"),
                _switch("after-toolcall", model_name="missing", call_id="call-rejected"),
            ),
        ],
        large=[answer("Finished with the successful model.")],
    )

    text = await team.respond(message=PROMPT, attempt_model_runtime=ToolRuntimeModelBinding())

    assert text.endswith("Finished with the successful model.")
    session = team.storage.load_session(SESSION_ID, "team")
    assert session is not None
    [member] = [run for run in session.runs if isinstance(run, RunRecord)]
    assert member.tools is not None
    assert [json.loads(str(tool.result))["status"] for tool in member.tools] == ["ok", "error"]


@pytest.mark.asyncio
async def test_repeated_switches_without_an_answer_end_with_the_limit_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = TURN_CONTINUATION_LIMIT + 1
    team = _team(
        tmp_path,
        monkeypatch,
        default=[step for _ in range(attempts) for step in (delegate("call-team", "general", "Switch"), answer(""))],
        general=[calls(_switch("next-turn")) for _ in range(attempts)],
    )

    text = await team.respond(message="Keep switching models.")

    assert len(team.models["default"].requests) == 2 * attempts
    assert "did not produce a final answer" in text
    assert team.recorder.outcome == "completed"
    assert "did not produce a final answer" in team.recorder.assistant_text


@pytest.mark.asyncio
async def test_a_stop_during_the_continuation_records_only_its_text_and_the_first_attempt_s_calls_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The spent attempt's text is gone from the interrupted turn; its finished calls are kept, once."""
    streaming = asyncio.Event()

    async def wait_to_be_stopped() -> None:
        streaming.set()
        await asyncio.Event().wait()

    team = _team(
        tmp_path,
        monkeypatch,
        default=[
            delegate("call-team", "general", "Switch"),
            answer("Switching."),
            [TextDelta("Half"), Await(wait_to_be_stopped)],
        ],
        general=[calls(_switch("next-turn"))],
    )
    task = asyncio.create_task(team.stream(message=PROMPT))
    await asyncio.wait_for(streaming.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    snapshot = team.recorder.interrupted_snapshot()
    assert "Switching." not in snapshot.partial_text
    assert snapshot.partial_text.endswith("Half")
    assert [tool.tool_name for tool in snapshot.completed_tools] == ["switch_thread_model", "delegate_task_to_member"]


def test_releasing_a_spent_attempt_s_members_drops_its_partial_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stop landing between the release and the next attempt records no text or calls of the spent attempt.

    Its finished calls already carried over through the turn state, so keeping them here would count them twice.
    No public entry can stop a turn inside that window deterministically, so this drives the private turn holder
    whose snapshot the stop reads.
    """
    team = _team(tmp_path, monkeypatch)
    members = materialize_team_members(["general"], team.orchestrator, team.identity)
    presentation = TeamStreamPresentation.new(["general"], ["GeneralAgent"], show_tool_calls=False)
    presentation.start_member_tool("general", ToolExecution(tool_call_id="call-1", tool_name="touch", tool_args={}))
    holder = _TeamTurnHolder(
        team_members=members,
        attempt=TeamAttempt(
            run_id="run-1",
            members=members,
            presentation=presentation,
            show_tool_calls=False,
            team_name="team",
            runtime_model_name="default",
            prepared_history=None,
            canonical_per_member={"general": "stale partial"},
        ),
    )
    assert holder.partial_snapshot().assistant_text

    holder.release_attempt_members(None)

    assert holder.team_members is None
    assert holder.partial_snapshot() == TurnPartialSnapshot()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_each_attempt_logs_the_prompt_it_sent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    stream: bool,
) -> None:
    """The request log of a continuation attempt names the continuation prompt, not the original message."""
    logged: list[object] = []

    async def log_prompt() -> None:
        logged.append(current_llm_request_log_context().get("current_turn_prompt"))

    team = _team(
        tmp_path,
        monkeypatch,
        default=[
            [Await(log_prompt), *delegate("call-team", "general", "Switch")],
            answer("Switching."),
            [Await(log_prompt), *answer("Finished the task.")],
        ],
        general=[calls(_switch("next-turn"))],
    )

    await team.reply(stream=stream, message=PROMPT)

    assert logged[0] == PROMPT
    assert CONTINUATION_MARK in str(logged[1])


def test_members_built_without_a_resumable_driver_leave_out_their_approval_gated_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller that cannot pause and resume a run, the default, builds members without the calls approval gates."""
    team = _team(tmp_path, monkeypatch)
    team.config.tool_approval = ToolApprovalConfig(rules=[ApprovalRuleConfig(match="touch", action="require_approval")])

    def tool_names(**kwargs: Any) -> set[str]:  # noqa: ANN401
        members = materialize_exact_team_members(
            ["general"],
            config=team.config,
            runtime_paths=team.runtime_paths,
            execution_identity=team.identity,
            **kwargs,
        )
        return set(members.builds[0].tool_owners)

    assert "touch" not in tool_names()
    assert {"touch", "scope"} <= tool_names(supports_native_tool_approval=True)
    assert "scope" in tool_names()
