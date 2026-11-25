"""Tests for agent validation in structured scheduling."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import nio
import pytest

from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import RouterConfig
from mindroom.scheduling.errors import SchedulingError
from mindroom.scheduling.tasks import SchedulingRuntime, schedule_task
from tests.authorization_helpers import (
    make_test_scheduling_runtime,
)
from tests.conftest import (
    bind_runtime_paths,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.identity_helpers import entity_ids


def _runtime_bound_config(config: Config) -> Config:
    """Return a runtime-bound config for scheduling tests."""
    return bind_runtime_paths(config, test_runtime_paths(Path(tempfile.mkdtemp())))


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
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("thread_id", [None, "$thread123"], ids=["room", "thread"])
async def test_schedule_validates_agents_in_scope(thread_id: str | None) -> None:
    """Reject a mentioned agent not configured for the room, always naming the room boundary."""
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

    with pytest.raises(SchedulingError) as exc_info:
        await schedule_task(
            runtime=_scheduling_runtime(client=client, config=config, room=room),
            room_id="test_room",
            thread_id=thread_id,
            scheduled_by="@user:localhost",
            message="@calculator please calculate 2+2",
            description="Calculate something",
            delay_seconds=300,
            new_thread=False,
        )

    response = str(exc_info.value)
    assert response.startswith("Failed to schedule")
    calculator_matrix_id = entity_ids(config, runtime_paths_for(config))["calculator"].full_id
    assert calculator_matrix_id in response
    assert "not available in this room" in response


@pytest.mark.asyncio
async def test_schedule_allows_agents_in_room() -> None:
    """Allow scheduling mentions of agents that are permitted in the room."""
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
                    rooms=["test_room"],
                ),
            },
            router=RouterConfig(model="default"),
        ),
    )

    client = AsyncMock()
    _allow_schedule_state_persistence(client, "test_room")
    room = _test_room(config, "assistant", "calculator")

    response = await schedule_task(
        runtime=_scheduling_runtime(client=client, config=config, room=room),
        room_id="test_room",
        thread_id="$thread123",
        scheduled_by="@user:localhost",
        message="@calculator please calculate 2+2",
        description="Calculate something",
        delay_seconds=300,
        new_thread=False,
    )

    assert "\n**Task ID:** `" in response
    assert "✅ Scheduled" in response


@pytest.mark.asyncio
async def test_schedule_with_multiple_agents_validation() -> None:
    """Only unavailable mentioned agents are reported when multiple agents are mentioned."""
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
                "researcher": AgentConfig(
                    display_name="Researcher",
                    role="Research",
                    rooms=["test_room"],
                ),
            },
            router=RouterConfig(model="default"),
        ),
    )

    client = AsyncMock()
    room = _test_room(config, "assistant", "researcher")

    with pytest.raises(SchedulingError) as exc_info:
        await schedule_task(
            runtime=_scheduling_runtime(client=client, config=config, room=room),
            room_id="test_room",
            thread_id=None,
            scheduled_by="@user:localhost",
            message="@researcher find info and @calculator calculate it",
            description="Research and calculate",
            delay_seconds=300,
            new_thread=False,
        )

    response = str(exc_info.value)
    assert response.startswith("Failed to schedule")
    calculator_matrix_id = entity_ids(config, runtime_paths_for(config))["calculator"].full_id
    assert calculator_matrix_id in response
    researcher_matrix_id = entity_ids(config, runtime_paths_for(config))["researcher"].full_id
    assert researcher_matrix_id not in response


@pytest.mark.asyncio
async def test_schedule_with_no_agent_mentions() -> None:
    """New-thread schedules without mentions succeed whenever the sender has a room responder."""
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

    response = await schedule_task(
        runtime=_scheduling_runtime(client=client, config=config, room=room),
        room_id="test_room",
        thread_id="$thread123",
        scheduled_by="@user:localhost",
        message="Remember to check the deployment",
        description="Deployment reminder",
        delay_seconds=300,
        new_thread=True,
    )

    assert "\n**Task ID:** `" in response
    assert "✅ Scheduled" in response
    assert "New thread per fire" in response


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

    with pytest.raises(SchedulingError) as exc_info:
        await schedule_task(
            runtime=_scheduling_runtime(client=client, config=config, room=room),
            room_id="test_room",
            thread_id=None,
            scheduled_by="@blocked:localhost",
            message="@calculator please calculate 2+2",
            description="Calculate something",
            delay_seconds=300,
            new_thread=False,
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

    response = await schedule_task(
        runtime=_scheduling_runtime(client=client, config=config, room=room),
        room_id="test_room",
        thread_id=None,
        scheduled_by="@user:localhost",
        message="@imaginary_agent do something",
        description="Imaginary task",
        delay_seconds=300,
        new_thread=False,
    )

    assert "\n**Task ID:** `" in response
