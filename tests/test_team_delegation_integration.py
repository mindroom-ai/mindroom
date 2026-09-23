"""Team response envelopes must drive durable child delegation before rendering."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agno.models.response import ModelResponse, ToolExecution
from agno.run.base import RunStatus
from agno.run.requirement import RunRequirement
from agno.run.team import RunPausedEvent as TeamRunPausedEvent
from agno.run.team import TeamRunOutput
from agno.team import Team
from agno.tools.function import Function

from mindroom.approval_response import identify_approval_tools, require_ordered_pause_presentation
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig
from mindroom.history.turn_recorder import TurnRecorder
from mindroom.response_turn import ResponsePausedForApproval, apply_local_approval_decisions
from mindroom.team_exact_members import ResolvedExactTeamMembers
from mindroom.teams import TeamMode, _team_approval_events, team_response, team_response_stream
from mindroom.tool_approval import evaluate_tool_approval
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.conftest import make_turn_context, runtime_paths_for
from tests.delegation_helpers import DelegationModel, _call, _delegate_runtime_context, _runtime_paths
from tests.identity_helpers import entity_ids
from tests.test_delegation_direct_audit import _identity
from tests.test_team_response import _build_test_config, _make_test_agent, _make_test_team

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


@pytest.mark.asyncio
@pytest.mark.parametrize("show_tool_calls", [True, False])
async def test_disabled_real_team_keeps_canonical_terminal_format(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    show_tool_calls: bool,
) -> None:
    """The ordinary streaming team keeps main's final member/consensus rendering."""
    config = Config(
        agents={"leader": AgentConfig(display_name="Leader", model="leader")},
        models={name: ModelConfig(provider="test", id=name) for name in ("default", "leader")},
        defaults=DefaultsConfig(tools=[], learning=False),
        memory={"backend": "none"},
    )
    paths = _runtime_paths(tmp_path)
    ids = entity_ids(config, paths)
    identity = _identity()
    models = {
        "default": DelegationModel(
            id="team",
            responses=[
                ModelResponse(
                    tool_calls=[_call("delegate_task_to_member", "member", member_id="leader", task="Report")],
                ),
                ModelResponse(content="Consensus answer."),
            ],
        ),
        "leader": DelegationModel(id="leader", responses=[ModelResponse(content="Member answer.")]),
    }
    monkeypatch.setattr(
        "mindroom.model_loading.get_model_instance",
        lambda _config, _paths, name, **_kwargs: models[name],
    )
    orchestrator = MagicMock(config=config, runtime_paths=paths, knowledge_refresh_scheduler=None)
    recorder = TurnRecorder(user_message="Report")
    with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=identity)):
        chunks = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=[ids["leader"]],
                message="Report",
                orchestrator=orchestrator,
                execution_identity=identity,
                ctx=make_turn_context(
                    session_id=identity.session_id,
                    room_id=identity.room_id,
                    thread_id=identity.resolved_thread_id,
                    requester_id=identity.requester_id,
                ),
                user_id=identity.requester_id,
                show_tool_calls=show_tool_calls,
                turn_recorder=recorder,
            )
        ]
    assert chunks[-1] == (
        "🤝 **Team Response** (Leader):\n\n**Leader**: Member answer.\n\n\n**Team Consensus**:\n\nConsensus answer."
    )
    assert "Member answer." in recorder.assistant_text
    assert "Consensus answer." in recorder.assistant_text


