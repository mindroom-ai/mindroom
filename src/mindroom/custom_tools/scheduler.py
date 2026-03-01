"""Scheduler tool that reuses the same backend as `!schedule`."""

from __future__ import annotations

from agno.tools import Toolkit

from mindroom.scheduling import (
    cancel_scheduled_task,
    edit_scheduled_task,
    list_scheduled_tasks,
    schedule_task,
)
from mindroom.tool_runtime_context import get_tool_runtime_context


class SchedulerTools(Toolkit):
    """Tools for scheduling tasks in the current Matrix room/thread."""

    def __init__(self) -> None:
        super().__init__(
            name="scheduler",
            tools=[self.schedule, self.edit_schedule, self.list_schedules, self.cancel_schedule],
        )

    async def schedule(self, request: str) -> str:
        """Schedule a task using natural language.

        This uses the exact same scheduling backend as the `!schedule` command.

        Args:
            request: The scheduling request, e.g. "in 5 minutes remind me to check logs"

        Returns:
            The scheduling result message.

        """
        context = get_tool_runtime_context()
        if context is None or context.room is None:
            return "❌ Scheduler tool is unavailable in this context."

        _, response_text = await schedule_task(
            client=context.client,
            room_id=context.room_id,
            thread_id=context.resolved_thread_id,
            scheduled_by=context.requester_id,
            full_text=request,
            config=context.config,
            room=context.room,
        )
        return response_text

    async def edit_schedule(self, task_id: str, request: str) -> str:
        """Edit an existing scheduled task by replacing its timing and content.

        Args:
            task_id: The ID of the task to edit (from list_schedules).
            request: The new scheduling request, e.g. "tomorrow at 9am check deployment"

        Returns:
            The edit result message.

        """
        context = get_tool_runtime_context()
        if context is None or context.room is None:
            return "❌ Scheduler tool is unavailable in this context."

        return await edit_scheduled_task(
            client=context.client,
            room_id=context.room_id,
            task_id=task_id,
            full_text=request,
            scheduled_by=context.requester_id,
            config=context.config,
            room=context.room,
            thread_id=context.resolved_thread_id,
        )

    async def list_schedules(self) -> str:
        """List all pending scheduled tasks in the current room/thread.

        Returns:
            A formatted list of scheduled tasks with their IDs.

        """
        context = get_tool_runtime_context()
        if context is None:
            return "❌ Scheduler tool is unavailable in this context."

        return await list_scheduled_tasks(
            client=context.client,
            room_id=context.room_id,
            thread_id=context.resolved_thread_id,
            config=context.config,
        )

    async def cancel_schedule(self, task_id: str) -> str:
        """Cancel a scheduled task.

        Args:
            task_id: The ID of the task to cancel (from list_schedules).

        Returns:
            The cancellation result message.

        """
        context = get_tool_runtime_context()
        if context is None:
            return "❌ Scheduler tool is unavailable in this context."

        return await cancel_scheduled_task(
            client=context.client,
            room_id=context.room_id,
            task_id=task_id,
        )
