"""Tests for the scheduling module with real AI-powered time parsing."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.scheduling import (
    ScheduledTimeResponse,
    ScheduleParseError,
    cancel_scheduled_task,
    list_scheduled_tasks,
    parse_schedule,
    restore_scheduled_tasks,
    schedule_task,
)


class TestScheduleTimeParsing:
    """Test AI-powered time parsing with real model responses."""

    @pytest.mark.asyncio
    async def test_parse_relative_time_minutes(self):
        """Test parsing 'in X minutes' expressions."""
        # Mock the AI agent to return a realistic response
        mock_response = MagicMock()
        mock_response.content = ScheduledTimeResponse(
            execute_at=datetime.now(UTC) + timedelta(minutes=5), interpretation="5 minutes from now"
        )

        with patch("mindroom.scheduling.Agent") as mock_agent_class:
            mock_agent = AsyncMock()
            mock_agent.arun.return_value = mock_response
            mock_agent_class.return_value = mock_agent

            result = await parse_schedule("in 5 minutes Check the deployment")

            assert isinstance(result, ScheduledTimeResponse)
            assert result.interpretation == "5 minutes from now"
            assert result.execute_at > datetime.now(UTC)
            assert result.execute_at < datetime.now(UTC) + timedelta(minutes=10)

    @pytest.mark.asyncio
    async def test_parse_relative_time_hours(self):
        """Test parsing 'in X hours' expressions."""
        mock_response = MagicMock()
        mock_response.content = ScheduledTimeResponse(
            execute_at=datetime.now(UTC) + timedelta(hours=2), interpretation="2 hours from now"
        )

        with patch("mindroom.scheduling.Agent") as mock_agent_class:
            mock_agent = AsyncMock()
            mock_agent.arun.return_value = mock_response
            mock_agent_class.return_value = mock_agent

            result = await parse_schedule_time("in 2 hours")

            assert isinstance(result, ScheduledTimeResponse)
            assert result.interpretation == "2 hours from now"

    @pytest.mark.asyncio
    async def test_parse_tomorrow(self):
        """Test parsing 'tomorrow' expressions."""
        tomorrow = datetime.now(UTC) + timedelta(days=1)
        mock_response = MagicMock()
        mock_response.content = ScheduledTimeResponse(
            execute_at=tomorrow.replace(hour=9, minute=0, second=0, microsecond=0), interpretation="Tomorrow at 9:00 AM"
        )

        with patch("mindroom.scheduling.Agent") as mock_agent_class:
            mock_agent = AsyncMock()
            mock_agent.arun.return_value = mock_response
            mock_agent_class.return_value = mock_agent

            result = await parse_schedule_time("tomorrow at 9am")

            assert isinstance(result, ScheduledTimeResponse)
            assert "Tomorrow" in result.interpretation

    @pytest.mark.asyncio
    async def test_parse_contextual_time(self):
        """Test parsing contextual expressions like 'later'."""
        mock_response = MagicMock()
        mock_response.content = ScheduledTimeResponse(
            execute_at=datetime.now(UTC) + timedelta(minutes=30), interpretation="30 minutes from now (later)"
        )

        with patch("mindroom.scheduling.Agent") as mock_agent_class:
            mock_agent = AsyncMock()
            mock_agent.arun.return_value = mock_response
            mock_agent_class.return_value = mock_agent

            result = await parse_schedule_time("later")

            assert isinstance(result, ScheduledTimeResponse)
            assert "30 minutes" in result.interpretation or "later" in result.interpretation

    @pytest.mark.asyncio
    async def test_parse_invalid_time(self):
        """Test parsing invalid time expressions."""
        # First attempt fails, second attempt returns error
        with patch("mindroom.scheduling.Agent") as mock_agent_class:
            # First agent raises exception
            mock_agent1 = AsyncMock()
            mock_agent1.arun.side_effect = Exception("Cannot parse")

            # Second agent returns error response
            mock_response = MagicMock()
            mock_response.content = ScheduleParseError(
                error="Cannot parse 'gibberish' as a time",
                suggestion="Try something like 'in 5 minutes' or 'tomorrow at 3pm'",
            )
            mock_agent2 = AsyncMock()
            mock_agent2.arun.return_value = mock_response

            mock_agent_class.side_effect = [mock_agent1, mock_agent2]

            result = await parse_schedule_time("gibberish")

            assert isinstance(result, ScheduleParseError)
            assert "Cannot parse" in result.error
            assert result.suggestion is not None

    @pytest.mark.asyncio
    async def test_parse_with_custom_current_time(self):
        """Test parsing with a custom current time."""
        custom_time = datetime(2024, 1, 15, 10, 0, 0)
        mock_response = MagicMock()
        mock_response.content = ScheduledTimeResponse(
            execute_at=custom_time + timedelta(minutes=15), interpretation="15 minutes from now"
        )

        with patch("mindroom.scheduling.Agent") as mock_agent_class:
            mock_agent = AsyncMock()
            mock_agent.arun.return_value = mock_response
            mock_agent_class.return_value = mock_agent

            result = await parse_schedule_time("in 15 minutes", current_time=custom_time)

            assert isinstance(result, ScheduledTimeResponse)
            # Verify the prompt included the custom time
            call_args = mock_agent.arun.call_args[0][0]
            assert "2024-01-15T10:00:00" in call_args


class TestScheduleTask:
    """Test the schedule_task function."""

    @pytest.mark.asyncio
    async def test_schedule_task_success(self):
        """Test successfully scheduling a task."""
        mock_client = AsyncMock()
        mock_client.room_put_state = AsyncMock(return_value=MagicMock())

        # Mock successful time parsing
        with patch("mindroom.scheduling.parse_schedule") as mock_parse:
            mock_parse.return_value = ScheduledTimeResponse(
                execute_at=datetime.now(UTC) + timedelta(minutes=5), 
                message="Check the deployment",
                interpretation="5 minutes from now"
            )

            task_id, message = await schedule_task(
                client=mock_client,
                room_id="!room:server",
                thread_id="$thread123",
                agent_user_id="@agent:server",
                scheduled_by="@user:server",
                full_text="in 5 minutes Check the deployment",
            )

            assert task_id is not None
            assert len(task_id) == 8  # Short UUID
            assert "✅" in message
            assert "5 minutes from now" in message
            assert "Check the deployment" in message
            assert task_id in message

            # Verify Matrix state was updated
            mock_client.room_put_state.assert_called_once()
            call_args = mock_client.room_put_state.call_args
            assert call_args[1]["room_id"] == "!room:server"
            assert call_args[1]["event_type"] == "com.mindroom.scheduled.task"
            assert call_args[1]["state_key"] == task_id
            content = call_args[1]["content"]
            assert content["message"] == "Check the deployment"
            assert content["status"] == "pending"

    @pytest.mark.asyncio
    async def test_schedule_task_parse_error(self):
        """Test scheduling with invalid time expression."""
        mock_client = AsyncMock()

        with patch("mindroom.scheduling.parse_schedule") as mock_parse:
            mock_parse.return_value = ScheduleParseError(error="Cannot parse request", suggestion="Try 'in 5 minutes Check deployment'")

            task_id, message = await schedule_task(
                client=mock_client,
                room_id="!room:server",
                thread_id="$thread123",
                agent_user_id="@agent:server",
                scheduled_by="@user:server",
                full_text="invalid gibberish",
            )

            assert task_id is None
            assert "❌" in message
            assert "Cannot parse request" in message
            assert "💡" in message
            assert "Try 'in 5 minutes Check deployment'" in message

    @pytest.mark.asyncio
    async def test_schedule_task_default_message(self):
        """Test scheduling with only time expression (AI provides default)."""
        mock_client = AsyncMock()
        mock_client.room_put_state = AsyncMock(return_value=MagicMock())

        with patch("mindroom.scheduling.parse_schedule") as mock_parse:
            mock_parse.return_value = ScheduledTimeResponse(
                execute_at=datetime.now(UTC) + timedelta(minutes=5), 
                message="Reminder",  # AI provides default
                interpretation="5 minutes from now"
            )

            task_id, message = await schedule_task(
                client=mock_client,
                room_id="!room:server",
                thread_id=None,
                agent_user_id="@agent:server",
                scheduled_by="@user:server",
                full_text="in 5 minutes",  # Just time, no message
            )

            assert task_id is not None
            assert '"Reminder"' in message  # Shows the default message


class TestListScheduledTasks:
    """Test listing scheduled tasks."""

    @pytest.mark.asyncio
    async def test_list_tasks_empty(self):
        """Test listing when no tasks exist."""
        mock_client = AsyncMock()
        mock_response = nio.RoomGetStateResponse(events=[], room_id="!room:server")
        mock_client.room_get_state = AsyncMock(return_value=mock_response)

        result = await list_scheduled_tasks(mock_client, "!room:server")
        assert result == "No scheduled tasks found."

    @pytest.mark.asyncio
    async def test_list_tasks_with_tasks(self):
        """Test listing multiple scheduled tasks."""
        mock_client = AsyncMock()
        # Create test events
        now = datetime.now(UTC)
        events = [
            {
                "type": "com.mindroom.scheduled.task",
                "state_key": "task123",
                "content": {
                    "status": "pending",
                    "execute_at": (now + timedelta(hours=1)).isoformat(),
                    "message": "Check the deployment status and send report",
                    "thread_id": "$thread123",
                },
            },
            {
                "type": "com.mindroom.scheduled.task",
                "state_key": "task456",
                "content": {
                    "status": "pending",
                    "execute_at": (now + timedelta(minutes=30)).isoformat(),
                    "message": "Quick reminder",
                    "thread_id": "$thread123",
                },
            },
            {
                "type": "com.mindroom.scheduled.task",
                "state_key": "task789",
                "content": {
                    "status": "completed",  # Should be filtered out
                    "execute_at": now.isoformat(),
                    "message": "Old task",
                    "thread_id": "$thread123",
                },
            },
        ]
        mock_response = nio.RoomGetStateResponse(events=events, room_id="!room:server")
        mock_client.room_get_state = AsyncMock(return_value=mock_response)

        result = await list_scheduled_tasks(mock_client, "!room:server", "$thread123")

        assert "**Scheduled Tasks:**" in result
        assert "task123" in result
        assert "task456" in result
        assert "task789" not in result  # Completed task filtered out
        assert "Check the deployment status and send report" in result  # Full message shown
        assert "Quick reminder" in result

        # Verify tasks are sorted by time (task456 should come first)
        lines = result.split("\n")
        task_lines = [line for line in lines if line.startswith("•")]
        assert "task456" in task_lines[0]  # 30 minutes comes before 1 hour

    @pytest.mark.asyncio
    async def test_list_tasks_filters_by_thread(self):
        """Test that listing filters by thread ID."""
        mock_client = AsyncMock()

        events = [
            {
                "type": "com.mindroom.scheduled.task",
                "state_key": "task1",
                "content": {
                    "status": "pending",
                    "execute_at": datetime.now(UTC).isoformat(),
                    "message": "In thread",
                    "thread_id": "$thread123",
                },
            },
            {
                "type": "com.mindroom.scheduled.task",
                "state_key": "task2",
                "content": {
                    "status": "pending",
                    "execute_at": datetime.now(UTC).isoformat(),
                    "message": "Different thread",
                    "thread_id": "$thread456",
                },
            },
        ]
        mock_response = nio.RoomGetStateResponse(events=events, room_id="!room:server")
        mock_client.room_get_state = AsyncMock(return_value=mock_response)

        result = await list_scheduled_tasks(mock_client, "!room:server", "$thread123")

        assert "task1" in result
        assert "task2" not in result


class TestCancelScheduledTask:
    """Test canceling scheduled tasks."""

    @pytest.mark.asyncio
    async def test_cancel_existing_task(self):
        """Test canceling an existing task."""
        mock_client = AsyncMock()
        mock_response = nio.RoomGetStateEventResponse(
            content={"status": "pending"},
            event_type="com.mindroom.scheduled.task",
            state_key="task123",
            room_id="!room:server",
        )
        mock_client.room_get_state_event = AsyncMock(return_value=mock_response)
        mock_client.room_put_state = AsyncMock(return_value=MagicMock())

        # Mock the running task
        mock_task = AsyncMock()
        mock_task.cancel = MagicMock()

        with patch("mindroom.scheduling._running_tasks", {"task123": mock_task}):
            result = await cancel_scheduled_task(mock_client, "!room:server", "task123")

            assert "✅" in result
            assert "task123" in result
            mock_task.cancel.assert_called_once()

            # Verify state was updated to cancelled
            mock_client.room_put_state.assert_called_once()
            call_args = mock_client.room_put_state.call_args
            assert call_args[1]["content"]["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_task(self):
        """Test canceling a task that doesn't exist."""
        mock_client = AsyncMock()
        # Simulate task not found
        mock_client.room_get_state_event = AsyncMock(return_value=MagicMock(spec=["error"]))

        result = await cancel_scheduled_task(mock_client, "!room:server", "nonexistent")

        assert "❌" in result
        assert "not found" in result


