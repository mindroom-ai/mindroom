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

from mindroom import teams as teams_module
from mindroom.config.models import ModelConfig
from mindroom.dynamic_tool_continuation import DYNAMIC_TOOL_CONTINUATION_LIMIT
from mindroom.history.turn_recorder import TurnRecorder
from mindroom.knowledge.utils import _KnowledgeResolution
from mindroom.message_target import MessageTarget
from mindroom.room_model_overrides import set_room_model_override
from mindroom.team_exact_members import ResolvedExactTeamMembers
from mindroom.teams import (
    TeamMode,
    _build_team_runtime_db_callbacks,
    _materialize_team_members,
    _TeamTurnHolder,
    materialize_exact_team_members,
    team_response,
    team_response_stream,
)
from mindroom.tool_system.runtime_context import (
    ToolRuntimeContext,
    ToolRuntimeModelBinding,
    get_tool_runtime_context,
    tool_runtime_context,
)
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.conftest import make_turn_context, runtime_paths_for
from tests.identity_helpers import entity_ids
from tests.test_team_response import _build_test_config, _make_test_agent, _make_test_team

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


def _model_switch_execution(when: str, *, model_name: str = "large") -> ToolExecution:
    return ToolExecution(
        tool_call_id="call-switch-model",
        tool_name="switch_thread_model",
        tool_args={"model_name": model_name, "when": when},
        result=json.dumps(
            {
                "action": "switch",
                "model": model_name,
                "status": "ok",
                "tool": "thread_model",
                "when": when,
            },
        ),
        stop_after_tool_call=True,
    )


def _model_switch_team_output(when: str) -> TeamRunOutput:
    return TeamRunOutput(
        content="",
        member_responses=[RunOutput(agent_name="GeneralAgent", content="", tools=[_model_switch_execution(when)])],
    )


def _make_orchestrator() -> tuple[MagicMock, object]:
    config = _build_test_config()
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths_for(config)
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock()}
    return orchestrator, config


def _team_tool_context(orchestrator: MagicMock) -> ToolRuntimeContext:
    """Build an ambient team tool context that starts on the default model."""
    return ToolRuntimeContext(
        agent_name="test-team",
        target=MessageTarget.resolve("!test:localhost", None, "$user-message", room_mode=True),
        requester_id="@user:localhost",
        client=AsyncMock(),
        config=orchestrator.config,
        runtime_paths=orchestrator.runtime_paths,
        conversation_reader=MagicMock(),
        relations=MagicMock(),
        active_model_name="default",
    )


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
    with patches[0] as mock_create, patches[1], patches[2]:
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Load the sleep tool and use it.",
            turn_recorder=recorder,
            orchestrator=orchestrator,
            execution_identity=None,
            ctx=make_turn_context(run_id="run-1", session_id=None),
            run_id_callback=run_ids.append,
        )

    assert "Used the loaded tool." in response
    assert mock_team.arun.await_count == 2
    # The continuation rebuilds member agents so the loaded tool's schema is baked in.
    assert mock_create.call_count == 2
    second_prompt = mock_team.arun.await_args_list[1].args[0]
    assert "DYNAMIC TOOL CALL COMPLETED" in second_prompt[-1].content
    assert "Continue the same task" in second_prompt[-1].content
    assert run_ids[0] == "run-1"
    assert len(run_ids) == 2
    assert run_ids[1] != "run-1"
    assert recorder.outcome == "completed"
    assert "Used the loaded tool." in recorder.assistant_text
    # The turn-level trace still carries the first attempt's dynamic tool call.
    assert any(entry.tool_name == "load_tool" for entry in recorder.completed_tools)


