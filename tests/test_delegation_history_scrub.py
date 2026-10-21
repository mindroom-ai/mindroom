"""A delegating parent run is stored without the history Agno replayed into it."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.models.message import Message
from agno.models.response import ModelResponse
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.team import TeamRunOutput
from agno.session.team import TeamSession
from agno.team import Team

from mindroom.agents import apply_tool_approval_capability, build_agent_toolkit
from mindroom.ai import run_delegated_child_response
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig
from mindroom.delegation.execution import drive_delegations, persist_delegation_state
from mindroom.delegation.state import DELEGATION_STATE_KEY, DelegationState
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.access_schema_support import with_responder_access
from tests.delegation_helpers import DelegationModel, _call, _delegate_runtime_context, _runtime_paths
from tests.identity_helpers import entity_ids

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["child_runs", "policy_denied"])
async def test_delegating_run_is_stored_without_replayed_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    """store_history_messages=False must hold for runs the delegation driver finishes."""
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
        config = with_responder_access(config, "child", users=["@alice:example.org"])
    entity_ids(config, paths)
    identity = ToolExecutionIdentity("matrix", "leader", "@alice:example.org", "!room:example.org", None, None, "s1")
    child_model = DelegationModel(id="child", responses=[ModelResponse(content="Child report")])
    monkeypatch.setattr("mindroom.agents._load_agent_model_instance", lambda *_args: child_model)
    toolkit = build_agent_toolkit(
        "delegate",
        agent_name="leader",
        config=config,
        runtime_paths=paths,
        worker_tools=[],
        runtime_overrides=None,
        execution_identity=identity,
    )
    assert toolkit is not None
    apply_tool_approval_capability(toolkit, config, supports_native_tool_approval=True, registered_tool_name="delegate")
    storage = create_session_storage("leader", config, paths, identity)
    parent = Agent(
        id="leader",
        name="leader",
        db=storage,
        tools=[toolkit],
        add_history_to_context=True,
        store_history_messages=False,
        model=DelegationModel(
            id="parent",
            responses=[
                ModelResponse(content="Hi there"),
                ModelResponse(tool_calls=[_call("run_subagent", "d1", agent_name="child", task="Write report")]),
                ModelResponse(content="Parent done"),
            ],
        ),
    )
    options = {"agent_name": "leader", "config": config, "runtime_paths": paths, "execution_identity": identity}
    try:
        with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=identity)):
            first = await parent.arun("Hello", session_id="s1", user_id=identity.requester_id)
            assert first.status == RunStatus.completed
            response = await parent.arun("Delegate", session_id="s1", user_id=identity.requester_id)
            assert response.status == RunStatus.paused
            live_history = [m for m in response.messages or [] if m.from_history]
            assert live_history, "precondition: Agno replayed the first turn into the live run"
            result = await drive_delegations(parent, response, run_child=run_delegated_child_response, **options)
            if outcome == "policy_denied":
                assert result.status == RunStatus.paused
                mid_row = storage.get_run(result.run_id)
                assert isinstance(mid_row, RunOutput)
                assert not any(m.from_history for m in mid_row.messages or [])
                result = await drive_delegations(
                    parent,
                    result,
                    run_child=run_delegated_child_response,
                    **options,
                    decisions={"d1": False},
                    denial_reasons={"d1": "No"},
                )
        assert result.status == RunStatus.completed
        stored = storage.get_run(result.run_id)
        assert isinstance(stored, RunOutput)
        assert stored.metadata is not None
        assert DELEGATION_STATE_KEY in stored.metadata
        stored_history = [m.content for m in stored.messages or [] if m.from_history]
        assert stored_history == []
        own_turn = [m.content for m in stored.messages or [] if m.role in {"user", "assistant"}]
        assert own_turn[0] == "Delegate"
        assert own_turn[-1] == "Parent done"
        resumed_history = [m.content for m in parent.model.seen_messages if m.from_history]
        assert resumed_history == ["Hello", "Hi there"], "the resumed request still replays prior turns"
    finally:
        storage.close()


@pytest.mark.asyncio
async def test_delegating_team_run_is_stored_without_replayed_history(tmp_path: Path) -> None:
    """A team parent's row gets the team's scrub; its own turn and member runs stay, and the live run is untouched."""
    db = SqliteDb(db_file=str(tmp_path / "team.db"))
    team = Team(
        id="team",
        name="team",
        members=[Agent(id="member", name="member")],
        db=db,
        store_history_messages=False,
    )
    member_run = RunOutput(
        run_id="member-run",
        agent_id="member",
        session_id="s1",
        parent_run_id="team-run",
        status=RunStatus.completed,
        messages=[Message(role="user", content="member task")],
    )
    response = TeamRunOutput(
        run_id="team-run",
        team_id="team",
        session_id="s1",
        user_id="@alice:example.org",
        status=RunStatus.paused,
        messages=[
            Message(role="user", content="Hello", from_history=True),
            Message(role="assistant", content="Hi there", from_history=True),
            Message(role="user", content="Delegate"),
        ],
        member_responses=[member_run],
    )
    db.upsert_session(TeamSession(session_id="s1", team_id="team", user_id="@alice:example.org"))

    await persist_delegation_state(team, response, DelegationState())

    stored = db.get_run("team-run")
    assert isinstance(stored, TeamRunOutput)
    assert [m.content for m in stored.messages or []] == ["Delegate"]
    assert [m.content for m in stored.member_responses[0].messages or []] == ["member task"]
    assert [m.content for m in response.messages or [] if m.from_history] == ["Hello", "Hi there"]
