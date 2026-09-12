"""Recurring schedules preserve due work across process restarts."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import nio
import pytest

from mindroom import recurring_schedule, scheduling, scheduling_executor
from mindroom.config.main import Config
from mindroom.config.plugin import PluginEntryConfig
from mindroom.hooks import EVENT_SCHEDULE_FIRED, HookRegistry, ScheduleFiredContext, hook
from mindroom.matrix import client_delivery
from mindroom.matrix.large_messages import MatrixEventTooLargeError
from mindroom.scheduling import CronSchedule, ScheduledWorkflow
from tests.conftest import delivered_matrix_event, delivered_matrix_side_effect
from tests.conftest import test_runtime_paths as runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.hooks import HookCallback
    from mindroom.recurring_schedule import RecurringOccurrence


def install_schedule_hook(monkeypatch: pytest.MonkeyPatch, callback: HookCallback) -> None:
    """Install one real hook with test-scoped registry restoration."""
    plugin = SimpleNamespace(
        name="schedule-test",
        discovered_hooks=(callback,),
        entry_config=PluginEntryConfig(path="./schedule-test"),
        plugin_order=0,
    )
    monkeypatch.setattr(
        scheduling_executor._SCHEDULING_HOOK_REGISTRY_STATE,
        "registry",
        HookRegistry.from_plugins([plugin]),
    )


class Clock:
    """Control wall time and stop a runner when it reaches its next wait."""

    now = datetime(2026, 1, 15, 6, 58, tzinfo=UTC)

    def utcnow(self, _tz: object = None) -> datetime:
        """Return the controlled aware time."""
        return self.now

    async def sleep(self, _delay: float) -> None:
        """Stop at the next timer boundary without advancing wall time."""
        raise asyncio.CancelledError


def workflow(hour: str = "7") -> ScheduledWorkflow:
    """Make a daily task with synthetic room and requester identities."""
    return ScheduledWorkflow(
        schedule_type="cron",
        cron_schedule=CronSchedule(minute="0", hour=hour),
        message="Summarize the latest updates",
        description="Daily summary",
        room_id="!room:example.org",
        created_by="@user:example.org",
        new_thread=True,
    )


def client_for(task: ScheduledWorkflow) -> AsyncMock:
    """Fake only Matrix IO; keep task parsing and runner behavior real."""
    client = AsyncMock()
    client.user_id = "@router:example.org"
    client.device_id = "TEST_DEVICE"
    client.homeserver = "https://example.org"
    client.rooms = {"!room:example.org": nio.MatrixRoom("!room:example.org", client.user_id)}
    client.room_get_state_event.return_value = nio.RoomGetStateEventResponse.from_dict(
        {"workflow": task.model_dump_json(), "status": "pending"},
        room_id="!room:example.org",
        event_type="com.mindroom.scheduled.task",
        state_key="daily",
    )
    return client


async def plan_at(
    tmp_path: Path,
    task: ScheduledWorkflow,
    now: datetime,
    grace: int = 3600,
) -> RecurringOccurrence:
    """Exercise checkpoint planning at an explicit time without starting a timer."""
    assert task.cron_schedule is not None
    return await recurring_schedule.plan_recurring_occurrence(
        runtime_paths(tmp_path),
        homeserver="https://example.org",
        sender="@router:example.org",
        room_id="!room:example.org",
        task_id="daily",
        workflow_json=task.model_dump_json(),
        cron=task.cron_schedule.to_cron_string(),
        now=now,
        grace_seconds=grace,
    )


async def run_until_wait(
    task: ScheduledWorkflow,
    client: AsyncMock,
    tmp_path: Path,
    config: Config | None = None,
) -> None:
    """Run the production scheduler until it sleeps or finishes a delivery."""
    with suppress(asyncio.CancelledError):
        await scheduling._run_cron_task(
            client,
            "daily",
            task,
            {},
            config or Config(),
            runtime_paths(tmp_path),
            AsyncMock(),
        )


@pytest.mark.asyncio
async def test_restart_catches_recent_daily_run_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Recomputing from restart time would lose the due run; losing its ACK would duplicate it."""
    clock = Clock()
    monkeypatch.setattr(scheduling, "datetime", type("Time", (datetime,), {"now": staticmethod(clock.utcnow)}))
    monkeypatch.setattr(scheduling.asyncio, "sleep", clock.sleep)
    sent = AsyncMock(side_effect=delivered_matrix_side_effect("$trigger"))
    monkeypatch.setattr(client_delivery, "send_message_outcome", sent)
    task = workflow()
    client = client_for(task)

    await run_until_wait(task, client, tmp_path)
    clock.now = datetime(2026, 1, 15, 7, 1, tzinfo=UTC)
    await run_until_wait(task, client, tmp_path)
    assert sent.await_count == 1

    await run_until_wait(task, client, tmp_path)
    assert sent.await_count == 1