@pytest.mark.asyncio
async def test_team_dynamic_continuation_pins_member_model_for_whole_turn() -> None:
    """A room-default change mid-turn must not switch rebuilt team members."""
    orchestrator, config = _make_orchestrator()
    config.models["large"] = ModelConfig(provider="openai", id="large-model")
    runtime_paths = runtime_paths_for(config)
    room_id = "!room:localhost"
    set_room_model_override(
        runtime_paths,
        room_id=room_id,
        model_name="large",
        set_by="@admin:localhost",
    )
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@user:localhost",
        room_id=room_id,
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    attempt = 0

    async def run_team(_prompt: object, **_kwargs: object) -> TeamRunOutput:
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            set_room_model_override(
                runtime_paths,
                room_id=room_id,
                model_name="default",
                set_by="@admin:localhost",
            )
            return _dynamic_tool_team_output()
        return TeamRunOutput(content="Used the loaded tool.")

    mock_team = _make_test_team()
    mock_team.arun = AsyncMock(side_effect=run_team)
    fake_agent = _make_test_agent("GeneralAgent")
    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent) as mock_create,
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=_KnowledgeResolution(knowledge=None)),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
    ):
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Load the sleep tool and use it.",
            turn_recorder=TurnRecorder(user_message="Load the sleep tool and use it."),
            orchestrator=orchestrator,
            execution_identity=identity,
            ctx=make_turn_context(session_id=None, room_id=room_id),
        )

    assert "Used the loaded tool." in response
    assert [call.kwargs["active_model_name"] for call in mock_create.call_args_list] == ["large", "large"]


@pytest.mark.asyncio
async def test_team_after_toolcall_switch_rebuilds_members_with_new_model() -> None:
    """Keeping the initial member snapshot would make team model switching ineffective until the next turn."""
    orchestrator, config = _make_orchestrator()
    config.models["large"] = ModelConfig(provider="openai", id="large-model")
    observed_tool_models: list[str | None] = []
    responses = iter(
        [
            _model_switch_team_output("after-toolcall"),
            TeamRunOutput(content="Finished with the new model."),
        ],
    )

    async def run_team(*_args: object, **_kwargs: object) -> TeamRunOutput:
        context = get_tool_runtime_context()
        observed_tool_models.append(context.active_model_name if context is not None else None)
        return next(responses)

    mock_team = _make_test_team()
    mock_team.arun = AsyncMock(side_effect=run_team)
    fake_agent = _make_test_agent("GeneralAgent")

    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent) as mock_create,
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=_KnowledgeResolution(knowledge=None)),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        tool_runtime_context(_team_tool_context(orchestrator)),
    ):
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Switch and finish the task.",
            turn_recorder=TurnRecorder(user_message="Switch and finish the task."),
            orchestrator=orchestrator,
            execution_identity=None,
            ctx=make_turn_context(session_id=None),
            attempt_model_runtime=ToolRuntimeModelBinding(),
        )

    assert "Finished with the new model." in response
    assert [call.kwargs["active_model_name"] for call in mock_create.call_args_list] == ["default", "large"]
    assert observed_tool_models == ["default", "large"]


@pytest.mark.asyncio
async def test_streaming_team_after_toolcall_switch_rebuilds_members_with_new_model() -> None:
    """Streaming teams must apply the same immediate member-model transition as blocking teams."""
    orchestrator, config = _make_orchestrator()
    config.models["large"] = ModelConfig(provider="openai", id="large-model")
    observed_tool_models: list[str | None] = []

    async def first_stream() -> AsyncIterator[object]:
        context = get_tool_runtime_context()
        observed_tool_models.append(context.active_model_name if context is not None else None)
        yield _model_switch_team_output("after-toolcall")

    async def second_stream() -> AsyncIterator[object]:
        context = get_tool_runtime_context()
        observed_tool_models.append(context.active_model_name if context is not None else None)
        yield TeamRunOutput(content="Finished with the new model.")

    mock_team = _make_test_team()
    mock_team.arun = MagicMock(side_effect=[first_stream(), second_stream()])
    fake_agent = _make_test_agent("GeneralAgent")

    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent) as mock_create,
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=_KnowledgeResolution(knowledge=None)),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        tool_runtime_context(_team_tool_context(orchestrator)),
    ):
        chunks = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=[entity_ids(config, runtime_paths_for(config))["general"]],
                mode=TeamMode.COORDINATE,
                message="Switch and finish the task.",
                turn_recorder=TurnRecorder(user_message="Switch and finish the task."),
                orchestrator=orchestrator,
                execution_identity=None,
                ctx=make_turn_context(session_id=None),
                attempt_model_runtime=ToolRuntimeModelBinding(),
            )
        ]

    assert chunks
    assert [call.kwargs["active_model_name"] for call in mock_create.call_args_list] == ["default", "large"]
    assert observed_tool_models == ["default", "large"]


