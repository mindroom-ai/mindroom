"""Tests for event-driven scheduling functionality."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.matrix.identity import MatrixID
from mindroom.scheduling.tasks import CronSchedule, ScheduledWorkflow, _parse_workflow_schedule
from tests.conftest import install_model_providers
from tests.fakes.scripted_model import ScriptedModel, answer
from tests.identity_helpers import persist_entity_accounts

if TYPE_CHECKING:
    from pathlib import Path


def _mid(name: str) -> MatrixID:
    return MatrixID(username=name, domain="localhost")


@pytest.fixture
def runtime_paths(tmp_path: Path) -> RuntimePaths:
    """Create isolated runtime paths for scheduling parser tests."""
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={},
    )


@pytest.fixture
def mock_config(runtime_paths: RuntimePaths) -> Config:
    """Create a typed config with test agents."""
    agent_names = (
        "email_assistant",
        "phone_agent",
        "crypto_agent",
        "notification_agent",
        "monitoring_agent",
        "ops_agent",
        "reddit_agent",
        "analyst",
        "ci_agent",
        "ticket_agent",
    )
    config = Config(
        agents={name: {"display_name": name.replace("_", " ").title()} for name in agent_names},
        models={"default": {"provider": "openai", "id": "gpt-6-astra"}},
    )
    persist_entity_accounts(config, runtime_paths)
    return config


@pytest.mark.asyncio
class TestEventDrivenScheduling:
    """Test event-driven scheduling conversions."""

    async def test_email_urgent_event(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_config: Config,
        runtime_paths: RuntimePaths,
    ) -> None:
        """Test converting 'if email urgent' to polling schedule."""
        # Create expected workflow response
        expected_workflow = ScheduledWorkflow(
            schedule_type="cron",
            is_conditional=True,
            cron_schedule=CronSchedule(minute="*/2", hour="*", day="*", month="*", weekday="*"),
            message="@email_assistant Check for emails containing 'urgent' in subject or body. If found, @phone_agent please call the user immediately about the urgent email.",
            description="Monitor for urgent emails and alert",
        )

        model = ScriptedModel([answer(expected_workflow.model_dump_json(exclude_unset=True))])
        install_model_providers(monkeypatch, {"default": model})

        # Execute
        result = await _parse_workflow_schedule(
            "If I get an email about 'urgent', call me",
            mock_config,
            runtime_paths,
            available_responders=[_mid("email_assistant"), _mid("phone_agent")],
        )

        # Verify the result is a workflow (not an error)
        assert isinstance(result, ScheduledWorkflow)
        assert result.schedule_type == "cron"

        # Verify the parser model was asked once, with the event guidance in its prompt
        [request] = model.requests
        call_args = request.messages[-1].get_content_string()
        assert "event-based" in call_args.lower()
        assert "if" in call_args.lower()
        assert "polling" in call_args.lower()

    async def test_bitcoin_price_event(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_config: Config,
        runtime_paths: RuntimePaths,
    ) -> None:
        """Test converting Bitcoin price condition to polling schedule."""
        expected_workflow = ScheduledWorkflow(
            schedule_type="cron",
            is_conditional=True,
            cron_schedule=CronSchedule(minute="*/5", hour="*", day="*", month="*", weekday="*"),
            message="@crypto_agent Check Bitcoin price. If below $40,000, @notification_agent alert the user about the price drop.",
            description="Monitor Bitcoin price threshold",
        )

        install_model_providers(
            monkeypatch,
            {"default": ScriptedModel([answer(expected_workflow.model_dump_json(exclude_unset=True))])},
        )

        # Execute
        result = await _parse_workflow_schedule(
            "When Bitcoin drops below $40k, notify me",
            mock_config,
            runtime_paths,
            available_responders=[_mid("crypto_agent"), _mid("notification_agent")],
        )

        # Verify
        assert isinstance(result, ScheduledWorkflow)
        assert result.schedule_type == "cron"

    async def test_server_monitoring_event(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_config: Config,
        runtime_paths: RuntimePaths,
    ) -> None:
        """Test converting server monitoring condition to polling schedule."""
        expected_workflow = ScheduledWorkflow(
            schedule_type="cron",
            is_conditional=True,
            cron_schedule=CronSchedule(minute="*", hour="*", day="*", month="*", weekday="*"),
            message="@monitoring_agent Check server CPU usage. If above 80%, @ops_agent scale up the servers immediately.",
            description="Monitor CPU and auto-scale",
        )

        install_model_providers(
            monkeypatch,
            {"default": ScriptedModel([answer(expected_workflow.model_dump_json(exclude_unset=True))])},
        )

        # Execute
        result = await _parse_workflow_schedule(
            "If server CPU goes above 80%, scale up",
            mock_config,
            runtime_paths,
            available_responders=[_mid("monitoring_agent"), _mid("ops_agent")],
        )

        # Verify
        assert isinstance(result, ScheduledWorkflow)
        assert result.schedule_type == "cron"

    async def test_build_failure_event(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_config: Config,
        runtime_paths: RuntimePaths,
    ) -> None:
        """Test converting build failure event to polling schedule."""
        expected_workflow = ScheduledWorkflow(
            schedule_type="cron",
            is_conditional=True,
            cron_schedule=CronSchedule(minute="*/5", hour="*", day="*", month="*", weekday="*"),
            message="@ci_agent Check the latest build status. If failed, @ticket_agent create a high-priority ticket with the failure details.",
            description="Monitor builds and create failure tickets",
        )

        install_model_providers(
            monkeypatch,
            {"default": ScriptedModel([answer(expected_workflow.model_dump_json(exclude_unset=True))])},
        )

        # Execute
        result = await _parse_workflow_schedule(
            "When the build fails, create a ticket",
            mock_config,
            runtime_paths,
            available_responders=[_mid("ci_agent"), _mid("ticket_agent")],
        )

        # Verify
        assert isinstance(result, ScheduledWorkflow)
        assert result.schedule_type == "cron"

    async def test_reddit_mention_event(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_config: Config,
        runtime_paths: RuntimePaths,
    ) -> None:
        """Test converting Reddit mention event to polling schedule."""
        expected_workflow = ScheduledWorkflow(
            schedule_type="cron",
            is_conditional=True,
            cron_schedule=CronSchedule(minute="*/10", hour="*", day="*", month="*", weekday="*"),
            message="@reddit_agent Check for new mentions of our product. If found, @analyst analyze the sentiment and key points of the discussions.",
            description="Monitor Reddit mentions and analyze",
        )

        install_model_providers(
            monkeypatch,
            {"default": ScriptedModel([answer(expected_workflow.model_dump_json(exclude_unset=True))])},
        )

        # Execute
        result = await _parse_workflow_schedule(
            "When someone mentions our product on Reddit, analyze it",
            mock_config,
            runtime_paths,
            available_responders=[_mid("reddit_agent"), _mid("analyst")],
        )

        # Verify
        assert isinstance(result, ScheduledWorkflow)
        assert result.schedule_type == "cron"

    async def test_boss_email_immediate_event(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_config: Config,
        runtime_paths: RuntimePaths,
    ) -> None:
        """Test converting boss email event with immediate urgency."""
        expected_workflow = ScheduledWorkflow(
            schedule_type="cron",
            is_conditional=True,
            cron_schedule=CronSchedule(minute="*", hour="*", day="*", month="*", weekday="*"),
            message="@email_assistant Check for new emails from boss. If any found, @notification_agent alert the user immediately.",
            description="Monitor for boss emails",
        )

        install_model_providers(
            monkeypatch,
            {"default": ScriptedModel([answer(expected_workflow.model_dump_json(exclude_unset=True))])},
        )

        # Execute
        result = await _parse_workflow_schedule(
            "Whenever I get an email from my boss, notify me immediately",
            mock_config,
            runtime_paths,
            available_responders=[_mid("email_assistant"), _mid("notification_agent")],
        )

        # Verify
        assert isinstance(result, ScheduledWorkflow)
        assert result.schedule_type == "cron"

    async def test_prompt_includes_event_examples(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_config: Config,
        runtime_paths: RuntimePaths,
    ) -> None:
        """Test that the prompt includes event-driven examples."""
        # We'll return a simple workflow just to complete the call
        simple_workflow = ScheduledWorkflow(
            schedule_type="once",
            execute_at=datetime.now(UTC),
            message="Test message",
            description="Test",
        )

        model = ScriptedModel([answer(simple_workflow.model_dump_json(exclude_unset=True))])
        install_model_providers(monkeypatch, {"default": model})

        # Execute
        await _parse_workflow_schedule(
            "Test request",
            mock_config,
            runtime_paths,
            available_responders=[_mid("test_agent")],
        )

        # Verify the prompt contains generic event-driven guidance
        [request] = model.requests
        call_args = request.messages[-1].get_content_string()

        # Check for schema-driven event guidance rather than trigger words
        assert "is_conditional=true" in call_args
        assert "is_conditional=false" in call_args
        assert "event" in call_args.lower()
        assert "condition" in call_args.lower()
        assert "polling" in call_args.lower()
        assert "check" in call_args.lower()

        # Check for specific examples
        assert "@email_assistant Check for emails" in call_args
        assert "@crypto_agent Check Bitcoin price" in call_args
        assert "@monitoring_agent Check server CPU" in call_args
        assert "@reddit_agent Check for new mentions" in call_args