@pytest.mark.asyncio
async def test_future_schedule_does_not_fire_during_recovery(tmp_path: Path) -> None:
    """Catch-up must leave a later daily occurrence waiting."""
    task = workflow("8")
    before = await plan_at(tmp_path, task, datetime(2026, 1, 15, 6, 58, tzinfo=UTC))
    recovered = await plan_at(tmp_path, task, datetime(2026, 1, 15, 7, 1, tzinfo=UTC))
    assert recovered == before
    assert recovered.checkpoint.next_run_at == datetime(2026, 1, 15, 8, 0, tzinfo=UTC)


@pytest.fixture
def controlled_clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """Control scheduler time while leaving its persistence and delivery real."""
    clock = Clock()
    monkeypatch.setattr(scheduling, "datetime", type("Time", (datetime,), {"now": staticmethod(clock.utcnow)}))
    monkeypatch.setattr(scheduling.asyncio, "sleep", clock.sleep)
    return clock


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("restart", "grace", "fires"),
    [
        (datetime(2026, 1, 15, 7, 59, 59, tzinfo=UTC), 3600, 1),
        (datetime(2026, 1, 15, 8, 0, tzinfo=UTC), 3600, 1),
        (datetime(2026, 1, 15, 8, 0, 1, tzinfo=UTC), 3600, 0),
        (datetime(2026, 1, 15, 7, 1, tzinfo=UTC), 0, 0),
        (datetime(2026, 1, 15, 9, 0, tzinfo=UTC), 7200, 1),
    ],
)
async def test_catch_up_grace(
    tmp_path: Path,
    restart: datetime,
    grace: int,
    fires: int,
) -> None:
    """Boundary and custom grace values determine whether overdue work remains due."""
    task = workflow()
    await plan_at(tmp_path, task, datetime(2026, 1, 15, 6, 58, tzinfo=UTC), grace)
    occurrence = await plan_at(tmp_path, task, restart, grace)
    expected = datetime(2026, 1, 15 if fires else 16, 7, 0, tzinfo=UTC)
    assert occurrence.checkpoint.next_run_at == expected
    assert (occurrence.checkpoint.next_run_at <= restart) == bool(fires)
    assert await plan_at(tmp_path, task, restart, grace) == occurrence


