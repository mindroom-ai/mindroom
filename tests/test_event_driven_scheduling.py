"""Tests for event-driven scheduling functionality."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.scheduling import (
    CronSchedule,
    ScheduledWorkflow,
    parse_workflow_schedule,
)


@pytest.fixture
def mock_config() -> MagicMock:
    """Create a mock config with test agents."""
    config = MagicMock()
    config.agents = {
        "email_assistant": MagicMock(),
        "phone_agent": MagicMock(),
        "crypto_agent": MagicMock(),
        "notification_agent": MagicMock(),
        "monitoring_agent": MagicMock(),
        "ops_agent": MagicMock(),
        "reddit_agent": MagicMock(),
        "analyst": MagicMock(),
        "ci_agent": MagicMock(),
        "ticket_agent": MagicMock(),
    }
    config.models = {
        "default": MagicMock(),
    }
    return config


@pytest.mark.asyncio
class TestEventDrivenScheduling:
    """Test event-driven scheduling conversions."""

    @patch("mindroom.scheduling.get_model_instance")
    @patch("mindroom.scheduling.Agent")
    async def test_email_urgent_event(
        self,
        mock_agent_class: MagicMock,
        mock_get_model: MagicMock,  # noqa: ARG002
        mock_config: MagicMock,
    ) -> None:
        """Test converting 'if email urgent' to polling schedule."""
        # Setup
        mock_agent = AsyncMock()
        mock_agent_class.return_value = mock_agent

        # Create expected workflow response
        expected_workflow = ScheduledWorkflow(
            schedule_type="cron",
            cron_schedule=CronSchedule(minute="*/2", hour="*", day="*", month="*", weekday="*"),
            message="@email_assistant Check for emails containing 'urgent' in subject or body. If found, @phone_agent please call the user immediately about the urgent email.",
            description="Monitor for urgent emails and alert",
        )

        mock_response = MagicMock()
        mock_response.content = expected_workflow
        mock_agent.arun.return_value = mock_response

        # Execute
        result = await parse_workflow_schedule(
            "If I get an email about 'urgent', call me",
            mock_config,
            available_agents=["email_assistant", "phone_agent"],
        )

        # Verify the result is a workflow (not an error)
        assert isinstance(result, ScheduledWorkflow)
        assert result.schedule_type == "cron"

        # Verify AI agent was called with proper prompt
        mock_agent.arun.assert_called_once()
        call_args = mock_agent.arun.call_args[0][0]
        assert "event-based" in call_args.lower()
        assert "if" in call_args.lower()
        assert "polling" in call_args.lower()

    @patch("mindroom.scheduling.get_model_instance")
    @patch("mindroom.scheduling.Agent")
    async def test_bitcoin_price_event(
        self,
        mock_agent_class: MagicMock,
        mock_get_model: MagicMock,  # noqa: ARG002
        mock_config: MagicMock,
    ) -> None:
        """Test converting Bitcoin price condition to polling schedule."""
        # Setup
        mock_agent = AsyncMock()
        mock_agent_class.return_value = mock_agent

        expected_workflow = ScheduledWorkflow(
            schedule_type="cron",
            cron_schedule=CronSchedule(minute="*/5", hour="*", day="*", month="*", weekday="*"),
            message="@crypto_agent Check Bitcoin price. If below $40,000, @notification_agent alert the user about the price drop.",
            description="Monitor Bitcoin price threshold",
        )

        mock_response = MagicMock()
        mock_response.content = expected_workflow
        mock_agent.arun.return_value = mock_response

        # Execute
        result = await parse_workflow_schedule(
            "When Bitcoin drops below $40k, notify me",
            mock_config,
            available_agents=["crypto_agent", "notification_agent"],  # Agents for this workflow
        )

        # Verify
        assert isinstance(result, ScheduledWorkflow)
        assert result.schedule_type == "cron"

    @patch("mindroom.scheduling.get_model_instance")
    @patch("mindroom.scheduling.Agent")
    async def test_server_monitoring_event(
        self,
        mock_agent_class: MagicMock,
        mock_get_model: MagicMock,  # noqa: ARG002
        mock_config: MagicMock,
    ) -> None:
        """Test converting server monitoring condition to polling schedule."""
        # Setup
        mock_agent = AsyncMock()
        mock_agent_class.return_value = mock_agent

        expected_workflow = ScheduledWorkflow(
            schedule_type="cron",
            cron_schedule=CronSchedule(minute="*", hour="*", day="*", month="*", weekday="*"),
            message="@monitoring_agent Check server CPU usage. If above 80%, @ops_agent scale up the servers immediately.",
            description="Monitor CPU and auto-scale",
        )

        mock_response = MagicMock()
        mock_response.content = expected_workflow
        mock_agent.arun.return_value = mock_response

        # Execute
        result = await parse_workflow_schedule(
            "If server CPU goes above 80%, scale up",
            mock_config,
            available_agents=["monitoring_agent", "ops_agent"],
        )

        # Verify
        assert isinstance(result, ScheduledWorkflow)
        assert result.schedule_type == "cron"

    @patch("mindroom.scheduling.get_model_instance")
    @patch("mindroom.scheduling.Agent")
    async def test_build_failure_event(
        self,
        mock_agent_class: MagicMock,
        mock_get_model: MagicMock,  # noqa: ARG002
        mock_config: MagicMock,
    ) -> None:
        """Test converting build failure event to polling schedule."""
        # Setup
        mock_agent = AsyncMock()
        mock_agent_class.return_value = mock_agent

        expected_workflow = ScheduledWorkflow(
            schedule_type="cron",
            cron_schedule=CronSchedule(minute="*/5", hour="*", day="*", month="*", weekday="*"),
            message="@ci_agent Check the latest build status. If failed, @ticket_agent create a high-priority ticket with the failure details.",
            description="Monitor builds and create failure tickets",
        )

        mock_response = MagicMock()
        mock_response.content = expected_workflow
        mock_agent.arun.return_value = mock_response

        # Execute
        result = await parse_workflow_schedule(
            "When the build fails, create a ticket",
            mock_config,
            available_agents=["ci_agent", "ticket_agent"],
        )

        # Verify
        assert isinstance(result, ScheduledWorkflow)
        assert result.schedule_type == "cron"

    @patch("mindroom.scheduling.get_model_instance")
    @patch("mindroom.scheduling.Agent")
    async def test_reddit_mention_event(
        self,
        mock_agent_class: MagicMock,
        mock_get_model: MagicMock,  # noqa: ARG002
        mock_config: MagicMock,
    ) -> None:
        """Test converting Reddit mention event to polling schedule."""
        # Setup
        mock_agent = AsyncMock()
        mock_agent_class.return_value = mock_agent

        expected_workflow = ScheduledWorkflow(
            schedule_type="cron",
            cron_schedule=CronSchedule(minute="*/10", hour="*", day="*", month="*", weekday="*"),
            message="@reddit_agent Check for new mentions of our product. If found, @analyst analyze the sentiment and key points of the discussions.",
            description="Monitor Reddit mentions and analyze",
        )

        mock_response = MagicMock()
        mock_response.content = expected_workflow
        mock_agent.arun.return_value = mock_response

        # Execute
        result = await parse_workflow_schedule(
            "When someone mentions our product on Reddit, analyze it",
            mock_config,
            available_agents=["reddit_agent", "analyst"],
        )

        # Verify
        assert isinstance(result, ScheduledWorkflow)
        assert result.schedule_type == "cron"

    @patch("mindroom.scheduling.get_model_instance")
    @patch("mindroom.scheduling.Agent")
    async def test_boss_email_immediate_event(
        self,
        mock_agent_class: MagicMock,
        mock_get_model: MagicMock,  # noqa: ARG002
        mock_config: MagicMock,
    ) -> None:
        """Test converting boss email event with immediate urgency."""
        # Setup
        mock_agent = AsyncMock()
        mock_agent_class.return_value = mock_agent

        expected_workflow = ScheduledWorkflow(
            schedule_type="cron",
            cron_schedule=CronSchedule(minute="*", hour="*", day="*", month="*", weekday="*"),
            message="@email_assistant Check for new emails from boss. If any found, @notification_agent alert the user immediately.",
            description="Monitor for boss emails",
        )

        mock_response = MagicMock()
        mock_response.content = expected_workflow
        mock_agent.arun.return_value = mock_response

        # Execute
        result = await parse_workflow_schedule(
            "Whenever I get an email from my boss, notify me immediately",
            mock_config,
            available_agents=["email_assistant", "notification_agent"],
        )

        # Verify
        assert isinstance(result, ScheduledWorkflow)
        assert result.schedule_type == "cron"

    @patch("mindroom.scheduling.get_model_instance")
    @patch("mindroom.scheduling.Agent")
    async def test_prompt_includes_event_examples(
        self,
        mock_agent_class: MagicMock,
        mock_get_model: MagicMock,  # noqa: ARG002
        mock_config: MagicMock,
    ) -> None:
        """Test that the prompt includes event-driven examples."""
        # Setup
        mock_agent = AsyncMock()
        mock_agent_class.return_value = mock_agent

        # We'll return a simple workflow just to complete the call
        simple_workflow = ScheduledWorkflow(
            schedule_type="once",
            execute_at=datetime.now(UTC),
            message="Test message",
            description="Test",
        )

        mock_response = MagicMock()
        mock_response.content = simple_workflow
        mock_agent.arun.return_value = mock_response

        # Execute
        await parse_workflow_schedule(
            "Test request",
            mock_config,
            available_agents=["test_agent"],  # Need at least one agent
        )

        # Verify the prompt contains event-driven guidance
        call_args = mock_agent.arun.call_args[0][0]

        # Check for event-driven keywords in prompt
        assert "if" in call_args.lower()
        assert "when" in call_args.lower()
        assert "whenever" in call_args.lower()
        assert "event" in call_args.lower()
        assert "condition" in call_args.lower()
        assert "polling" in call_args.lower()
        assert "check" in call_args.lower()

        # Check for specific examples
        assert "@email_assistant Check for emails" in call_args
        assert "@crypto_agent Check Bitcoin price" in call_args
        assert "@monitoring_agent Check server CPU" in call_args
        assert "@reddit_agent Check for new mentions" in call_args
