"""Scheduled task management with structured workflow scheduling."""

from __future__ import annotations

import asyncio
import hashlib
import json
import typing
import uuid
from collections import Counter
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal
from weakref import WeakValueDictionary
from zoneinfo import ZoneInfo

import aiohttp
import humanize
import nio
from cron_descriptor import Options, get_description
from croniter import CroniterError, croniter
from pydantic import BaseModel, Field, field_validator
from pydantic.json_schema import SkipJsonSchema  # noqa: TC002 - pydantic resolves field annotations at runtime

from mindroom.approval import manager as approval_manager
from mindroom.approval.tool_approval import (
    ToolApprovalScriptError,
    evaluate_tool_approval,
    resolve_tool_approval_approver,
    scheduled_call_offers_any_arguments,
    toolkit_approval_alias,
)
from mindroom.config.models import unsupported_level_message
from mindroom.config.resolution import entity_timezone
from mindroom.config.validation import validate_timezone_name
from mindroom.constants import SCHEDULED_TASK_EVENT_TYPE
from mindroom.entity_resolution import entity_identity_registry, persisted_bot_user_ids
from mindroom.logging_config import get_logger
from mindroom.matrix.identity import MatrixID
from mindroom.matrix.mentions import parse_mentions_in_text
from mindroom.matrix.state_failures import StateFailure, state_failure
from mindroom.scheduling.errors import (
    ScheduledTaskNotFoundError,
    ScheduledTaskStateReadError,
    ScheduledTaskStateRefusedError,
    ScheduledTaskStorageError,
    SchedulingError,
    refused_for_power,
)
from mindroom.scheduling.recurring_schedule import next_cron_run_utc
from mindroom.steward_duties import moderator_rights_message
from mindroom.tool_approval_grants import ScheduledCallBinding, canonical_arguments
from mindroom.tool_system.plugins import config_plugins

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.scheduling.runtime import ScheduledTaskRuntime
    from mindroom.scheduling.scheduled_tool_calls import LiveFunction
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

logger = get_logger(__name__)


# Maximum length for message preview in task listings
_MESSAGE_PREVIEW_LENGTH = 50

# Reasons shown on a scheduled tool call's approval card when its task changes.
SCHEDULE_CANCELLED_REASON = "Schedule cancelled."

# Shared validation message for edit attempts that change task type.
_SCHEDULE_TYPE_CHANGE_NOT_SUPPORTED_ERROR = "Changing schedule_type is not supported; cancel and recreate the schedule"

# Shared by the runtime and API clients in this process; Matrix state has no compare-and-swap.
_scheduled_task_state_locks: WeakValueDictionary[tuple[str, str, str], asyncio.Lock] = WeakValueDictionary()


def scheduled_task_state_lock(client: nio.AsyncClient, room_id: str, task_id: str) -> asyncio.Lock:
    """Serialize every write of an existing task's state across local clients: edits, cancellations, and status writes."""
    key = (client.homeserver, room_id, task_id)
    lock = _scheduled_task_state_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _scheduled_task_state_locks[key] = lock
    return lock


_logged_unmanaged_authors: set[tuple[str, str]] = set()


def _runtime_authored_task_content(
    room_id: str,
    event: dict[str, typing.Any],
    runtime_account_ids: frozenset[str],
) -> dict[str, typing.Any] | None:
    """Return a scheduled-task state event's content only when a MindRoom-managed bot account wrote it.

    Any member with power to write room state can write this event type, and its workflow names
    the requester each trigger runs as, so state from any other sender is ignored.
    """
    content = event.get("content")
    sender = event.get("sender")
    if not isinstance(content, dict) or not isinstance(sender, str):
        return None
    if sender in runtime_account_ids:
        # LEGACY_COMPAT: Scheduled tasks without a stored owner.
        # Legacy format: Scheduled-task state content whose "owner" is absent, empty or not a string; every task
        #   saved before owners were stored.
        # Last legacy release: v2026.10.675 wrote scheduled-task state without an owner; replacement: the next
        #   release stores the owner's Matrix ID in the task's state content when the task is saved.
        # Handling: The account that last wrote the task reads as its owner, and every rewrite stores that owner,
        #   so a write by the room's steward never moves the task; the router's ID marks a task the router owned.
        # Coverage: tests/test_scheduled_task_runtime.py::test_task_saved_without_an_owner_keeps_its_writer_as_owner_through_a_rewrite.
        owner = content.get("owner")
        return {**content, "owner": owner if isinstance(owner, str) and owner else sender}
    logger.debug(
        "scheduled_task_event_ignored_unmanaged_author",
        room_id=room_id,
        task_id=event.get("state_key"),
        event_id=event.get("event_id"),
        sender=sender,
    )
    return None


# ---- Workflow scheduling primitives ----


class CronSchedule(BaseModel):
    """Standard cron-like schedule definition."""

    minute: str = Field(default="*", description="0-59, *, */5, or comma-separated")
    hour: str = Field(default="*", description="0-23, *, */2, or comma-separated")
    day: str = Field(default="*", description="1-31, *, or comma-separated")
    month: str = Field(default="*", description="1-12, *, or comma-separated")
    weekday: str = Field(default="*", description="0-6 (0=Sunday), *, or comma-separated")

    def to_cron_string(self) -> str:
        """Convert to standard cron format."""
        return f"{self.minute} {self.hour} {self.day} {self.month} {self.weekday}"

    def to_natural_language(
        self,
        timezone: str,
        current_time: datetime | None = None,
        *,
        cron_timezone: str = "UTC",
    ) -> str:
        """Describe the schedule in wall time and name its timezone.

        Local-time crons (``cron_timezone != "UTC"``) describe their fields directly in ``cron_timezone``.
        Legacy UTC crons convert a fixed time of day into ``timezone``; other UTC patterns keep their UTC fields.
        """
        if cron_timezone != "UTC":
            target, label = self, cron_timezone
        else:
            local = self._in_timezone(ZoneInfo(timezone), current_time or datetime.now(UTC))
            target, label = (local, timezone) if local is not None else (self, "UTC")
        try:
            description = str(get_description(target.to_cron_string(), Options(use_24hour_time_format=True)))
        except Exception:
            return f"Cron: {self.to_cron_string()} ({cron_timezone})"
        return f"{description} ({label})"

    def _in_timezone(self, zone: ZoneInfo, current_time: datetime) -> CronSchedule | None:
        """Return this schedule in `zone` wall time when it runs at one fixed UTC time of day."""
        if not (self.minute.isdigit() and self.hour.isdigit()):
            return None
        try:
            next_run = croniter(self.to_cron_string(), current_time).get_next(datetime)
        except CroniterError:
            return None
        local = next_run.astimezone(zone)
        shift = (local.date() - next_run.date()).days
        weekday = self.weekday if shift == 0 or self.weekday == "*" else _shift_cron_weekdays(self.weekday, shift)
        if weekday is None or (shift != 0 and self.day != "*"):
            return None
        return self.model_copy(update={"minute": str(local.minute), "hour": str(local.hour), "weekday": weekday})


def _shift_cron_weekdays(field: str, shift: int) -> str | None:
    """Move numeric cron weekdays (0 or 7 is Sunday) by whole days, or None for other syntax."""
    days: list[int] = []
    for part in field.split(","):
        start, _, end = part.partition("-")
        if not (start.isdigit() and (end == "" or end.isdigit())):
            return None
        days += range(int(start), int(end or start) + 1)
    return ",".join(str(day) for day in sorted({(day + shift) % 7 for day in days}))


# LEGACY_COMPAT: Scheduled workflows without an explicit new-thread setting.
# Legacy format: Scheduled workflows omitted new_thread and therefore reused their persisted thread by default.
# Last legacy release: v2026.3.109; replacement: v2026.3.110 persisted explicit thread-creation mode.
# Handling: Pydantic defaults missing new_thread to false, preserving the old same-thread behavior.
# Coverage: tests/test_workflow_scheduling.py::TestWorkflowSerialization::test_workflow_old_payload_defaults_new_thread_false.


