"""Tests for workflow scheduling functionality."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from mindroom.scheduling import (
    CronSchedule,
    ScheduledWorkflow,
    WorkflowParseError,
    execute_scheduled_workflow,
    parse_workflow_schedule,
    schedule_task,
)


@pytest.fixture
def mock_config() -> MagicMock:
    """Create a mock config with test agents."""
    config = MagicMock()
    config.agents = {
        "research": MagicMock(),
        "email_assistant": MagicMock(),
        "finance": MagicMock(),
        "shell": MagicMock(),
        "analyst": MagicMock(),
    }
    config.models = {
        "default": MagicMock(),
    }
    return config


class TestCronSchedule:
    """Test CronSchedule model."""

    def test_to_cron_string_default(self) -> None:
        """Test converting default schedule to cron string."""
        schedule = CronSchedule()
        assert schedule.to_cron_string() == "* * * * *"

    def test_to_cron_string_daily(self) -> None:
        """Test daily schedule at 9am."""
        schedule = CronSchedule(minute="0", hour="9")
        assert schedule.to_cron_string() == "0 9 * * *"

    def test_to_cron_string_weekly(self) -> None:
        """Test weekly schedule on Monday at 3pm."""
        schedule = CronSchedule(minute="0", hour="15", weekday="1")
        assert schedule.to_cron_string() == "0 15 * * 1"

    def test_to_cron_string_hourly(self) -> None:
        """Test hourly schedule."""
        schedule = CronSchedule(minute="0")
        assert schedule.to_cron_string() == "0 * * * *"


class TestScheduledWorkflow:
    """Test ScheduledWorkflow model."""

    def test_once_workflow(self) -> None:
        """Test creating a one-time workflow."""
        exec_time = datetime.now(UTC) + timedelta(hours=1)
        workflow = ScheduledWorkflow(
            schedule_type="once",
            execute_at=exec_time,
            message="@research Please find AI news",
            description="One-time research task",
        )
        assert workflow.schedule_type == "once"
        assert workflow.execute_at == exec_time
        assert "@research" in workflow.message

    def test_cron_workflow(self) -> None:
        """Test creating a recurring workflow."""
        cron = CronSchedule(minute="0", hour="9")
        workflow = ScheduledWorkflow(
            schedule_type="cron",
            cron_schedule=cron,
            message="@finance Daily market analysis",
            description="Daily market report",
        )
        assert workflow.schedule_type == "cron"
        assert workflow.cron_schedule.to_cron_string() == "0 9 * * *"


@pytest.mark.asyncio
class TestParseWorkflowSchedule:
    """Test parse_workflow_schedule function."""

    @patch("mindroom.scheduling.get_model_instance")
    @patch("mindroom.scheduling.Agent")
    async def test_parse_research_email_workflow(
        self,
        mock_agent_class: Mock,
        mock_get_model: Mock,  # noqa: ARG002
        mock_config: MagicMock,
    ) -> None:
        """Test parsing research + email workflow."""
        # Setup mock agent response
        mock_agent = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = ScheduledWorkflow(
            schedule_type="cron",
            cron_schedule=CronSchedule(minute="0", hour="9", weekday="1"),
            message="@research @email_assistant Please research the latest AI news and email me a summary",
            description="Weekly AI news research and email",
        )
        mock_agent.arun.return_value = mock_response
        mock_agent_class.return_value = mock_agent

        # Parse the request
        result = await parse_workflow_schedule(
            "Every Monday at 9am, research AI news and email me a summary",
            config=mock_config,
            available_agents=["research", "email_assistant"],
        )

        assert isinstance(result, ScheduledWorkflow)
        assert result.schedule_type == "cron"
        assert result.cron_schedule.weekday == "1"
        assert "@research" in result.message
        assert "@email_assistant" in result.message

    @patch("mindroom.scheduling.get_model_instance")
    @patch("mindroom.scheduling.Agent")
    async def test_parse_simple_reminder(
        self,
        mock_agent_class: Mock,
        mock_get_model: Mock,  # noqa: ARG002
        mock_config: MagicMock,
    ) -> None:
        """Test parsing simple reminder without agents."""
        mock_agent = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = ScheduledWorkflow(
            schedule_type="once",
            execute_at=datetime.now(UTC) + timedelta(minutes=5),
            message="Check the deployment status",
            description="Deployment check reminder",
        )
        mock_agent.arun.return_value = mock_response
        mock_agent_class.return_value = mock_agent

        result = await parse_workflow_schedule(
            "ping me in 5 minutes to check the deployment",
            config=mock_config,
            available_agents=["general"],  # At least one agent required
        )

        assert isinstance(result, ScheduledWorkflow)
        assert result.schedule_type == "once"
        assert result.message == "Check the deployment status"

    @patch("mindroom.scheduling.get_model_instance")
    @patch("mindroom.scheduling.Agent")
    async def test_parse_daily_task(self, mock_agent_class: Mock, mock_get_model: Mock, mock_config: MagicMock) -> None:  # noqa: ARG002
        """Test parsing daily recurring task."""
        mock_agent = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = ScheduledWorkflow(
            schedule_type="cron",
            cron_schedule=CronSchedule(minute="0", hour="9"),
            message="@finance Please provide a market analysis for today",
            description="Daily market analysis",
        )
        mock_agent.arun.return_value = mock_response
        mock_agent_class.return_value = mock_agent

        result = await parse_workflow_schedule(
            "Daily at 9am, give me a market analysis",
            config=mock_config,
            available_agents=["finance"],  # Finance agent available
        )

        assert isinstance(result, ScheduledWorkflow)
        assert result.schedule_type == "cron"
        assert result.cron_schedule.to_cron_string() == "0 9 * * *"
        assert "@finance" in result.message

    @patch("mindroom.scheduling.get_model_instance")
    @patch("mindroom.scheduling.Agent")
    async def test_parse_error_handling(
        self,
        mock_agent_class: Mock,
        mock_get_model: Mock,  # noqa: ARG002
        mock_config: MagicMock,
    ) -> None:
        """Test error handling in parse_workflow_schedule."""
        mock_agent = AsyncMock()
        mock_agent.arun.side_effect = Exception("AI service error")
        mock_agent_class.return_value = mock_agent

        result = await parse_workflow_schedule(
            "Schedule something",
            config=mock_config,
            available_agents=["general"],  # At least one agent required
        )

        assert isinstance(result, WorkflowParseError)
        assert "Error parsing schedule" in result.error
        assert result.suggestion is not None

    @patch("mindroom.scheduling.get_model_instance")
    @patch("mindroom.scheduling.Agent")
    async def test_parse_missing_fields_fallbacks(
        self,
        mock_agent_class: Mock,
        mock_get_model: Mock,  # noqa: ARG002
        mock_config: MagicMock,
    ) -> None:
        """Missing execute_at/cron_schedule fields get sensible defaults."""
        mock_agent = AsyncMock()

        # once without execute_at
        resp_once = MagicMock()
        resp_once.content = ScheduledWorkflow(
            schedule_type="once",
            execute_at=None,
            message="Check",
            description="Check later",
        )

        # cron without cron_schedule
        resp_cron = MagicMock()
        resp_cron.content = ScheduledWorkflow(
            schedule_type="cron",
            cron_schedule=None,
            message="Daily",
            description="Daily task",
        )

        # Alternate responses
        mock_agent.arun.side_effect = [resp_once, resp_cron]
        mock_agent_class.return_value = mock_agent

        result_once = await parse_workflow_schedule("remind me later", mock_config, ["general"])
        assert isinstance(result_once, ScheduledWorkflow)
        assert result_once.schedule_type == "once"
        assert result_once.execute_at is not None

        result_cron = await parse_workflow_schedule("every day", mock_config, ["general"])
        assert isinstance(result_cron, ScheduledWorkflow)
        assert result_cron.schedule_type == "cron"
        assert result_cron.cron_schedule is not None


@pytest.mark.asyncio
class TestExecuteScheduledWorkflow:
    """Test execute_scheduled_workflow function."""

    async def test_execute_workflow_with_agents(self) -> None:
        """Test executing a workflow that mentions agents."""
        client = AsyncMock()
        config = MagicMock()  # Add a mock config
        workflow = ScheduledWorkflow(
            schedule_type="once",
            execute_at=datetime.now(UTC),
            message="@research @analyst Please analyze the latest AI trends",
            description="AI trend analysis",
            thread_id="$thread123",
            room_id="!room:server",
            created_by="@user:server",
        )

        await execute_scheduled_workflow(client, workflow, config)

        # Verify message was sent

        with patch("mindroom.scheduling.send_message", new=AsyncMock()) as mock_send:
            await execute_scheduled_workflow(client, workflow, config)
            mock_send.assert_called_once()

            # Check the message content
            call_args = mock_send.call_args
            assert call_args[0][0] == client  # client
            assert call_args[0][1] == "!room:server"  # room_id
            content = call_args[0][2]
            assert "@research" in content["body"]
            assert "@analyst" in content["body"]
            assert content["m.relates_to"]["event_id"] == "$thread123"

    async def test_execute_workflow_simple_reminder(self) -> None:
        """Test executing a simple reminder without agents."""
        client = AsyncMock()
        config = MagicMock()  # Add a mock config
        workflow = ScheduledWorkflow(
            schedule_type="once",
            execute_at=datetime.now(UTC),
            message="Check the server status",
            description="Server check reminder",
            room_id="!room:server",
        )

        with patch("mindroom.scheduling.send_message", new=AsyncMock()) as mock_send:
            await execute_scheduled_workflow(client, workflow, config)
            mock_send.assert_called_once()

            # Check the message content
            call_args = mock_send.call_args
            content = call_args[0][2]
            assert "Check the server status" in content["body"]
            assert "m.relates_to" not in content  # No thread

    async def test_execute_workflow_error_handling(self) -> None:
        """Test error handling in execute_scheduled_workflow."""
        client = AsyncMock()
        config = MagicMock()  # Add a mock config
        workflow = ScheduledWorkflow(
            schedule_type="once",
            execute_at=datetime.now(UTC),
            message="Test message",
            description="Test task",
            room_id="!room:server",
            thread_id="$thread123",
        )

        # Mock send_message to raise an error only on the first call
        mock_send = AsyncMock(side_effect=[Exception("Send failed"), None])

        with patch("mindroom.scheduling.send_message", new=mock_send):
            # Should not raise, but log error
            await execute_scheduled_workflow(client, workflow, config)

            # Should have tried to send original and error message
            assert mock_send.call_count == 2

            # Check error message was sent
            error_call = mock_send.call_args_list[1]
            error_content = error_call[0][2]
            assert "failed" in error_content["body"].lower()

    async def test_execute_workflow_no_room_id(self) -> None:
        """Test that workflow without room_id doesn't execute."""
        client = AsyncMock()
        config = MagicMock()  # Add a mock config
        workflow = ScheduledWorkflow(
            schedule_type="once",
            execute_at=datetime.now(UTC),
            message="Test message",
            description="Test task",
            room_id=None,  # No room ID
        )

        with patch("mindroom.scheduling.send_message", new=AsyncMock()) as mock_send:
            await execute_scheduled_workflow(client, workflow, config)
            mock_send.assert_not_called()