@pytest.mark.asyncio
async def test_first_adoption_does_not_replay_unknown_history(tmp_path: Path) -> None:
    """An old schedule without a checkpoint may already have fired before upgrade."""
    occurrence = await plan_at(tmp_path, workflow(), datetime(2026, 1, 15, 7, 1, tzinfo=UTC))
    assert occurrence.checkpoint.next_run_at == datetime(2026, 1, 16, 7, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_multiple_missed_occurrences_coalesce(tmp_path: Path) -> None:
    """A frequent schedule resumes once at the latest missed slot and keeps its cadence."""
    task = workflow()
    task.cron_schedule = CronSchedule(minute="*/10")
    await plan_at(tmp_path, task, datetime(2026, 1, 15, 6, 58, tzinfo=UTC))
    occurrence = await plan_at(tmp_path, task, datetime(2026, 1, 15, 7, 37, tzinfo=UTC))
    assert occurrence.checkpoint.next_run_at == datetime(2026, 1, 15, 7, 30, tzinfo=UTC)
    assert occurrence.checkpoint.last_skipped_at == datetime(2026, 1, 15, 7, 20, tzinfo=UTC)
    assert occurrence.checkpoint.skip_reason
    await recurring_schedule.complete_recurring_occurrence(
        occurrence,
        "*/10 * * * *",
        datetime(2026, 1, 15, 7, 37, tzinfo=UTC),
    )
    following = await plan_at(tmp_path, task, datetime(2026, 1, 15, 7, 37, tzinfo=UTC))
    assert following.checkpoint.next_run_at == datetime(2026, 1, 15, 7, 40, tzinfo=UTC)
    assert following.checkpoint.last_skipped_at == datetime(2026, 1, 15, 7, 20, tzinfo=UTC)


@pytest.mark.asyncio
@pytest.mark.parametrize("silent", [False, True])
@pytest.mark.parametrize("crash_after_accept", [False, True])
async def test_crash_retry_reuses_frozen_trigger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    controlled_clock: Clock,
    silent: bool,
    crash_after_accept: bool,
) -> None:
    """A crash on either side of Matrix acceptance must retain one trigger identity and payload."""
    task = workflow()
    task.silent = silent
    client = client_for(task)
    accepted: dict[str, dict[str, object]] = {}
    attempts: list[str] = []
    fail = True

    async def send(
        _client: object,
        _room: str,
        content: dict[str, object],
        **kwargs: object,
    ) -> object:
        nonlocal fail
        transaction_id = str(kwargs["transaction_id"])
        attempts.append(transaction_id)
        if not fail or crash_after_accept:
            if transaction_id in accepted:
                assert accepted[transaction_id] == content
            accepted.setdefault(transaction_id, content.copy())
        if fail:
            fail = False
            raise asyncio.CancelledError
        return await delivered_matrix_side_effect("$trigger")(_client, _room, content)

    monkeypatch.setattr(client_delivery, "send_message_outcome", send)
    await run_until_wait(task, client, tmp_path)
    controlled_clock.now = datetime(2026, 1, 15, 7, 1, tzinfo=UTC)
    await run_until_wait(task, client, tmp_path)
    controlled_clock.now = datetime(2026, 1, 15, 7, 2, tzinfo=UTC)
    await run_until_wait(task, client, tmp_path)
    await run_until_wait(task, client, tmp_path)
    assert len(accepted) == 1
    assert len(attempts) == 2
    assert attempts[0] == attempts[1]


@pytest.mark.asyncio
async def test_crash_after_send_before_checkpoint_does_not_duplicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    controlled_clock: Clock,
) -> None:
    """Losing the local delivery acknowledgement must resend only the same transaction."""
    task = workflow()
    client = client_for(task)
    sent = AsyncMock(side_effect=delivered_matrix_side_effect("$trigger"))
    monkeypatch.setattr(client_delivery, "send_message_outcome", sent)
    await run_until_wait(task, client, tmp_path)
    controlled_clock.now = datetime(2026, 1, 15, 7, 1, tzinfo=UTC)
    complete = scheduling.complete_recurring_occurrence
    monkeypatch.setattr(scheduling, "complete_recurring_occurrence", AsyncMock(side_effect=asyncio.CancelledError))
    await run_until_wait(task, client, tmp_path)
    monkeypatch.setattr(scheduling, "complete_recurring_occurrence", complete)
    await run_until_wait(task, client, tmp_path)
    await run_until_wait(task, client, tmp_path)
    assert sent.await_count == 2
    assert sent.await_args_list[0].kwargs["transaction_id"] == sent.await_args_list[1].kwargs["transaction_id"]
    assert sent.await_args_list[0].args[2] == sent.await_args_list[1].args[2]


@pytest.mark.asyncio
async def test_device_change_holds_ambiguous_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    controlled_clock: Clock,
) -> None:
    """A new Matrix device cannot safely reuse the old device's transaction namespace."""
    task = workflow()
    client = client_for(task)
    sent = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr(client_delivery, "send_message_outcome", sent)
    await run_until_wait(task, client, tmp_path)
    controlled_clock.now = datetime(2026, 1, 15, 7, 1, tzinfo=UTC)
    await run_until_wait(task, client, tmp_path)
    assert sent.await_count == 1
    client.device_id = "REPLACEMENT_DEVICE"
    await run_until_wait(task, client, tmp_path)
    assert sent.await_count == 1


@pytest.mark.asyncio
async def test_edit_during_downtime_does_not_replay_previous_definition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    controlled_clock: Clock,
) -> None:
    """Editing an overdue task establishes a new cursor instead of firing stale work."""
    task = workflow()
    client = client_for(task)
    sent = AsyncMock(side_effect=delivered_matrix_side_effect("$trigger"))
    monkeypatch.setattr(client_delivery, "send_message_outcome", sent)
    await run_until_wait(task, client, tmp_path)
    controlled_clock.now = datetime(2026, 1, 15, 7, 1, tzinfo=UTC)
    task.message = "Summarize a different topic"
    await run_until_wait(task, client_for(task), tmp_path)
    assert sent.await_count == 0


