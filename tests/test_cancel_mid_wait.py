"""Test that cancellation during wait periods (not during tool calls) propagates correctly."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from mindroom.scheduling import CronSchedule, ScheduledWorkflow, run_cron_task


@pytest.mark.asyncio
async def test_cancel_mid_wait_cron_task() -> None:
    """Test that cancellation during wait periods propagates correctly."""
    client = AsyncMock()
    config = AsyncMock()

    workflow = ScheduledWorkflow(
        schedule_type="cron",
        cron_schedule=CronSchedule(minute="*", hour="*", day="*", month="*", weekday="*"),
        message="Msg",
        description="Desc",
        room_id="!r:server",
        thread_id="$t",
    )

    # Patch croniter to return next run far in the future to guarantee sleep
    class DummyCron:
        def get_next(self, _) -> datetime:  # noqa: ANN001
            return datetime.now(UTC) + timedelta(hours=1)

    with patch("mindroom.scheduling.croniter", return_value=DummyCron()):
        task = asyncio.create_task(run_cron_task(client, "tid", workflow, {}, config))
        await asyncio.sleep(0)  # let it start and hit sleep
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
