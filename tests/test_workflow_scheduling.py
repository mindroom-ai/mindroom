"""Tests for workflow scheduling functionality."""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import nio
import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, RouterConfig
from mindroom.constants import ORIGINAL_SENDER_KEY
from mindroom.entity_resolution import entity_identity_registry
from mindroom.hooks import HookRegistry, HookRegistryState
from mindroom.matrix.client_delivery import DeliveredMatrixEvent, MatrixDeliveryFailure, MatrixDeliveryFailureKind
from mindroom.message_target import MessageTarget
from mindroom.scheduling.executor import execute_scheduled_workflow, send_scheduled_failure_notice
from mindroom.scheduling.recurring_schedule import next_cron_run_utc
from mindroom.scheduling.tasks import (
    CronSchedule,
    ScheduledTaskRecord,
    ScheduledWorkflow,
    build_edited_scheduled_workflow,
    edit_scheduled_task,
    schedule_task,
    scheduled_cron_timezone,
)
from tests.authorization_helpers import (
    make_test_scheduling_runtime,
)
from tests.conftest import (
    bind_runtime_paths,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.identity_helpers import persist_entity_accounts
from tests.scheduling_helpers import serve_task_state_events


def _runtime_bound_config(config: Config, *, usernames: dict[str, str] | None = None) -> Config:
    """Return a runtime-bound workflow config whose entity accounts use ``usernames`` where given."""
    runtime_paths = test_runtime_paths(Path(tempfile.mkdtemp()))
    bound_config = bind_runtime_paths(config, runtime_paths)
    persist_entity_accounts(bound_config, runtime_paths, usernames=usernames)
    return bound_config


def _research_room_config() -> tuple[Config, nio.MatrixRoom]:
    """Return a config whose research agent is configured for ``!room:server`` and that room with research in it."""
    config = _runtime_bound_config(
        Config(
            agents={
                "research": AgentConfig(
                    display_name="Research",
                    role="Research agent",
                    rooms=["!room:server"],
                ),
            },
            router=RouterConfig(model="default"),
        ),
        usernames={"router": "router", "research": "research"},
    )
    room = nio.MatrixRoom("!room:server", "@bot:server")
    research_matrix_id = entity_identity_registry(config, runtime_paths_for(config)).current_id("research").full_id
    room.users[research_matrix_id] = nio.RoomMember(
        user_id=research_matrix_id,
        display_name="Research",
        avatar_url=None,
    )
    room.members_synced = True
    return config, room


def _conversation_reader(*, latest_thread_event_id: str | None = None) -> AsyncMock:
    reader = AsyncMock()
    reader.latest_thread_event_id = AsyncMock(return_value=latest_thread_event_id)
    return reader


def test_naive_execute_at_is_treated_as_utc() -> None:
    """Naive parser output must be pinned to UTC so instants compare against datetime.now(UTC)."""
    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime(2026, 7, 3, 23, 45),  # noqa: DTZ001
        message="Reminder",
        description="Reminder",
    )
    assert workflow.execute_at == datetime(2026, 7, 3, 23, 45, tzinfo=UTC)


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

    def test_local_time_cron_round_trips_and_tracks_dst(self) -> None:
        """Local-time crons describe directly in cron_timezone and track DST, while legacy UTC crons shift for display."""
        schedule = CronSchedule(minute="0", hour="9")
        assert (
            schedule.to_natural_language(
                "America/Los_Angeles",
                datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
                cron_timezone="America/Los_Angeles",
            )
            == "At 09:00 (America/Los_Angeles)"
        )
        assert (
            schedule.to_natural_language(
                "America/Los_Angeles",
                datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
                cron_timezone="UTC",
            )
            == "At 01:00 (America/Los_Angeles)"
        )

        # Across US spring-forward (2026-03-08) and fall-back (2026-11-01), 09:00 America/Los_Angeles
        # moves between 17:00 UTC (PST) and 16:00 UTC (PDT).
        spring_next = next_cron_run_utc(
            schedule.to_cron_string(),
            datetime(2026, 3, 7, 17, 0, tzinfo=UTC),
            "America/Los_Angeles",
        )
        assert spring_next == datetime(2026, 3, 8, 16, 0, tzinfo=UTC)
        fall_next = next_cron_run_utc(
            schedule.to_cron_string(),
            datetime(2026, 10, 31, 16, 0, tzinfo=UTC),
            "America/Los_Angeles",
        )
        assert fall_next == datetime(2026, 11, 1, 17, 0, tzinfo=UTC)

        local_workflow = ScheduledWorkflow(
            schedule_type="cron",
            cron_schedule=schedule,
            message="Standup",
            description="Standup",
            timezone="America/Los_Angeles",
            cron_timezone="America/Los_Angeles",
        )
        reloaded_local = ScheduledWorkflow.model_validate_json(local_workflow.model_dump_json())
        assert reloaded_local.cron_timezone == "America/Los_Angeles"
        assert scheduled_cron_timezone(reloaded_local) == "America/Los_Angeles"

        legacy_workflow = ScheduledWorkflow(
            schedule_type="cron",
            cron_schedule=schedule,
            message="Standup",
            description="Standup",
            timezone="America/Los_Angeles",
        )
        assert scheduled_cron_timezone(legacy_workflow) == "UTC"