@pytest.mark.asyncio
async def test_team_uses_latest_successful_nested_switch_when_later_call_is_rejected() -> None:
    """A failed sibling call cannot hide a successful switch nested in one member response."""
    orchestrator, config = _make_orchestrator()
    config.models["large"] = ModelConfig(provider="openai", id="large-model")
    rejected = ToolExecution(
        tool_call_id="call-rejected-switch",
        tool_name="switch_thread_model",
        tool_args={"model_name": "missing", "when": "after-toolcall"},
        result=json.dumps(
            {
                "action": "switch",
                "message": "Unknown model 'missing'.",
                "status": "error",
                "tool": "thread_model",
            },
        ),
        stop_after_tool_call=True,
    )
    first_response = TeamRunOutput(
        content="",
        member_responses=[
            RunOutput(
                agent_name="GeneralAgent",
                content="",
                tools=[_model_switch_execution("after-toolcall"), rejected],
            ),
        ],
    )
    mock_team = _make_test_team()
    mock_team.arun = AsyncMock(
        side_effect=[first_response, TeamRunOutput(content="Finished with the successful model.")],
    )
    fake_agent = _make_test_agent("GeneralAgent")

    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent) as mock_create,
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=_KnowledgeResolution(knowledge=None)),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
    ):
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Switch and finish the task.",
            turn_recorder=TurnRecorder(user_message="Switch and finish the task."),
            orchestrator=orchestrator,
            execution_identity=None,
            ctx=make_turn_context(session_id=None),
            attempt_model_runtime=ToolRuntimeModelBinding(),
        )

    assert "Finished with the successful model." in response
    assert [call.kwargs["active_model_name"] for call in mock_create.call_args_list] == ["default", "large"]


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
            ctx=make_turn_context(session_id=None),
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
    with patches[0] as mock_create, patches[1], patches[2]:
        chunks = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=[entity_ids(config, runtime_paths_for(config))["general"]],
                mode=TeamMode.COORDINATE,
                message="Load the sleep tool and use it.",
                turn_recorder=recorder,
                orchestrator=orchestrator,
                execution_identity=None,
                ctx=make_turn_context(session_id=None),
            )
        ]

    assert mock_team.arun.call_count == 2
    # The streamed continuation rebuilds member agents like the blocking path.
    assert mock_create.call_count == 2
    second_prompt = mock_team.arun.call_args_list[1].args[0]
    assert "DYNAMIC TOOL CALL COMPLETED" in second_prompt[-1].content
    rendered = "".join(chunk.content if hasattr(chunk, "content") else str(chunk) for chunk in chunks)
    assert "Used the loaded tool." in rendered
    # The superseded first attempt's fallback document must not leak.
    assert "No team response generated." not in rendered
    assert recorder.outcome == "completed"
    assert "Used the loaded tool." in recorder.assistant_text