class TestRestoreScheduledTasks:
    """Test restoring tasks after bot restart."""

    @pytest.mark.asyncio
    async def test_restore_future_tasks(self):
        """Test restoring tasks that are still in the future."""
        mock_client = AsyncMock()

        future_time = datetime.now(UTC) + timedelta(hours=1)
        events = [
            {
                "type": "com.mindroom.scheduled.task",
                "state_key": "task123",
                "content": {
                    "status": "pending",
                    "room_id": "!room:server",
                    "thread_id": "$thread123",
                    "agent_user_id": "@agent:server",
                    "execute_at": future_time.isoformat(),
                    "message": "Future task",
                },
            }
        ]
        mock_response = nio.RoomGetStateResponse(events=events, room_id="!room:server")
        mock_client.room_get_state = AsyncMock(return_value=mock_response)
        mock_client.room_send = AsyncMock()

        with patch("mindroom.scheduling._running_tasks", {}):
            count = await restore_scheduled_tasks(mock_client, "!room:server")

            assert count == 1
            # Task should be in running tasks
            from mindroom.scheduling import _running_tasks

            assert len(_running_tasks) == 1

            # Give async task a moment to start
            await asyncio.sleep(0.1)

    @pytest.mark.asyncio
    async def test_restore_skips_past_tasks(self):
        """Test that past tasks are not restored."""
        mock_client = AsyncMock()

        past_time = datetime.now(UTC) - timedelta(hours=1)
        events = [
            {
                "type": "com.mindroom.scheduled.task",
                "state_key": "task123",
                "content": {
                    "status": "pending",
                    "room_id": "!room:server",
                    "thread_id": "$thread123",
                    "agent_user_id": "@agent:server",
                    "execute_at": past_time.isoformat(),
                    "message": "Past task",
                },
            }
        ]
        mock_response = nio.RoomGetStateResponse(events=events, room_id="!room:server")
        mock_client.room_get_state = AsyncMock(return_value=mock_response)

        count = await restore_scheduled_tasks(mock_client, "!room:server")
        assert count == 0