@pytest.mark.asyncio
async def test_cancelled_task_never_replays_pending_trigger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    controlled_clock: Clock,
) -> None:
    """Cancellation in authoritative room state fences a previously attempted send."""
    task = workflow()
    client = client_for(task)
    sent = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr(client_delivery, "send_message_outcome", sent)
    await run_until_wait(task, client, tmp_path)
    controlled_clock.now = datetime(2026, 1, 15, 7, 1, tzinfo=UTC)
    await run_until_wait(task, client, tmp_path)
    client.room_get_state_event.return_value = nio.RoomGetStateEventResponse.from_dict(
        {"workflow": task.model_dump_json(), "status": "cancelled"},
        room_id="!room:example.org",
        event_type="com.mindroom.scheduled.task",
        state_key="daily",
    )
    await run_until_wait(task, client, tmp_path)
    assert sent.await_count == 1


@pytest.mark.asyncio
async def test_live_timer_fires_with_catch_up_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    controlled_clock: Clock,
) -> None:
    """Disabling restart catch-up must not disable ordinary timer execution."""

    async def reach_due_time(_delay: float) -> None:
        controlled_clock.now = datetime(2026, 1, 15, 7, 0, tzinfo=UTC)

    monkeypatch.setattr(scheduling.asyncio, "sleep", reach_due_time)
    sent = AsyncMock(side_effect=delivered_matrix_side_effect("$trigger"))
    monkeypatch.setattr(client_delivery, "send_message_outcome", sent)
    task = workflow()
    await run_until_wait(task, client_for(task), tmp_path, Config(scheduler_catch_up_grace_seconds=0))
    assert sent.await_count == 1


@pytest.mark.asyncio
async def test_transaction_identity_reaches_matrix_transport(
    tmp_path: Path,
    controlled_clock: Clock,
) -> None:
    """The real hook sender and delivery stack must carry stable IDs through to nio."""
    task = workflow()
    client = client_for(task)
    client.rooms = {"!room:example.org": nio.MatrixRoom("!room:example.org", client.user_id)}
    client.room_send = AsyncMock(
        side_effect=[asyncio.CancelledError, nio.RoomSendResponse("$trigger", "!room:example.org")],
    )
    await run_until_wait(task, client, tmp_path)
    controlled_clock.now = datetime(2026, 1, 15, 7, 1, tzinfo=UTC)
    await run_until_wait(task, client, tmp_path)
    await run_until_wait(task, client, tmp_path)
    assert client.room_send.await_count == 2
    first, retry = client.room_send.await_args_list
    assert first.kwargs["tx_id"].startswith("schedule_")
    assert first.kwargs["tx_id"] == retry.kwargs["tx_id"]
    assert first.kwargs["content"] == retry.kwargs["content"]


@pytest.mark.asyncio
async def test_failed_checkpoint_write_prevents_network_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    controlled_clock: Clock,
) -> None:
    """A trigger cannot escape before its retry identity and content are durable."""
    task = workflow()
    client = client_for(task)
    sent = AsyncMock(side_effect=delivered_matrix_side_effect("$trigger"))
    monkeypatch.setattr(client_delivery, "send_message_outcome", sent)
    await run_until_wait(task, client, tmp_path)
    controlled_clock.now = datetime(2026, 1, 15, 7, 1, tzinfo=UTC)
    with monkeypatch.context() as failure:
        failure.setattr(recurring_schedule, "write_json_file_durable", Mock(side_effect=OSError("disk full")))
        await run_until_wait(task, client, tmp_path)
    assert sent.await_count == 0
    await run_until_wait(task, client, tmp_path)
    assert sent.await_count == 1


def test_negative_catch_up_grace_is_rejected() -> None:
    """Invalid recovery windows must fail configuration validation."""
    with pytest.raises(ValueError, match="greater than or equal"):
        Config(scheduler_catch_up_grace_seconds=-1)


@pytest.mark.asyncio
async def test_live_stall_rechecks_grace_before_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    controlled_clock: Clock,
) -> None:
    """A late wake-up is a missed occurrence even if the process never restarted."""
    waits = 0

    async def stalled_sleep(_delay: float) -> None:
        nonlocal waits
        waits += 1
        if waits > 1:
            raise asyncio.CancelledError
        controlled_clock.now = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)

    monkeypatch.setattr(scheduling.asyncio, "sleep", stalled_sleep)
    sent = AsyncMock(side_effect=delivered_matrix_side_effect("$trigger"))
    monkeypatch.setattr(client_delivery, "send_message_outcome", sent)
    task = workflow()
    await run_until_wait(task, client_for(task), tmp_path)
    assert sent.await_count == 0


