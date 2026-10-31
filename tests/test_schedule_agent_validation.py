"""Tests for agent validation in schedule commands."""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import nio
import pytest

from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import RouterConfig
from mindroom.scheduling.errors import SchedulingError
from mindroom.scheduling.tasks import ScheduledWorkflow, SchedulingRuntime, schedule_task
from tests.authorization_helpers import (
    make_test_scheduling_runtime,
)
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_reader_mock,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.identity_helpers import entity_ids
from tests.scheduling_helpers import serve_full_room_state


def _runtime_bound_config(config: Config) -> Config:
    """Return a runtime-bound config for scheduling tests."""
    runtime_paths = test_runtime_paths(Path(tempfile.mkdtemp()))
    return bind_runtime_paths(config, runtime_paths)


def _test_room(config: Config, *agent_names: str) -> nio.MatrixRoom:
    """Return ``test_room`` with the named agents' Matrix accounts as its synced members."""
    ids = entity_ids(config, runtime_paths_for(config))
    room = nio.MatrixRoom("test_room", "@bot:localhost")
    for agent_name in agent_names:
        user_id = ids[agent_name].full_id
        room.users[user_id] = nio.RoomMember(
            user_id=user_id,
            display_name=user_id,
            avatar_url=None,
        )
    room.members_synced = True
    return room


def _allow_schedule_state_persistence(client: AsyncMock, room_id: str) -> None:
    serve_full_room_state(client, room_id)
    client.room_put_state = AsyncMock(return_value=nio.RoomPutStateResponse("$scheduled-state", room_id))


def _scheduling_runtime(
    *,
    client: AsyncMock,
    config: Config,
    room: nio.MatrixRoom,
) -> SchedulingRuntime:
    return make_test_scheduling_runtime(
        client=client,
        config=config,
        runtime_paths=runtime_paths_for(config),
        room=room,
        conversation_reader=make_conversation_reader_mock(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("thread_id", "scope"), [(None, "room"), ("$thread123", "thread")])
async def test_schedule_validates_agents_in_scope(thread_id: str | None, scope: str) -> None:
    """Test that schedule command rejects a mentioned agent not configured for the room or invited to the thread."""
    config = _runtime_bound_config(
        Config(
            agents={
                "assistant": AgentConfig(
                    display_name="Assistant",
                    role="General assistance",
                    rooms=["test_room"],
                ),
                "calculator": AgentConfig(
                    display_name="Calculator",
                    role="Math calculations",
                    rooms=[],
                ),
            },
            router=RouterConfig(model="default"),
        ),
    )
    client = AsyncMock()
    room = _test_room(config, "assistant")
    mock_workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="@calculator please calculate 2+2",
        description="Calculate something",
    )

    with patch("mindroom.scheduling.tasks._parse_workflow_schedule") as mock_parse:
        mock_parse.return_value = mock_workflow

        with pytest.raises(SchedulingError) as exc_info:
            await schedule_task(
                runtime=_scheduling_runtime(client=client, config=config, room=room),
                room_id="test_room",
                thread_id=thread_id,
                scheduled_by="@user:localhost",
                full_text="in 5 minutes ask calculator to calculate",
            )

        response = str(exc_info.value)
        assert response.startswith("Failed to schedule")
        calculator_matrix_id = entity_ids(config, runtime_paths_for(config))["calculator"].full_id
        assert calculator_matrix_id in response
        assert f"not available in this {scope}" in response


@pytest.mark.asyncio
async def test_schedule_allows_agents_in_room() -> None:
    """Test that schedule command allows agents that are in the room."""
    config = _runtime_bound_config(
        Config(
            agents={
                "assistant": AgentConfig(
                    display_name="Assistant",
                    role="General assistance",
                    rooms=["test_room"],
                ),
                "calculator": AgentConfig(
                    display_name="Calculator",
                    role="Math calculations",
                    rooms=["test_room"],  # Calculator is also in the room
                ),
            },
            router=RouterConfig(model="default"),
        ),
    )

    client = AsyncMock()
    _allow_schedule_state_persistence(client, "test_room")

    room = _test_room(config, "assistant", "calculator")

    # Mock the workflow parsing
    mock_workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="@calculator please calculate 2+2",
        description="Calculate something",
    )

    with patch("mindroom.scheduling.tasks._parse_workflow_schedule") as mock_parse:
        mock_parse.return_value = mock_workflow

        # Try to schedule in a thread where calculator is in the room
        response = await schedule_task(
            runtime=_scheduling_runtime(client=client, config=config, room=room),
            room_id="test_room",
            thread_id="$thread123",
            scheduled_by="@user:localhost",
            full_text="in 5 minutes ask calculator to calculate",
        )

        # Should succeed because calculator is in the room
        assert "\n**Task ID:** `" in response
        assert "✅ Scheduled" in response


