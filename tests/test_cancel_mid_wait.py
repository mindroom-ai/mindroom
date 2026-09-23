"""Test that cancellation during wait periods (not during tool calls) propagates correctly."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.config.main import Config
from mindroom.scheduling import CronSchedule, ScheduledTaskRecord, ScheduledWorkflow, _run_cron_task
from tests.conftest import test_runtime_paths as runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_cancel_mid_wait_cron_task(tmp_path: Path) -> None:
    """Test that cancellation during wait periods propagates correctly."""
    client = AsyncMock()
    client.homeserver = "https://example.org"
    client.user_id = "@router:example.org"
    config = Config()

    workflow = ScheduledWorkflow(
        schedule_type="cron",
        cron_schedule=CronSchedule(minute="*", hour="*", day="*", month="*", weekday="*"),
        message="Msg",
        description="Desc",
        room_id="!r:server",
        thread_id="$t",
    )
    pending_record = ScheduledTaskRecord(
        task_id="tid",
        room_id="!r:server",
        status="pending",
        created_at=datetime.now(UTC),
        workflow=workflow,
    )

    waiting = asyncio.Event()

    async def wait_until_cancelled(_delay: float) -> None:
        waiting.set()
        await asyncio.Event().wait()

    with (
        patch("mindroom.scheduling.asyncio.sleep", new=wait_until_cancelled),
        patch("mindroom.scheduling.get_scheduled_task", new=AsyncMock(return_value=pending_record)),
    ):
        task = asyncio.create_task(
            _run_cron_task(
                client,
                "tid",
                workflow,
                {},
                config,
                runtime_paths(tmp_path),
                MagicMock(),
            ),
        )
        await waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
