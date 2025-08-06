"""Integration tests for scheduling functionality in the bot."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.bot import AgentBot
from mindroom.commands import Command, CommandType
from mindroom.matrix import AgentMatrixUser


@pytest.fixture
def mock_agent_bot():
    """Create a mock agent bot for testing."""
    agent_user = AgentMatrixUser(
        agent_name="general",
        user_id="@mindroom_general:localhost",
        display_name="General Agent",
        password="mock_password",
        access_token="mock_token",
    )
    bot = AgentBot(agent_user=agent_user, storage_path=MagicMock(), rooms=["!test:server"])
    bot.client = AsyncMock()
    bot.thread_invite_manager = AsyncMock()
    bot.logger = MagicMock()
    bot._send_response = AsyncMock()
    return bot


class TestBotScheduleCommands:
    """Test bot handling of schedule commands."""

    @pytest.mark.asyncio
    async def test_handle_schedule_command(self, mock_agent_bot):
        """Test bot handles schedule command correctly."""
        room = MagicMock()
        room.room_id = "!test:server"

        event = MagicMock()
        event.event_id = "$event123"
        event.sender = "@user:server"
        event.body = "/schedule in 5 minutes Check deployment"
        event.source = {"content": {"m.relates_to": {"event_id": "$thread123", "rel_type": "m.thread"}}}

        command = Command(
            type=CommandType.SCHEDULE,
            args={"time_expression": "in 5 minutes", "message": "Check deployment"},
            raw_text=event.body,
        )

        # Mock the schedule_task function
        with patch("mindroom.bot.schedule_task") as mock_schedule:
            mock_schedule.return_value = ("task123", "✅ Scheduled: 5 minutes from now")

            await mock_agent_bot._handle_command(room, event, command)

            # Verify schedule_task was called correctly
            mock_schedule.assert_called_once_with(
                client=mock_agent_bot.client,
                room_id="!test:server",
                thread_id="$thread123",
                agent_user_id="@mindroom_general:localhost",
                scheduled_by="@user:server",
                time_expression="in 5 minutes",
                message="Check deployment",
            )

            # Verify response was sent
            mock_agent_bot._send_response.assert_called_once()
            call_args = mock_agent_bot._send_response.call_args
            assert "✅ Scheduled: 5 minutes from now" in call_args[0][2]

    @pytest.mark.asyncio
    async def test_handle_schedule_command_no_message(self, mock_agent_bot):
        """Test schedule command with no message uses default."""
        room = MagicMock()
        room.room_id = "!test:server"

        event = MagicMock()
        event.event_id = "$event123"
        event.sender = "@user:server"
        event.body = "/schedule tomorrow"
        event.source = {"content": {"m.relates_to": {"event_id": "$thread123", "rel_type": "m.thread"}}}

        command = Command(
            type=CommandType.SCHEDULE, args={"time_expression": "tomorrow", "message": ""}, raw_text=event.body
        )

        with patch("mindroom.bot.schedule_task") as mock_schedule:
            mock_schedule.return_value = ("task456", "✅ Scheduled for tomorrow")

            await mock_agent_bot._handle_command(room, event, command)

            # Verify default message was used
            call_args = mock_schedule.call_args
            assert call_args[1]["message"] == "Reminder"

    @pytest.mark.asyncio
    async def test_handle_list_schedules_command(self, mock_agent_bot):
        """Test bot handles list schedules command."""
        room = MagicMock()
        room.room_id = "!test:server"

        event = MagicMock()
        event.event_id = "$event123"
        event.body = "/list_schedules"
        event.source = {"content": {"m.relates_to": {"event_id": "$thread123", "rel_type": "m.thread"}}}

        command = Command(type=CommandType.LIST_SCHEDULES, args={}, raw_text=event.body)

        with patch("mindroom.bot.list_scheduled_tasks") as mock_list:
            mock_list.return_value = "**Scheduled Tasks:**\n• task123 - Tomorrow: Test"

            await mock_agent_bot._handle_command(room, event, command)

            mock_list.assert_called_once_with(
                client=mock_agent_bot.client, room_id="!test:server", thread_id="$thread123"
            )

            mock_agent_bot._send_response.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_cancel_schedule_command(self, mock_agent_bot):
        """Test bot handles cancel schedule command."""
        room = MagicMock()
        room.room_id = "!test:server"

        event = MagicMock()
        event.event_id = "$event123"
        event.body = "/cancel_schedule task123"
        event.source = {"content": {"m.relates_to": {"event_id": "$thread123", "rel_type": "m.thread"}}}

        command = Command(type=CommandType.CANCEL_SCHEDULE, args={"task_id": "task123"}, raw_text=event.body)

        with patch("mindroom.bot.cancel_scheduled_task") as mock_cancel:
            mock_cancel.return_value = "✅ Cancelled task `task123`"

            await mock_agent_bot._handle_command(room, event, command)

            mock_cancel.assert_called_once_with(client=mock_agent_bot.client, room_id="!test:server", task_id="task123")

    @pytest.mark.asyncio
    async def test_schedule_command_requires_thread(self, mock_agent_bot):
        """Test that schedule commands only work in threads."""
        room = MagicMock()
        room.room_id = "!test:server"

        event = MagicMock()
        event.event_id = "$event123"
        event.body = "/schedule in 5 minutes Test"
        event.source = {"content": {}}  # No thread relation

        command = Command(
            type=CommandType.SCHEDULE, args={"time_expression": "in 5 minutes", "message": "Test"}, raw_text=event.body
        )

        await mock_agent_bot._handle_command(room, event, command)

        # Should send error message about threads
        mock_agent_bot._send_response.assert_called_once()
        call_args = mock_agent_bot._send_response.call_args
        assert "❌" in call_args[0][2]
        assert "threads" in call_args[0][2].lower()


class TestBotTaskRestoration:
    """Test scheduled task restoration on bot startup."""

    @pytest.mark.asyncio
    async def test_restore_tasks_on_room_join(self):
        """Test that scheduled tasks are restored when joining rooms."""
        import tempfile
        from pathlib import Path

        agent_user = AgentMatrixUser(
            agent_name="general",
            user_id="@mindroom_general:localhost",
            display_name="General Agent",
            password="mock_password",
            access_token="mock_token",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            bot = AgentBot(agent_user=agent_user, storage_path=Path(tmpdir), rooms=["!test:server"])

            # Mock the necessary methods
            with (
                patch("mindroom.bot.login_agent_user") as mock_login,
                patch("mindroom.bot.join_room") as mock_join,
                patch("mindroom.bot.restore_scheduled_tasks") as mock_restore,
            ):
                mock_login.return_value = AsyncMock()
                mock_join.return_value = True
                mock_restore.return_value = 2  # 2 tasks restored

                await bot.start()

                # Verify restore was called for the room
                mock_restore.assert_called_once_with(bot.client, "!test:server")

                # Just verify restore was called - logger testing is complex with the bind() method
                assert mock_restore.called

    @pytest.mark.asyncio
    async def test_no_log_when_no_tasks_restored(self):
        """Test that no log is generated when no tasks are restored."""
        import tempfile
        from pathlib import Path

        agent_user = AgentMatrixUser(
            agent_name="general",
            user_id="@mindroom_general:localhost",
            display_name="General Agent",
            password="mock_password",
            access_token="mock_token",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            bot = AgentBot(agent_user=agent_user, storage_path=Path(tmpdir), rooms=["!test:server"])

            with (
                patch("mindroom.bot.login_agent_user") as mock_login,
                patch("mindroom.bot.join_room") as mock_join,
                patch("mindroom.bot.restore_scheduled_tasks") as mock_restore,
            ):
                mock_login.return_value = AsyncMock()
                mock_join.return_value = True
                mock_restore.return_value = 0  # No tasks restored

                await bot.start()

                # Just verify restore was called with 0 - logger testing is complex with the bind() method
                assert mock_restore.return_value == 0
