"""Tests for dynamic-tool same-turn continuation on the team paths."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agno.models.response import ToolExecution
from agno.run.agent import RunContentEvent as AgentRunContentEvent
from agno.run.agent import RunOutput
from agno.run.agent import ToolCallCompletedEvent as AgentToolCallCompletedEvent
from agno.run.team import TeamRunOutput

from mindroom.dynamic_tool_continuation import DYNAMIC_TOOL_CONTINUATION_LIMIT
from mindroom.history.turn_recorder import TurnRecorder
from mindroom.knowledge.utils import _KnowledgeResolution
from mindroom.teams import TeamMode, _materialize_team_members, team_response, team_response_stream
from tests.conftest import runtime_paths_for
from tests.identity_helpers import entity_ids
from tests.test_team_media_fallback import _build_test_config, _make_test_agent, _make_test_team

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from contextlib import AbstractContextManager

    from agno.team import Team as AgnoTeam


def _load_tool_execution(tool_name: str = "sleep") -> ToolExecution:
    return ToolExecution(
        tool_call_id="call-load",
        tool_name="load_tool",
        tool_args={"tool_name": tool_name},
        result=json.dumps({"status": "loaded", "tool": "dynamic_tools", "tool_name": tool_name}),
        stop_after_tool_call=True,
    )


def _dynamic_tool_team_output() -> TeamRunOutput:
    return TeamRunOutput(
        content="",
        member_responses=[RunOutput(agent_name="GeneralAgent", content="", tools=[_load_tool_execution()])],
    )


def _make_orchestrator() -> tuple[MagicMock, object]:
    config = _build_test_config()
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths_for(config)
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock()}
    return orchestrator, config


def _team_patches(mock_team: AgnoTeam) -> list[AbstractContextManager[object]]:
    fake_agent = _make_test_agent("GeneralAgent")
    return [
        patch("mindroom.teams.create_agent", return_value=fake_agent),
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=_KnowledgeResolution(knowledge=None)),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
    ]


@pytest.mark.asyncio
async def test_team_response_continues_after_member_dynamic_tool_load() -> None:
    """A member dynamic-tool load should rerun the team turn with a continuation prompt."""
    orchestrator, _config = _make_orchestrator()
    mock_team = _make_test_team()
    mock_team.arun = AsyncMock(
        side_effect=[
            _dynamic_tool_team_output(),
            TeamRunOutput(content="Used the loaded tool."),
        ],
    )
    recorder = TurnRecorder(user_message="Load the sleep tool and use it.")
    run_ids: list[str] = []

    patches = _team_patches(mock_team)
    with patches[0], patches[1], patches[2]:
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Load the sleep tool and use it.",
            turn_recorder=recorder,
            orchestrator=orchestrator,
            execution_identity=None,
            run_id="run-1",
            run_id_callback=run_ids.append,
        )

    assert "Used the loaded tool." in response
    assert mock_team.arun.await_count == 2
    second_prompt = mock_team.arun.await_args_list[1].args[0]
    assert "DYNAMIC TOOL CALL COMPLETED" in second_prompt
    assert "Continue the same task" in second_prompt
    assert run_ids[0] == "run-1"
    assert len(run_ids) == 2
    assert run_ids[1] != "run-1"
    assert recorder.outcome == "completed"
    assert "Used the loaded tool." in recorder.assistant_text
    # The turn-level trace still carries the first attempt's dynamic tool call.
    assert any(entry.tool_name == "load_tool" for entry in recorder.completed_tools)


@pytest.mark.asyncio
async def test_team_response_returns_limit_message_after_dynamic_tool_limit() -> None:
    """Repeated dynamic-tool calls without an answer should surface the limit message."""
    orchestrator, _config = _make_orchestrator()
    mock_team = _make_test_team()
    mock_team.arun = AsyncMock(
        side_effect=[_dynamic_tool_team_output() for _ in range(DYNAMIC_TOOL_CONTINUATION_LIMIT + 1)],
    )
    recorder = TurnRecorder(user_message="Keep loading tools.")

    patches = _team_patches(mock_team)
    with patches[0], patches[1], patches[2]:
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Keep loading tools.",
            turn_recorder=recorder,
            orchestrator=orchestrator,
            execution_identity=None,
        )

    assert mock_team.arun.await_count == DYNAMIC_TOOL_CONTINUATION_LIMIT + 1
    assert "did not produce a final answer" in response
    assert recorder.outcome == "completed"
    assert "did not produce a final answer" in recorder.assistant_text


@pytest.mark.asyncio
async def test_team_response_stream_continues_after_terminal_dynamic_tool_output() -> None:
    """A terminal fallback output carrying a member dynamic-tool load continues the turn."""
    orchestrator, config = _make_orchestrator()

    async def first_stream() -> AsyncIterator[object]:
        yield _dynamic_tool_team_output()

    async def second_stream() -> AsyncIterator[object]:
        yield TeamRunOutput(content="Used the loaded tool.")

    mock_team = _make_test_team()
    mock_team.arun = MagicMock(side_effect=[first_stream(), second_stream()])
    recorder = TurnRecorder(user_message="Load the sleep tool and use it.")

    patches = _team_patches(mock_team)
    with patches[0], patches[1], patches[2]:
        chunks = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=[entity_ids(config, runtime_paths_for(config))["general"]],
                mode=TeamMode.COORDINATE,
                message="Load the sleep tool and use it.",
                turn_recorder=recorder,
                orchestrator=orchestrator,
                execution_identity=None,
            )
        ]

    assert mock_team.arun.call_count == 2
    second_prompt = mock_team.arun.call_args_list[1].args[0]
    assert "DYNAMIC TOOL CALL COMPLETED" in second_prompt
    rendered = "".join(chunk.content if hasattr(chunk, "content") else str(chunk) for chunk in chunks)
    assert "Used the loaded tool." in rendered
    assert recorder.outcome == "completed"
    assert "Used the loaded tool." in recorder.assistant_text


@pytest.mark.asyncio
async def test_team_response_stream_continues_after_streamed_member_dynamic_tool() -> None:
    """A streamed member dynamic-tool completion (no terminal output) continues the turn."""
    orchestrator, config = _make_orchestrator()

    async def first_stream() -> AsyncIterator[object]:
        yield AgentRunContentEvent(agent_name="GeneralAgent", content="Loading the tool.")
        yield AgentToolCallCompletedEvent(agent_name="GeneralAgent", tool=_load_tool_execution())

    async def second_stream() -> AsyncIterator[object]:
        yield AgentRunContentEvent(agent_name="GeneralAgent", content="Used the loaded tool.")

    mock_team = _make_test_team()
    mock_team.arun = MagicMock(side_effect=[first_stream(), second_stream()])
    recorder = TurnRecorder(user_message="Load the sleep tool and use it.")

    patches = _team_patches(mock_team)
    with patches[0], patches[1], patches[2]:
        chunks = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=[entity_ids(config, runtime_paths_for(config))["general"]],
                mode=TeamMode.COORDINATE,
                message="Load the sleep tool and use it.",
                turn_recorder=recorder,
                orchestrator=orchestrator,
                execution_identity=None,
            )
        ]

    assert mock_team.arun.call_count == 2
    second_prompt = mock_team.arun.call_args_list[1].args[0]
    assert "DYNAMIC TOOL CALL COMPLETED" in second_prompt
    rendered = "".join(chunk.content if hasattr(chunk, "content") else str(chunk) for chunk in chunks)
    assert "Used the loaded tool." in rendered
    assert recorder.outcome == "completed"
    assert "Used the loaded tool." in recorder.assistant_text
    assert any(entry.tool_name == "load_tool" for entry in recorder.completed_tools)


def test_team_members_are_built_with_dynamic_tool_continuation() -> None:
    """Materialized team members opt into same-turn dynamic-tool continuation."""
    orchestrator, _config = _make_orchestrator()
    fake_agent = _make_test_agent("GeneralAgent")
    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent) as mock_create,
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=_KnowledgeResolution(knowledge=None)),
    ):
        _materialize_team_members(
            ["general"],
            orchestrator,
            None,
            session_id="session-1",
            unavailable_bases={},
            reason_prefix="Team request",
            configured_team_name=None,
        )

    assert mock_create.call_args.kwargs["dynamic_tool_continuation"] is True
