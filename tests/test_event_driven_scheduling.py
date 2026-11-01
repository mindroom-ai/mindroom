"""Tests for event-driven scheduling functionality."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.llm.errors import ProviderError
from mindroom.matrix.identity import MatrixID
from mindroom.scheduling.errors import SchedulingError
from mindroom.scheduling.tasks import CronSchedule, ScheduledWorkflow, _parse_workflow_schedule
from tests.conftest import install_model_providers
from tests.fakes.scripted_model import Await, ScriptedModel, answer
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


def _polling_workflow(minute: str, message: str, description: str) -> ScheduledWorkflow:
    """Return the conditional polling workflow a parser would write for an event-driven request."""
    return ScheduledWorkflow(
        schedule_type="cron",
        is_conditional=True,
        cron_schedule=CronSchedule(minute=minute, hour="*", day="*", month="*", weekday="*"),
        message=message,
        description=description,
    )


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
        expected_workflow = _polling_workflow(
            "*/2",
            "@email_assistant Check for emails containing 'urgent' in subject or body. If found, @phone_agent please call the user immediately about the urgent email.",
            "Monitor for urgent emails and alert",
        )

        model = ScriptedModel([answer(expected_workflow.model_dump_json(exclude_unset=True))])
        install_model_providers(monkeypatch, {"default": model})

        result = await _parse_workflow_schedule(
            "If I get an email about 'urgent', call me",
            mock_config,
            runtime_paths,
            available_responders=[_mid("email_assistant"), _mid("phone_agent")],
            timezone="UTC",
        )

        assert isinstance(result, ScheduledWorkflow)
        assert result.schedule_type == "cron"

        [request] = model.requests
        call_args = request.messages[-1].get_content_string()
        assert "event-based" in call_args.lower()
        assert "if" in call_args.lower()
        assert "polling" in call_args.lower()

    @pytest.mark.parametrize(
        ("request_text", "workflow", "responders"),
        [
            pytest.param(
                "When Bitcoin drops below $40k, notify me",
                _polling_workflow(
                    "*/5",
                    "@crypto_agent Check Bitcoin price. If below $40,000, @notification_agent alert the user about the price drop.",
                    "Monitor Bitcoin price threshold",
                ),
                ("crypto_agent", "notification_agent"),
                id="bitcoin_price",
            ),
            pytest.param(
                "If server CPU goes above 80%, scale up",
                _polling_workflow(
                    "*",
                    "@monitoring_agent Check server CPU usage. If above 80%, @ops_agent scale up the servers immediately.",
                    "Monitor CPU and auto-scale",
                ),
                ("monitoring_agent", "ops_agent"),
                id="server_monitoring",
            ),
            pytest.param(
                "When the build fails, create a ticket",
                _polling_workflow(
                    "*/5",
                    "@ci_agent Check the latest build status. If failed, @ticket_agent create a high-priority ticket with the failure details.",
                    "Monitor builds and create failure tickets",
                ),
                ("ci_agent", "ticket_agent"),
                id="build_failure",
            ),
            pytest.param(
                "When someone mentions our product on Reddit, analyze it",
                _polling_workflow(
                    "*/10",
                    "@reddit_agent Check for new mentions of our product. If found, @analyst analyze the sentiment and key points of the discussions.",
                    "Monitor Reddit mentions and analyze",
                ),
                ("reddit_agent", "analyst"),
                id="reddit_mention",
            ),
            pytest.param(
                "Whenever I get an email from my boss, notify me immediately",
                _polling_workflow(
                    "*",
                    "@email_assistant Check for new emails from boss. If any found, @notification_agent alert the user immediately.",
                    "Monitor for boss emails",
                ),
                ("email_assistant", "notification_agent"),
                id="boss_email",
            ),
        ],
    )
    async def test_event_request_parses_to_polling_schedule(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mock_config: Config,
        runtime_paths: RuntimePaths,
        request_text: str,
        workflow: ScheduledWorkflow,
        responders: tuple[str, ...],
    ) -> None:
        """An event-driven request round-trips through the parser as a recurring polling schedule."""
        model = ScriptedModel([answer(workflow.model_dump_json(exclude_unset=True))])
        install_model_providers(monkeypatch, {"default": model})

        result = await _parse_workflow_schedule(
            request_text,
            mock_config,
            runtime_paths,
            available_responders=[_mid(name) for name in responders],
            timezone="UTC",
        )

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

        await _parse_workflow_schedule(
            "Test request",
            mock_config,
            runtime_paths,
            available_responders=[_mid("test_agent")],
            timezone="UTC",
        )

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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "text"),
    [
        (RuntimeError("parser unavailable"), "parser unavailable"),
        (TimeoutError(), "TimeoutError"),
        (ProviderError("AI service error", provider="Synthetic"), "AI service error"),
    ],
    ids=["with_text", "without_text", "provider_error_is_not_a_refusal"],
)
async def test_a_parser_call_that_raises_reports_why(
    monkeypatch: pytest.MonkeyPatch,
    mock_config: Config,
    runtime_paths: RuntimePaths,
    error: Exception,
    text: str,
) -> None:
    """A parser call that raises reports the exception's text, or its type name when it has none."""

    async def fail() -> None:
        raise error

    install_model_providers(monkeypatch, {"default": ScriptedModel([[Await(fail)]])})

    with pytest.raises(SchedulingError) as exc_info:
        await _parse_workflow_schedule(
            "Every morning, check the queue",
            mock_config,
            runtime_paths,
            available_responders=[_mid("ops_agent")],
            timezone="UTC",
        )

    assert str(exc_info.value) == (
        f"Error parsing schedule: {text}\n\n💡 Try a simpler format like 'Daily at 9am, check my email'"
    )