@pytest.mark.asyncio
async def test_retry_keeps_prepared_transport_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    controlled_clock: Clock,
) -> None:
    """Sidecar preparation must happen before freezing, never again on a resend."""
    preparations = 0

    async def prepare(_client: object, _room: str, content: dict[str, object], **_kwargs: object) -> dict[str, object]:
        nonlocal preparations
        preparations += 1
        return {**content, "attachment": f"mxc://example.org/upload-{preparations}"}

    monkeypatch.setattr(client_delivery, "prepare_large_message", prepare)
    task = workflow()
    client = client_for(task)
    client.rooms = {"!room:example.org": nio.MatrixRoom("!room:example.org", client.user_id)}
    client.room_send = AsyncMock(
        side_effect=[asyncio.CancelledError, nio.RoomSendResponse("$trigger", "!room:example.org")],
    )
    await run_until_wait(task, client, tmp_path)
    controlled_clock.now = datetime(2026, 1, 15, 7, 1, tzinfo=UTC)
    await run_until_wait(task, client, tmp_path)
    await run_until_wait(task, client, tmp_path)
    first, retry = client.room_send.await_args_list
    assert first.kwargs["content"] == retry.kwargs["content"]
    assert preparations == 1


@pytest.mark.asyncio
async def test_frozen_sidecar_survives_encryption_enabled_before_retry(
    tmp_path: Path,
    controlled_clock: Clock,
) -> None:
    """A durable trigger prepared in plaintext must support a later encrypted send."""
    task = workflow()
    task.message = "x" * 100_000
    client = client_for(task)
    client.upload.return_value = (nio.UploadResponse("mxc://example.org/sidecar"), None)
    client.room_send = AsyncMock(
        side_effect=[asyncio.CancelledError, nio.RoomSendResponse("$trigger", "!room:example.org")],
    )
    await run_until_wait(task, client, tmp_path)
    controlled_clock.now = datetime(2026, 1, 15, 7, 1, tzinfo=UTC)
    await run_until_wait(task, client, tmp_path)
    client.rooms["!room:example.org"].encrypted = True
    await run_until_wait(task, client, tmp_path)
    first, retry = client.room_send.await_args_list
    assert first.kwargs["content"] == retry.kwargs["content"]
    frozen = first.kwargs["content"]
    assert "url" not in frozen
    assert frozen["file"]["key"]
    assert frozen["file"]["iv"]
    assert client.upload.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["plan", "prepare", "complete"])
async def test_checkpoint_io_failure_keeps_runner_alive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    controlled_clock: Clock,
    stage: str,
) -> None:
    """A transient checkpoint failure must recover without another process restart."""
    task = workflow()
    client = client_for(task)
    sent = AsyncMock(side_effect=delivered_matrix_side_effect("$trigger"))
    monkeypatch.setattr(client_delivery, "send_message_outcome", sent)
    await run_until_wait(task, client, tmp_path)
    controlled_clock.now = datetime(2026, 1, 15, 7, 1, tzinfo=UTC)
    save = recurring_schedule._save
    plan = recurring_schedule._plan
    failed = False
    pauses = 0

    def flaky_plan(
        path: Path,
        workflow_key: str,
        cron: str,
        now: datetime,
        grace_seconds: int,
    ) -> recurring_schedule.RecurringOccurrence:
        nonlocal failed
        if stage == "plan" and not failed:
            failed = True
            msg = "temporary checkpoint read failure"
            raise OSError(msg)
        return plan(path, workflow_key, cron, now, grace_seconds)

    def flaky_save(path: Path, checkpoint: recurring_schedule._RecurringCheckpoint) -> None:
        nonlocal failed
        target_stage = "prepare" if checkpoint.prepared is not None else "complete"
        if stage == target_stage and not failed:
            failed = True
            msg = "temporary checkpoint write failure"
            raise OSError(msg)
        save(path, checkpoint)

    async def retry_pause(_delay: float) -> None:
        nonlocal pauses
        pauses += 1
        assert pauses == 1

    monkeypatch.setattr(recurring_schedule, "_plan", flaky_plan)
    monkeypatch.setattr(recurring_schedule, "_save", flaky_save)
    monkeypatch.setattr(scheduling.asyncio, "sleep", retry_pause)
    await run_until_wait(task, client, tmp_path)
    assert failed
    assert pauses == 1
    assert sent.await_count == 1
    checkpoint = json.loads(next((tmp_path / "mindroom_data/tracking/recurring_schedules").glob("*.json")).read_text())
    assert checkpoint["next_run_at"] == "2026-01-16T07:00:00Z"
    assert checkpoint["prepared"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["plan", "prepare"])