class TestScheduledWorkflow:
    """Test ScheduledWorkflow model."""

    def test_history_limit_round_trips_through_persisted_json(self) -> None:
        """The per-schedule history limit must survive the Matrix-state serialization format."""
        workflow = ScheduledWorkflow(
            schedule_type="cron",
            cron_schedule=CronSchedule(minute="*/25"),
            message="Poll the queue",
            description="Queue poller",
            history_limit=0,
        )

        restored = ScheduledWorkflow(**json.loads(workflow.model_dump_json()))

        assert restored.history_limit == 0
        assert restored == workflow

    def test_persisted_workflow_without_history_limit_loads_as_unlimited(self) -> None:
        """Tasks persisted before the field existed must load with full-history behavior."""
        legacy_payload = json.dumps(
            {
                "schedule_type": "once",
                "execute_at": "2026-07-12T09:00:00+00:00",
                "message": "Check deployment",
                "description": "Deployment check",
            },
        )

        restored = ScheduledWorkflow(**json.loads(legacy_payload))

        assert restored.history_limit is None

    def test_negative_history_limit_is_rejected(self) -> None:
        """The parse schema must never accept a negative history limit."""
        with pytest.raises(ValueError, match="history_limit"):
            ScheduledWorkflow(
                schedule_type="once",
                execute_at=datetime.now(UTC),
                message="Check deployment",
                description="Deployment check",
                history_limit=-1,
            )

    def test_message_target_for_scheduled_task_uses_persisted_thread_id(self) -> None:
        """Scheduled workflows should honor the persisted thread even if live routing is room mode."""
        workflow = ScheduledWorkflow(
            schedule_type="once",
            execute_at=datetime.now(UTC),
            message="Check the queue depth",
            description="Queue check",
            room_id="!room:server",
            thread_id="$thread-root",
            new_thread=False,
        )

        target = MessageTarget.for_scheduled_task(workflow)

        assert target.resolved_thread_id == "$thread-root"
        assert target.session_id == "!room:server:$thread-root"