@pytest.mark.asyncio
async def test_team_response_stream_continues_after_streamed_member_dynamic_tool() -> None:
    """A streamed member dynamic-tool completion (no terminal output) continues the turn."""
    orchestrator, config = _make_orchestrator()

    async def first_stream() -> AsyncIterator[object]:
        yield AgentRunContentEvent(agent_id="general", agent_name="GeneralAgent", content="Loading the tool.")
        yield AgentToolCallCompletedEvent(
            agent_id="general",
            agent_name="GeneralAgent",
            tool=_load_tool_execution(),
        )

    async def second_stream() -> AsyncIterator[object]:
        yield AgentRunContentEvent(agent_id="general", agent_name="GeneralAgent", content="Used the loaded tool.")

    mock_team = _make_test_team()
    mock_team.arun = MagicMock(side_effect=[first_stream(), second_stream()])
    recorder = TurnRecorder(user_message="Load the sleep tool and use it.")
    first_agent = _make_test_agent("GeneralAgent")
    second_agent = _make_test_agent("GeneralAgent")

    with (
        patch("mindroom.teams.create_agent", side_effect=[first_agent, second_agent]) as mock_create,
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=_KnowledgeResolution(knowledge=None)),
        patch("mindroom.teams._create_team_instance", return_value=mock_team) as mock_instance,
    ):
        chunks = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=[entity_ids(config, runtime_paths_for(config))["general"]],
                mode=TeamMode.COORDINATE,
                message="Load the sleep tool and use it.",
                turn_recorder=recorder,
                orchestrator=orchestrator,
                execution_identity=None,
                ctx=make_turn_context(session_id=None),
            )
        ]

    assert mock_team.arun.call_count == 2
    # The second attempt's Team instance is built from the rebuilt members,
    # not the spent first-attempt agents.
    assert mock_create.call_count == 2
    assert mock_instance.call_args_list[0].kwargs["agents"] == [first_agent]
    assert mock_instance.call_args_list[1].kwargs["agents"] == [second_agent]
    second_prompt = mock_team.arun.call_args_list[1].args[0]
    assert "DYNAMIC TOOL CALL COMPLETED" in second_prompt[-1].content
    rendered = "".join(chunk.content if hasattr(chunk, "content") else str(chunk) for chunk in chunks)
    assert "Used the loaded tool." in rendered
    assert recorder.outcome == "completed"
    assert "Used the loaded tool." in recorder.assistant_text
    assert any(entry.tool_name == "load_tool" for entry in recorder.completed_tools)


@pytest.mark.asyncio
async def test_team_response_stream_continues_from_hidden_member_dynamic_tool() -> None:
    """A tools-only attempt with hidden tool calls still continues the turn."""
    orchestrator, config = _make_orchestrator()

    async def first_stream() -> AsyncIterator[object]:
        yield AgentToolCallCompletedEvent(
            agent_id="general",
            agent_name="GeneralAgent",
            tool=_load_tool_execution(),
        )

    async def second_stream() -> AsyncIterator[object]:
        yield AgentRunContentEvent(agent_id="general", agent_name="GeneralAgent", content="Used the loaded tool.")

    mock_team = _make_test_team()
    mock_team.arun = MagicMock(side_effect=[first_stream(), second_stream()])
    recorder = TurnRecorder(user_message="Load the sleep tool and use it.")

    patches = _team_patches(mock_team)
    with patches[0] as mock_create, patches[1], patches[2]:
        chunks = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=[entity_ids(config, runtime_paths_for(config))["general"]],
                mode=TeamMode.COORDINATE,
                message="Load the sleep tool and use it.",
                turn_recorder=recorder,
                orchestrator=orchestrator,
                execution_identity=None,
                ctx=make_turn_context(session_id=None),
                show_tool_calls=False,
            )
        ]

    assert mock_team.arun.call_count == 2
    assert mock_create.call_count == 2
    rendered = "".join(chunk.content if hasattr(chunk, "content") else str(chunk) for chunk in chunks)
    assert "Used the loaded tool." in rendered
    assert recorder.outcome == "completed"


def test_release_attempt_members_resets_snapshot_state() -> None:
    """Releasing spent members clears the partial-snapshot sources on the holder.

    A cancel landing between the release and the next attempt must snapshot
    empty partials instead of the spent attempt's text and tools (which would
    be double-counted via the turn state's prior completed tools).
    """
    holder = _TeamTurnHolder(
        team=None,
        team_members=ResolvedExactTeamMembers(
            requested_agent_names=["general"],
            agents=[],
            display_names=["GeneralAgent"],
            materialized_agent_names={"general"},
            failed_agent_names=[],
        ),
        render_partial=lambda: "stale partial",
    )
    stale_tracker = holder.tool_tracker
    release, _close = _build_team_runtime_db_callbacks(holder)

    release(None)

    assert holder.team_members is None
    assert holder.render_partial() == ""
    assert holder.tool_tracker is not stale_tracker