@pytest.mark.parametrize("change", ["elapsed", "cancelled", "edited"])
async def test_checkpoint_retry_rechecks_time_and_task_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    controlled_clock: Clock,
    stage: str,
    change: str,
) -> None:
    """Recovery must not send an old occurrence after time or authoritative state changed."""
    task = workflow()
    client = client_for(task)
    sent = AsyncMock(side_effect=delivered_matrix_side_effect("$trigger"))
    monkeypatch.setattr(client_delivery, "send_message_outcome", sent)
    await run_until_wait(task, client, tmp_path)
    controlled_clock.now = datetime(2026, 1, 15, 7, 1, tzinfo=UTC)
    plan = recurring_schedule._plan
    save = recurring_schedule._save
    plans = 0
    failed = False
    pauses = 0

    def fail_final_plan(
        path: Path,
        workflow_key: str,
        cron: str,
        now: datetime,
        grace_seconds: int,
    ) -> recurring_schedule.RecurringOccurrence:
        nonlocal plans
        plans += 1
        if stage == "plan" and plans == 2:
            msg = "temporary final planning failure"
            raise OSError(msg)
        return plan(path, workflow_key, cron, now, grace_seconds)

    def fail_preparation(path: Path, checkpoint: recurring_schedule._RecurringCheckpoint) -> None:
        nonlocal failed
        if stage == "prepare" and checkpoint.prepared is not None and not failed:
            failed = True
            msg = "temporary preparation checkpoint failure"
            raise OSError(msg)
        save(path, checkpoint)

    async def change_during_retry(_delay: float) -> None:
        nonlocal pauses
        pauses += 1
        if pauses > 1:
            raise asyncio.CancelledError
        if change == "elapsed":
            controlled_clock.now = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
        else:
            if change == "edited":
                task.message = "Revised summary"
            client.room_get_state_event.return_value = nio.RoomGetStateEventResponse.from_dict(
                {"workflow": task.model_dump_json(), "status": "cancelled" if change == "cancelled" else "pending"},
                room_id="!room:example.org",
                event_type="com.mindroom.scheduled.task",
                state_key="daily",
            )

    monkeypatch.setattr(recurring_schedule, "_plan", fail_final_plan)
    monkeypatch.setattr(recurring_schedule, "_save", fail_preparation)
    monkeypatch.setattr(scheduling.asyncio, "sleep", change_during_retry)
    await run_until_wait(task, client, tmp_path)
    assert pauses >= 1
    assert sent.await_count == 0


@pytest.mark.asyncio
async def test_hook_retry_identity_is_specific_to_the_occurrence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    controlled_clock: Clock,
) -> None:
    """Hooks can deduplicate replay before freezing without suppressing tomorrow's run."""
    identities: list[str] = []

    @hook(EVENT_SCHEDULE_FIRED)
    async def record_identity(ctx: ScheduleFiredContext) -> None:
        identities.append(ctx.correlation_id)

    install_schedule_hook(monkeypatch, record_identity)
    task = workflow()
    client = client_for(task)
    content = {"body": "summary", "msgtype": "m.text"}
    monkeypatch.setattr(
        client_delivery,
        "prepare_message_content",
        AsyncMock(side_effect=[RuntimeError("temporary preparation failure"), content, content]),
    )
    sent = AsyncMock(
        side_effect=[asyncio.CancelledError, delivered_matrix_event("$first"), delivered_matrix_event("$next")],
    )
    monkeypatch.setattr(client_delivery, "send_message_outcome", sent)
    await run_until_wait(task, client, tmp_path)
    controlled_clock.now = datetime(2026, 1, 15, 7, 1, tzinfo=UTC)
    await run_until_wait(task, client, tmp_path)
    await run_until_wait(task, client, tmp_path)
    await run_until_wait(task, client, tmp_path)
    assert len(identities) == 2  # Frozen delivery retries no longer invoke hooks.
    controlled_clock.now = datetime(2026, 1, 16, 7, 1, tzinfo=UTC)
    await run_until_wait(task, client, tmp_path)
    assert len(identities) == 3
    assert identities[0] == identities[1]
    assert identities[1] != identities[2]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["empty", "too_large"])
