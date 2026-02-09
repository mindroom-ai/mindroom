"""Tests for shared schedule entrypoint and scheduler tool."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import mindroom.tools  # noqa: F401
from mindroom.config import AgentConfig, Config
from mindroom.custom_tools.scheduler import SchedulerTools
from mindroom.matrix.identity import MatrixID
from mindroom.scheduling import _extract_mentioned_agents_from_text
from mindroom.scheduling_context import SchedulingToolContext, scheduling_tool_context
from mindroom.tools_metadata import TOOL_METADATA


def test_extract_mentioned_agents_from_text() -> None:
    """Agent mentions should be extracted from scheduling text."""
    config = Config(agents={"general": AgentConfig(display_name="General Agent")})
    result = _extract_mentioned_agents_from_text("in 5 minutes @general check deployment", config)
    expected_agent = MatrixID.from_agent("general", config.domain)
    assert result == [expected_agent]


@pytest.mark.asyncio
async def test_scheduler_tool_requires_context() -> None:
    """Tool should fail clearly when called outside Matrix response context."""
    tools = SchedulerTools()

    result = await tools.schedule("in 10 minutes remind me to check logs")

    assert "unavailable" in result


@pytest.mark.asyncio
async def test_scheduler_tool_uses_shared_backend() -> None:
    """Tool should call the same scheduling backend path as !schedule."""
    tools = SchedulerTools()
    config = Config(agents={"general": AgentConfig(display_name="General Agent")})
    context = SchedulingToolContext(
        client=AsyncMock(),
        room=MagicMock(),
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        config=config,
    )

    with (
        patch(
            "mindroom.custom_tools.scheduler.schedule_task",
            new=AsyncMock(return_value=("task123", "✅ Scheduled")),
        ) as mock_schedule,
        scheduling_tool_context(context),
    ):
        result = await tools.schedule("tomorrow at 3pm check deployment")

    assert result == "✅ Scheduled"
    mock_schedule.assert_awaited_once_with(
        client=context.client,
        room_id=context.room_id,
        thread_id=context.thread_id,
        scheduled_by=context.requester_id,
        full_text="tomorrow at 3pm check deployment",
        config=context.config,
        room=context.room,
    )


def test_scheduler_tool_registered_in_metadata() -> None:
    """Scheduler tool should be visible in tool metadata."""
    assert "scheduler" in TOOL_METADATA
