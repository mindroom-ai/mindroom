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
        """Schedule a one-time (`execute_at`/`delay_seconds`) or recurring (`cron`) task in this room/thread or a fresh thread (`new_thread=True`).

        Supports `@agent_name`/`@team_name` mentions, silent monitors (`silent=True`), and per-run `model` overrides. Pass exactly one of `execute_at`, `delay_seconds`, or `cron`.

        Args:
            message: Trigger message posted on fire (non-empty); include `@agent_name`/`@team_name` to target responders.
            new_thread: `False` = current room/thread; `True` = new thread per fire (room-level root + fresh session). With `silent=True`, trigger stays hidden and findings/failures post as a room-level root.
            execute_at: Future ISO 8601 datetime with time (e.g. "2026-11-25T09:00:00-08:00", "...Z", or naive in `timezone`; pass offset during DST).
            delay_seconds: One-time delay in seconds (>= 1; e.g. 900 = 15m).
            cron: 5-field cron (`min hour day month weekday`, e.g. "0 9 * * 1-5") in `timezone` (DST-safe).
            description: Short listing label (defaults to trimmed `message`).
            timezone: IANA timezone (e.g. "America/Los_Angeles"); defaults to agent timezone.
            history_limit: Max recent thread messages per fire (`0` = none, best for polling; unset = unlimited).
            silent: Keep trigger hidden and suppress empty, whitespace-only, or `NO_REPLY`-ending finals; findings, failures, and direct tool messages stay visible. Defaults to `False`.
            model: Per-run model alias (including team members, e.g. "cheap" or "sol@high"); unset = default. Does not change conversation model.

        Returns:
            The scheduling result message.

        """
        context = _scheduling_context()
        return await schedule_task(
            context=context,
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
        )

    async def schedule_tool_call(
        self,
        tool_name: str,
        arguments_json: str,
        execute_at: str,
        description: str,
    ) -> str:
        """Pre-approve and schedule an approval-gated tool call to run later (e.g. while requester is away).

        Posts an approval card in this thread; when it fires, call `run_scheduled_call(task_id)` to run it once without re-approval. Tell the requester to approve the card; never claim it ran until `run_scheduled_call` reports its result.

        Args:
            tool_name: Your tool function name (e.g. "post_slack_message").
            arguments_json: Arguments as a JSON object string.
            execute_at: ISO 8601 timestamp with UTC offset (e.g. "2026-10-04T09:00:00-04:00").
            description: Short action summary (guides execution if any arguments are approved).

        Returns:
            The scheduling result message.

        """
        context = _scheduling_context()
        live, arguments = prepare_scheduled_call(tool_name, arguments_json)
        return await schedule_approved_tool_call(
            context=context,
            call=live,
            tool_name=tool_name,
            arguments=arguments,
            execute_at=execute_at,
            description=description,
        )

    async def run_scheduled_call(
        self,
        task_id: str,
        arguments_json: str | None = None,
    ) -> object:
        """Run a scheduled pre-approved tool call once without re-approval when prompted.

        Pass `arguments_json` only if the requester approved any arguments; otherwise omit it.

        Args:
            task_id: Task ID from the scheduled prompt.
            arguments_json: Replacement JSON object string (only when any arguments were approved).

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
        """Patch a pending schedule (omitted fields unchanged; cannot switch one-time <-> cron; pass at most one timing field).

        Args:
            task_id: Task ID from `list_schedules`.
            message: New trigger message.
            description: New listing label.
            execute_at: New one-time ISO 8601 datetime with time.
            delay_seconds: New one-time delay in seconds (>= 1).
            cron: New 5-field cron in `timezone`.
            timezone: New IANA timezone (legacy UTC cron also requires `cron` in the target timezone).
            history_limit: New max thread messages (`0` = none; cannot combine with `clear_history_limit=True`).
            clear_history_limit: Set `True` to remove the history limit (unlimited).
            silent: Update quiet delivery mode.
            model: Per-run model alias or `name@level`; `""` restores default.

        Returns:
            The edit result message.

        """
        context = _scheduling_context()
        return await edit_scheduled_task(
            context=context,
            task_id=task_id,
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
            task_id: Task ID from `list_schedules`.

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