@pytest.mark.asyncio
@pytest.mark.parametrize("show_tool_calls", [True, False])
async def test_real_streaming_team_preserves_child_approval_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    show_tool_calls: bool,
) -> None:
    """Agent-sensitive policy must see the child when the real streaming team pauses."""
    policy = tmp_path / "approval.py"
    policy.write_text("def check(tool_name, arguments, agent_name):\n    return agent_name == 'child'\n")
    config = Config(
        agents={
            "leader": AgentConfig(display_name="Leader", model="leader", delegate_to=["child"]),
            "child": AgentConfig(
                display_name="Child",
                model="child",
                tools=["calculator"],
            ),
        },
        models={name: ModelConfig(provider="test", id=name) for name in ("default", "leader", "child")},
        defaults=DefaultsConfig(tools=[], learning=False),
        memory={"backend": "none"},
        tool_approval={"rules": [{"match": "add", "script": str(policy)}]},
    )
    paths = _runtime_paths(tmp_path)
    ids = entity_ids(config, paths)
    identity = _identity()
    models = {
        "default": DelegationModel(
            id="team",
            responses=[
                ModelResponse(
                    tool_calls=[_call("delegate_task_to_member", "member", member_id="leader", task="Delegate report")],
                ),
            ],
        ),
        "leader": DelegationModel(
            id="leader",
            responses=[ModelResponse(tool_calls=[_call("run_subagent", "delegate", agent_name="child", task="Add")])],
        ),
        "child": DelegationModel(
            id="child",
            responses=[ModelResponse(tool_calls=[_call("add", "sum", a=1, b=2)]), ModelResponse(content="Sum is 3")],
        ),
    }
    monkeypatch.setattr(
        "mindroom.model_loading.get_model_instance",
        lambda _config, _paths, name, **_kwargs: models[name],
    )
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = paths
    orchestrator.knowledge_refresh_scheduler = None
    orchestrator.agent_bots = {"leader": MagicMock(running=True)}
    with (
        tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=identity)),
        pytest.raises(ResponsePausedForApproval) as raised,
    ):
        async for _chunk in team_response_stream(
            agent_ids=[ids["leader"]],
            message="Ask the child to add",
            orchestrator=orchestrator,
            execution_identity=identity,
            ctx=make_turn_context(
                session_id=identity.session_id,
                room_id=identity.room_id,
                thread_id=identity.resolved_thread_id,
                requester_id=identity.requester_id,
            ),
            user_id=identity.requester_id,
            show_tool_calls=show_tool_calls,
            turn_recorder=TurnRecorder(user_message="Ask the child to add"),
        ):
            pass

    paused = raised.value.paused
    identified = identify_approval_tools(paused, default_agent_name="team")
    assert len(identified) == 1
    tool, _call_id, name, invoking_agent = identified[0]
    requires_approval, _timeout = await evaluate_tool_approval(
        config,
        paths,
        name,
        tool.tool_args or {},
        invoking_agent,
    )
    assert requires_approval is True
    assert invoking_agent == paused.approval_agent_name == "child"
    require_ordered_pause_presentation(paused, show_tool_calls=show_tool_calls)
    assert len(models["child"].responses) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_team_envelope_drives_delegation_before_rendering(streaming: bool) -> None:
    """A native parent wait must produce the driven result, not an unsupported-pause error."""
    config = _build_test_config()
    runtime_paths = runtime_paths_for(config)
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths
    orchestrator.knowledge_refresh_scheduler = None
    member = _make_test_agent("GeneralAgent")
    members = ResolvedExactTeamMembers(
        requested_agent_names=["general"],
        agents=[member],
        display_names=["GeneralAgent"],
        materialized_agent_names={"general"},
        failed_agent_names=[],
    )
    team = _make_test_team()
    tool = ToolExecution(
        tool_call_id="delegate-call",
        tool_name="run_subagent",
        tool_args={"agent_name": "general", "task": "Inspect this independently"},
        external_execution_required=True,
    )
    pending = TeamRunOutput(
        run_id="parent-run",
        session_id="parent-session",
        status=RunStatus.paused,
        tools=[tool],
        requirements=[RunRequirement(tool_execution=tool)],
    )
    completed = TeamRunOutput(
        run_id="parent-run",
        session_id="parent-session",
        status=RunStatus.completed,
        content="Independent inspection finished.",
    )
    raw_stream_drained = False

    async def raw_events() -> AsyncIterator[object]:
        nonlocal raw_stream_drained
        yield TeamRunPausedEvent(
            run_id="parent-run",
            session_id="parent-session",
            tools=[tool],
            requirements=pending.requirements,
        )
        yield pending
        raw_stream_drained = True

    async def drive(_entity: object, response: object, **kwargs: object) -> TeamRunOutput:
        assert _entity is team
        assert response is pending
        assert kwargs["config"] is config
        assert kwargs["runtime_paths"] == runtime_paths
        return completed

    async def drive_stream(
        entity: object,
        events: AsyncIterator[object],
        **kwargs: object,
    ) -> AsyncIterator[object]:
        terminal: object = None
        async for event in events:
            if isinstance(event, TeamRunOutput):
                terminal = event
        yield await drive(entity, terminal, **kwargs)

    team.arun = MagicMock(return_value=raw_events()) if streaming else AsyncMock(return_value=pending)
    driver_name = "drive_delegation_stream" if streaming else "drive_delegations"
    driver = drive_stream if streaming else drive
    with (
        patch("mindroom.teams._materialize_team_members", return_value=members),
        patch("mindroom.teams.build_materialized_team_instance", return_value=team),
        patch(f"mindroom.teams.{driver_name}", new=driver),
    ):
        if streaming:
            chunks = [
                str(chunk)
                async for chunk in team_response_stream(
                    agent_ids=[entity_ids(config, runtime_paths)["general"]],
                    message="Inspect this",
                    orchestrator=orchestrator,
                    execution_identity=None,
                    ctx=make_turn_context(session_id="parent-session"),
                    turn_recorder=TurnRecorder(user_message="Inspect this"),
                )
            ]
            response = "".join(chunks)
            assert raw_stream_drained
        else:
            response = await team_response(
                agent_names=["general"],
                mode=TeamMode.COORDINATE,
                message="Inspect this",
                orchestrator=orchestrator,
                execution_identity=None,
                ctx=make_turn_context(session_id="parent-session"),
                turn_recorder=TurnRecorder(user_message="Inspect this"),
            )

    assert "Independent inspection finished." in response