@pytest.mark.asyncio
class TestExecuteScheduledWorkflow:
    """Test execute_scheduled_workflow function."""

    async def test_execute_workflow_with_agents(self) -> None:
        """Current-scope workflow execution should keep the automated threaded wrapper."""
        client = AsyncMock()
        config = _runtime_bound_config(
            Config(
                agents={
                    "research": AgentConfig(display_name="Research"),
                    "analyst": AgentConfig(display_name="Analyst"),
                },
                models={"default": ModelConfig(provider="openai", id="test-model")},
            ),
            usernames={"router": "router", "research": "research", "analyst": "analyst"},
        )
        workflow = ScheduledWorkflow(
            schedule_type="once",
            execute_at=datetime.now(UTC),
            message="@research @analyst Please analyze the latest AI trends",
            description="AI trend analysis",
            thread_id="$thread123",
            room_id="!room:server",
            created_by="@user:server",
        )

        conversation_reader = _conversation_reader(latest_thread_event_id="$latest456")
        with patch(
            "mindroom.matrix.client_delivery.send_message_outcome",
            new=AsyncMock(
                return_value=DeliveredMatrixEvent(
                    event_id="$event123",
                    content_sent={"body": "sent"},
                ),
            ),
        ) as mock_send:
            await execute_scheduled_workflow(
                client,
                workflow,
                config,
                runtime_paths_for(config),
                conversation_reader,
                HookRegistryState(HookRegistry.empty()),
            )

        conversation_reader.latest_thread_event_id.assert_awaited_once_with(
            room_id="!room:server",
            thread_id="$thread123",
        )
        mock_send.assert_awaited_once()
        call_args = mock_send.await_args
        assert call_args.args[0] == client
        assert call_args.args[1] == "!room:server"
        content = call_args.args[2]
        assert content["body"].startswith("⏰ [Automated Task]\n")
        registry = entity_identity_registry(config, runtime_paths_for(config))
        assert registry.current_id("research").full_id in content["body"]
        assert registry.current_id("analyst").full_id in content["body"]
        assert content["m.relates_to"]["event_id"] == "$thread123"
        assert content[ORIGINAL_SENDER_KEY] == "@user:server"

    async def test_execute_workflow_new_thread_posts_room_level_message(self) -> None:
        """New-thread workflow execution should post a plain room-level message."""
        client = AsyncMock()
        config = _runtime_bound_config(
            Config(
                agents={
                    "research": AgentConfig(display_name="Research"),
                    "analyst": AgentConfig(display_name="Analyst"),
                },
                models={"default": ModelConfig(provider="openai", id="test-model")},
            ),
            usernames={"router": "router", "research": "research", "analyst": "analyst"},
        )
        workflow = ScheduledWorkflow(
            schedule_type="once",
            execute_at=datetime.now(UTC),
            message="@research @analyst Please analyze the latest AI trends",
            description="AI trend analysis",
            thread_id=None,
            room_id="!room:server",
            created_by="@user:server",
            new_thread=True,
        )

        with patch(
            "mindroom.matrix.client_delivery.send_message_outcome",
            new=AsyncMock(
                return_value=DeliveredMatrixEvent(
                    event_id="$event456",
                    content_sent={"body": "sent"},
                ),
            ),
        ) as mock_send:
            conversation_reader = _conversation_reader()
            await execute_scheduled_workflow(
                client,
                workflow,
                config,
                runtime_paths_for(config),
                conversation_reader,
                HookRegistryState(HookRegistry.empty()),
            )

        conversation_reader.latest_thread_event_id.assert_not_awaited()
        mock_send.assert_awaited_once()
        content = mock_send.await_args.args[2]
        assert "⏰ [Automated Task]" not in content["body"]
        registry = entity_identity_registry(config, runtime_paths_for(config))
        assert registry.current_id("research").full_id in content["body"]
        assert registry.current_id("analyst").full_id in content["body"]
        assert "m.relates_to" not in content
        assert content[ORIGINAL_SENDER_KEY] == "@user:server"

    async def test_scheduled_failure_notice_labels_latest_thread_lookup(self) -> None:
        """Scheduled failure notices should attribute latest-thread lookups."""
        target = MessageTarget.resolve("!room:server", "$thread123", None)
        conversation_reader = _conversation_reader(latest_thread_event_id="$latest456")

        with patch(
            "mindroom.matrix.client_delivery.send_message_outcome",
            new=AsyncMock(
                return_value=DeliveredMatrixEvent(
                    event_id="$notice123",
                    content_sent={"body": "sent"},
                ),
            ),
        ) as mock_send:
            await send_scheduled_failure_notice(
                AsyncMock(),
                target,
                "Workflow failed",
                conversation_reader,
            )

        mock_send.assert_awaited_once()
        assert mock_send.await_args.args[1] == "!room:server"
        content = mock_send.await_args.args[2]
        assert content["body"] == "Workflow failed"
        assert content["m.relates_to"]["m.in_reply_to"]["event_id"] == "$latest456"
        conversation_reader.latest_thread_event_id.assert_awaited_once_with(
            room_id="!room:server",
            thread_id="$thread123",
        )

    async def test_execute_workflow_simple_reminder(self) -> None:
        """Test executing a simple reminder without agents."""
        client = AsyncMock()
        config = _runtime_bound_config(Config())
        workflow = ScheduledWorkflow(
            schedule_type="once",
            execute_at=datetime.now(UTC),
            message="Check the server status",
            description="Server check reminder",
            room_id="!room:server",
        )

        with patch(
            "mindroom.matrix.client_delivery.send_message_outcome",
            new=AsyncMock(
                return_value=DeliveredMatrixEvent(
                    event_id="$event789",
                    content_sent={"body": "sent"},
                ),
            ),
        ) as mock_send:
            await execute_scheduled_workflow(
                client,
                workflow,
                config,
                runtime_paths_for(config),
                _conversation_reader(latest_thread_event_id="$thread123"),
                HookRegistryState(HookRegistry.empty()),
            )
            mock_send.assert_awaited_once()

            # Check the message content
            content = mock_send.await_args.args[2]
            assert content["body"].startswith("⏰ [Automated Task]\n")
            assert "Check the server status" in content["body"]
            assert "m.relates_to" not in content  # No thread
            assert ORIGINAL_SENDER_KEY not in content

    async def test_execute_workflow_error_handling(self) -> None:
        """Test error handling in execute_scheduled_workflow."""
        client = AsyncMock()
        config = _runtime_bound_config(Config())
        workflow = ScheduledWorkflow(
            schedule_type="once",
            execute_at=datetime.now(UTC),
            message="Test message",
            description="Test task",
            room_id="!room:server",
            thread_id="$thread123",
        )

        # Mock send_message to raise an error only on the first call
        mock_send = AsyncMock(
            side_effect=[
                Exception("Send failed"),
                DeliveredMatrixEvent(event_id="$error123", content_sent={"body": "error"}),
            ],
        )

        with patch("mindroom.matrix.client_delivery.send_message_outcome", new=mock_send):
            # Should not raise, but log error
            await execute_scheduled_workflow(
                client,
                workflow,
                config,
                runtime_paths_for(config),
                _conversation_reader(latest_thread_event_id="$thread123"),
                HookRegistryState(HookRegistry.empty()),
            )

            # Should have tried to send original and error message
            assert mock_send.call_count == 2

            # Check error message was sent
            error_call = mock_send.call_args_list[1]
            error_content = error_call[0][2]
            assert "failed" in error_content["body"].lower()

    async def test_execute_workflow_typed_send_failure_is_reported(self) -> None:
        """A typed send failure should trigger failure handling instead of success logging."""
        client = AsyncMock()
        config = _runtime_bound_config(Config())
        workflow = ScheduledWorkflow(
            schedule_type="once",
            execute_at=datetime.now(UTC),
            message="Check the queue depth",
            description="Queue check",
            room_id="!room:server",
            thread_id="$thread123",
        )

        with (
            patch(
                "mindroom.matrix.client_delivery.send_message_outcome",
                new=AsyncMock(
                    side_effect=[
                        MatrixDeliveryFailure(MatrixDeliveryFailureKind.SEND_EXCEPTION, "Send failed"),
                        DeliveredMatrixEvent(
                            event_id="$error456",
                            content_sent={"body": "error"},
                        ),
                    ],
                ),
            ) as mock_send,
            patch("mindroom.scheduling.executor.logger.info") as mock_info,
        ):
            conversation_reader = _conversation_reader(latest_thread_event_id="$latest123")
            await execute_scheduled_workflow(
                client,
                workflow,
                config,
                runtime_paths_for(config),
                conversation_reader,
                HookRegistryState(HookRegistry.empty()),
            )

        assert mock_send.await_count == 2
        mock_info.assert_not_called()
        error_content = mock_send.await_args_list[1].args[2]
        assert "Scheduled task failed" in error_content["body"]


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
            thread_id=None,
            created_by="@user:server",
            new_thread=True,
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
        assert restored.new_thread is True

    def test_workflow_old_payload_defaults_new_thread_false(self) -> None:
        """Older persisted workflows should deserialize with new_thread=False."""
        data = {
            "schedule_type": "once",
            "execute_at": datetime(2026, 2, 1, 10, 0, tzinfo=UTC).isoformat(),
            "message": "Check deployment",
            "description": "Deployment check",
            "room_id": "!room:server",
            "thread_id": "$thread123",
            "created_by": "@user:server",
        }

        restored = ScheduledWorkflow(**data)

        assert restored.new_thread is False

    def test_workflow_silent_flag_round_trips_and_edits_as_a_patch(self) -> None:
        """Persisted workflows should default silently and retain explicit mode edits."""
        old_payload = json.dumps(
            {
                "schedule_type": "once",
                "execute_at": "2026-02-01T10:00:00+00:00",
                "message": "Check deployment",
                "description": "Deployment check",
            },
        )

        workflow = ScheduledWorkflow.model_validate_json(old_payload)

        assert workflow.silent is False
        assert (
            ScheduledWorkflow.model_validate_json(
                workflow.model_copy(update={"silent": True}).model_dump_json(),
            ).silent
            is True
        )

        edited = build_edited_scheduled_workflow(workflow, room_id="!room:test", silent=True)
        assert edited.silent is True
        assert build_edited_scheduled_workflow(edited, room_id="!room:test").silent is True
        assert build_edited_scheduled_workflow(edited, room_id="!room:test", silent=False).silent is False

    @pytest.mark.parametrize(
        "legacy_payload",
        [
            (
                '{"schedule_type":"cron","is_conditional":false,"execute_at":null,'
                '"cron_schedule":{"minute":"0","hour":"9","day":"*","month":"*","weekday":"*"},'
                '"message":"Daily check","description":"Daily check","history_limit":null,"model":null,'
                '"created_by":"@user:test","thread_id":"$thread1","room_id":"!room:test",'
                '"new_thread":false,"silent":false,"pre_approved_call":false,'
                '"timezone":"America/Los_Angeles","source_event_id":"$src1"}'
            ),
            (
                '{"schedule_type":"once","is_conditional":true,"execute_at":"2026-12-01T15:00:00Z",'
                '"cron_schedule":null,"message":"Check deploy","description":"Deploy check",'
                '"history_limit":5,"model":"cheap","created_by":"@user:test","thread_id":null,'
                '"room_id":"!room:test","new_thread":true,"silent":true,"pre_approved_call":false,'
                '"timezone":"UTC","source_event_id":null}'
            ),
        ],
        ids=["legacy_cron_false", "legacy_once_conditional_true"],
    )
    def test_legacy_workflow_json_is_byte_identical(self, legacy_payload: str) -> None:
        """Persisted legacy workflows without cron_timezone re-serialize to identical JSON bytes."""
        workflow = ScheduledWorkflow.model_validate_json(legacy_payload)
        assert workflow.cron_timezone is None
        assert workflow.model_dump_json() == legacy_payload