class TestWorkflowSerialization:
    """Test workflow serialization for Matrix state storage."""

    def test_workflow_json_serialization(self) -> None:
        """Test that workflows can be serialized to JSON and back."""
        workflow = ScheduledWorkflow(
            schedule_type="cron",
            cron_schedule=CronSchedule(minute="0", hour="9"),
            message="@finance Daily report",
            description="Daily finance report",
            room_id="!room:server",
            thread_id="$thread123",
            created_by="@user:server",
        )

        # Serialize to JSON
        json_str = workflow.model_dump_json()
        data = json.loads(json_str)

        # Deserialize back
        restored = ScheduledWorkflow(**data)

        assert restored.schedule_type == workflow.schedule_type
        assert restored.cron_schedule.to_cron_string() == workflow.cron_schedule.to_cron_string()
        assert restored.message == workflow.message
        assert restored.description == workflow.description
        assert restored.room_id == workflow.room_id


@pytest.mark.asyncio
class TestIntegrationWithScheduling:
    """Test integration with the main scheduling module."""

    @patch("mindroom.scheduling.parse_workflow_schedule")
    async def test_schedule_task_workflow_path(self, mock_parse_workflow: AsyncMock) -> None:
        """Test that schedule_task uses workflow parsing for complex requests."""
        client = AsyncMock()
        mock_parse_workflow.return_value = ScheduledWorkflow(
            schedule_type="cron",
            cron_schedule=CronSchedule(minute="0", hour="9"),
            message="@research Daily AI news",
            description="Daily AI research",
        )

        # Create a proper config with the research agent configured for the room
        import nio  # noqa: PLC0415

        from mindroom.config import AgentConfig, Config, RouterConfig  # noqa: PLC0415

        config = Config(
            agents={
                "research": AgentConfig(
                    display_name="Research",
                    role="Research agent",
                    rooms=["!room:server"],
                ),
            },
            router=RouterConfig(model="default"),
        )

        # Create a mock room with research agent using the correct MatrixID
        room = nio.MatrixRoom("!room:server", "@bot:server")
        research_matrix_id = config.ids["research"].full_id
        room.users[research_matrix_id] = nio.RoomMember(
            user_id=research_matrix_id,
            display_name="Research",
            avatar_url=None,
        )

        with patch("mindroom.scheduling.run_cron_task", new=AsyncMock()):
            task_id, message = await schedule_task(
                client=client,
                room_id="!room:server",
                thread_id="$thread123",
                scheduled_by="@user:server",
                full_text="Daily at 9am, research AI news",
                config=config,
                room=room,
            )

            assert task_id is not None
            assert "recurring task" in message
            assert "0 9 * * *" in message