def test_materialize_exact_team_members_defaults_to_no_continuation() -> None:
    """Callers without a continuation-capable driver keep load/unload non-truncating."""
    config = _build_test_config()
    fake_agent = _make_test_agent("GeneralAgent")
    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent) as mock_create,
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=_KnowledgeResolution(knowledge=None)),
    ):
        materialize_exact_team_members(
            ["general"],
            config=config,
            runtime_paths=runtime_paths_for(config),
            execution_identity=None,
            session_id="session-1",
        )

    assert mock_create.call_args.kwargs["dynamic_tool_continuation"] is False
    assert mock_create.call_args.kwargs["supports_native_tool_approval"] is False


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
    assert mock_create.call_args.kwargs["supports_native_tool_approval"] is True


@pytest.mark.asyncio
async def test_team_response_logs_continuation_prompt_in_request_log_context() -> None:
    """Continuation attempts log the active continuation prompt, not the original message."""
    orchestrator, _config = _make_orchestrator()
    mock_team = _make_test_team()
    mock_team.arun = AsyncMock(
        side_effect=[
            _dynamic_tool_team_output(),
            TeamRunOutput(content="Used the loaded tool."),
        ],
    )
    logged_prompts: list[object] = []
    real_log_context = teams_module._team_request_log_context

    def capture_log_context(ctx: object, **kwargs: object) -> dict[str, object]:
        logged_prompts.append(kwargs.get("prompt"))
        return real_log_context(ctx, **kwargs)

    patches = _team_patches(mock_team)
    with (
        patches[0],
        patches[1],
        patches[2],
        patch("mindroom.teams._team_request_log_context", side_effect=capture_log_context),
    ):
        await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Load the sleep tool and use it.",
            turn_recorder=TurnRecorder(user_message="Load the sleep tool and use it."),
            orchestrator=orchestrator,
            execution_identity=None,
            ctx=make_turn_context(run_id="run-1", session_id=None),
        )

    assert logged_prompts[0] == "Load the sleep tool and use it."
    assert "DYNAMIC TOOL CALL COMPLETED" in str(logged_prompts[1])


@pytest.mark.asyncio
async def test_team_response_stream_logs_continuation_prompt_in_request_log_context() -> None:
    """Streamed continuation attempts log the active continuation prompt (agent-path parity)."""
    orchestrator, config = _make_orchestrator()

    async def first_stream() -> AsyncIterator[object]:
        yield _dynamic_tool_team_output()

    async def second_stream() -> AsyncIterator[object]:
        yield TeamRunOutput(content="Used the loaded tool.")

    mock_team = _make_test_team()
    mock_team.arun = MagicMock(side_effect=[first_stream(), second_stream()])
    logged_prompts: list[object] = []
    real_log_context = teams_module._team_request_log_context

    def capture_log_context(ctx: object, **kwargs: object) -> dict[str, object]:
        logged_prompts.append(kwargs.get("prompt"))
        return real_log_context(ctx, **kwargs)

    patches = _team_patches(mock_team)
    with (
        patches[0],
        patches[1],
        patches[2],
        patch("mindroom.teams._team_request_log_context", side_effect=capture_log_context),
    ):
        _ = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=[entity_ids(config, runtime_paths_for(config))["general"]],
                mode=TeamMode.COORDINATE,
                message="Load the sleep tool and use it.",
                turn_recorder=TurnRecorder(user_message="Load the sleep tool and use it."),
                orchestrator=orchestrator,
                execution_identity=None,
                ctx=make_turn_context(session_id=None),
            )
        ]

    assert logged_prompts[0] == "Load the sleep tool and use it."
    assert "DYNAMIC TOOL CALL COMPLETED" in str(logged_prompts[1])