@pytest.mark.asyncio
class TestIntegrationWithScheduling:
    """Test integration with the main scheduling module."""

    async def test_schedule_task_workflow_path(self) -> None:
        """Test that schedule_task persists a local-time recurring cron workflow."""
        client = AsyncMock()
        client.room_put_state = AsyncMock(return_value=nio.RoomPutStateResponse("$scheduled-state", "!room:server"))
        config, room = _research_room_config()

        message = await schedule_task(
            runtime=make_test_scheduling_runtime(
                client=client,
                config=config,
                runtime_paths=runtime_paths_for(config),
                room=room,
            ),
            room_id="!room:server",
            thread_id="$thread123",
            scheduled_by="@user:server",
            message="@research Daily AI news",
            description="Daily AI research",
            cron="0 9 * * *",
            timezone="America/Los_Angeles",
            new_thread=False,
        )

        assert "\n**Task ID:** `" in message
        assert "recurring task" in message
        assert "0 9 * * *" in message
        assert "America/Los_Angeles" in message

    @pytest.mark.parametrize(
        ("original_message", "clear_history_limit", "stored_history_limit"),
        [
            pytest.param(
                "tool-audit schedule test — safe to ignore",
                True,
                None,
                id="restore_full_history_clears_limit",
            ),
            pytest.param(
                "tool-audit schedule test - safe to ignore",
                False,
                5,
                id="omitted_history_limit_keeps_limit",
            ),
        ],
    )
    async def test_edit_preserves_message_and_updates_history_limit(
        self,
        original_message: str,
        clear_history_limit: bool,
        stored_history_limit: int | None,
    ) -> None:
        """An edit keeps the original scheduled message and updates or preserves history_limit."""
        client = AsyncMock()
        client.room_put_state = AsyncMock(return_value=nio.RoomPutStateResponse("$scheduled-state", "!room:server"))
        config, room = _research_room_config()

        existing_task = ScheduledTaskRecord(
            task_id="task123",
            room_id="!room:server",
            status="pending",
            created_at=datetime.now(UTC) - timedelta(hours=1),
            workflow=ScheduledWorkflow(
                schedule_type="once",
                execute_at=datetime.now(UTC) + timedelta(hours=2),
                message=original_message,
                description="Post a test reminder in the current thread.",
                history_limit=5,
                room_id="!room:server",
                thread_id="$thread123",
                created_by="@user:server",
            ),
        )

        assert existing_task.created_at is not None
        client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
            content={
                "status": "pending",
                "workflow": existing_task.workflow.model_dump_json(),
                "created_at": existing_task.created_at.isoformat(),
            },
            event_type="com.mindroom.scheduled.task",
            state_key="task123",
            room_id="!room:server",
        )
        serve_task_state_events(
            client,
            sender=entity_identity_registry(config, runtime_paths_for(config)).current_id("router").full_id,
        )

        message = await edit_scheduled_task(
            runtime=make_test_scheduling_runtime(
                client=client,
                config=config,
                runtime_paths=runtime_paths_for(config),
                room=room,
            ),
            room_id="!room:server",
            task_id="task123",
            scheduled_by="@user:server",
            delay_seconds=6 * 3600,
            clear_history_limit=clear_history_limit,
        )

        assert "**Task ID:** `task123`" in message
        assert f"**Will post:** {original_message}" in message
        stored_content = client.room_put_state.await_args.kwargs["content"]
        stored_workflow = ScheduledWorkflow(**json.loads(stored_content["workflow"]))
        assert stored_workflow.message == original_message
        assert stored_workflow.history_limit == stored_history_limit
