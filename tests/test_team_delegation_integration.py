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

from mindroom.history.turn_recorder import TurnRecorder
from mindroom.team_exact_members import ResolvedExactTeamMembers
from mindroom.teams import TeamMode, _team_approval_events, team_response, team_response_stream
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.conftest import make_turn_context, runtime_paths_for
from tests.identity_helpers import entity_ids
from tests.test_delegation_execution import DelegationModel, _call
from tests.test_team_response import _build_test_config, _make_test_agent, _make_test_team

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


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
                members=members,
                refresh_scheduler=None,
                decisions={"prepare": True},
                denial_reasons={"prepare": None},
            )
        ]
    assert executed == ["prepared"]
    assert driven
    assert isinstance(events[-1], TeamRunOutput)
    assert events[-1].content == "Child completed"
