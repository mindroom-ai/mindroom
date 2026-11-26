"""Scheduler tool for creating, editing, listing, and cancelling scheduled tasks."""

from __future__ import annotations

from typing import Annotated, Literal

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


def _reject_disallowed(action: str, provided: dict[str, bool]) -> None:
    """Fail the call when parameters that do not belong to ``action`` were supplied."""
    unexpected = [name for name, is_set in provided.items() if is_set]
    if unexpected:
        names = ", ".join(f"`{name}`" for name in unexpected)
        msg = f"Parameter(s) {names} cannot be used with action={action!r}."
        raise SchedulingError(msg)


def _require_non_empty(value: str | None, *, name: str, action: str) -> str:
    """Return ``value`` if non-empty, or raise ``SchedulingError``."""
    if value is None or not value.strip():
        msg = f"`{name}` is required and must be non-empty for action={action!r}."
        raise SchedulingError(msg)
    return value


class SchedulerTools(Toolkit):
    """Tools for scheduling tasks in the current Matrix room/thread."""

    def __init__(self) -> None:
        super().__init__(
            name="scheduler",
            tools=[
                self.scheduler,
                self.schedule_tool_call,
                self.run_scheduled_call,
            ],
        )

    async def scheduler(
        self,
        action: Literal["create", "edit", "list", "cancel"],
        task_id: str | None = None,
        message: str | None = None,
        new_thread: bool | None = None,
        execute_at: str | None = None,
        delay_seconds: Annotated[int | None, Strict()] = None,
        cron: str | None = None,
        description: str | None = None,
        timezone: str | None = None,
        history_limit: Annotated[int | None, Strict()] = None,
        clear_history_limit: bool = False,
        silent: bool | None = None,
        model: str | None = None,
    ) -> str:
        """Create, edit, list, or cancel one-time or recurring cron tasks in this room/thread.

        - `create`: needs `message`, `new_thread`, and one timing field (`execute_at`/`delay_seconds`/`cron`). Supports `@agent`/`@team`, fresh threads, silent monitors (`NO_REPLY`), and per-run `model`.
        - `edit`: patch by `task_id` (omitted unchanged; no one-time <-> cron switch; <=1 timing field; `new_thread` immutable; empty `model` resets; `clear_history_limit=True` restores unlimited history).
        - `list` / `cancel`: list pending tasks or cancel by `task_id` (no other params).

        Args:
            action: Operation to run.
            task_id: Task ID from `list` (`edit`/`cancel`).
            message: Trigger text (`create` required); include `@agent`/`@team` to mention responders.
            new_thread: `create` required: `False` = current room/thread, `True` = fresh thread per fire (with `silent=True`, findings/failures post as a room-level root).
            execute_at: Future ISO 8601 datetime with time (or naive in `timezone`; pass offset during DST).
            delay_seconds: One-time delay in seconds (>= 1).
            cron: 5-field cron (`min hour day month weekday`) in `timezone`.
            description: Short label (defaults to trimmed `message`).
            timezone: IANA timezone (default: agent timezone; legacy UTC cron edit also needs `cron`).
            history_limit: Max thread messages per fire (`0` = none; unset = unlimited).
            clear_history_limit: `edit` only: set `True` to remove the history limit (unlimited).
            silent: Hidden trigger; suppress empty, whitespace, or `NO_REPLY`-ending finals while findings/failures/tool messages stay visible (defaults to `False` on `create`).
            model: Per-run model alias or `name@level` (pass an empty string on `edit` to reset to default; conversation model unchanged).

        """
        if action == "create":
            _reject_disallowed(
                "create",
                {
                    "task_id": task_id is not None,
                    "clear_history_limit": clear_history_limit,
                },
            )
            validated_message = _require_non_empty(message, name="message", action="create")
            if new_thread is None:
                msg = "`new_thread` is required for action='create' (pass `False` for current room/thread or `True` for a new thread per fire)."
                raise SchedulingError(msg)
            context = _scheduling_context()
            return await schedule_task(
                context=context,
                message=validated_message,
                new_thread=new_thread,
                execute_at=execute_at,
                delay_seconds=delay_seconds,
                cron=cron,
                description=description,
                timezone=timezone,
                history_limit=history_limit,
                silent=False if silent is None else silent,
                model=model,
            )

        if action == "edit":
            _reject_disallowed("edit", {"new_thread": new_thread is not None})
            validated_task_id = _require_non_empty(task_id, name="task_id", action="edit")
            context = _scheduling_context()
            return await edit_scheduled_task(
                context=context,
                task_id=validated_task_id,
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

        if action == "list":
            _reject_disallowed(
                "list",
                {
                    "task_id": task_id is not None,
                    "message": message is not None,
                    "new_thread": new_thread is not None,
                    "execute_at": execute_at is not None,
                    "delay_seconds": delay_seconds is not None,
                    "cron": cron is not None,
                    "description": description is not None,
                    "timezone": timezone is not None,
                    "history_limit": history_limit is not None,
                    "clear_history_limit": clear_history_limit,
                    "silent": silent is not None,
                    "model": model is not None,
                },
            )
            context = _scheduling_context(needs_room=False)
            return await list_scheduled_tasks(
                client=context.client,
                room_id=context.room_id,
                runtime_paths=context.runtime_paths,
                thread_id=context.resolved_thread_id,
                config=context.config,
            )

        if action == "cancel":
            _reject_disallowed(
                "cancel",
                {
                    "message": message is not None,
                    "new_thread": new_thread is not None,
                    "execute_at": execute_at is not None,
                    "delay_seconds": delay_seconds is not None,
                    "cron": cron is not None,
                    "description": description is not None,
                    "timezone": timezone is not None,
                    "history_limit": history_limit is not None,
                    "clear_history_limit": clear_history_limit,
                    "silent": silent is not None,
                    "model": model is not None,
                },
            )
            validated_task_id = _require_non_empty(task_id, name="task_id", action="cancel")
            context = _scheduling_context(needs_room=False)
            return await cancel_scheduled_task(
                client=context.client,
                room_id=context.room_id,
                task_id=validated_task_id,
                runtime_paths=context.runtime_paths,
                task_runtime=context.orchestrator.scheduled_tasks if context.orchestrator is not None else None,
            )

        msg = f"Unsupported scheduler action: {action!r}. Expected one of: create, edit, list, cancel."
        raise SchedulingError(msg)

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