# LEGACY_COMPAT: Scheduled workflows without a history limit.
# Legacy format: Scheduled workflows omitted history_limit and exposed the full available thread history.
# Last legacy release: v2026.7.136; replacement: v2026.7.137 persisted the optional history bound.
# Handling: Pydantic defaults absence to None, which retains unlimited history for old workflows.
# Coverage: tests/test_workflow_scheduling.py::TestScheduledWorkflow::test_persisted_workflow_without_history_limit_loads_as_unlimited.
class ScheduledWorkflow(BaseModel):
    """Structured representation of a scheduled task or workflow."""

    schedule_type: Literal["once", "cron"]
    # LEGACY_COMPAT: Inert conditional-schedule flag on persisted workflows.
    # Legacy format: Workflows persisted before structured scheduling included `is_conditional` in `model_dump_json()`,
    #   whose exact bytes are hashed for recurring checkpoint `workflow_key` and one-time Matrix `transaction_id`.
    # Last legacy release: v2026.11.574; replacement: unreleased; structured schedules no longer parse or validate it.
    # Handling: Keep the field in its original position with default False so loaded legacy workflows re-serialize
    #   byte-identically and never rotate existing checkpoint keys or transaction IDs.
    # Coverage: tests/test_workflow_scheduling.py::TestWorkflowSerialization::test_legacy_workflow_json_is_byte_identical.
    is_conditional: bool = False
    execute_at: datetime | None = None
    cron_schedule: CronSchedule | None = None
    message: str
    description: str
    history_limit: int | None = Field(
        default=None,
        ge=0,
        description="Max recent thread messages the responding agent sees when the task fires; 0 means no history",
    )
    model: str | None = Field(default=None, description="Configured model alias for this scheduled run only")
    created_by: str | None = None
    thread_id: str | None = None
    room_id: str | None = None
    new_thread: bool = False
    silent: bool = False
    # LEGACY_COMPAT: Scheduled workflows without the pre-approved call flag.
    # Legacy format: Scheduled workflows omitted pre_approved_call before scheduled tool calls existed.
    # Last legacy release: v2026.10.150; replacement: v2026.10.151 writes the flag on every workflow.
    # Handling: Pydantic defaults absence to False, which is correct because no earlier task carried an approval.
    # Coverage: tests/test_scheduled_tool_approval.py::test_workflow_without_flag_loads_as_ordinary_task.
    pre_approved_call: bool = Field(
        default=False,
        description="Whether the requester approved this one-time task's tool call while scheduling it",
    )
    # LEGACY_COMPAT: Scheduled workflows without a timezone.
    # Legacy format: Scheduled workflows omitted timezone and were interpreted and shown in the global timezone.
    # Last legacy release: v2026.10.533; replacement: the next release stores the creating agent's timezone.
    # Handling: Pydantic defaults absence to None, and scheduled_task_timezone reads None as the global timezone,
    #   so existing tasks keep the wall time they were interpreted and shown in; edits keep None.
    # Coverage: tests/test_scheduling.py::test_a_stored_task_without_a_timezone_keeps_the_global_timezone.
    # SkipJsonSchema keeps `ScheduledWorkflow.model_json_schema()` aligned with the recorded provider
    # `structured_output_nested` goldens without affecting `model_dump_json()`.
    timezone: SkipJsonSchema[str | None] = None
    # The message the scheduling reply answered, so a client can show the task under that reply; MindRoom sets it.
    source_event_id: SkipJsonSchema[str | None] = None
    # LEGACY_COMPAT: Legacy UTC cron schedules without an explicit cron_timezone.
    # Legacy format: Recurring workflows persisted before local-time crons omitted `cron_timezone` and ran in UTC;
    #   one-time workflows also omit it. Both are hashed via `model_dump_json()` for `workflow_key` / `transaction_id`.
    # Last legacy release: v2026.11.574; replacement: unreleased; new recurring schedules store `cron_timezone`.
    # Handling: Default absence to None and omit None from serialization (`exclude_if`) so existing persisted tasks
    #   produce byte-identical JSON and keep running in UTC via `scheduled_cron_timezone`.
    # Coverage: tests/test_workflow_scheduling.py::TestWorkflowSerialization::test_legacy_workflow_json_is_byte_identical,
    #   tests/test_workflow_scheduling.py::TestCronSchedule::test_local_time_cron_round_trips_and_tracks_dst.
    cron_timezone: SkipJsonSchema[str | None] = Field(default=None, exclude_if=lambda value: value is None)

    @field_validator("execute_at")
    @classmethod
    def _naive_execute_at_is_utc(cls, value: datetime | None) -> datetime | None:
        """Treat any naive persisted or API-supplied execute_at as UTC."""
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value

    def cron_description(self, timezone: str, current_time: datetime | None = None) -> str | None:
        """Describe this workflow's cron schedule in wall time, or None when it has no cron schedule."""
        if self.schedule_type != "cron" or self.cron_schedule is None:
            return None
        return self.cron_schedule.to_natural_language(
            timezone,
            current_time,
            cron_timezone=scheduled_cron_timezone(self),
        )

    def next_run_at(self, current_time: datetime | None = None) -> datetime | None:
        """Return the next UTC run time for this workflow, or None if it cannot be computed."""
        if self.schedule_type == "once":
            return self.execute_at
        if self.cron_schedule is None:
            return None
        try:
            return next_cron_run_utc(
                self.cron_schedule.to_cron_string(),
                current_time or datetime.now(UTC),
                scheduled_cron_timezone(self),
            )
        except CroniterError:
            return None


def scheduled_task_timezone(workflow: ScheduledWorkflow, config: Config) -> str:
    """Return the timezone a task's times are interpreted and shown in: its own, else the global one."""
    return workflow.timezone or config.timezone


def scheduled_cron_timezone(workflow: ScheduledWorkflow) -> str:
    """Return the timezone a cron workflow's expression is evaluated in: its own, else legacy UTC."""
    return workflow.cron_timezone or "UTC"


@dataclass
class ScheduledTaskRecord:
    """Parsed scheduled task state from Matrix."""

    task_id: str
    room_id: str
    status: str
    created_at: datetime | None
    workflow: ScheduledWorkflow
    revision: str | None = None
    # Matrix ID of the bot that runs the task. A record read from a room always has one: a task stored without an
    # owner reads with its last writer (`_runtime_authored_task_content`). None only on a record built without a read.
    owner_id: str | None = None


def _require_context_room(context: ToolRuntimeContext) -> nio.MatrixRoom:
    """Return the cached Matrix room from ``context``, or raise when absent."""
    if context.room is None:
        msg = "Scheduling requires a cached Matrix room in tool runtime context"
        raise RuntimeError(msg)
    return context.room


def _context_task_runtime(context: ToolRuntimeContext) -> ScheduledTaskRuntime | None:
    """Return the live scheduled-task runtime attached to ``context``, if any."""
    return context.orchestrator.scheduled_tasks if context.orchestrator is not None else None


def _parse_datetime(value: object) -> datetime | None:
    """Parse an ISO datetime string into a datetime object."""
    if not isinstance(value, str) or not value:
        return None

    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


_EXECUTE_AT_FORMAT_ERROR = (
    "execute_at must be an ISO 8601 datetime with a time component "
    "(e.g. '2026-11-25T09:00:00-08:00' or '2026-11-25T09:00:00')."
)


def _raise_schedule_edit_error(message: str) -> typing.NoReturn:
    raise SchedulingError(message)


def _validate_timezone(timezone: str) -> str:
    """Validate an IANA timezone name, raising SchedulingError on invalid input."""
    try:
        return validate_timezone_name(timezone)
    except ValueError as exc:
        raise SchedulingError(str(exc)) from exc


