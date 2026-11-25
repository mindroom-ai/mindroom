"""Scheduler tool for creating, editing, listing, and cancelling scheduled tasks."""

from __future__ import annotations

from typing import Annotated

from pydantic import Strict

from mindroom.scheduling.errors import SchedulingError
from mindroom.scheduling.scheduled_tool_calls import prepare_scheduled_call, run_scheduled_call
from mindroom.scheduling.tasks import (
    cancel_scheduled_task,
    edit_scheduled_task,
    list_scheduled_tasks,
    schedule_approved_tool_call,
    schedule_task,
)
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.runtime_context import (
    ToolRuntimeContext,
    build_scheduling_runtime_from_tool_runtime_context,
    get_tool_runtime_context,
)


def _scheduling_context(*, needs_room: bool = True) -> ToolRuntimeContext:
    """Return this call's runtime context, or fail the call where scheduling cannot run.

    A scheduling operation fails the call by raising ``SchedulingError``, whose text is what the model reads.
    """
    context = get_tool_runtime_context()
    if context is None or (needs_room and context.room is None):
        msg = "Scheduler tool is unavailable in this context."
        raise SchedulingError(msg)
    return context


class SchedulerTools(Toolkit):
    """Tools for scheduling tasks in the current Matrix room/thread."""

    def __init__(self) -> None:
        super().__init__(
            name="scheduler",
            tools=[
                self.schedule,
                self.schedule_tool_call,
                self.run_scheduled_call,
                self.edit_schedule,
                self.list_schedules,
                self.cancel_schedule,
            ],
        )

    async def schedule(
        self,
        message: str,
        new_thread: bool,
        execute_at: str | None = None,
        delay_seconds: Annotated[int | None, Strict()] = None,
        cron: str | None = None,
        description: str | None = None,
        timezone: str | None = None,
        history_limit: Annotated[int | None, Strict()] = None,
        silent: bool = False,
        model: str | None = None,
    ) -> str:
        """Schedule a one-time or recurring task with structured timing parameters.

        Provide exactly one of `execute_at`, `delay_seconds`, or `cron`.

        Args:
            message: Exact trigger message posted when the schedule fires (non-empty).
                Include `@agent_name` or `@team_name` if targeting specific responders.
            new_thread: Required delivery choice. Use `False` to post in the current
                room/thread scope. Use `True` to start a fresh thread per fire: each
                fire posts a room-level root and the responding agent answers in a
                new thread under it with a fresh session. When `silent=True`, the
                trigger stays hidden instead, and any finding or failure is posted
                as a room-level root rather than beneath the hidden trigger.
            execute_at: One-time run timestamp as an ISO 8601 datetime with a time
                component (e.g. "2026-11-25T09:00:00-08:00", "...Z", or naive
                "2026-11-25T09:00:00" interpreted in `timezone`). Must be in the
                future; include an explicit UTC offset during DST transitions.
            delay_seconds: One-time relative delay in positive seconds (>= 1),
                e.g. 900 for 15 minutes, 3600 for 1 hour.
            cron: Recurring 5-field cron expression (`minute hour day month weekday`,
                e.g. "0 9 * * 1-5"), evaluated in `timezone` (DST-safe).
            description: Optional short human-readable label for schedule listings;
                defaults to trimmed `message`.
            timezone: Optional IANA timezone name (e.g. "America/Los_Angeles");
                defaults to this agent's configured timezone.
            history_limit: Max recent thread messages included as context each time
                the task fires. Use 0 for no history (recommended for recurring
                polling tasks), or leave unset for unlimited history.
            silent: Keep the trigger hidden and omit a final response when it is empty,
                whitespace-only, or ends with `NO_REPLY`. Findings, failures, and
                independently sent tool messages remain visible. Defaults to `False`.
            model: Configured model alias for each scheduled run (including team
                members), e.g. "cheap", or `name@level` for one of its reasoning
                levels, e.g. "sol@high". Leave unset to use normal agent, room, and
                thread model selection. This does not change the conversation model.

        Returns:
            The scheduling result message.

        """
        context = _scheduling_context()
        return await schedule_task(
            runtime=build_scheduling_runtime_from_tool_runtime_context(context),
            room_id=context.room_id,
            thread_id=context.resolved_thread_id,
            scheduled_by=context.requester_id,
            message=message,
            new_thread=new_thread,
            execute_at=execute_at,
            delay_seconds=delay_seconds,
            cron=cron,
            description=description,
            timezone=timezone,
            history_limit=history_limit,
            silent=silent,
            model=model,
            source_event_id=context.reply_to_event_id,
        )

    async def schedule_tool_call(
        self,
        tool_name: str,
        arguments_json: str,
        execute_at: str,
        description: str,
    ) -> str:
        """Schedule one of your approval-gated tool calls that the requester approves now.

        Use this when a gated action, such as sending an external message, must run at
        a set time while the requester may be away. MindRoom stores the call and posts an
        approval card in this thread showing it and the send time. When the task fires,
        you are asked to call `run_scheduled_call` with its task ID, which runs the stored
        call once without another approval. Tell the requester to approve the card; do
        not claim the call ran until `run_scheduled_call` reports its result.

        Args:
            tool_name: One of your tool functions, e.g. "post_slack_message".
            arguments_json: The call's arguments as a JSON object string.
            execute_at: When to run, as ISO 8601 with a UTC offset,
                e.g. "2026-10-04T09:00:00-04:00".
            description: Short description of the scheduled action; if the requester
                approves any arguments, it tells you what the call should do when it runs.

        Returns:
            The scheduling result message.

        """
        context = _scheduling_context()
        live, arguments = prepare_scheduled_call(tool_name, arguments_json)
        return await schedule_approved_tool_call(
            runtime=build_scheduling_runtime_from_tool_runtime_context(context),
            room_id=context.room_id,
            thread_id=context.resolved_thread_id,
            scheduled_by=context.requester_id,
            agent_name=context.agent_name,
            call=live,
            tool_name=tool_name,
            arguments=arguments,
            execute_at=execute_at,
            description=description,
            source_event_id=context.reply_to_event_id,
        )

    async def run_scheduled_call(
        self,
        task_id: str,
        arguments_json: str | None = None,
    ) -> object:
        """Run the call a scheduled task stored, once, with the requester's approval.

        Call this when a scheduled task asks you to. It runs the stored call without
        another approval card and returns the tool's result. If the requester approved
        any arguments, you may pass arguments_json to replace the stored arguments for
        the same tool; otherwise omit it.

        Args:
            task_id: The scheduled task's ID from its message.
            arguments_json: Optional replacement arguments as a JSON object string,
                only when the requester approved any arguments.

        Returns:
            The tool's result, or why the call did not run.

        """
        return await run_scheduled_call(_scheduling_context(needs_room=False), task_id, arguments_json)

    async def edit_schedule(
        self,
        task_id: str,
        message: str | None = None,
        description: str | None = None,
        execute_at: str | None = None,
        delay_seconds: Annotated[int | None, Strict()] = None,
        cron: str | None = None,
        timezone: str | None = None,
        history_limit: Annotated[int | None, Strict()] = None,
        clear_history_limit: bool = False,
        silent: bool | None = None,
        model: str | None = None,
    ) -> str:
        """Edit an existing pending scheduled task using patch semantics.

        Omitted parameters keep their existing values. One-time tasks cannot switch
        to cron and cron tasks cannot switch to one-time; provide at most one of
        `execute_at`, `delay_seconds`, or `cron`.

        Args:
            task_id: The ID of the task to edit (from list_schedules).
            message: New trigger message posted when the schedule fires.
            description: New short label for schedule listings.
            execute_at: New one-time ISO 8601 datetime with a time component
                (valid only for one-time schedules).
            delay_seconds: New one-time relative delay in positive seconds (>= 1),
                e.g. 900 for 15 minutes, 3600 for 1 hour (valid only for one-time
                schedules).
            cron: New 5-field cron expression evaluated in `timezone` (valid only
                for cron schedules).
            timezone: New IANA timezone name (e.g. "America/Los_Angeles"). For
                legacy UTC cron schedules, also pass `cron` in the target timezone.
            history_limit: New max recent thread messages included per fire (0 for
                no history). Cannot be combined with `clear_history_limit=True`.
            clear_history_limit: Set to `True` to remove an existing history limit
                and restore unlimited history.
            silent: Whether to update quiet delivery. Silent tasks hide their trigger
                and omit empty, whitespace-only, or `NO_REPLY` finals while keeping
                findings, failures, and independently sent tool messages visible.
            model: Configured model alias for each run, including team members,
                or `name@level` for one of its reasoning levels. Leave unset to
                preserve the existing choice. Pass an empty string to restore
                normal agent, room, and thread model selection.

        Returns:
            The edit result message.

        """
        context = _scheduling_context()
        return await edit_scheduled_task(
            runtime=build_scheduling_runtime_from_tool_runtime_context(context),
            room_id=context.room_id,
            task_id=task_id,
            scheduled_by=context.requester_id,
            message=message,
            description=description,
            execute_at=execute_at,
            delay_seconds=delay_seconds,
            cron=cron,
            timezone=timezone,
            history_limit=history_limit,
            clear_history_limit=clear_history_limit,
            silent=silent,
            model=model,
        )

    async def list_schedules(self) -> str:
        """List all pending scheduled tasks in the current room/thread.

        Returns:
            A formatted list of scheduled tasks with their IDs.

        """
        context = _scheduling_context(needs_room=False)
        return await list_scheduled_tasks(
            client=context.client,
            room_id=context.room_id,
            runtime_paths=context.runtime_paths,
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
        context = _scheduling_context(needs_room=False)
        return await cancel_scheduled_task(
            client=context.client,
            room_id=context.room_id,
            task_id=task_id,
            runtime_paths=context.runtime_paths,
            task_runtime=context.orchestrator.scheduled_tasks if context.orchestrator is not None else None,
        )