class TestScheduledTaskExecution:
    """Test the actual execution of scheduled tasks."""

    @pytest.mark.asyncio
    async def test_task_executes_at_scheduled_time(self):
        """Test that a task executes at the right time."""
        mock_client = AsyncMock()
        mock_client.room_send = AsyncMock()
        mock_client.room_put_state = AsyncMock()

        # Schedule for 0.1 seconds in the future
        execute_time = datetime.now(UTC) + timedelta(seconds=0.1)

        # Import the private function for testing
        from mindroom.scheduling import _execute_scheduled_task

        asyncio.create_task(
            _execute_scheduled_task(
                mock_client, "task123", "!room:server", "$thread123", "@agent:server", execute_time, "Test message"
            )
        )

        # Wait for task to complete
        await asyncio.sleep(0.2)

        # Verify message was sent
        mock_client.room_send.assert_called_once()
        call_args = mock_client.room_send.call_args
        assert call_args[1]["room_id"] == "!room:server"
        content = call_args[1]["content"]
        assert content["msgtype"] == "m.text"
        assert "⏰ Scheduled reminder: Test message" in content["body"]
        assert content["m.relates_to"]["event_id"] == "$thread123"

        # Verify status was updated
        mock_client.room_put_state.assert_called()
        status_call = mock_client.room_put_state.call_args
        assert status_call[1]["content"]["status"] == "completed"