def _validate_history_limit(history_limit: int | None) -> None:
    """Validate an optional non-negative integer history limit, rejecting booleans."""
    if history_limit is not None and (
        isinstance(history_limit, bool) or not isinstance(history_limit, int) or history_limit < 0
    ):
        _raise_schedule_edit_error("history_limit must be a non-negative integer.")


def _resolve_model(model: str | None, *, existing_model: str | None, config: Config) -> str | None:
    """Resolve and validate an optional model override; empty or whitespace-only string clears to None."""
    selected = existing_model if model is None else (model.strip() or None)
    if selected is not None and selected not in config.models:
        msg = unsupported_level_message(config.models, selected) or (
            f"Unknown model: {selected}. Available models: {', '.join(sorted(config.models))}"
        )
        raise SchedulingError(msg)
    return selected


def _parse_cron_schedule(cron_expression: str, *, cron_timezone: str) -> CronSchedule:
    """Parse and validate a 5-field cron expression in ``cron_timezone``."""
    raw = cron_expression.strip()
    fields = raw.split()
    if len(fields) != 5:
        _raise_schedule_edit_error(
            "Invalid cron expression: Cron expression must have exactly 5 fields: minute hour day month weekday",
        )
    try:
        next_cron_run_utc(raw, datetime.now(UTC), cron_timezone)
    except (ValueError, CroniterError) as e:
        msg = f"Invalid cron expression: {e!s}"
        raise SchedulingError(msg) from e
    return CronSchedule(minute=fields[0], hour=fields[1], day=fields[2], month=fields[3], weekday=fields[4])


def _parse_execute_at(execute_at: str, *, timezone: str | None) -> datetime:
    """Parse an ISO 8601 datetime with a time component into a future UTC instant.

    When ``timezone`` is None, ``execute_at`` must include an explicit UTC offset.
    """
    raw = execute_at.strip()
    if not any(sep in raw for sep in ("T", "t", " ")):
        raise SchedulingError(_EXECUTE_AT_FORMAT_ERROR)
    try:
        parsed = datetime.fromisoformat(f"{raw[:-1]}Z" if raw.endswith("z") else raw)
    except ValueError as exc:
        raise SchedulingError(_EXECUTE_AT_FORMAT_ERROR) from exc
    if parsed.tzinfo is not None:
        resolved = parsed.astimezone(UTC)
    elif timezone is None:
        _raise_schedule_edit_error("execute_at must include a UTC offset, e.g. 2026-10-04T09:00:00-04:00.")
    else:
        zone = ZoneInfo(timezone)
        local = parsed.replace(tzinfo=zone)
        if local.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != parsed:
            _raise_schedule_edit_error(
                f"Local time {raw!r} does not exist in {timezone} (DST spring-forward gap); provide an explicit UTC offset.",
            )
        if local.utcoffset() != parsed.replace(tzinfo=zone, fold=1).utcoffset():
            _raise_schedule_edit_error(
                f"Local time {raw!r} is ambiguous in {timezone} (DST fall-back fold); provide an explicit UTC offset.",
            )
        resolved = local.astimezone(UTC)
    if resolved <= datetime.now(UTC):
        _raise_schedule_edit_error("execute_at must be in the future.")
    return resolved


def _parse_once_time(*, execute_at: str | None, delay_seconds: int | None, timezone: str) -> datetime | None:
    """Resolve one-time ``execute_at`` or ``delay_seconds`` into a future UTC instant."""
    if execute_at is not None:
        return _parse_execute_at(execute_at, timezone=timezone)
    if delay_seconds is None:
        return None
    if isinstance(delay_seconds, bool) or not isinstance(delay_seconds, int) or delay_seconds < 1:
        _raise_schedule_edit_error("delay_seconds must be a positive integer (>= 1).")
    try:
        return datetime.now(UTC) + timedelta(seconds=delay_seconds)
    except OverflowError as exc:
        msg = "delay_seconds is too large."
        raise SchedulingError(msg) from exc


def build_edited_scheduled_workflow(
    existing_workflow: ScheduledWorkflow,
    room_id: str,
    *,
    message: str | None = None,
    description: str | None = None,
    schedule_type: Literal["once", "cron"] | None = None,
    execute_at: datetime | None = None,
    cron_expression: str | None = None,
    silent: bool | None = None,
) -> ScheduledWorkflow:
    """Build a validated patch-style workflow edit while preserving immutable metadata."""
    if schedule_type and schedule_type != existing_workflow.schedule_type:
        _raise_schedule_edit_error(_SCHEDULE_TYPE_CHANGE_NOT_SUPPORTED_ERROR)

    if existing_workflow.schedule_type == "once":
        if cron_expression is not None:
            _raise_schedule_edit_error("cron_expression is only valid for cron schedules")
        resolved_execute_at = execute_at or existing_workflow.execute_at
        if resolved_execute_at is None:
            _raise_schedule_edit_error("execute_at is required for one-time schedules")
        if resolved_execute_at.tzinfo is None:
            resolved_execute_at = resolved_execute_at.replace(tzinfo=UTC)
        cron_schedule = None
    else:
        if execute_at is not None:
            _raise_schedule_edit_error("execute_at is only valid for one-time schedules")
        cron_schedule = (
            _parse_cron_schedule(cron_expression, cron_timezone=scheduled_cron_timezone(existing_workflow))
            if cron_expression is not None
            else existing_workflow.cron_schedule
        )
        if cron_schedule is None:
            _raise_schedule_edit_error("cron_expression is required for cron schedules")
        resolved_execute_at = None

    message_value = (message if message is not None else existing_workflow.message).strip()
    if not message_value:
        _raise_schedule_edit_error("message cannot be empty")
    description_value = (description if description is not None else existing_workflow.description).strip()
    return existing_workflow.model_copy(
        update={
            "execute_at": resolved_execute_at,
            "cron_schedule": cron_schedule,
            "message": message_value,
            "description": description_value or message_value,
            "room_id": room_id,
            "silent": existing_workflow.silent if silent is None else silent,
        },
    )


def _stored_task_status(content: dict[str, object]) -> str:
    """Return a stored task's status, which every reader shares; a task stored without one is pending."""
    return str(content.get("status", "pending"))


def _parse_scheduled_task_record(
    room_id: str,
    task_id: str,
    content: dict[str, object],
) -> ScheduledTaskRecord | None:
    """Parse a Matrix state event content payload into a scheduled task record."""
    status = _stored_task_status(content)
    workflow_data_raw = content.get("workflow")
    if not isinstance(workflow_data_raw, str):
        return None
    try:
        workflow = ScheduledWorkflow(**json.loads(workflow_data_raw))
    except (TypeError, ValueError):
        logger.exception("Failed to parse scheduled task workflow", room_id=room_id, task_id=task_id)
        return None
    if not workflow.created_by:
        return None

    created_at = _parse_datetime(content.get("created_at"))
    revision = content.get("revision")
    owner = content.get("owner")
    return ScheduledTaskRecord(
        task_id=task_id,
        room_id=room_id,
        status=status,
        created_at=created_at,
        workflow=workflow,
        revision=revision if isinstance(revision, str) else None,
        owner_id=owner if isinstance(owner, str) else None,
    )


def _cancelled_task_content(task_id: str, existing_content: dict[str, object]) -> dict[str, object]:
    """Build cancelled task state while preserving existing metadata where possible."""
    cancelled_content: dict[str, object] = {"status": "cancelled", "task_id": task_id}

    workflow = existing_content.get("workflow")
    if isinstance(workflow, str):
        cancelled_content["workflow"] = workflow

    created_at = existing_content.get("created_at")
    if isinstance(created_at, str) and created_at:
        cancelled_content["created_at"] = created_at

    original_task_id = existing_content.get("task_id")
    if isinstance(original_task_id, str) and original_task_id:
        cancelled_content["task_id"] = original_task_id

    owner = existing_content.get("owner")
    if isinstance(owner, str):
        cancelled_content["owner"] = owner

    cancelled_content["updated_at"] = datetime.now(UTC).isoformat()
    return cancelled_content