@pytest.mark.asyncio
async def test_schedule_with_multiple_agents_validation() -> None:
    """Test validation when multiple agents are mentioned."""
    config = _runtime_bound_config(
        Config(
            agents={
                "assistant": AgentConfig(
                    display_name="Assistant",
                    role="General assistance",
                    rooms=["test_room"],
                ),
                "calculator": AgentConfig(
                    display_name="Calculator",
                    role="Math calculations",
                    rooms=[],  # Not in room
                ),
                "researcher": AgentConfig(
                    display_name="Researcher",
                    role="Research",
                    rooms=["test_room"],  # In room
                ),
            },
            router=RouterConfig(model="default"),
        ),
    )

    client = AsyncMock()

    room = _test_room(config, "assistant", "researcher")

    # Mock workflow with multiple agents
    mock_workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="@researcher find info and @calculator calculate it",
        description="Research and calculate",
    )

    with patch("mindroom.scheduling.tasks._parse_workflow_schedule") as mock_parse:
        mock_parse.return_value = mock_workflow

        with pytest.raises(SchedulingError) as exc_info:
            await schedule_task(
                runtime=_scheduling_runtime(client=client, config=config, room=room),
                room_id="test_room",
                thread_id=None,
                scheduled_by="@user:localhost",
                full_text="in 5 minutes research and calculate",
            )

        # Should fail because calculator is not in room
        response = str(exc_info.value)
        assert response.startswith("Failed to schedule")
        # The response will contain the full Matrix ID
        calculator_matrix_id = entity_ids(config, runtime_paths_for(config))["calculator"].full_id
        assert calculator_matrix_id in response
        # Researcher should not be mentioned as invalid
        researcher_matrix_id = entity_ids(config, runtime_paths_for(config))["researcher"].full_id
        assert researcher_matrix_id not in response


@pytest.mark.asyncio
async def test_schedule_with_no_agent_mentions() -> None:
    """new_thread schedules without mentions should use room-scope agents and skip thread history."""
    config = _runtime_bound_config(
        Config(
            agents={
                "assistant": AgentConfig(
                    display_name="Assistant",
                    role="General assistance",
                    rooms=["test_room"],
                ),
                "researcher": AgentConfig(
                    display_name="Researcher",
                    role="Research support",
                    rooms=["test_room"],
                ),
            },
            router=RouterConfig(model="default"),
        ),
    )

    client = AsyncMock()
    _allow_schedule_state_persistence(client, "test_room")

    room = _test_room(config, "assistant", "researcher")

    # Mock workflow without any agent mentions
    mock_workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="Remember to check the deployment",
        description="Deployment reminder",
    )

    with patch("mindroom.scheduling.tasks._parse_workflow_schedule") as mock_parse:
        mock_parse.return_value = mock_workflow

        response = await schedule_task(
            runtime=_scheduling_runtime(client=client, config=config, room=room),
            room_id="test_room",
            thread_id="$thread123",
            scheduled_by="@user:localhost",
            full_text="in 5 minutes remind me about deployment",
            new_thread=True,
        )

    assert "\n**Task ID:** `" in response
    assert "✅ Scheduled" in response
    assert "New thread per fire" in response
    available_agents = mock_parse.await_args.args[3]
    expected_agents = [
        entity_ids(config, runtime_paths_for(config))["assistant"],
        entity_ids(config, runtime_paths_for(config))["researcher"],
    ]
    assert available_agents == expected_agents


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_schedule_validation_respects_sender_reply_permissions() -> None:
    """Explicit mentions should validate against sender-permitted room agents, not raw membership."""
    config = _runtime_bound_config(
        Config(
            agents={
                "assistant": AgentConfig(
                    display_name="Assistant",
                    role="General assistance",
                    rooms=["test_room"],
                    access=ResponderAccessConfig(users=["@blocked:localhost"]),
                ),
                "calculator": AgentConfig(
                    display_name="Calculator",
                    role="Math calculations",
                    rooms=["test_room"],
                    access=ResponderAccessConfig(users=["@allowed:localhost"]),
                ),
            },
            router=RouterConfig(model="default"),
        ),
    )

    client = AsyncMock()
    _allow_schedule_state_persistence(client, "test_room")
    room = _test_room(config, "assistant", "calculator")
    mock_workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="@calculator please calculate 2+2",
        description="Calculate something",
    )

    with patch("mindroom.scheduling.tasks._parse_workflow_schedule") as mock_parse:
        mock_parse.return_value = mock_workflow

        with pytest.raises(SchedulingError) as exc_info:
            await schedule_task(
                runtime=_scheduling_runtime(client=client, config=config, room=room),
                room_id="test_room",
                thread_id=None,
                scheduled_by="@blocked:localhost",
                full_text="in 5 minutes ask calculator to calculate",
            )

    response = str(exc_info.value)
    calculator_matrix_id = entity_ids(config, runtime_paths_for(config))["calculator"].full_id
    assert calculator_matrix_id in response
    assert "not available in this room" in response


@pytest.mark.asyncio
async def test_schedule_with_nonexistent_agent() -> None:
    """Mentioning an unknown agent still schedules the task, since only configured agents count as mentions."""
    config = _runtime_bound_config(
        Config(
            agents={
                "assistant": AgentConfig(
                    display_name="Assistant",
                    role="General assistance",
                    rooms=["test_room"],
                ),
            },
            router=RouterConfig(model="default"),
        ),
    )

    client = AsyncMock()
    _allow_schedule_state_persistence(client, "test_room")

    room = _test_room(config, "assistant")

    # Mock workflow mentioning non-existent agent
    mock_workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="@imaginary_agent do something",
        description="Imaginary task",
    )

    with patch("mindroom.scheduling.tasks._parse_workflow_schedule") as mock_parse:
        mock_parse.return_value = mock_workflow

        response = await schedule_task(
            runtime=_scheduling_runtime(client=client, config=config, room=room),
            room_id="test_room",
            thread_id=None,
            scheduled_by="@user:localhost",
            full_text="in 5 minutes ask imaginary agent",
        )

        assert "\n**Task ID:** `" in response
