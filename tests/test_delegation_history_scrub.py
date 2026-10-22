"""A delegating parent run, an agent's or a team's, is stored without the history replayed into its request."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.approval import ApprovalRuleConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig
from mindroom.delegation.state import DELEGATION_STATE_KEY, DelegationState
from mindroom.llm.runs import RunRecord, RunStatus, TeamRunRecord
from mindroom.response_turn import CompletedApprovalRun, PausedAttempt, ResponsePausedForApproval
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.access_schema_support import with_responder_access
from tests.delegation_helpers import (
    ScriptedAgents,
    _runtime_paths,
    _saved_approval_calls,
    approval_calls_for,
    approval_executor,
    respond,
    resume,
)
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call
from tests.identity_helpers import entity_ids
from tests.team_turn_helpers import SESSION_ID, TeamTurn, delegate, squad_probe, squad_turn  # noqa: F401 - fixture

if TYPE_CHECKING:
    from pathlib import Path

REQUESTER = "@alice:example.org"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["child_runs", "policy_denied"])
async def test_delegating_run_is_stored_without_replayed_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    """The stored parent row keeps its own turn only, also when the delegation driver finishes it after a pause."""
    paths = _runtime_paths(tmp_path)
    config = Config(
        agents={
            "leader": AgentConfig(display_name="Leader", delegate_to=["child"]),
            "child": AgentConfig(display_name="Child"),
        },
        defaults=DefaultsConfig(tools=[]),
        memory={"backend": "none"},
        tool_approval={"default": "require_approval"} if outcome == "policy_denied" else {},
    )
    if outcome == "policy_denied":
        config = with_responder_access(config, "child", users=[REQUESTER])
    entity_ids(config, paths)
    identity = ToolExecutionIdentity("matrix", "leader", REQUESTER, "!room:example.org", None, None, "s1")
    leader = ScriptedModel(
        [
            answer("Hi there"),
            calls(tool_call("run_subagent", "d1", agent_name="child", task="Write report")),
            answer("Parent done"),
        ],
    )
    ScriptedAgents({"leader": leader, "child": ScriptedModel([answer("Child report")])}).install(monkeypatch)
    storage = create_session_storage("leader", config, paths, identity)
    options = {"config": config, "runtime_paths": paths, "identity": identity}

    first = await respond("leader", "Hello", run_id="run-1", **options)
    assert first == "Hi there"
    result = await respond("leader", "Delegate", run_id="run-2", **options)
    if outcome == "policy_denied":
        assert isinstance(result, PausedAttempt)
        mid_row = storage.get_run("run-2")
        assert isinstance(mid_row, RunRecord)
        assert mid_row.status is RunStatus.paused
        assert not any(message.from_history for message in mid_row.messages)
        [call] = approval_calls_for(result, default_agent="leader", toolkit_names={"run_subagent": "delegate"})
        result = await resume(
            approval_executor(config, paths),
            result,
            (call,),
            agent_name="leader",
            decisions={call.tool_call_id: False},
            denial_reasons={call.tool_call_id: "No"},
            **options,
        )
        assert isinstance(result, CompletedApprovalRun)

    stored = storage.get_run("run-2")
    assert isinstance(stored, RunRecord)
    assert stored.status is RunStatus.completed
    assert stored.metadata is not None
    assert DELEGATION_STATE_KEY in stored.metadata
    assert [message.content for message in stored.messages if message.from_history] == []
    own_turn = [message.content for message in stored.messages if message.role in {"user", "assistant"}]
    assert own_turn[-1] == "Parent done"
    assert any(message.role == "user" and "Delegate" in str(message.content) for message in stored.messages)
    # The request that delegated, and the one that resumed, both replayed the earlier turn.
    for request in leader.requests[1:]:
        replayed = [message.content for message in request.messages if message.from_history]
        assert replayed == ["Hello", "Hi there"] or any("Hello" in str(item) for item in replayed), replayed


def _team_rows(team: TeamTurn, run_id: str) -> tuple[TeamRunRecord, list[RunRecord]]:
    """Return the team's stored row of ``run_id`` and its members' rows."""
    session = team.storage.load_session(SESSION_ID, "team")
    assert session is not None
    [record] = [run for run in session.runs if isinstance(run, TeamRunRecord) and run.run_id == run_id]
    return record, [run for run in session.runs if isinstance(run, RunRecord) and run.parent_run_id == run_id]


@pytest.mark.asyncio
@pytest.mark.usefixtures("squad_probe")
@pytest.mark.parametrize("outcome", ["child_runs", "policy_denied"])
async def test_delegating_team_run_is_stored_without_replayed_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    """A team whose member delegates stores its own turn only, also when the delegation driver finishes it after a pause."""
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[answer("Hi there"), delegate("call-team", "writer", "Write report"), answer("Parent done")],
        writer=[
            calls(tool_call("run_subagent", "d1", agent_name="helper", task="Write report")),
            answer("Writer done"),
        ],
        helper=[answer("Child report")],
        approval_rules=[ApprovalRuleConfig(match="run_subagent", action="require_approval")]
        if outcome == "policy_denied"
        else (),
    )

    await team.respond(message="Hello")
    delegating = team.ctx(run_id="run-2")
    if outcome == "policy_denied":
        with pytest.raises(ResponsePausedForApproval) as raised:
            await team.respond(message="Delegate", ctx=delegating)
        mid_row, mid_members = _team_rows(team, "run-2")
        assert mid_row.status is RunStatus.paused
        assert [member.agent_id for member in mid_members] == ["writer"]
        for row in (mid_row, *mid_row.member_responses, *mid_members):
            assert not any(message.from_history for message in row.messages or [])
        result = await team.resume(
            raised.value.paused,
            approved=False,
            approvals=_saved_approval_calls(DelegationState.from_metadata(mid_row.metadata)),
        )
        assert isinstance(result, CompletedApprovalRun)
    else:
        await team.respond(message="Delegate", ctx=delegating)

    stored, members = _team_rows(team, "run-2")
    assert stored.status is RunStatus.completed
    assert stored.metadata is not None
    assert DELEGATION_STATE_KEY in stored.metadata
    assert [member.agent_id for member in members] == ["writer"]
    for row in (stored, *stored.member_responses, *members):
        assert [message.content for message in row.messages or [] if message.from_history] == []
    own_turn = [message.content for message in stored.messages or [] if message.role in {"user", "assistant"}]
    assert "Delegate" in str(own_turn[0])
    assert own_turn[-1] == "Parent done"
    # The leader's request that delegated, and the one after its member's delegation, both replayed the earlier turn.
    for request in team.models["default"].requests[1:]:
        replayed = [message for message in request.messages if message.from_history]
        assert [message.role for message in replayed] == ["user", "assistant"]
        assert "Hello" in str(replayed[0].content)
        assert replayed[1].content == "Hi there"