def task_records(
    room_id: str,
    state_response: nio.RoomGetStateResponse,
    runtime_paths: RuntimePaths,
    include_non_pending: bool = False,
) -> list[ScheduledTaskRecord]:
    """Parse runtime-authored scheduled task records from a room state response."""
    runtime_account_ids = persisted_bot_user_ids(runtime_paths)
    tasks: list[ScheduledTaskRecord] = []
    unmanaged: Counter[str] = Counter()
    for event in state_response.events:
        if not isinstance(event, dict) or event.get("type") != SCHEDULED_TASK_EVENT_TYPE:
            continue

        state_key = event.get("state_key")
        content = _runtime_authored_task_content(room_id, event, runtime_account_ids)
        sender = event.get("sender")
        if content is None and isinstance(sender, str) and isinstance(event.get("content"), dict):
            unmanaged[sender] += 1
        if not isinstance(state_key, str) or content is None:
            continue
        # A room keeps every finished task's state, so only the records a caller wants are parsed.
        if not include_non_pending and _stored_task_status(content) != "pending":
            continue

        task = _parse_scheduled_task_record(room_id, state_key, content)
        if not task:
            continue
        tasks.append(task)

    for sender, count in unmanaged.items():
        key = (room_id, sender)
        if key not in _logged_unmanaged_authors:
            _logged_unmanaged_authors.add(key)
            logger.info("scheduled_task_state_ignored_unmanaged_author", room_id=room_id, sender=sender, count=count)
    return tasks


async def _room_state_for_tasks(client: nio.AsyncClient, room_id: str) -> nio.RoomGetStateResponse:
    """Read a room's full state to list or cancel its tasks, or raise that its tasks cannot be retrieved."""
    try:
        response = await client.room_get_state(room_id)
    except (aiohttp.ClientError, nio.LocalProtocolError) as exc:
        # nio raises a response that broke off, or a read through a lent client its bot closed since.
        msg = f"Unable to retrieve scheduled tasks: {type(exc).__name__}: {exc}"
        raise ScheduledTaskStateReadError(msg) from exc
    if not isinstance(response, nio.RoomGetStateResponse):
        msg = f"Unable to retrieve scheduled tasks: {response}"
        raise ScheduledTaskStateReadError(msg)
    return response


async def get_scheduled_tasks_for_room(
    client: nio.AsyncClient,
    room_id: str,
    runtime_paths: RuntimePaths,
    include_non_pending: bool = False,
) -> list[ScheduledTaskRecord]:
    """Fetch and parse scheduled tasks for a room.

    Raises:
        ScheduledTaskStateReadError: When the room's state cannot be read.

    """
    response = await _room_state_for_tasks(client, room_id)
    return task_records(room_id, response, runtime_paths, include_non_pending)


async def get_pending_schedule_thread_ids_for_room(
    client: nio.AsyncClient,
    room_id: str,
    runtime_paths: RuntimePaths,
) -> frozenset[str | None]:
    """Return existing-thread scopes suppressed by pending schedules in one room.

    Raises:
        ScheduledTaskStateReadError: When the room's state cannot be read.

    """
    tasks = await get_scheduled_tasks_for_room(client, room_id, runtime_paths)
    return frozenset(
        None if task.workflow.thread_id in {None, "main"} else task.workflow.thread_id
        for task in tasks
        if not task.workflow.new_thread
    )


async def _homeserver_returns_full_state_events(client: nio.AsyncClient, room_id: str) -> bool:
    """Return whether the homeserver honours ``format=event``, proven on the room's never-changing create event.

    A server that ignores ``format=event`` answers both reads with the same content, which cannot hold itself
    under ``content``.
    """
    failure = f"Failed to read the create event of room `{room_id}`"
    try:
        full, bare = [
            await client._send(
                nio.RoomGetStateEventResponse,
                "GET",
                nio.Api._build_path(["rooms", room_id, "state", "m.room.create", ""], query),
                response_data=("m.room.create", "", room_id),
            )
            for query in ({"format": "event"}, None)
        ]
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        msg = f"{failure}: {type(exc).__name__}: {exc}"
        raise ScheduledTaskStateReadError(msg) from exc
    if not isinstance(full, nio.RoomGetStateEventResponse) or not isinstance(bare, nio.RoomGetStateEventResponse):
        msg = f"{failure}: {full} {bare}"
        raise ScheduledTaskStateReadError(msg)
    return full.content.get("content") == bare.content


