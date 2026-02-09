"""Scheduler tool that reuses the same backend as `!schedule`."""

from __future__ import annotations

from agno.tools import Toolkit

from mindroom.scheduling import schedule_task
from mindroom.scheduling_context import get_scheduling_tool_context


class SchedulerTools(Toolkit):
    """Tools for scheduling tasks in the current Matrix room/thread."""

    def __init__(self) -> None:
        super().__init__(
            name="scheduler",
            tools=[self.schedule],
        )

    async def schedule(self, request: str) -> str:
        """Schedule a task using natural language.

        This uses the exact same scheduling backend as the `!schedule` command.

        Args:
            request: The scheduling request, e.g. "in 5 minutes remind me to check logs"

        Returns:
            The scheduling result message.

        """
        context = get_scheduling_tool_context()
        if context is None:
            return "❌ Scheduler tool is unavailable in this context."

        _, response_text = await schedule_task(
            client=context.client,
            room_id=context.room_id,
            thread_id=context.thread_id,
            scheduled_by=context.requester_id,
            full_text=request,
            config=context.config,
            room=context.room,
        )
        return response_text