async def test_permanent_preparation_failure_advances_occurrence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    controlled_clock: Clock,
    failure: str,
) -> None:
    """Invalid content fails visibly once rather than being retried every thirty seconds."""
    if failure == "empty":

        @hook(EVENT_SCHEDULE_FIRED)
        async def empty_body(ctx: ScheduleFiredContext) -> None:
            ctx.message_text = ""

        install_schedule_hook(monkeypatch, empty_body)
    else:
        monkeypatch.setattr(
            client_delivery,
            "prepare_large_message",
            AsyncMock(side_effect=MatrixEventTooLargeError("unrepresentable payload")),
        )
    task = workflow()
    client = client_for(task)
    sent = AsyncMock(side_effect=delivered_matrix_side_effect("$failure"))
    monkeypatch.setattr(client_delivery, "send_message_outcome", sent)
    await run_until_wait(task, client, tmp_path)
    controlled_clock.now = datetime(2026, 1, 15, 7, 1, tzinfo=UTC)
    await run_until_wait(task, client, tmp_path)
    assert sent.await_count == 1
    assert "Scheduled task failed" in sent.await_args.args[2]["body"]
    checkpoint = json.loads(next((tmp_path / "mindroom_data/tracking/recurring_schedules").glob("*.json")).read_text())
    assert checkpoint["next_run_at"] == "2026-01-16T07:00:00Z"
    assert checkpoint["prepared"] is None


@pytest.mark.asyncio
async def test_delayed_ack_records_intervening_skipped_slots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    controlled_clock: Clock,
) -> None:
    """Completing an old in-flight trigger must explain the slots it coalesces."""
    task = workflow()
    task.cron_schedule = CronSchedule(minute="*/10")
    client = client_for(task)
    sent = AsyncMock(side_effect=[asyncio.CancelledError, delivered_matrix_event("$trigger")])
    monkeypatch.setattr(client_delivery, "send_message_outcome", sent)
    await run_until_wait(task, client, tmp_path)
    controlled_clock.now = datetime(2026, 1, 15, 7, 0, tzinfo=UTC)
    await run_until_wait(task, client, tmp_path)
    controlled_clock.now = datetime(2026, 1, 15, 7, 37, tzinfo=UTC)
    await run_until_wait(task, client, tmp_path)
    checkpoint = json.loads(next((tmp_path / "mindroom_data/tracking/recurring_schedules").glob("*.json")).read_text())
    assert checkpoint["last_skipped_at"] == "2026-01-15T07:30:00Z"
    assert checkpoint["skip_reason"]
    assert checkpoint["next_run_at"] == "2026-01-15T07:40:00Z"


@pytest.mark.asyncio
async def test_preparation_returns_resumable_occurrence(tmp_path: Path) -> None:
    """The saved state must be immediately usable without reloading or a separate flag."""
    task = workflow()
    occurrence = await plan_at(tmp_path, task, datetime(2026, 1, 15, 6, 58, tzinfo=UTC))
    content = {"body": "Frozen summary", "msgtype": "m.text"}
    prepared = await recurring_schedule.prepare_recurring_delivery(occurrence, content, "TEST_DEVICE")
    assert prepared is not None
    assert recurring_schedule.recurring_delivery_content(prepared, "TEST_DEVICE") == content
    assert recurring_schedule.recurring_delivery_content(occurrence, "TEST_DEVICE") is None
    resumed = await plan_at(tmp_path, task, datetime(2026, 1, 16, 8, 1, tzinfo=UTC))
    assert resumed == prepared
    assert resumed.transaction_id == occurrence.transaction_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "expected_status"),
    [
        (client_delivery.MatrixDeliveryFailureKind.PAYLOAD_TOO_LARGE, "failed"),
        (client_delivery.MatrixDeliveryFailureKind.UNKNOWN_ENCRYPTION_STATE, "retry"),
        (client_delivery.MatrixDeliveryFailureKind.ENCRYPTION_GUARD, "retry"),
    ],
)
async def test_preparation_failure_uses_matrix_classification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: client_delivery.MatrixDeliveryFailureKind,
    expected_status: str,
) -> None:
    """Only an unrepresentable payload is terminal before the trigger is frozen."""
    task = workflow()
    client = client_for(task)
    occurrence = await plan_at(tmp_path, task, datetime(2026, 1, 15, 6, 58, tzinfo=UTC))
    failure = client_delivery.MatrixDeliveryFailure(kind, "Preparation failed")
    monkeypatch.setattr(client_delivery, "prepare_message_content", AsyncMock(return_value=failure))
    client.room_send.return_value = nio.RoomSendResponse.from_dict({"event_id": "$notice"}, "!room:example.org")
    outcome = await scheduling_executor.execute_scheduled_workflow(
        client,
        task,
        Config(),
        runtime_paths(tmp_path),
        AsyncMock(),
        occurrence=occurrence,
    )
    assert outcome.status == expected_status
    assert outcome.failure_reason == "Preparation failed"
    assert client.room_send.await_count == (1 if expected_status == "failed" else 0)
    assert json.loads(occurrence.path.read_text())["prepared"] is None