@pytest.mark.asyncio
async def test_team_can_first_delegate_after_ordinary_approval() -> None:
    """An ordinary approval may continue into a new external delegation wait."""
    executed: list[str] = []

    def prepare_report() -> str:
        executed.append("prepared")
        return "ready"

    def run_subagent(agent_name: str, task: str) -> str:
        del agent_name, task
        pytest.fail("Agno must leave the external delegation for MindRoom")

    external = Function.from_callable(run_subagent)
    external.external_execution = True
    approval = Function.from_callable(prepare_report)
    approval.requires_confirmation = True
    team = Team(
        name="research",
        members=[],
        tools=[approval, external],
        model=DelegationModel(
            id="test",
            responses=[
                ModelResponse(tool_calls=[_call("prepare_report", "prepare")]),
                ModelResponse(tool_calls=[_call("run_subagent", "delegate", agent_name="general", task="Inspect")]),
            ],
        ),
    )
    paused = await team.arun("Inspect", session_id="parent", user_id="@user:localhost")
    assert paused.status == RunStatus.paused
    assert executed == []
    driven = False

    async def drive_stream(
        entity: object,
        events: AsyncIterator[object],
        **kwargs: object,
    ) -> AsyncIterator[object]:
        nonlocal driven
        assert entity is team
        assert kwargs["decisions"] is None
        async for event in events:
            if isinstance(event, TeamRunOutput):
                assert event.status == RunStatus.paused
                assert any(
                    requirement.tool_execution is not None
                    and requirement.tool_execution.tool_name == "run_subagent"
                    and requirement.tool_execution.external_execution_required
                    for requirement in event.requirements or ()
                )
                driven = True
        yield TeamRunOutput(run_id=paused.run_id, session_id="parent", content="Child completed")

    config = _build_test_config()
    identity = ToolExecutionIdentity("matrix", "research", "@user:localhost", "!room:localhost", None, None, "parent")
    members = ResolvedExactTeamMembers([], [], [], set(), [])
    with patch("mindroom.teams.drive_delegation_stream", new=drive_stream):
        events = [
            event
            async for event in _team_approval_events(
                team,
                paused,
                configured_team_name="research",
                config=config,
                runtime_paths=runtime_paths_for(config),
                execution_identity=identity,
                user_id=identity.requester_id,
                members=members,
                refresh_scheduler=None,
                decisions={"prepare": True},
                denial_reasons={"prepare": None},
                approval_calls=(),
                requirements=apply_local_approval_decisions(
                    paused,
                    decisions={"prepare": True},
                    denial_reasons={"prepare": None},
                ),
            )
        ]
    assert executed == ["prepared"]
    assert driven
    assert isinstance(events[-1], TeamRunOutput)
    assert events[-1].content == "Child completed"