async def _read_scheduled_task_state(
    client: nio.AsyncClient,
    room_id: str,
    task_id: str,
    runtime_paths: RuntimePaths,
) -> dict[str, typing.Any] | None:
    """Fetch one scheduled-task state payload, treating state not written by MindRoom as absent."""
    failure = f"Failed to get scheduled task `{task_id}` from room `{room_id}`"
    try:
        # Nio's state-event read omits the sender; Synapse and Tuwunel return the full event for format=event.
        response = await client._send(
            nio.RoomGetStateEventResponse,
            "GET",
            nio.Api._build_path(["rooms", room_id, "state", SCHEDULED_TASK_EVENT_TYPE, task_id], {"format": "event"}),
            response_data=(SCHEDULED_TASK_EVENT_TYPE, task_id, room_id),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        msg = f"{failure}: {type(exc).__name__}: {exc}"
        raise ScheduledTaskStateReadError(msg) from exc
    if isinstance(response, nio.RoomGetStateEventError) and state_failure(response) is StateFailure.MISSING:
        return None
    if not isinstance(response, nio.RoomGetStateEventResponse):
        msg = f"{failure}: {response}"
        raise ScheduledTaskStateReadError(msg)
    event = response.content
    if not isinstance(event.get("sender"), str) or not isinstance(event.get("content"), dict):
        msg = f"Scheduled task `{task_id}` in room `{room_id}` was not returned as a full state event"
        raise ScheduledTaskStateReadError(msg)
    # A server that ignores format=event returns the state content itself, whose author can shape it like a
    # bot-written event, so only a server proven to return whole events names the sender.
    if not await _homeserver_returns_full_state_events(client, room_id):
        msg = f"Scheduled task `{task_id}` in room `{room_id}` was read from a homeserver that ignores format=event"
        raise ScheduledTaskStateReadError(msg)
    return _runtime_authored_task_content(room_id, event, persisted_bot_user_ids(runtime_paths))


async def get_scheduled_task(
    client: nio.AsyncClient,
    room_id: str,
    task_id: str,
    runtime_paths: RuntimePaths,
) -> ScheduledTaskRecord | None:
    """Fetch and parse a single runtime-authored scheduled task from Matrix state."""
    content = await _read_scheduled_task_state(client, room_id, task_id, runtime_paths)
    if content is None:
        return None
    task = _parse_scheduled_task_record(room_id, task_id, content)
    if task is None:
        msg = f"Task `{task_id}` has invalid stored state; cancel it and schedule it again."
        raise SchedulingError(msg)
    return task


async def require_scheduled_task(
    client: nio.AsyncClient,
    room_id: str,
    task_id: str,
    runtime_paths: RuntimePaths,
) -> ScheduledTaskRecord:
    """Return a stored runtime-authored task, or raise ScheduledTaskNotFoundError when the room does not store it."""
    task = await get_scheduled_task(client, room_id, task_id, runtime_paths)
    if task is None:
        raise ScheduledTaskNotFoundError(task_id)
    return task


async def _put_scheduled_task_state_content(
    client: nio.AsyncClient,
    room_id: str,
    task_id: str,
    content: dict[str, typing.Any],
    writers: ScheduledTaskRuntime | None = None,
) -> None:
    """Write scheduled-task state as the room's steward, whose power there no other running bot exceeds.

    ``client`` writes only where there is no runtime to name a steward.
    A room without a steward, or whose steward lacks the power, gets the moderator-rights answer; any other failure
    may succeed on a retry.
    """
    with nullcontext(client) if writers is None else writers.lend_client(room_id, client) as writer:
        if writer is None:
            logger.warning("scheduled_task_state_refused", room_id=room_id, task_id=task_id, error="no steward")
            raise ScheduledTaskStateRefusedError(moderator_rights_message("save schedules"))
        try:
            outcome: object = await writer.room_put_state(
                room_id=room_id,
                event_type=SCHEDULED_TASK_EVENT_TYPE,
                content=content,
                state_key=task_id,
            )
        except Exception as exc:
            outcome = f"{type(exc).__name__}: {exc!s}"
    if isinstance(outcome, nio.RoomPutStateResponse):
        return
    if isinstance(outcome, nio.RoomPutStateError) and state_failure(outcome) is StateFailure.REFUSED:
        logger.warning("scheduled_task_state_refused", room_id=room_id, task_id=task_id, error=str(outcome))
        raise ScheduledTaskStateRefusedError(moderator_rights_message("save schedules"))
    msg = f"Failed to persist scheduled task state for task `{task_id}`: {outcome}."
    raise ScheduledTaskStorageError(msg)


async def persist_scheduled_task_state(
    client: nio.AsyncClient,
    room_id: str,
    task_id: str,
    workflow: ScheduledWorkflow,
    *,
    config: Config,
    owner_id: str | None,
    status: str = "pending",
    created_at: datetime | None = None,
    writers: ScheduledTaskRuntime | None = None,
) -> str:
    """Persist scheduled task state to Matrix and return its unique revision.

    The stored `cron_description` is written for clients in the task's own timezone, which `config` supplies for a
    task that has none.
    `owner_id` is the task's owner, which every rewrite keeps.
    """
    revision = str(uuid.uuid4())
    content = {
        "task_id": task_id,
        "revision": revision,
        "workflow": workflow.model_dump_json(),
        "cron_description": workflow.cron_description(scheduled_task_timezone(workflow, config)),
        "status": status,
        "created_at": (created_at or datetime.now(UTC)).isoformat(),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    if owner_id is not None:
        content["owner"] = owner_id
    await _put_scheduled_task_state_content(
        client=client,
        room_id=room_id,
        task_id=task_id,
        content=content,
        writers=writers,
    )
    return revision


def _raise_unless_editable(task: ScheduledTaskRecord) -> None:
    """Refuse an edit to a task that no longer runs, or to one whose stored call was approved as scheduled."""
    if task.status != "pending":
        msg = f"Task `{task.task_id}` cannot be edited because it is `{task.status}`."
        raise SchedulingError(msg)
    # A stored call and its approval belong to the task as scheduled, so changing it means scheduling it again.
    if task.workflow.pre_approved_call:
        msg = (
            f"Task `{task.task_id}` runs a pre-approved tool call, which cannot be edited; "
            "cancel it and schedule the call again."
        )
        raise SchedulingError(msg)


async def save_edited_scheduled_task(
    client: nio.AsyncClient,
    workflow: ScheduledWorkflow,
    existing_task: ScheduledTaskRecord,
    runtime_paths: RuntimePaths,
    *,
    config: Config,
    writers: ScheduledTaskRuntime | None = None,
) -> ScheduledTaskRecord:
    """Persist `workflow` as the new content of `existing_task`, unless the task changed since it was read.

    Runtime task runners are untouched; they pick the edit up from the stored state.
    """
    _raise_unless_editable(existing_task)
    if workflow.schedule_type != existing_task.workflow.schedule_type:
        raise SchedulingError(_SCHEDULE_TYPE_CHANGE_NOT_SUPPORTED_ERROR)

    room_id, task_id = existing_task.room_id, existing_task.task_id
    async with scheduled_task_state_lock(client, room_id, task_id):
        current_task = await get_scheduled_task(client, room_id, task_id, runtime_paths)
        if current_task is None or current_task.status != "pending":
            msg = f"Task `{task_id}` cannot be edited because it is no longer pending."
            raise SchedulingError(msg)
        if current_task != existing_task:
            msg = f"Task `{task_id}` changed while it was being edited; refresh and retry."
            raise SchedulingError(msg)
        revision = await persist_scheduled_task_state(
            client=client,
            room_id=room_id,
            task_id=task_id,
            workflow=workflow,
            config=config,
            # An edit never changes which bot runs the task, whichever bot writes it.
            owner_id=current_task.owner_id,
            status="pending",
            created_at=current_task.created_at,
            writers=writers,
        )

    return replace(current_task, workflow=workflow, revision=revision)


def _format_local_time(dt: datetime, timezone_str: str) -> str:
    """Format a datetime as 24-hour wall time in the configured timezone, like "2024-01-15 15:30 EST"."""
    return dt.astimezone(ZoneInfo(timezone_str)).strftime("%Y-%m-%d %H:%M %Z")


def _format_scheduled_time(dt: datetime, timezone_str: str) -> str:
    """Format a datetime with timezone and relative time delta.

    Args:
        dt: Datetime in UTC
        timezone_str: Timezone string (e.g., 'America/New_York')

    Returns:
        Formatted string like "2024-01-15 15:30 EST (2 hours from now)"

    """
    # A timedelta of aware instants, because humanize subtracts datetimes as naive host-local wall times,
    # which differ from real elapsed time whenever the host's UTC offset differs at the two instants.
    relative_str = humanize.naturaltime(datetime.now(UTC) - dt)
    return f"{_format_local_time(dt, timezone_str)} ({relative_str})"


def _extract_mentioned_agents_from_text(
    message: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> list[MatrixID]:
    """Extract valid agent mentions from a scheduled message."""
    mentioned_user_ids = parse_mentions_in_text(message, config, runtime_paths)[1]
    # Text without mentions does not need prepared Matrix accounts.
    if not mentioned_user_ids:
        return []
    registry = entity_identity_registry(config, runtime_paths)
    return [
        MatrixID.parse(user_id)
        for user_id in mentioned_user_ids
        if registry.current_entity_name_for_user_id(user_id, include_router=False)
    ]


def _history_limit_display(history_limit: int) -> str:
    """Render one scheduled-task history limit for chat surfaces."""
    if history_limit == 0:
        return "none"
    return f"last {history_limit} message{'s' if history_limit != 1 else ''}"


def _scheduled_task_response_text(
    workflow: ScheduledWorkflow,
    *,
    task_id: str,
    config: Config,
) -> str:
    """Render the confirmation message for a scheduled or edited task."""
    timezone = scheduled_task_timezone(workflow, config)
    if workflow.schedule_type == "once" and workflow.execute_at:
        response_text = f"✅ Scheduled for {_format_scheduled_time(workflow.execute_at, timezone)}\n"
    elif workflow.cron_schedule:
        cron_tz = scheduled_cron_timezone(workflow)
        natural_desc = workflow.cron_description(timezone)
        cron_str = workflow.cron_schedule.to_cron_string()
        response_text = f"✅ Scheduled recurring task: **{natural_desc}**\n"
        response_text += f"   _(Cron: `{cron_str}` {cron_tz})_\n"
    else:
        response_text = "✅ Task scheduled\n"

    response_text += f"\n**Task:** {workflow.description}\n"
    response_text += f"**Will post:** {workflow.message}\n"
    if workflow.model is not None:
        response_text += f"**Model:** {workflow.model}\n"
    if workflow.history_limit is not None:
        response_text += f"**History:** {_history_limit_display(workflow.history_limit)}\n"
    mode = "Silent (hidden trigger; no-report final omitted)" if workflow.silent else "Visible"
    response_text += f"**Mode:** {mode}\n"
    if workflow.new_thread and workflow.silent:
        delivery = "Room-level roots for findings/failures"
    else:
        delivery = "New thread per fire" if workflow.new_thread else "Current room/thread scope"
    response_text += f"**Delivery:** {delivery}\n"
    return response_text + f"\n**Task ID:** `{task_id}`"


async def _validate_schedule_responders(context: ToolRuntimeContext, *, message: str) -> None:
    """Ensure at least one responder may answer the requester and every mentioned entity is allowed in the room."""
    config, runtime_paths = context.current_config, context.runtime_paths
    candidates = await context.responder_candidates_for_current_room(
        _require_context_room(context),
        context.requester_id,
    )
    if not candidates:
        _raise_schedule_edit_error("No agents or teams in this room are allowed to reply to you.")
    unavailable = [
        m for m in _extract_mentioned_agents_from_text(message, config, runtime_paths) if m not in candidates
    ]
    if unavailable:
        registry = entity_identity_registry(config, runtime_paths)
        suggestions = "\n💡 ".join(
            f"@{registry.current_entity_name_for_user_id(a.full_id, include_router=False)} is not available in this room"
            for a in unavailable
        )
        _raise_schedule_edit_error(
            "Failed to schedule: The following agents or teams are not available in this room: "
            f"{', '.join(a.full_id for a in unavailable)}\n\n💡 {suggestions}",
        )


async def schedule_task(
    context: ToolRuntimeContext,
    *,
    message: str,
    new_thread: bool,
    execute_at: str | None = None,
    delay_seconds: int | None = None,
    cron: str | None = None,
    description: str | None = None,
    timezone: str | None = None,
    history_limit: int | None = None,
    silent: bool = False,
    model: str | None = None,
) -> str:
    """Schedule a structured one-time or recurring workflow and return the confirmation.

    Raises:
        SchedulingError: Why the task was not scheduled.

    """
    message_value = message.strip()
    if not message_value:
        _raise_schedule_edit_error("message cannot be empty.")

    _validate_history_limit(history_limit)
    config = context.current_config
    room_id = context.room_id
    task_runtime = _context_task_runtime(context)
    selected_model = _resolve_model(model, existing_model=None, config=config)
    if sum(value is not None for value in (execute_at, delay_seconds, cron)) != 1:
        _raise_schedule_edit_error("Provide exactly one of execute_at, delay_seconds, or cron.")

    task_timezone = (
        _validate_timezone(timezone) if timezone is not None else entity_timezone(config, context.agent_name)
    )
    resolved_execute_at = _parse_once_time(execute_at=execute_at, delay_seconds=delay_seconds, timezone=task_timezone)
    cron_schedule = _parse_cron_schedule(cron, cron_timezone=task_timezone) if cron is not None else None
    schedule_type: Literal["once", "cron"] = "cron" if cron_schedule is not None else "once"
    cron_timezone = task_timezone if cron_schedule is not None else None

    await _validate_schedule_responders(context, message=message_value)

    workflow = ScheduledWorkflow(
        schedule_type=schedule_type,
        execute_at=resolved_execute_at,
        cron_schedule=cron_schedule,
        message=message_value,
        description=(description or "").strip() or message_value,
        history_limit=history_limit,
        model=selected_model,
        created_by=context.requester_id,
        thread_id=None if new_thread else context.resolved_thread_id,
        room_id=room_id,
        new_thread=new_thread,
        silent=silent,
        pre_approved_call=False,
        timezone=task_timezone,
        source_event_id=context.reply_to_event_id,
        cron_timezone=cron_timezone,
    )

    task_id = str(uuid.uuid4())[:8]
    logger.info(
        "Storing workflow task in Matrix state",
        task_id=task_id,
        room_id=room_id,
        thread_id=workflow.thread_id,
        new_thread=new_thread,
        schedule_type=workflow.schedule_type,
    )

    try:
        # A new task is owned by the bot that saves it; the room's steward only writes its state.
        await persist_scheduled_task_state(
            context.client,
            room_id,
            task_id,
            workflow,
            config=config,
            owner_id=context.client.user_id,
            writers=task_runtime,
        )
        if task_runtime is not None:
            task_runtime.start_new_task(task_id, workflow, owner_id=context.client.user_id)
    except SchedulingError as e:
        msg = f"Failed to schedule: {e}"
        raise SchedulingError(msg) from e

    return _scheduled_task_response_text(workflow, task_id=task_id, config=config)


def scheduled_call_workflow_digest(task_id: str, workflow: ScheduledWorkflow) -> str:
    """Fingerprint the fields that define the call a scheduled-call approval was given for."""
    execute_at = None if workflow.execute_at is None else workflow.execute_at.astimezone(UTC).isoformat()
    defining_fields = [task_id, workflow.room_id, workflow.thread_id, workflow.created_by, workflow.message, execute_at]
    return hashlib.sha256(json.dumps(defining_fields, separators=(",", ":")).encode()).hexdigest()


def _stored_task_has_scheduled_call(room_id: str, task_id: str, content: dict[str, object]) -> bool:
    """Return whether a stored task carries a call approval; an unreadable task counts, so cancelling fails closed."""
    record = _parse_scheduled_task_record(room_id, task_id, content)
    return record is None or record.workflow.pre_approved_call


async def withdraw_scheduled_call(task_id: str, *, reason: str) -> None:
    """Withdraw a task's call approval, reporting a failure or an unready approval runtime as task-state failures are."""
    manager = approval_manager.get_approval_store()
    if manager is None:
        msg = f"could not withdraw the approval for its scheduled call ({approval_manager.RUNTIME_NOT_READY_REASON})"
        raise ScheduledTaskStorageError(msg)
    try:
        await manager.withdraw_scheduled_call_approval(task_id, reason=reason)
    except Exception as exc:
        msg = f"could not withdraw the approval for its scheduled call ({exc})"
        raise ScheduledTaskStorageError(msg) from exc


def _scheduled_call_trigger_message(
    agent_name: str,
    task_id: str,
    tool_name: str,
    description: str,
    *,
    any_arguments_offered: bool,
) -> str:
    """Ask the scheduling agent to run the stored call by its task ID; the arguments stay out of Matrix."""
    message = (
        f"@{agent_name} Run scheduled call `{task_id}` now: call `run_scheduled_call` with task_id "
        f'"{task_id}" once, then report its result. It runs the `{tool_name}` call the requester approved.'
    )
    if any_arguments_offered:
        message += (
            "\n\nIf the requester approved any arguments, you may pass `arguments_json` with the "
            f"arguments for `{tool_name}` that this task needs: {description}"
        )
    return message


async def _checked_scheduled_call(
    context: ToolRuntimeContext,
    *,
    call: LiveFunction,
    tool_name: str,
    arguments: dict[str, object],
    execute_at: str,
) -> tuple[datetime, str]:
    """Return the send time and approver for a call that may be pre-approved, or raise why it may not."""
    config = context.current_config
    runtime_paths = context.runtime_paths
    agent_name = context.agent_name
    scheduled_by = context.requester_id
    if agent_name not in config.agents:
        msg = "Only an agent, not a team, can schedule a pre-approved tool call."
        raise SchedulingError(msg)
    send_at = _parse_execute_at(execute_at, timezone=None)
    approver_id = resolve_tool_approval_approver(config, runtime_paths, scheduled_by)
    if approver_id is None:
        msg = "Only a human requester can pre-approve a scheduled tool call."
        raise SchedulingError(msg)
    try:
        requires_approval, _timeout_seconds = await evaluate_tool_approval(
            config,
            runtime_paths,
            tool_name,
            arguments,
            agent_name,
            toolkit_approval_alias(config, config_plugins(config, runtime_paths).tools, call.toolkit_name, tool_name),
        )
    except ToolApprovalScriptError as exc:
        msg = f"Could not evaluate the approval policy for `{tool_name}`: {exc}"
        raise SchedulingError(msg) from exc
    if not requires_approval and not call.authored_confirmation:
        msg = f'`{tool_name}` does not require approval; use `scheduler(action="create", ...)` instead.'
        raise SchedulingError(msg)
    responders = await context.responder_candidates_for_current_room(_require_context_room(context), scheduled_by)
    if entity_identity_registry(config, runtime_paths).current_id(agent_name) not in responders:
        msg = f"`{agent_name}` cannot receive a scheduled call from you in this room."
        raise SchedulingError(msg)
    return send_at, approver_id


async def schedule_approved_tool_call(
    context: ToolRuntimeContext,
    *,
    call: LiveFunction,
    tool_name: str,
    arguments: dict[str, object],
    execute_at: str,
    description: str,
) -> str:
    """Store one gated call of the agent's own, ask the requester to approve it now, and return the confirmation.

    Raises:
        SchedulingError: Why the call was not scheduled.

    """
    room_id = context.room_id
    thread_id = context.resolved_thread_id
    scheduled_by = context.requester_id
    agent_name = context.agent_name
    if thread_id is None:
        msg = "Pre-approved tool calls must be scheduled from a thread."
        raise SchedulingError(msg)
    config = context.current_config
    task_runtime = _context_task_runtime(context)
    send_at, approver_id = await _checked_scheduled_call(
        context,
        call=call,
        tool_name=tool_name,
        arguments=arguments,
        execute_at=execute_at,
    )

    task_id = str(uuid.uuid4())[:8]
    timezone = entity_timezone(config, agent_name)
    any_arguments_offered = scheduled_call_offers_any_arguments(
        config,
        tool_name,
        arguments,
        requester_id=scheduled_by,
        approver_id=approver_id,
        authored_confirmation=call.authored_confirmation,
        tools=config_plugins(config, context.runtime_paths).tools,
    )
    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=send_at,
        message=_scheduled_call_trigger_message(
            agent_name,
            task_id,
            tool_name,
            description,
            any_arguments_offered=any_arguments_offered,
        ),
        description=description,
        history_limit=0,
        created_by=scheduled_by,
        thread_id=thread_id,
        room_id=room_id,
        pre_approved_call=True,
        timezone=timezone,
        source_event_id=context.reply_to_event_id,
    )
    binding = ScheduledCallBinding(
        task_id=task_id,
        room_id=room_id,
        thread_id=thread_id,
        requester_id=scheduled_by,
        agent_name=agent_name,
        toolkit_name=call.toolkit_name,
        tool_name=tool_name,
        arguments_json=canonical_arguments(arguments),
        workflow_digest=scheduled_call_workflow_digest(task_id, workflow),
        execute_at_ns=int(send_at.timestamp() * 1_000_000_000),
    )
    # Publish the task only once its card exists, so no cancel or edit can reach a task whose
    # card is still being prepared; a card whose task never published is withdrawn.
    task_published = False
    # None until the request answers: a request that raised may have reserved its card.
    card_reserved: bool | None = None
    manager = approval_manager.get_approval_store()
    try:
        card_reserved = manager is not None and await manager.request_scheduled_call_approval(
            binding,
            approver_user_id=approver_id,
            scheduled_for_text=_format_local_time(send_at, timezone),
            any_arguments_offered=any_arguments_offered,
        )
        if not card_reserved:
            msg = "Could not post an approvable approval card for this call; nothing was scheduled."
            raise SchedulingError(msg)
        try:
            await persist_scheduled_task_state(
                context.client,
                room_id,
                task_id,
                workflow,
                config=config,
                owner_id=context.client.user_id,
                writers=task_runtime,
            )
        except SchedulingError as e:
            msg = f"Failed to schedule: {e}"
            raise SchedulingError(msg) from e
        task_published = True
    finally:
        if not task_published and card_reserved is not False:
            await withdraw_scheduled_call(task_id, reason=SCHEDULE_CANCELLED_REASON)
    # Started only once the task is published, so a failed start never withdraws the approval a stored task uses.
    if task_runtime is not None:
        task_runtime.start_new_task(task_id, workflow, owner_id=context.client.user_id)
    scheduled_for = _format_scheduled_time(send_at, timezone)
    return (
        f"✅ Scheduled `{tool_name}` for {scheduled_for} (task `{task_id}`).\n"
        "Approve the card in this thread now to let it run then without asking again; "
        "without that approval it will ask for approval when it runs."
    )


def _resolve_tool_edit_schedule(
    existing: ScheduledWorkflow,
    *,
    config: Config,
    execute_at: str | None,
    delay_seconds: int | None,
    cron: str | None,
    timezone: str | None,
) -> tuple[datetime | None, str | None, str | None]:
    """Resolve tool-edit timing and timezone fields."""
    if sum(value is not None for value in (execute_at, delay_seconds, cron)) > 1:
        _raise_schedule_edit_error("Provide at most one of execute_at, delay_seconds, or cron.")
    validated_tz = _validate_timezone(timezone) if timezone is not None else None
    if existing.schedule_type == "once":
        parse_tz = validated_tz or scheduled_task_timezone(existing, config)
        return (
            _parse_once_time(execute_at=execute_at, delay_seconds=delay_seconds, timezone=parse_tz),
            validated_tz or existing.timezone,
            None,
        )

    if execute_at is not None or delay_seconds is not None:
        _raise_schedule_edit_error(
            f"Cannot set execute_at or delay_seconds on a cron schedule. {_SCHEDULE_TYPE_CHANGE_NOT_SUPPORTED_ERROR}.",
        )
    if cron is not None:
        new_cron_tz = validated_tz or existing.cron_timezone or scheduled_task_timezone(existing, config)
        new_tz = validated_tz or (new_cron_tz if existing.timezone is None else existing.timezone)
        return None, new_tz, new_cron_tz
    if validated_tz is not None:
        if existing.cron_timezone is None:
            _raise_schedule_edit_error(
                "Cannot update timezone on a legacy UTC cron schedule without also passing `cron` in the target timezone.",
            )
        if existing.cron_schedule is not None:
            _parse_cron_schedule(existing.cron_schedule.to_cron_string(), cron_timezone=validated_tz)
        return None, validated_tz, validated_tz
    return None, existing.timezone, existing.cron_timezone


async def edit_scheduled_task(
    context: ToolRuntimeContext,
    task_id: str,
    *,
    message: str | None = None,
    description: str | None = None,
    execute_at: str | None = None,
    delay_seconds: int | None = None,
    cron: str | None = None,
    timezone: str | None = None,
    history_limit: int | None = None,
    clear_history_limit: bool = False,
    silent: bool | None = None,
    model: str | None = None,
) -> str:
    """Edit an existing scheduled task using structured patch semantics and return the confirmation.

    Raises:
        SchedulingError: Why the task was not edited; ScheduledTaskNotFoundError when the room does not store it.

    """
    room_id = context.room_id
    existing_task = await require_scheduled_task(context.client, room_id, task_id, context.runtime_paths)
    _raise_unless_editable(existing_task)

    existing = existing_task.workflow
    config = context.current_config

    try:
        if history_limit is not None and clear_history_limit:
            _raise_schedule_edit_error("Cannot set both history_limit and clear_history_limit.")
        _validate_history_limit(history_limit)
        selected_model = _resolve_model(model, existing_model=existing.model, config=config)
        resolved_execute_at, new_tz, new_cron_tz = _resolve_tool_edit_schedule(
            existing,
            config=config,
            execute_at=execute_at,
            delay_seconds=delay_seconds,
            cron=cron,
            timezone=timezone,
        )
        tool_updated = existing.model_copy(
            update={
                "timezone": new_tz,
                "cron_timezone": new_cron_tz,
                "history_limit": (
                    None
                    if clear_history_limit
                    else (existing.history_limit if history_limit is None else history_limit)
                ),
                "model": selected_model,
                "created_by": context.requester_id,
            },
        )
        updated_workflow = build_edited_scheduled_workflow(
            tool_updated,
            room_id=room_id,
            message=message,
            description=description,
            execute_at=resolved_execute_at,
            cron_expression=cron,
            silent=silent,
        )
        if existing == updated_workflow.model_copy(update={"created_by": existing.created_by}):
            _raise_schedule_edit_error("No changes specified for this scheduled task.")
        await _validate_schedule_responders(context, message=updated_workflow.message)
        await save_edited_scheduled_task(
            client=context.client,
            workflow=updated_workflow,
            config=config,
            existing_task=existing_task,
            runtime_paths=context.runtime_paths,
            writers=_context_task_runtime(context),
        )
    except SchedulingError as e:
        msg = f"Failed to edit task `{task_id}`.\n\n{e}"
        raise SchedulingError(msg) from e

    return f"✅ Updated task `{task_id}`.\n\n{_scheduled_task_response_text(updated_workflow, task_id=task_id, config=config)}"


async def list_scheduled_tasks(  # noqa: C901
    client: nio.AsyncClient,
    room_id: str,
    runtime_paths: RuntimePaths,
    config: Config,
    thread_id: str | None = None,
) -> str:
    """List scheduled tasks in human-readable format, each in its own timezone.

    Raises:
        SchedulingError: When the room's state cannot be read.

    """
    task_records = await get_scheduled_tasks_for_room(client, room_id, runtime_paths)

    tasks: list[ScheduledTaskRecord] = []
    tasks_in_other_threads: list[ScheduledTaskRecord] = []
    new_thread_tasks: list[ScheduledTaskRecord] = []

    for record in task_records:
        if thread_id:
            if record.workflow.new_thread:
                new_thread_tasks.append(record)
            elif record.workflow.thread_id and record.workflow.thread_id != thread_id:
                tasks_in_other_threads.append(record)
            else:
                tasks.append(record)
        else:
            tasks.append(record)

    if not tasks and not tasks_in_other_threads and not new_thread_tasks:
        return "No scheduled tasks found."

    # Sort by execution time (one-time tasks) or put recurring tasks at the end
    def _sort_key(r: ScheduledTaskRecord) -> tuple[bool, datetime]:
        t = r.workflow.execute_at if r.workflow.schedule_type == "once" else None
        return (t is None, t or datetime.max.replace(tzinfo=UTC))

    tasks.sort(key=_sort_key)
    new_thread_tasks.sort(key=_sort_key)

    def _append_task_lines(lines: list[str], records: list[ScheduledTaskRecord]) -> None:
        for record in records:
            workflow = record.workflow
            timezone = scheduled_task_timezone(workflow, config)
            if workflow.schedule_type == "once" and workflow.execute_at:
                time_str = _format_scheduled_time(workflow.execute_at, timezone)
            else:
                time_str = workflow.cron_description(timezone) or "recurring"

            msg_preview = workflow.message[:_MESSAGE_PREVIEW_LENGTH] + (
                "..." if len(workflow.message) > _MESSAGE_PREVIEW_LENGTH else ""
            )
            task_line = f'• `{record.task_id}` - {time_str}\n  {workflow.description}\n  Message: "{msg_preview}"'
            if workflow.model is not None:
                task_line += f"\n  Model: {workflow.model}"
            if workflow.history_limit is not None:
                task_line += f"\n  History: {_history_limit_display(workflow.history_limit)}"
            task_line += f"\n  Mode: {'Silent' if workflow.silent else 'Visible'}"
            lines.append(task_line)

    if tasks:
        lines = ["**Scheduled Tasks:**"]
        _append_task_lines(lines, tasks)
    else:
        lines = ["No scheduled tasks in this thread."]

    if new_thread_tasks:
        lines.append("")
        lines.append("**New Room-Level Thread Roots:**")
        _append_task_lines(lines, new_thread_tasks)

    if tasks_in_other_threads:
        lines.append("")
        lines.append(
            f"📌 {len(tasks_in_other_threads)} task(s) scheduled in other threads. Use /list_schedules in those threads to see details.",
        )

    return "\n".join(lines)


async def _cancel_stored_task(
    client: nio.AsyncClient,
    room_id: str,
    task_id: str,
    content: dict[str, object],
    task_runtime: ScheduledTaskRuntime | None,
) -> None:
    """Publish one stored task's cancellation and end its runner; the caller holds the task's state lock.

    A scheduled call's approval is withdrawn before the cancellation is published, so an interruption between the
    two leaves a runnable task that asks again rather than a cancelled task whose approval still works.
    """
    if _stored_task_has_scheduled_call(room_id, task_id, content):
        await withdraw_scheduled_call(task_id, reason=SCHEDULE_CANCELLED_REASON)
    await _put_scheduled_task_state_content(
        client=client,
        room_id=room_id,
        task_id=task_id,
        content=_cancelled_task_content(task_id, content),
        writers=task_runtime,
    )
    if task_runtime is not None:
        task_runtime.cancel_runner(task_id)


async def cancel_scheduled_task(
    client: nio.AsyncClient,
    room_id: str,
    task_id: str,
    runtime_paths: RuntimePaths,
    task_runtime: ScheduledTaskRuntime | None = None,
) -> str:
    """Cancel a scheduled task, end its runner, and return the confirmation.

    Raises:
        SchedulingError: Why the task was not cancelled; ScheduledTaskNotFoundError when the room does not store it.

    """
    async with scheduled_task_state_lock(client, room_id, task_id):
        existing_content = await _read_scheduled_task_state(client, room_id, task_id, runtime_paths)
        if existing_content is None:
            raise ScheduledTaskNotFoundError(task_id)
        status = _stored_task_status(existing_content)
        if status != "pending":
            msg = f"Task `{task_id}` cannot be cancelled because it is `{status}`."
            raise SchedulingError(msg)
        try:
            await _cancel_stored_task(client, room_id, task_id, existing_content, task_runtime)
        except ScheduledTaskStorageError as e:
            msg = f"Failed to cancel task `{task_id}`: {e}"
            raise ScheduledTaskStorageError(msg) from e

    record = _parse_scheduled_task_record(room_id, task_id, existing_content)
    if record is not None and record.workflow.pre_approved_call:
        return f"✅ Cancelled task `{task_id}`; any approval given for its scheduled call is withdrawn."
    return f"✅ Cancelled task `{task_id}`"


async def cancel_all_scheduled_tasks(
    client: nio.AsyncClient,
    room_id: str,
    runtime_paths: RuntimePaths,
    task_runtime: ScheduledTaskRuntime | None = None,
) -> str:
    """Cancel all scheduled tasks in a room, end their runners, and return how many were cancelled.

    Raises:
        SchedulingError: When the room's state cannot be read, or when every cancellation failed.

    """
    response = await _room_state_for_tasks(client, room_id)
    cancelled_count = 0
    failures: list[ScheduledTaskStateRefusedError | None] = []
    runtime_account_ids = persisted_bot_user_ids(runtime_paths)

    for event in response.events:
        if event["type"] != SCHEDULED_TASK_EVENT_TYPE:
            continue
        content = _runtime_authored_task_content(room_id, event, runtime_account_ids)
        if content is None or _stored_task_status(content) != "pending":
            continue
        task_id = event["state_key"]
        # The room snapshot only picks candidates; each task is re-read under its lock,
        # so an edit that is still writing cannot land after the cancellation.
        try:
            async with scheduled_task_state_lock(client, room_id, task_id):
                current_content = await _read_scheduled_task_state(client, room_id, task_id, runtime_paths)
                if current_content is None or _stored_task_status(current_content) != "pending":
                    continue
                await _cancel_stored_task(client, room_id, task_id, current_content, task_runtime)
            cancelled_count += 1
            logger.info("scheduled_task_cancelled", task_id=task_id)
        except Exception as error:
            logger.exception("scheduled_task_cancel_failed", task_id=task_id)
            failures.append(refused_for_power(error))
    if cancelled_count == 0:
        if failures and (refusal := failures[0]) is not None and all(failures):
            raise refusal
        if failures:
            msg = f"Failed to cancel {len(failures)} scheduled task(s)"
            raise SchedulingError(msg)
        return "No scheduled tasks to cancel."

    result = f"✅ Cancelled {cancelled_count} scheduled task(s)"
    if failures:
        result += f"\n⚠️ Failed to cancel {len(failures)} task(s)"

    return result