@pytest.mark.asyncio
async def test_unexpected_preparation_value_error_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exception's Python base class must not silently discard a valid occurrence."""
    task = workflow()
    client = client_for(task)
    occurrence = await plan_at(tmp_path, task, datetime(2026, 1, 15, 6, 58, tzinfo=UTC))
    monkeypatch.setattr(
        client_delivery,
        "prepare_message_content",
        AsyncMock(side_effect=ValueError("temporary failure")),
    )
    outcome = await scheduling_executor.execute_scheduled_workflow(
        client,
        task,
        Config(),
        runtime_paths(tmp_path),
        AsyncMock(),
        occurrence=occurrence,
    )
    assert outcome.status == "retry"
    assert client.room_send.await_count == 0
    assert json.loads(occurrence.path.read_text())["next_run_at"] == "2026-01-15T07:00:00Z"


@pytest.mark.asyncio
async def test_device_mismatch_is_held(tmp_path: Path) -> None:
    """A changed transaction namespace must hold the frozen trigger without sending it."""
    task = workflow()
    client = client_for(task)
    occurrence = await plan_at(tmp_path, task, datetime(2026, 1, 15, 6, 58, tzinfo=UTC))
    content = {"body": "Frozen summary", "msgtype": "m.text"}
    await recurring_schedule.prepare_recurring_delivery(occurrence, content, "PREVIOUS_DEVICE")
    resumed = await plan_at(tmp_path, task, datetime(2026, 1, 15, 7, 1, tzinfo=UTC))
    outcome = await scheduling_executor.execute_scheduled_workflow(
        client,
        task,
        Config(),
        runtime_paths(tmp_path),
        AsyncMock(),
        occurrence=resumed,
    )
    assert outcome.status == "held"
    assert client.room_send.await_count == 0
    assert recurring_schedule.recurring_delivery_content(resumed, "PREVIOUS_DEVICE") == content


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(client_delivery.MatrixDeliveryFailureKind))
async def test_frozen_delivery_failure_preserves_retry_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: client_delivery.MatrixDeliveryFailureKind,
) -> None:
    """Even a terminal-looking transport failure cannot discard an already frozen trigger."""
    task = workflow()
    client = client_for(task)
    occurrence = await plan_at(tmp_path, task, datetime(2026, 1, 15, 6, 58, tzinfo=UTC))
    send = AsyncMock(return_value=client_delivery.MatrixDeliveryFailure(kind, "Send failed"))
    monkeypatch.setattr(client_delivery, "send_message_outcome", send)
    outcome = await scheduling_executor.execute_scheduled_workflow(
        client,
        task,
        Config(),
        runtime_paths(tmp_path),
        AsyncMock(),
        occurrence=occurrence,
    )
    assert outcome.status == "retry"
    assert outcome.failure_reason == "Send failed"
    first_attempt = send.await_args
    assert send.await_count == 1
    resumed = await plan_at(tmp_path, task, datetime(2026, 1, 16, 8, 1, tzinfo=UTC))
    assert recurring_schedule.recurring_delivery_content(resumed, "TEST_DEVICE") == first_attempt.args[2]
    assert resumed.transaction_id == occurrence.transaction_id
    send.return_value = delivered_matrix_event("$retry")
    outcome = await scheduling_executor.execute_scheduled_workflow(
        client,
        task,
        Config(),
        runtime_paths(tmp_path),
        AsyncMock(),
        occurrence=resumed,
    )
    assert outcome.status == "delivered"
    assert send.await_count == 2
    assert send.await_args == first_attempt
