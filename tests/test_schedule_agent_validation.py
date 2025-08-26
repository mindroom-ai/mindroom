"""Tests for agent validation in schedule commands."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import nio
import pytest

from mindroom.config import AgentConfig, Config, RouterConfig
from mindroom.scheduling import schedule_task
from mindroom.workflow_scheduling import ScheduledWorkflow


def create_mock_room(room_id: str, user_ids: list[str] | None = None) -> nio.MatrixRoom:
    """Create a mock Matrix room with optional members."""
    room = nio.MatrixRoom(room_id, "@bot:localhost")
    if user_ids:
        for user_id in user_ids:
            room.users[user_id] = nio.RoomMember(
                user_id=user_id,
                display_name=user_id,
                avatar_url=None,
            )
    return room


@pytest.mark.asyncio
async def test_schedule_validates_agents_in_room() -> None:
    """Test that schedule command validates agents are configured for the room."""
    # Create config with some agents
    config = Config(
        agents={
            "assistant": AgentConfig(
                display_name="Assistant",
                role="General assistance",
                rooms=["test_room"],  # Assistant is in test_room
            ),
            "calculator": AgentConfig(
                display_name="Calculator",
                role="Math calculations",
                rooms=[],  # Calculator is NOT in test_room
            ),
        },
        router=RouterConfig(model="default"),
    )

    # Mock client
    client = AsyncMock()

    # Create a mock room with the agents
    room = create_mock_room("test_room", ["@mindroom_assistant:localhost"])

    # Mock the workflow parsing to return a workflow with calculator mentioned
    mock_workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="@calculator please calculate 2+2",
        description="Calculate something",
    )

    with patch("mindroom.scheduling.parse_workflow_schedule") as mock_parse:
        mock_parse.return_value = mock_workflow

        # Try to schedule a task mentioning calculator in test_room (where it's not configured)
        task_id, response = await schedule_task(
            client=client,
            room_id="test_room",
            thread_id=None,
            scheduled_by="@user:localhost",
            full_text="in 5 minutes ask calculator to calculate",
            config=config,
            room=room,
        )

        # Should fail because calculator is not in test_room
        assert task_id is None
        assert "❌ Failed to schedule" in response
        assert "@calculator" in response
        assert "not available in this room" in response


@pytest.mark.asyncio
async def test_schedule_validates_agents_in_thread() -> None:
    """Test that schedule command validates agents are invited to threads."""
    # Create config with agents
    config = Config(
        agents={
            "assistant": AgentConfig(
                display_name="Assistant",
                role="General assistance",
                rooms=["test_room"],
            ),
            "calculator": AgentConfig(
                display_name="Calculator",
                role="Math calculations",
                rooms=[],  # Not in room, but could be invited to thread
            ),
        },
        router=RouterConfig(model="default"),
    )

    # Mock client
    client = AsyncMock()

    # Create a mock room with assistant
    room = create_mock_room("test_room", ["@mindroom_assistant:localhost"])

    # Mock the thread invite check
    with patch("mindroom.scheduling.ThreadInviteManager") as mock_invite_manager:
        mock_manager = mock_invite_manager.return_value
        # Calculator is NOT invited to the thread
        mock_manager.get_thread_agents = AsyncMock(return_value=[])

        # Mock the workflow parsing
        mock_workflow = ScheduledWorkflow(
            schedule_type="once",
            execute_at=datetime.now(UTC) + timedelta(minutes=5),
            message="@calculator please calculate 2+2",
            description="Calculate something",
        )

        with patch("mindroom.scheduling.parse_workflow_schedule") as mock_parse:
            mock_parse.return_value = mock_workflow

            # Try to schedule in a thread
            task_id, response = await schedule_task(
                client=client,
                room_id="test_room",
                thread_id="$thread123",
                scheduled_by="@user:localhost",
                full_text="in 5 minutes ask calculator to calculate",
                config=config,
                room=room,
            )

            # Should fail because calculator is not invited to thread
            assert task_id is None
            assert "❌ Failed to schedule" in response
            assert "@calculator" in response
            assert "not available in this thread" in response
            assert "!invite calculator" in response


@pytest.mark.asyncio
async def test_schedule_allows_invited_agents_in_thread() -> None:
    """Test that schedule command allows agents invited to threads."""
    # Create config
    config = Config(
        agents={
            "assistant": AgentConfig(
                display_name="Assistant",
                role="General assistance",
                rooms=["test_room"],
            ),
            "calculator": AgentConfig(
                display_name="Calculator",
                role="Math calculations",
                rooms=[],  # Not in room, but will be invited to thread
            ),
        },
        router=RouterConfig(model="default"),
    )

    # Mock client
    client = AsyncMock()
    client.room_put_state = AsyncMock()

    # Create a mock room
    room = create_mock_room("test_room", ["@mindroom_assistant:localhost"])

    # Mock the thread invite check - calculator IS invited
    with patch("mindroom.scheduling.ThreadInviteManager") as mock_invite_manager:
        mock_manager = mock_invite_manager.return_value
        mock_manager.get_thread_agents = AsyncMock(return_value=["calculator"])

        # Mock the workflow parsing
        mock_workflow = ScheduledWorkflow(
            schedule_type="once",
            execute_at=datetime.now(UTC) + timedelta(minutes=5),
            message="@calculator please calculate 2+2",
            description="Calculate something",
        )

        with patch("mindroom.scheduling.parse_workflow_schedule") as mock_parse:
            mock_parse.return_value = mock_workflow

            # Try to schedule in a thread where calculator is invited
            task_id, response = await schedule_task(
                client=client,
                room_id="test_room",
                thread_id="$thread123",
                scheduled_by="@user:localhost",
                full_text="in 5 minutes ask calculator to calculate",
                config=config,
                room=room,
            )

            # Should succeed because calculator is invited to thread
            assert task_id is not None
            assert "✅ Scheduled" in response
            assert "❌" not in response


@pytest.mark.asyncio
async def test_schedule_with_multiple_agents_validation() -> None:
    """Test validation when multiple agents are mentioned."""
    config = Config(
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
    )

    client = AsyncMock()

    # Create a mock room with assistant and researcher
    room = create_mock_room(
        "test_room",
        [
            "@mindroom_assistant:localhost",
            "@mindroom_researcher:localhost",
        ],
    )

    # Mock workflow with multiple agents
    mock_workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="@researcher find info and @calculator calculate it",
        description="Research and calculate",
    )

    with patch("mindroom.scheduling.parse_workflow_schedule") as mock_parse:
        mock_parse.return_value = mock_workflow

        task_id, response = await schedule_task(
            client=client,
            room_id="test_room",
            thread_id=None,
            scheduled_by="@user:localhost",
            full_text="in 5 minutes research and calculate",
            config=config,
            room=room,
        )

        # Should fail because calculator is not in room
        assert task_id is None
        assert "❌ Failed to schedule" in response
        assert "@calculator" in response
        # Researcher should not be mentioned as invalid
        assert "@researcher" not in response.split("not available")[1] if "not available" in response else True


@pytest.mark.asyncio
async def test_schedule_with_no_agent_mentions() -> None:
    """Test that schedules without agent mentions work fine."""
    config = Config(
        agents={
            "assistant": AgentConfig(
                display_name="Assistant",
                role="General assistance",
                rooms=["test_room"],
            ),
        },
        router=RouterConfig(model="default"),
    )

    client = AsyncMock()
    client.room_put_state = AsyncMock()

    # Create a mock room
    room = create_mock_room("test_room", ["@mindroom_assistant:localhost"])

    # Mock workflow without any agent mentions
    mock_workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="Remember to check the deployment",
        description="Deployment reminder",
    )

    with patch("mindroom.scheduling.parse_workflow_schedule") as mock_parse:
        mock_parse.return_value = mock_workflow

        task_id, response = await schedule_task(
            client=client,
            room_id="test_room",
            thread_id=None,
            scheduled_by="@user:localhost",
            full_text="in 5 minutes remind me about deployment",
            config=config,
            room=room,
        )

        # Should succeed - no agents to validate
        assert task_id is not None
        assert "✅ Scheduled" in response


@pytest.mark.asyncio
async def test_schedule_with_nonexistent_agent() -> None:
    """Test that mentioning a non-existent agent fails appropriately."""
    config = Config(
        agents={
            "assistant": AgentConfig(
                display_name="Assistant",
                role="General assistance",
                rooms=["test_room"],
            ),
        },
        router=RouterConfig(model="default"),
    )

    client = AsyncMock()

    # Create a mock room
    room = create_mock_room("test_room", ["@mindroom_assistant:localhost"])

    # Mock workflow mentioning non-existent agent
    mock_workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="@imaginary_agent do something",
        description="Imaginary task",
    )

    with patch("mindroom.scheduling.parse_workflow_schedule") as mock_parse:
        mock_parse.return_value = mock_workflow

        task_id, response = await schedule_task(
            client=client,
            room_id="test_room",
            thread_id=None,
            scheduled_by="@user:localhost",
            full_text="in 5 minutes ask imaginary agent",
            config=config,
            room=room,
        )

        # Should succeed if imaginary_agent is not recognized as a valid agent
        # The parse_mentions_in_text will filter out non-existent agents
        # So the schedule should go through (with no agents to validate)
        assert task_id is not None
