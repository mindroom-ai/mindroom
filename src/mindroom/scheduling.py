"""Scheduled task management with AI-powered workflow scheduling."""

from __future__ import annotations

import asyncio
import json
import typing
import uuid
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, NamedTuple
from zoneinfo import ZoneInfo

import humanize
import nio
from agno.agent import Agent
from cron_descriptor import get_description
from croniter import croniter
from pydantic import BaseModel, Field

from mindroom.ai import get_model_instance
from mindroom.authorization import get_available_agents_for_sender
from mindroom.constants import (
    ORIGINAL_SENDER_KEY,
    ROUTER_AGENT_NAME,
    SCHEDULED_TASK_EVENT_TYPE,
    runtime_matrix_homeserver,
)
from mindroom.hooks import (
    HookRegistry,
    ScheduleFiredContext,
    build_hook_matrix_admin,
    build_hook_room_state_putter,
    build_hook_room_state_querier,
    emit,
)
from mindroom.hooks.sender import build_hook_message_sender
from mindroom.hooks.types import EVENT_SCHEDULE_FIRED
from mindroom.logging_config import bound_log_context, get_logger
from mindroom.matrix.client import describe_matrix_response_error, send_message_result
from mindroom.matrix.identity import MatrixID
from mindroom.matrix.mentions import format_message_with_mentions, parse_mentions_in_text
from mindroom.matrix.message_builder import build_message_content
from mindroom.matrix.power_levels import (
    POWER_LEVELS_EVENT_TYPE,
    required_state_event_power_level,
    user_power_level,
)
from mindroom.matrix.state import MatrixState
from mindroom.matrix.users import INTERNAL_USER_ACCOUNT_KEY, create_agent_user, login_agent_user
from mindroom.message_target import MessageTarget
from mindroom.thread_utils import get_agents_in_thread

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.hooks.types import HookMatrixAdmin
    from mindroom.matrix.conversation_cache import ConversationCacheProtocol, ConversationEventCache
    from mindroom.router_helpers import LiveRouterRuntime

logger = get_logger(__name__)

# Maximum length for message preview in task listings
_MESSAGE_PREVIEW_LENGTH = 50

# Shared validation message for edit attempts that change task type.
SCHEDULE_TYPE_CHANGE_NOT_SUPPORTED_ERROR = "Changing schedule_type is not supported; cancel and recreate the schedule"

# How often running tasks should re-check persisted Matrix state for edits/cancellations.
_TASK_STATE_POLL_INTERVAL_SECONDS = 30
_MAX_PENDING_TASK_READ_RETRIES = 10

# Maximum age (in seconds) for a missed one-time task to still be executed on restart.
# Tasks older than this are marked as failed instead of executed.
_MISSED_TASK_MAX_AGE_SECONDS = 86400  # 24 hours

# Small pause between draining overdue one-time tasks after sync is ready.
_DEFERRED_OVERDUE_TASK_START_DELAY_SECONDS = 0.25

# Global task storage for running asyncio tasks
_running_tasks: dict[str, asyncio.Task] = {}
_deferred_overdue_tasks: deque[_DeferredOverdueTaskStart] = deque()
_deferred_overdue_task_ids: set[str] = set()
_ACTIVE_HOOK_REGISTRY: HookRegistry = HookRegistry.empty()


class _AgentValidationResult(NamedTuple):
    """Result of agent mention validation."""

    all_valid: bool
    valid_agents: list[MatrixID]
    invalid_agents: list[MatrixID]


type ScheduledTaskOperationReason = Literal[
    "not_found",
    "invalid_state",
    "schedule_type_change_not_supported",
    "permission_denied",
    "state_unavailable",
    "runtime_unavailable",
    "persist_failed",
    "cancel_failed",
    "writer_unavailable",
    "writer_state_unavailable",
]


@dataclass(frozen=True, slots=True)
class ScheduledTaskExecutionRuntime:
    """Live collaborators required to start and run one scheduled task."""

    client: nio.AsyncClient
    conversation_cache: ConversationCacheProtocol
    event_cache: ConversationEventCache


class ScheduledTaskWriterDecision(StrEnum):
    """Decision produced for one scheduled-task writer candidate."""

    WRITE = "write"
    SKIP = "skip"
    ERROR = "error"


class ScheduledTaskOperationError(ValueError):
    """User-facing scheduling operation failure."""

    reason: ScheduledTaskOperationReason
    public_message: str
    diagnostic_message: str

    def __init__(
        self,
        reason: ScheduledTaskOperationReason,
        public_message: str,
        *,
        diagnostic_message: str | None = None,
    ) -> None:
        super().__init__(public_message)
        self.reason = reason
        self.public_message = public_message
        self.diagnostic_message = diagnostic_message or public_message


def _scheduled_task_operation_error(
    *,
    reason: ScheduledTaskOperationReason,
    public_message: str,
    diagnostic_message: str | None = None,
) -> ScheduledTaskOperationError:
    """Build one structured scheduling operation error."""
    return ScheduledTaskOperationError(
        reason,
        public_message,
        diagnostic_message=diagnostic_message,
    )


def _raise_scheduled_task_operation_error(
    *,
    reason: ScheduledTaskOperationReason,
    public_message: str,
    diagnostic_message: str | None = None,
) -> typing.NoReturn:
    """Raise one user-facing scheduling operation failure."""
    raise _scheduled_task_operation_error(
        reason=reason,
        public_message=public_message,
        diagnostic_message=diagnostic_message,
    )


def _raise_scheduled_task_not_found(task_id: str) -> typing.NoReturn:
    """Raise when one scheduled task does not exist."""
    _raise_scheduled_task_operation_error(
        reason="not_found",
        public_message=f"Task `{task_id}` not found.",
    )


def _membership_check_failed_due_to_not_joined(response: object) -> bool:
    """Return whether a joined-members failure most likely means the client is not joined."""
    return _matrix_errcode_is(response, "M_FORBIDDEN", "M_NOT_FOUND")


def _scheduled_task_writer_public_message(detail: str) -> str:
    """Render the shared user-facing prefix for writer resolution failures."""
    return f"Cannot persist scheduled tasks in this room. {detail}"


def _format_scheduled_task_failure(action: str, message: str) -> str:
    """Render one scheduling failure at the user-facing boundary."""
    return f"❌ Failed to {action}: {message}"


def _describe_matrix_call_exception(error: Exception) -> str:
    """Render one raised Matrix client exception for logs and user-facing diagnostics."""
    error_message = str(error)
    if error_message:
        return f"{type(error).__name__}: {error_message}"
    return type(error).__name__


def _matrix_errcode_is(response: object, *errcodes: str) -> bool:
    """Return whether one Matrix error response advertises one of the given errcodes."""
    return isinstance(response, nio.ErrorResponse) and response.status_code in errcodes


def _scheduled_task_writer_skip(
    *,
    public_detail: str,
    diagnostic_message: str,
) -> tuple[ScheduledTaskWriterDecision, str | None, str]:
    """Return one non-fatal writer candidate result."""
    return (
        ScheduledTaskWriterDecision.SKIP,
        _scheduled_task_writer_public_message(public_detail),
        diagnostic_message,
    )


def _scheduled_task_writer_error(
    *,
    public_detail: str,
    diagnostic_message: str,
) -> tuple[ScheduledTaskWriterDecision, str | None, str]:
    """Return one writer candidate failure caused by state or transport errors."""
    return (
        ScheduledTaskWriterDecision.ERROR,
        _scheduled_task_writer_public_message(public_detail),
        diagnostic_message,
    )


async def _read_scheduled_task_state_event(
    client: nio.AsyncClient,
    room_id: str,
    task_id: str,
) -> nio.RoomGetStateEventResponse:
    """Read one scheduled-task state event and normalize transport exceptions."""
    try:
        response = await client.room_get_state_event(
            room_id=room_id,
            event_type=SCHEDULED_TASK_EVENT_TYPE,
            state_key=task_id,
        )
    except Exception as error:
        diagnostic_message = (
            f"Failed to read scheduled task `{task_id}` in room `{room_id}`: {_describe_matrix_call_exception(error)}."
        )
        raise _scheduled_task_operation_error(
            reason="state_unavailable",
            public_message="Unable to retrieve scheduled task state.",
            diagnostic_message=diagnostic_message,
        ) from error

    if isinstance(response, nio.RoomGetStateEventResponse):
        return response
    if isinstance(response, nio.ErrorResponse) and response.status_code == "M_NOT_FOUND":
        _raise_scheduled_task_not_found(task_id)

    diagnostic_message = (
        f"Failed to read scheduled task `{task_id}` in room `{room_id}`: {describe_matrix_response_error(response)}."
    )
    raise _scheduled_task_operation_error(
        reason="state_unavailable",
        public_message="Unable to retrieve scheduled task state.",
        diagnostic_message=diagnostic_message,
    )


async def _read_scheduled_tasks_room_state(
    client: nio.AsyncClient,
    room_id: str,
) -> nio.RoomGetStateResponse:
    """Read scheduled-task room state and normalize transport failures."""
    try:
        response = await client.room_get_state(room_id)
    except Exception as error:
        diagnostic_message = (
            f"Failed to read scheduled tasks for room `{room_id}`: {_describe_matrix_call_exception(error)}."
        )
        raise _scheduled_task_operation_error(
            reason="state_unavailable",
            public_message="Unable to retrieve scheduled tasks.",
            diagnostic_message=diagnostic_message,
        ) from error

    if isinstance(response, nio.RoomGetStateResponse):
        return response

    diagnostic_message = (
        f"Failed to read scheduled tasks for room `{room_id}`: {describe_matrix_response_error(response)}."
    )
    raise _scheduled_task_operation_error(
        reason="state_unavailable",
        public_message="Unable to retrieve scheduled tasks.",
        diagnostic_message=diagnostic_message,
    )


async def _scheduled_task_joined_member_ids(
    client: nio.AsyncClient,
    room_id: str,
    *,
    subject_label: str,
) -> set[str] | tuple[ScheduledTaskWriterDecision, str | None, str]:
    """Return joined member ids or one writer-status failure."""
    try:
        joined_members_response = await client.joined_members(room_id)
    except Exception as error:
        return _scheduled_task_writer_error(
            public_detail="MindRoom could not verify room membership for scheduled tasks. Retry in a moment.",
            diagnostic_message=(
                f"{subject_label} cannot verify joined membership in `{room_id}`: "
                f"{_describe_matrix_call_exception(error)}."
            ),
        )

    if not isinstance(joined_members_response, nio.JoinedMembersResponse):
        if _membership_check_failed_due_to_not_joined(joined_members_response):
            return _scheduled_task_writer_skip(
                public_detail="Invite the router to the room. If the router was just invited, wait for it to join and retry.",
                diagnostic_message=(
                    f"{subject_label} appears not joined to room `{room_id}`: "
                    f"{describe_matrix_response_error(joined_members_response)}."
                ),
            )
        return _scheduled_task_writer_error(
            public_detail="MindRoom could not verify room membership for scheduled tasks. Retry in a moment.",
            diagnostic_message=(
                f"{subject_label} cannot verify joined membership in `{room_id}`: "
                f"{describe_matrix_response_error(joined_members_response)}."
            ),
        )

    return {member.user_id for member in joined_members_response.members}


async def _scheduled_task_power_level_status(
    client: nio.AsyncClient,
    room_id: str,
    *,
    subject_label: str,
    user_id: str,
) -> tuple[ScheduledTaskWriterDecision, str | None, str]:
    """Return whether one joined client can write scheduled-task state based on power levels."""
    try:
        power_levels_response = await client.room_get_state_event(room_id, POWER_LEVELS_EVENT_TYPE)
    except Exception as error:
        return _scheduled_task_writer_error(
            public_detail=(
                "MindRoom could not read the room power levels needed for scheduled tasks. "
                "Ask a room admin to check room permissions and retry."
            ),
            diagnostic_message=(
                f"{subject_label} cannot read room power levels in `{room_id}`: "
                f"{_describe_matrix_call_exception(error)}."
            ),
        )

    if not isinstance(power_levels_response, nio.RoomGetStateEventResponse):
        return _scheduled_task_writer_error(
            public_detail=(
                "MindRoom could not read the room power levels needed for scheduled tasks. "
                "Ask a room admin to check room permissions and retry."
            ),
            diagnostic_message=(
                f"{subject_label} cannot read room power levels in `{room_id}`: "
                f"{describe_matrix_response_error(power_levels_response)}."
            ),
        )
    if not isinstance(power_levels_response.content, dict):
        return _scheduled_task_writer_error(
            public_detail=(
                "MindRoom found invalid room power-level state. Ask a room admin to repair the room permissions and retry."
            ),
            diagnostic_message=f"{subject_label} received invalid power-level content for `{room_id}`.",
        )

    required_power_level = required_state_event_power_level(
        power_levels_response.content,
        event_type=SCHEDULED_TASK_EVENT_TYPE,
    )
    current_power_level = user_power_level(
        power_levels_response.content,
        user_id=user_id,
    )
    if current_power_level >= required_power_level:
        return (ScheduledTaskWriterDecision.WRITE, None, "")

    return _scheduled_task_writer_skip(
        public_detail="Ask a room admin to grant a joined MindRoom bot enough power to manage scheduled tasks, then retry.",
        diagnostic_message=(
            f"{subject_label} has Matrix power level {current_power_level}, but "
            f"`{SCHEDULED_TASK_EVENT_TYPE}` requires {required_power_level} in `{room_id}`."
        ),
    )


async def _scheduled_task_writer_status(
    client: nio.AsyncClient,
    room_id: str,
    *,
    subject_label: str,
) -> tuple[ScheduledTaskWriterDecision, str | None, str]:
    """Return whether one client can write scheduled-task state in one room."""
    user_id = client.user_id
    if not isinstance(user_id, str) or not user_id:
        return _scheduled_task_writer_skip(
            public_detail="MindRoom is not ready to manage scheduled tasks yet. Retry in a moment.",
            diagnostic_message=f"{subject_label} is not logged in to Matrix.",
        )

    joined_member_ids = await _scheduled_task_joined_member_ids(
        client,
        room_id,
        subject_label=subject_label,
    )
    if isinstance(joined_member_ids, tuple):
        return joined_member_ids
    if user_id not in joined_member_ids:
        return _scheduled_task_writer_skip(
            public_detail="Invite the router to the room. If the router was just invited, wait for it to join and retry.",
            diagnostic_message=f"{subject_label} is not joined to room `{room_id}` ({user_id}).",
        )

    return await _scheduled_task_power_level_status(
        client,
        room_id,
        subject_label=subject_label,
        user_id=user_id,
    )


async def resolve_scheduled_task_writer(
    client: nio.AsyncClient,
    room_id: str,
    *,
    router_client: nio.AsyncClient | None = None,
) -> nio.AsyncClient:
    """Return the Matrix client that should own scheduled-task writes for one room."""
    return await _resolve_scheduled_task_writer_candidates(
        room_id,
        _scheduled_task_writer_candidates(
            current_client=client,
            router_client=router_client,
        ),
    )


def _scheduled_task_writer_candidates(
    *,
    current_client: nio.AsyncClient,
    router_client: nio.AsyncClient | None = None,
    additional_clients: typing.Sequence[nio.AsyncClient] = (),
) -> tuple[tuple[str, nio.AsyncClient], ...]:
    """Return ordered unique writer candidates for one scheduled-task operation."""
    candidates: list[tuple[str, nio.AsyncClient]] = []

    def append_candidate(label: str, candidate: nio.AsyncClient | None) -> None:
        if candidate is None:
            return
        if any(existing is candidate for _, existing in candidates):
            return
        candidates.append((label, candidate))

    append_candidate("Router", router_client)
    append_candidate("Current bot", current_client)
    for index, candidate in enumerate(additional_clients, start=1):
        label = "Task writer bot" if len(additional_clients) == 1 else f"Task writer bot {index}"
        append_candidate(label, candidate)

    return tuple(candidates)


async def _resolve_scheduled_task_writer_candidates(
    room_id: str,
    candidates: typing.Sequence[tuple[str, nio.AsyncClient]],
) -> nio.AsyncClient:
    """Return the first writer candidate that can manage scheduled-task state."""
    last_skip_message: str | None = None
    last_error_message: str | None = None
    diagnostics: list[str] = []

    for subject_label, candidate in candidates:
        decision, public_message, diagnostic_message = await _scheduled_task_writer_status(
            candidate,
            room_id,
            subject_label=subject_label,
        )
        if diagnostic_message:
            diagnostics.append(diagnostic_message)
        if decision == ScheduledTaskWriterDecision.WRITE:
            return candidate
        if decision == ScheduledTaskWriterDecision.SKIP and public_message is not None and last_skip_message is None:
            last_skip_message = public_message
        if decision == ScheduledTaskWriterDecision.ERROR and public_message is not None:
            last_error_message = public_message

    error_reason: ScheduledTaskOperationReason = (
        "writer_state_unavailable" if last_error_message is not None else "writer_unavailable"
    )
    public_message = last_error_message or last_skip_message
    if public_message is None:
        public_message = _scheduled_task_writer_public_message(
            "MindRoom could not determine which joined bot should manage scheduled tasks. Retry in a moment.",
        )
    diagnostic_message = " ".join(part for part in diagnostics if part)
    logger.warning(
        "scheduled_task_writer_resolution_failed",
        room_id=room_id,
        reason=error_reason,
        public_message=public_message,
        diagnostic_message=diagnostic_message,
    )
    raise _scheduled_task_operation_error(
        reason=error_reason,
        public_message=public_message,
        diagnostic_message=diagnostic_message,
    )


async def _resolve_scheduled_task_writer_with_retry(
    *,
    room_id: str,
    current_client: nio.AsyncClient,
    router_client: nio.AsyncClient | None = None,
    additional_clients: typing.Sequence[nio.AsyncClient] = (),
) -> tuple[nio.AsyncClient, typing.Callable[[], typing.Awaitable[nio.AsyncClient]]]:
    """Return one writer plus a matching re-resolution closure."""
    candidates = _scheduled_task_writer_candidates(
        current_client=current_client,
        router_client=router_client,
        additional_clients=additional_clients,
    )

    async def re_resolve_writer() -> nio.AsyncClient:
        return await _resolve_scheduled_task_writer_candidates(room_id, candidates)

    return (await re_resolve_writer(), re_resolve_writer)


def _raise_scheduled_workflow_send_error() -> typing.NoReturn:
    """Raise when a scheduled workflow message cannot be sent."""
    msg = "Failed to send scheduled workflow message to Matrix"
    raise RuntimeError(msg)


def set_scheduling_hook_registry(hook_registry: HookRegistry) -> None:
    """Update the immutable hook snapshot used by scheduled task runners."""
    global _ACTIVE_HOOK_REGISTRY
    _ACTIVE_HOOK_REGISTRY = hook_registry


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

    def to_natural_language(self) -> str:
        """Convert cron schedule to natural language description."""
        try:
            cron_str = self.to_cron_string()
            return str(get_description(cron_str))
        except Exception:
            return f"Cron: {self.to_cron_string()}"


class ScheduledWorkflow(BaseModel):
    """Structured representation of a scheduled task or workflow."""

    schedule_type: Literal["once", "cron"]
    is_conditional: bool = False
    execute_at: datetime | None = None
    cron_schedule: CronSchedule | None = None
    message: str
    description: str
    created_by: str | None = None
    thread_id: str | None = None
    room_id: str | None = None
    new_thread: bool = False


class _WorkflowParseError(BaseModel):
    """Error response when workflow parsing fails."""

    error: str
    suggestion: str | None = None


@dataclass
class ScheduledTaskRecord:
    """Parsed scheduled task state from Matrix."""

    task_id: str
    room_id: str
    status: str
    created_at: datetime | None
    workflow: ScheduledWorkflow


@dataclass(frozen=True)
class SchedulingRuntime:
    """Live scheduling collaborators required to create or edit running tasks."""

    client: nio.AsyncClient
    config: Config
    runtime_paths: RuntimePaths
    room: nio.MatrixRoom
    conversation_cache: ConversationCacheProtocol
    event_cache: ConversationEventCache
    matrix_admin: HookMatrixAdmin | None = None
    router_client: nio.AsyncClient | None = None
    router_runtime: LiveRouterRuntime | None = None


@dataclass(frozen=True)
class ScheduledTaskMutationContext:
    """Task lookup plus the writer selection needed for one existing task mutation."""

    task: ScheduledTaskRecord
    task_sender_id: str | None
    writer_client: nio.AsyncClient
    re_resolve_writer: typing.Callable[[], typing.Awaitable[nio.AsyncClient]]
    temporary_clients: tuple[nio.AsyncClient, ...] = ()
    task_content: dict[str, object] | None = None

    async def close(self) -> None:
        """Close any temporary clients opened for task mutation."""
        seen_client_ids: set[int] = set()
        for client in self.temporary_clients:
            client_id = id(client)
            if client_id in seen_client_ids:
                continue
            seen_client_ids.add(client_id)
            await client.close()


@dataclass
class _DeferredOverdueTaskStart:
    """A one-time scheduled task that should start after Matrix sync is live."""

    task_id: str
    workflow: ScheduledWorkflow


def _current_execution_runtime(runtime: SchedulingRuntime) -> ScheduledTaskExecutionRuntime:
    """Return the execution runtime for the requesting bot."""
    return ScheduledTaskExecutionRuntime(
        client=runtime.client,
        conversation_cache=runtime.conversation_cache,
        event_cache=runtime.event_cache,
    )


def _execution_runtime_for_writer(
    runtime: SchedulingRuntime,
    *,
    writer_client: nio.AsyncClient,
) -> ScheduledTaskExecutionRuntime:
    """Return the live runtime bundle that belongs to the resolved scheduled-task writer."""
    current_runtime = _current_execution_runtime(runtime)
    if writer_client is runtime.client:
        return current_runtime

    router_runtime = runtime.router_runtime
    if router_runtime is not None and writer_client is router_runtime.client:
        return ScheduledTaskExecutionRuntime(
            client=router_runtime.client,
            conversation_cache=router_runtime.conversation_cache,
            event_cache=router_runtime.event_cache,
        )

    router_runtime_client = router_runtime.client.user_id if router_runtime is not None else None
    _raise_scheduled_task_operation_error(
        reason="runtime_unavailable",
        public_message="MindRoom could not start the scheduled task runner from the selected bot runtime. Retry in a moment.",
        diagnostic_message=(
            "Resolved scheduled-task writer has no matching live runtime bundle. "
            f"writer_client={writer_client.user_id!r} current_client={runtime.client.user_id!r} "
            f"router_runtime_client={router_runtime_client!r}."
        ),
    )


def _scheduled_task_restore_sender_ids(
    config: Config,
    runtime_paths: RuntimePaths,
) -> set[str]:
    """Return known bot user ids allowed to restore scheduled-task state."""
    known_bot_user_ids = {matrix_id.full_id for matrix_id in config.get_ids(runtime_paths).values()}
    state = MatrixState.load(runtime_paths=runtime_paths)
    domain = config.get_domain(runtime_paths)
    for account_key, account in state.accounts.items():
        if not account_key.startswith("agent_") or account_key == INTERNAL_USER_ACCOUNT_KEY:
            continue
        known_bot_user_ids.add(MatrixID.from_username(account.username, domain).full_id)
    return known_bot_user_ids


def _parse_datetime(value: object) -> datetime | None:
    """Parse an ISO datetime string into a datetime object."""
    if not isinstance(value, str) or not value:
        return None

    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _parse_scheduled_task_record(
    room_id: str,
    task_id: str,
    content: dict[str, object],
) -> ScheduledTaskRecord | None:
    """Parse a Matrix state event content payload into a scheduled task record."""
    status = str(content.get("status", "pending"))
    workflow_data_raw = content.get("workflow")
    if isinstance(workflow_data_raw, str):
        try:
            workflow = ScheduledWorkflow(**json.loads(workflow_data_raw))
        except (ValueError, json.JSONDecodeError):
            logger.exception("Failed to parse scheduled task workflow", room_id=room_id, task_id=task_id)
            return None
    elif status != "pending":
        # Backward compatibility: older cancellation paths wrote only {"status": "cancelled"}.
        description_value = content.get("description")
        description = (
            description_value if isinstance(description_value, str) and description_value else "Cancelled task"
        )
        message_value = content.get("message")
        message = message_value if isinstance(message_value, str) else ""
        thread_id_value = content.get("thread_id")
        thread_id = thread_id_value if isinstance(thread_id_value, str) else None
        created_by_value = content.get("created_by")
        created_by = created_by_value if isinstance(created_by_value, str) else None
        workflow = ScheduledWorkflow(
            schedule_type="once",
            execute_at=None,
            message=message,
            description=description,
            created_by=created_by,
            thread_id=thread_id,
            room_id=room_id,
        )
    else:
        return None

    created_at = _parse_datetime(content.get("created_at"))
    return ScheduledTaskRecord(
        task_id=task_id,
        room_id=room_id,
        status=status,
        created_at=created_at,
        workflow=workflow,
    )


def _cancelled_task_content(
    task_id: str,
    existing_content: dict[str, object] | None,
) -> dict[str, object]:
    """Build cancelled task state while preserving existing metadata where possible."""
    cancelled_content: dict[str, object] = {"status": "cancelled", "task_id": task_id}

    if existing_content:
        workflow = existing_content.get("workflow")
        if isinstance(workflow, str):
            cancelled_content["workflow"] = workflow

        created_at = existing_content.get("created_at")
        if isinstance(created_at, str) and created_at:
            cancelled_content["created_at"] = created_at

        original_task_id = existing_content.get("task_id")
        if isinstance(original_task_id, str) and original_task_id:
            cancelled_content["task_id"] = original_task_id

    cancelled_content["updated_at"] = datetime.now(UTC).isoformat()
    return cancelled_content


def _is_polling_cron_schedule(cron_schedule: CronSchedule) -> bool:
    """Return whether a cron schedule looks like an interval-based polling cadence."""
    if cron_schedule.day != "*" or cron_schedule.month != "*" or cron_schedule.weekday != "*":
        return False

    minute = cron_schedule.minute.strip()
    hour = cron_schedule.hour.strip()

    def is_interval(field: str) -> bool:
        return field == "*" or field.startswith("*/")

    return (is_interval(minute) and is_interval(hour)) or (minute.isdigit() and is_interval(hour))


def _validate_conditional_workflow(
    workflow: ScheduledWorkflow,
) -> _WorkflowParseError | None:
    """Reject conditional parses that do not resolve to a polling-style recurring schedule."""
    if not workflow.is_conditional:
        return None

    if workflow.schedule_type != "cron" or workflow.cron_schedule is None:
        return _WorkflowParseError(
            error="Conditional schedules must resolve to a recurring polling schedule.",
            suggestion="Try again, or specify the polling cadence explicitly.",
        )

    cron_string = workflow.cron_schedule.to_cron_string()
    if _is_polling_cron_schedule(workflow.cron_schedule):
        return None

    return _WorkflowParseError(
        error=f"Conditional schedules must use a polling cron, but the parsed schedule was `{cron_string}`.",
        suggestion="Try again, or specify the polling cadence explicitly.",
    )


def _start_scheduled_task(
    client: nio.AsyncClient,
    task_id: str,
    workflow: ScheduledWorkflow,
    config: Config,
    runtime_paths: RuntimePaths,
    event_cache: ConversationEventCache,
    conversation_cache: ConversationCacheProtocol,
    matrix_admin: HookMatrixAdmin | None = None,
) -> bool:
    """Start the asyncio task for a scheduled workflow and track it globally."""
    existing_task = _running_tasks.get(task_id)
    if existing_task is not None:
        if existing_task.done():
            del _running_tasks[task_id]
        else:
            logger.debug("Scheduled task already running; skipping duplicate start", task_id=task_id)
            return False

    if workflow.schedule_type == "once":
        task = asyncio.create_task(
            _run_once_task(
                client,
                task_id,
                workflow,
                config,
                runtime_paths,
                event_cache,
                conversation_cache,
                matrix_admin,
            ),
        )
    else:
        task = asyncio.create_task(
            _run_cron_task(
                client,
                task_id,
                workflow,
                _running_tasks,
                config,
                runtime_paths,
                conversation_cache,
                matrix_admin,
            ),
        )
    _running_tasks[task_id] = task
    return True


def _queue_deferred_overdue_task(task_id: str, workflow: ScheduledWorkflow) -> bool:
    """Queue one missed one-time task to be started after Matrix sync is ready."""
    existing_task = _running_tasks.get(task_id)
    if existing_task is not None and not existing_task.done():
        logger.debug("Scheduled task already running; skipping deferred queue", task_id=task_id)
        return False

    if task_id in _deferred_overdue_task_ids:
        logger.debug("Scheduled task already queued for deferred start", task_id=task_id)
        return False

    _deferred_overdue_tasks.append(_DeferredOverdueTaskStart(task_id=task_id, workflow=workflow))
    _deferred_overdue_task_ids.add(task_id)
    return True


async def _resolve_restore_writer_client(
    *,
    room_id: str,
    current_client: nio.AsyncClient,
    additional_writer_clients: typing.Sequence[nio.AsyncClient],
) -> nio.AsyncClient | None:
    """Resolve one live writer client for restore/drain flows without assuming the router can write."""
    if not isinstance(current_client.user_id, str) or not current_client.user_id:
        return current_client
    try:
        writer_client, _re_resolve_writer = await _resolve_scheduled_task_writer_with_retry(
            room_id=room_id,
            current_client=current_client,
            router_client=current_client,
            additional_clients=additional_writer_clients,
        )
    except ScheduledTaskOperationError as error:
        logger.warning(
            "scheduled_task_restore_writer_resolution_failed",
            room_id=room_id,
            reason=error.reason,
            error=error.diagnostic_message,
        )
        return None
    return writer_client


async def drain_deferred_overdue_tasks(
    client: nio.AsyncClient,
    config: Config,
    runtime_paths: RuntimePaths,
    event_cache: ConversationEventCache,
    conversation_cache: ConversationCacheProtocol,
    additional_writer_clients: typing.Sequence[nio.AsyncClient] = (),
) -> int:
    """Start queued overdue one-time tasks after Matrix sync is ready."""
    drained_count = 0

    while _deferred_overdue_tasks:
        queued_task = _deferred_overdue_tasks.popleft()
        _deferred_overdue_task_ids.discard(queued_task.task_id)

        try:
            writer_client = await _resolve_restore_writer_client(
                room_id=queued_task.workflow.room_id or "",
                current_client=client,
                additional_writer_clients=additional_writer_clients,
            )
            if writer_client is None:
                continue
            if _start_scheduled_task(
                writer_client,
                queued_task.task_id,
                queued_task.workflow,
                config,
                runtime_paths,
                event_cache,
                conversation_cache,
                matrix_admin=build_hook_matrix_admin(client, runtime_paths),
            ):
                drained_count += 1
        except Exception:
            logger.exception(
                "Failed to start deferred overdue scheduled task",
                task_id=queued_task.task_id,
            )

        if _deferred_overdue_tasks:
            await asyncio.sleep(_DEFERRED_OVERDUE_TASK_START_DELAY_SECONDS)

    if drained_count > 0:
        logger.info("Drained deferred overdue scheduled tasks", drained_count=drained_count)

    return drained_count


def clear_deferred_overdue_tasks() -> int:
    """Clear queued overdue one-time tasks that have not started yet."""
    queued_count = len(_deferred_overdue_tasks)
    _deferred_overdue_tasks.clear()
    _deferred_overdue_task_ids.clear()
    return queued_count


def has_deferred_overdue_tasks() -> bool:
    """Return whether any overdue one-time tasks are still queued."""
    return bool(_deferred_overdue_tasks)


def _cancel_running_task(task_id: str) -> None:
    """Cancel a running scheduled task if it exists."""
    if task_id in _running_tasks:
        _running_tasks[task_id].cancel()
        del _running_tasks[task_id]


async def cancel_all_running_scheduled_tasks() -> int:
    """Cancel all in-memory scheduled tasks and wait for shutdown."""
    running_items = list(_running_tasks.items())
    if not running_items:
        return 0

    for task_id, task in running_items:
        task.cancel()
        del _running_tasks[task_id]

    await asyncio.gather(*(task for _, task in running_items), return_exceptions=True)

    return len(running_items)


def _workflows_differ(left: ScheduledWorkflow, right: ScheduledWorkflow) -> bool:
    """Return whether two workflows differ in persisted state."""
    return left.model_dump(mode="json") != right.model_dump(mode="json")


def _cleanup_task_if_current(task_id: str, running_tasks: dict[str, asyncio.Task]) -> None:
    """Remove task tracking if this coroutine still owns the task slot."""
    current_task = asyncio.current_task()
    if current_task and running_tasks.get(task_id) is current_task:
        del running_tasks[task_id]


def _parse_task_records_from_state(
    room_id: str,
    state_response: nio.RoomGetStateResponse,
    include_non_pending: bool = False,
) -> list[ScheduledTaskRecord]:
    """Parse scheduled task records from a room state response."""
    tasks: list[ScheduledTaskRecord] = []
    for event in state_response.events:
        if event.get("type") != SCHEDULED_TASK_EVENT_TYPE:
            continue

        state_key = event.get("state_key")
        content = event.get("content")
        if not isinstance(state_key, str) or not isinstance(content, dict):
            continue

        task = _parse_scheduled_task_record(room_id, state_key, content)
        if not task:
            continue
        if not include_non_pending and task.status != "pending":
            continue
        tasks.append(task)

    return tasks


async def get_scheduled_tasks_for_room(
    client: nio.AsyncClient,
    room_id: str,
    include_non_pending: bool = False,
) -> list[ScheduledTaskRecord]:
    """Fetch and parse scheduled tasks for a room."""
    response = await _read_scheduled_tasks_room_state(client, room_id)
    return _parse_task_records_from_state(room_id, response, include_non_pending)


async def get_scheduled_task_with_sender(
    client: nio.AsyncClient,
    room_id: str,
    task_id: str,
) -> tuple[ScheduledTaskRecord | None, str | None]:
    """Fetch one scheduled task together with the Matrix sender that wrote it."""
    task_record, task_sender_id, _task_content = await get_scheduled_task_with_sender_and_content(
        client=client,
        room_id=room_id,
        task_id=task_id,
    )
    return (task_record, task_sender_id)


async def get_scheduled_task_with_sender_and_content(
    client: nio.AsyncClient,
    room_id: str,
    task_id: str,
) -> tuple[ScheduledTaskRecord | None, str | None, dict[str, object] | None]:
    """Fetch one scheduled task together with its sender and raw content payload."""
    response = await _read_scheduled_tasks_room_state(client, room_id)
    for event in response.events:
        if event.get("type") != SCHEDULED_TASK_EVENT_TYPE or event.get("state_key") != task_id:
            continue
        content = event.get("content")
        if not isinstance(content, dict):
            return (None, None, None)
        return (_parse_scheduled_task_record(room_id, task_id, content), event.get("sender"), content)
    return (None, None, None)


def _scheduled_task_scope_matches(
    task: ScheduledTaskRecord,
    requester_thread_id: str | None,
) -> bool:
    """Return whether one requester thread scope matches the task scope."""
    task_thread_id = None if task.workflow.new_thread else task.workflow.thread_id
    return task_thread_id == requester_thread_id


async def _requester_has_scheduled_task_admin_access(
    reader_client: nio.AsyncClient,
    room_id: str,
    requester_id: str,
) -> bool:
    """Return whether the requester can administer room power levels in this room."""
    try:
        power_levels_response = await reader_client.room_get_state_event(room_id, POWER_LEVELS_EVENT_TYPE)
    except Exception as error:
        raise _scheduled_task_operation_error(
            reason="state_unavailable",
            public_message="MindRoom could not verify the room permissions for this scheduled task. Retry in a moment.",
            diagnostic_message=(
                "Failed to read room power levels for scheduled-task mutation authorization: "
                f"{_describe_matrix_call_exception(error)}."
            ),
        ) from error

    if not isinstance(power_levels_response, nio.RoomGetStateEventResponse):
        raise _scheduled_task_operation_error(
            reason="state_unavailable",
            public_message="MindRoom could not verify the room permissions for this scheduled task. Retry in a moment.",
            diagnostic_message=(
                "Failed to read room power levels for scheduled-task mutation authorization: "
                f"{describe_matrix_response_error(power_levels_response)}."
            ),
        )
    if not isinstance(power_levels_response.content, dict):
        raise _scheduled_task_operation_error(
            reason="state_unavailable",
            public_message="MindRoom found invalid room permissions while checking this scheduled task. Ask a room admin to repair the room and retry.",
            diagnostic_message="Scheduled-task mutation authorization received invalid power-level content.",
        )

    admin_power_level = required_state_event_power_level(
        power_levels_response.content,
        event_type=POWER_LEVELS_EVENT_TYPE,
    )
    requester_power_level = user_power_level(
        power_levels_response.content,
        user_id=requester_id,
    )
    return requester_power_level >= admin_power_level


async def _ensure_requester_can_mutate_scheduled_task(
    *,
    reader_client: nio.AsyncClient,
    room_id: str,
    task: ScheduledTaskRecord,
    requester_id: str | None,
    requester_thread_id: str | None,
) -> None:
    """Reject scheduled-task mutations that are outside the requester's ownership or scope."""
    if not requester_id:
        return
    if task.workflow.created_by == requester_id:
        return
    if _scheduled_task_scope_matches(task, requester_thread_id):
        return
    if await _requester_has_scheduled_task_admin_access(reader_client, room_id, requester_id):
        return

    _raise_scheduled_task_operation_error(
        reason="permission_denied",
        public_message=(
            "Only the task creator, someone operating in the same thread, or a room admin can manage this scheduled task."
        ),
    )


def _configured_entity_name_for_user_id(
    config: Config,
    runtime_paths: RuntimePaths,
    user_id: str | None,
) -> str | None:
    """Return the configured entity name for one Matrix user id."""
    if not isinstance(user_id, str) or not user_id:
        return None
    entity_ids = config.get_ids(runtime_paths)
    if not isinstance(entity_ids, dict):
        entity_ids = {}
    for entity_name, matrix_id in entity_ids.items():
        if matrix_id.full_id == user_id:
            return entity_name
    return None


def _scheduled_task_entity_display_name(entity_name: str, config: Config) -> str:
    """Return one display name for a configured or historical writer entity."""
    if entity_name == ROUTER_AGENT_NAME:
        return "RouterAgent"
    if entity_name in config.agents:
        return config.agents[entity_name].display_name
    if entity_name in config.teams:
        return config.teams[entity_name].display_name
    return entity_name


async def _login_configured_entity_client(
    config: Config,
    runtime_paths: RuntimePaths,
    entity_name: str,
) -> nio.AsyncClient:
    """Login one configured agent or team user for scheduled-task writes."""
    homeserver = runtime_matrix_homeserver(runtime_paths=runtime_paths)
    display_name = _scheduled_task_entity_display_name(entity_name, config)
    try:
        entity_user = await create_agent_user(
            homeserver,
            entity_name,
            display_name,
            runtime_paths=runtime_paths,
        )
        return await login_agent_user(homeserver, entity_user, runtime_paths)
    except ScheduledTaskOperationError:
        raise
    except Exception as error:
        raise _scheduled_task_operation_error(
            reason="writer_unavailable",
            public_message=_scheduled_task_writer_public_message(
                "MindRoom could not log in the bot that wrote this scheduled task. Retry in a moment.",
            ),
            diagnostic_message=(
                f"Failed to log in scheduled-task writer `{entity_name}`: {_describe_matrix_call_exception(error)}."
            ),
        ) from error


async def resolve_existing_scheduled_task_mutation(
    *,
    reader_client: nio.AsyncClient,
    room_id: str,
    task_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    router_client: nio.AsyncClient | None = None,
    requester_id: str | None = None,
    requester_thread_id: str | None = None,
) -> ScheduledTaskMutationContext:
    """Resolve task lookup and writer ownership for one edit/cancel operation."""
    existing_task, task_sender_id, task_content = await get_scheduled_task_with_sender_and_content(
        client=reader_client,
        room_id=room_id,
        task_id=task_id,
    )
    if existing_task is None:
        _raise_scheduled_task_not_found(task_id)
    await _ensure_requester_can_mutate_scheduled_task(
        reader_client=reader_client,
        room_id=room_id,
        task=existing_task,
        requester_id=requester_id,
        requester_thread_id=requester_thread_id,
    )

    temporary_clients: list[nio.AsyncClient] = []
    try:
        try:
            writer_client, re_resolve_writer = await _resolve_scheduled_task_writer_with_retry(
                room_id=room_id,
                current_client=reader_client,
                router_client=router_client,
            )
        except ScheduledTaskOperationError as live_writer_error:
            sender_entity_name = _configured_entity_name_for_user_id(config, runtime_paths, task_sender_id)
            sender_matches_live_client = isinstance(task_sender_id, str) and task_sender_id in {
                reader_client.user_id,
                None if router_client is None else router_client.user_id,
            }
            if sender_entity_name is None or sender_matches_live_client:
                raise

            sender_client: nio.AsyncClient | None = None
            sender_login_error: ScheduledTaskOperationError | None = None
            try:
                sender_client = await _login_configured_entity_client(
                    config,
                    runtime_paths,
                    sender_entity_name,
                )
            except ScheduledTaskOperationError as error:
                sender_login_error = error
                logger.warning(
                    "scheduled_task_sender_writer_login_skipped",
                    room_id=room_id,
                    task_id=task_id,
                    sender_entity_name=sender_entity_name,
                    sender=task_sender_id,
                    error=error.diagnostic_message,
                )

            if sender_client is None:
                if sender_login_error is None:
                    raise
                diagnostic_message = " ".join(
                    part
                    for part in [live_writer_error.diagnostic_message, sender_login_error.diagnostic_message]
                    if part
                )
                raise _scheduled_task_operation_error(
                    reason=live_writer_error.reason,
                    public_message=live_writer_error.public_message,
                    diagnostic_message=diagnostic_message,
                ) from sender_login_error

            temporary_clients.append(sender_client)
            writer_client, re_resolve_writer = await _resolve_scheduled_task_writer_with_retry(
                room_id=room_id,
                current_client=reader_client,
                router_client=router_client,
                additional_clients=(sender_client,),
            )
    except Exception:
        for client in temporary_clients:
            await client.close()
        raise

    return ScheduledTaskMutationContext(
        task=existing_task,
        task_sender_id=task_sender_id,
        writer_client=writer_client,
        re_resolve_writer=re_resolve_writer,
        temporary_clients=tuple(temporary_clients),
        task_content=task_content,
    )


async def get_scheduled_task(
    client: nio.AsyncClient,
    room_id: str,
    task_id: str,
) -> ScheduledTaskRecord | None:
    """Fetch and parse a single scheduled task from Matrix state."""
    try:
        response = await _read_scheduled_task_state_event(client, room_id, task_id)
    except ScheduledTaskOperationError as error:
        if error.reason == "not_found":
            return None
        raise
    if not isinstance(response.content, dict):
        return None
    return _parse_scheduled_task_record(room_id, task_id, response.content)


async def _get_pending_task_record(
    client: nio.AsyncClient,
    room_id: str | None,
    task_id: str,
    *,
    trusted_sender_ids: set[str] | None = None,
) -> ScheduledTaskRecord | None:
    """Return the latest pending task state for a task id, if it still exists."""
    if not room_id:
        return None

    task_record, task_sender_id = await get_scheduled_task_with_sender(
        client=client,
        room_id=room_id,
        task_id=task_id,
    )
    if task_record is None:
        return None
    if trusted_sender_ids is not None and task_sender_id not in trusted_sender_ids:
        logger.warning(
            "Skipping scheduled task update with untrusted sender",
            room_id=room_id,
            task_id=task_id,
            sender=task_sender_id,
        )
        return None
    if not task_record or task_record.status != "pending":
        return None
    return task_record


async def _get_pending_task_record_with_retry(
    client: nio.AsyncClient,
    room_id: str | None,
    task_id: str,
    *,
    trusted_sender_ids: set[str] | None = None,
) -> ScheduledTaskRecord | None:
    """Retry pending-task state reads instead of failing runners on transient read errors."""
    retry_delay_seconds = 1.0
    for attempt in range(1, _MAX_PENDING_TASK_READ_RETRIES + 1):
        try:
            return await _get_pending_task_record(
                client=client,
                room_id=room_id,
                task_id=task_id,
                trusted_sender_ids=trusted_sender_ids,
            )
        except ScheduledTaskOperationError as error:
            if attempt == _MAX_PENDING_TASK_READ_RETRIES:
                logger.exception(
                    "scheduled_task_state_read_retry_exhausted",
                    room_id=room_id,
                    task_id=task_id,
                    reason=error.reason,
                    error=error.diagnostic_message,
                    retry_attempts=attempt,
                )
                raise
            logger.warning(
                "scheduled_task_state_read_retrying",
                room_id=room_id,
                task_id=task_id,
                reason=error.reason,
                error=error.diagnostic_message,
                retry_attempt=attempt,
                retry_delay_seconds=retry_delay_seconds,
            )
            await asyncio.sleep(retry_delay_seconds)
            retry_delay_seconds = min(retry_delay_seconds * 2, _TASK_STATE_POLL_INTERVAL_SECONDS)

    return None


def _serialize_scheduled_task_created_at(created_at: datetime | str | None) -> str:
    """Normalize persisted scheduled-task timestamps."""
    if isinstance(created_at, datetime):
        return created_at.isoformat()
    if isinstance(created_at, str) and created_at:
        return created_at
    return datetime.now(UTC).isoformat()


async def _write_scheduled_task_state(
    client: nio.AsyncClient,
    room_id: str,
    task_id: str,
    content: dict[str, object],
    *,
    reason: Literal["persist_failed", "cancel_failed"],
    public_message: str,
    diagnostic_action: str,
    re_resolve_writer: typing.Callable[[], typing.Awaitable[nio.AsyncClient]] | None = None,
) -> nio.AsyncClient:
    """Persist scheduled-task state and normalize Matrix write failures."""
    writer_client = client
    actual_reason = reason
    for attempt in range(2):
        try:
            response = await writer_client.room_put_state(
                room_id=room_id,
                event_type=SCHEDULED_TASK_EVENT_TYPE,
                content=content,
                state_key=task_id,
            )
        except Exception as error:
            response_description = _describe_matrix_call_exception(error)
            break
        if isinstance(response, nio.RoomPutStateResponse):
            return writer_client
        if attempt == 0 and re_resolve_writer is not None and _matrix_errcode_is(response, "M_FORBIDDEN"):
            retry_writer = await re_resolve_writer()
            logger.warning(
                "scheduled_task_writer_retry_after_forbidden",
                room_id=room_id,
                task_id=task_id,
                previous_writer=writer_client.user_id,
                retry_writer=retry_writer.user_id,
            )
            writer_client = retry_writer
            continue
        if _matrix_errcode_is(response, "M_FORBIDDEN"):
            actual_reason = "permission_denied"
        response_description = describe_matrix_response_error(response)
        break

    diagnostic_message = (
        f"Failed to {diagnostic_action} in Matrix state for room `{room_id}`: "
        f"{response_description}. "
        f"Ensure this bot can send `{SCHEDULED_TASK_EVENT_TYPE}` state events."
    )
    raise _scheduled_task_operation_error(
        reason=actual_reason,
        public_message=public_message,
        diagnostic_message=diagnostic_message,
    )


async def _persist_scheduled_task_state(
    client: nio.AsyncClient,
    room_id: str,
    task_id: str,
    workflow: ScheduledWorkflow,
    status: str = "pending",
    created_at: datetime | str | None = None,
    re_resolve_writer: typing.Callable[[], typing.Awaitable[nio.AsyncClient]] | None = None,
) -> nio.AsyncClient:
    """Persist scheduled task state to Matrix."""
    return await _write_scheduled_task_state(
        client=client,
        room_id=room_id,
        task_id=task_id,
        content={
            "task_id": task_id,
            "workflow": workflow.model_dump_json(),
            "status": status,
            "created_at": _serialize_scheduled_task_created_at(created_at),
            "updated_at": datetime.now(UTC).isoformat(),
        },
        reason="persist_failed",
        public_message=(
            "MindRoom could not save this scheduled task because Matrix rejected the room-state write. "
            f"Ensure a joined MindRoom bot can send `{SCHEDULED_TASK_EVENT_TYPE}` state events and retry."
        ),
        diagnostic_action=f"persist scheduled task `{task_id}` as `{status}`",
        re_resolve_writer=re_resolve_writer,
    )


async def _persist_cancelled_scheduled_task_state(
    client: nio.AsyncClient,
    room_id: str,
    task_id: str,
    existing_content: dict[str, object] | None,
    re_resolve_writer: typing.Callable[[], typing.Awaitable[nio.AsyncClient]] | None = None,
) -> nio.AsyncClient:
    """Persist cancelled task state and surface Matrix write failures."""
    return await _write_scheduled_task_state(
        client=client,
        room_id=room_id,
        task_id=task_id,
        content=_cancelled_task_content(task_id, existing_content),
        reason="cancel_failed",
        public_message=(
            "MindRoom could not cancel this scheduled task because Matrix rejected the room-state write. "
            f"Ensure a joined MindRoom bot can send `{SCHEDULED_TASK_EVENT_TYPE}` state events and retry."
        ),
        diagnostic_action=f"cancel scheduled task `{task_id}`",
        re_resolve_writer=re_resolve_writer,
    )


async def _save_pending_scheduled_task(
    writer_client: nio.AsyncClient,
    room_id: str,
    task_id: str,
    workflow: ScheduledWorkflow,
    runtime: SchedulingRuntime,
    *,
    created_at: datetime | str | None = None,
    re_resolve_writer: typing.Callable[[], typing.Awaitable[nio.AsyncClient]] | None = None,
) -> None:
    """Persist one pending task and start or replace its in-memory runner."""
    _cancel_running_task(task_id)
    _execution_runtime_for_writer(
        runtime,
        writer_client=writer_client,
    )
    runtime_checked_re_resolve_writer = re_resolve_writer
    if re_resolve_writer is not None:

        async def _runtime_checked_re_resolve_writer() -> nio.AsyncClient:
            retry_writer = await re_resolve_writer()
            _execution_runtime_for_writer(
                runtime,
                writer_client=retry_writer,
            )
            return retry_writer

        runtime_checked_re_resolve_writer = _runtime_checked_re_resolve_writer

    final_writer_client = await _persist_scheduled_task_state(
        client=writer_client,
        room_id=room_id,
        task_id=task_id,
        workflow=workflow,
        status="pending",
        created_at=created_at,
        re_resolve_writer=runtime_checked_re_resolve_writer,
    )
    try:
        latest_task = await _get_pending_task_record(
            client=final_writer_client,
            room_id=room_id,
            task_id=task_id,
        )
    except ScheduledTaskOperationError as error:
        logger.warning(
            "scheduled_task_startup_state_recheck_failed",
            room_id=room_id,
            task_id=task_id,
            reason=error.reason,
            error=error.diagnostic_message,
        )
    else:
        if latest_task is None:
            logger.info(
                "Skipping scheduled task runner start because pending state changed before startup",
                room_id=room_id,
                task_id=task_id,
            )
            return
    execution_runtime = _execution_runtime_for_writer(
        runtime,
        writer_client=final_writer_client,
    )
    _start_scheduled_task(
        execution_runtime.client,
        task_id,
        workflow,
        runtime.config,
        runtime.runtime_paths,
        execution_runtime.event_cache,
        execution_runtime.conversation_cache,
        matrix_admin=(
            runtime.matrix_admin
            if execution_runtime.client is runtime.client and runtime.matrix_admin is not None
            else build_hook_matrix_admin(execution_runtime.client, runtime.runtime_paths)
        ),
    )


async def _save_one_time_task_status(
    client: nio.AsyncClient,
    task: ScheduledTaskRecord,
    status: str,
) -> None:
    """Persist the terminal status for a one-time task without restarting it."""
    await _persist_scheduled_task_state(
        client=client,
        room_id=task.room_id,
        task_id=task.task_id,
        workflow=task.workflow,
        status=status,
        created_at=task.created_at,
    )


async def save_edited_scheduled_task(
    client: nio.AsyncClient,
    room_id: str,
    task_id: str,
    workflow: ScheduledWorkflow,
    existing_task: ScheduledTaskRecord,
    re_resolve_writer: typing.Callable[[], typing.Awaitable[nio.AsyncClient]] | None = None,
) -> ScheduledTaskRecord:
    """Persist edits to an existing task without touching runtime task runners."""
    if existing_task.status != "pending":
        _raise_scheduled_task_operation_error(
            reason="invalid_state",
            public_message=f"Task `{task_id}` cannot be edited because it is `{existing_task.status}`.",
        )

    if workflow.schedule_type != existing_task.workflow.schedule_type:
        _raise_scheduled_task_operation_error(
            reason="schedule_type_change_not_supported",
            public_message=SCHEDULE_TYPE_CHANGE_NOT_SUPPORTED_ERROR,
        )

    await _persist_scheduled_task_state(
        client=client,
        room_id=room_id,
        task_id=task_id,
        workflow=workflow,
        status="pending",
        created_at=existing_task.created_at,
        re_resolve_writer=re_resolve_writer,
    )

    return ScheduledTaskRecord(
        task_id=task_id,
        room_id=room_id,
        status="pending",
        created_at=existing_task.created_at,
        workflow=workflow,
    )


async def _parse_workflow_schedule(
    request: str,
    config: Config,
    runtime_paths: RuntimePaths,
    available_agents: typing.Sequence[MatrixID],
    current_time: datetime | None = None,
) -> ScheduledWorkflow | _WorkflowParseError:
    """Parse natural language into structured workflow using AI."""
    if current_time is None:
        current_time = datetime.now(UTC)

    assert available_agents, "No agents available for scheduling"
    agent_list = ", ".join(f"@{a.username}" for a in available_agents)

    prompt = f"""Parse this scheduling request into a structured workflow.

Current time (UTC): {current_time.isoformat()}Z
Request: "{request}"

Your task is to:
1. Determine if this is a one-time task or recurring (cron)
2. Extract the schedule/timing
3. Create a message that mentions the appropriate agents
4. Set is_conditional=true only when the request is event-based or conditional

Available agents: {agent_list}

IMPORTANT: Event-based and conditional requests:
When the request depends on an external event or condition rather than a fixed time:
1. Convert to an appropriate recurring (cron) schedule for polling
2. Include BOTH the condition check AND the action in the message
3. Choose polling frequency based on urgency and type
4. Set is_conditional to true

Important rules:
- Set is_conditional=false for normal time-based schedules
- For conditional/event-based requests, ALWAYS include the check condition in the message
- Mention relevant agents with @ only when needed
- Convert time expressions to UTC for the schedule, but DO NOT include them in the message
- Remove time phrases like "in 15 seconds" from the message itself
- If schedule_type is "once", you MUST provide execute_at
- If schedule_type is "cron", you MUST provide cron_schedule

Examples of event/condition phrasing to include in the message (do not include times in these examples):
- @email_assistant Check for emails containing 'urgent'. If found, @phone_agent notify the user.
- @crypto_agent Check Bitcoin price. If below $40,000, @notification_agent alert the user.
- @monitoring_agent Check server CPU usage. If above 80%, @ops_agent scale up the servers.
- @reddit_agent Check for new mentions of our product. If found, @analyst analyze the sentiment and key points.
"""

    model = get_model_instance(config, runtime_paths, "default")

    agent = Agent(
        name="WorkflowParser",
        role="Parse scheduling requests into structured workflows",
        model=model,
        output_schema=ScheduledWorkflow,
    )

    try:
        response = await agent.arun(prompt, session_id=f"workflow_parse_{uuid.uuid4()}")
        result = response.content

        if isinstance(result, ScheduledWorkflow):
            if result.schedule_type == "once" and not result.execute_at:
                # Match previous behavior: default to 30 minutes from now
                result.execute_at = current_time + timedelta(minutes=30)
            elif result.schedule_type == "cron" and not result.cron_schedule:
                result.cron_schedule = CronSchedule(minute="0", hour="9", day="*", month="*", weekday="*")

            conditional_validation_error = _validate_conditional_workflow(result)
            if conditional_validation_error is not None:
                return conditional_validation_error

            logger.info("Successfully parsed workflow schedule", request=request, schedule_type=result.schedule_type)
            return result

        logger.error("Unexpected response type from AI", response_type=type(result).__name__)
        return _WorkflowParseError(
            error="Failed to parse the schedule request",
            suggestion="Try being more specific about the timing and what you want to happen",
        )

    except Exception as e:
        logger.exception("Error parsing workflow schedule", error=str(e), request=request)
        return _WorkflowParseError(
            error=f"Error parsing schedule: {e!s}",
            suggestion="Try a simpler format like 'Daily at 9am, check my email'",
        )


async def _build_workflow_message_content(
    workflow: ScheduledWorkflow,
    target: MessageTarget,
    config: Config,
    runtime_paths: RuntimePaths,
    message_text: str,
    conversation_cache: ConversationCacheProtocol,
) -> dict[str, typing.Any]:
    """Build Matrix message content for a scheduled workflow."""
    if workflow.new_thread:
        return format_message_with_mentions(
            config,
            runtime_paths,
            message_text,
            sender_domain=config.get_domain(runtime_paths),
            thread_event_id=None,
        )
    automated_message = (
        f"⏰ [Automated Task]\n{message_text}\n\n_Note: Automated task - follow-up expected when complete._"
    )
    assert workflow.room_id is not None  # Caller checks this
    latest_thread_event_id = None
    if target.resolved_thread_id is not None:
        latest_thread_event_id = await conversation_cache.get_latest_thread_event_id_if_needed(
            workflow.room_id,
            target.resolved_thread_id,
        )
    return format_message_with_mentions(
        config,
        runtime_paths,
        automated_message,
        sender_domain=config.get_domain(runtime_paths),
        thread_event_id=target.resolved_thread_id,
        latest_thread_event_id=latest_thread_event_id,
    )


async def _build_scheduled_failure_content(
    workflow: ScheduledWorkflow,
    target: MessageTarget,
    error_message: str,
    conversation_cache: ConversationCacheProtocol,
) -> dict[str, typing.Any]:
    """Build a failure message that follows the scheduled workflow target."""
    latest_thread_event_id = None
    if target.resolved_thread_id is not None:
        assert workflow.room_id is not None
        latest_thread_event_id = await conversation_cache.get_latest_thread_event_id_if_needed(
            workflow.room_id,
            target.resolved_thread_id,
        )
    return build_message_content(
        body=error_message,
        thread_event_id=target.resolved_thread_id,
        latest_thread_event_id=latest_thread_event_id,
    )


async def _notify_scheduled_workflow_failure(
    client: nio.AsyncClient,
    workflow: ScheduledWorkflow,
    target: MessageTarget,
    error: Exception,
    conversation_cache: ConversationCacheProtocol,
) -> None:
    """Send the visible failure notice for one scheduled workflow when possible."""
    if not workflow.room_id:
        return
    error_message = f"❌ Scheduled task failed: {workflow.description}\nError: {error!s}"
    error_content = await _build_scheduled_failure_content(
        workflow,
        target,
        error_message,
        conversation_cache,
    )
    try:
        delivered = await send_message_result(client, workflow.room_id, error_content)
        if delivered is not None:
            conversation_cache.notify_outbound_message(
                workflow.room_id,
                delivered.event_id,
                delivered.content_sent,
            )
    except Exception:
        logger.exception("Failed to send scheduled workflow failure message")


async def _execute_scheduled_workflow(
    client: nio.AsyncClient,
    workflow: ScheduledWorkflow,
    config: Config,
    runtime_paths: RuntimePaths,
    conversation_cache: ConversationCacheProtocol,
    task_id: str = "scheduled-task",
    matrix_admin: HookMatrixAdmin | None = None,
) -> bool:
    """Execute a scheduled workflow by posting its message to the thread."""
    if not workflow.room_id:
        logger.error("Cannot execute workflow without room_id")
        return False

    target = MessageTarget.for_scheduled_task(
        workflow,
    )

    with bound_log_context(**target.log_context):
        try:
            message_text = workflow.message
            if _ACTIVE_HOOK_REGISTRY.has_hooks(EVENT_SCHEDULE_FIRED):
                context = ScheduleFiredContext(
                    event_name=EVENT_SCHEDULE_FIRED,
                    plugin_name="",
                    settings={},
                    config=config,
                    runtime_paths=runtime_paths,
                    logger=logger.bind(event_name=EVENT_SCHEDULE_FIRED),
                    correlation_id=f"{EVENT_SCHEDULE_FIRED}:{task_id}",
                    message_sender=build_hook_message_sender(
                        client,
                        config,
                        runtime_paths,
                        conversation_cache=conversation_cache,
                    ),
                    matrix_admin=matrix_admin,
                    room_state_querier=build_hook_room_state_querier(client),
                    room_state_putter=build_hook_room_state_putter(client),
                    task_id=task_id,
                    workflow=workflow,
                    room_id=workflow.room_id,
                    thread_id=target.resolved_thread_id,
                    created_by=workflow.created_by,
                    message_text=message_text,
                )
                await emit(_ACTIVE_HOOK_REGISTRY, EVENT_SCHEDULE_FIRED, context)
                if context.suppress:
                    logger.info("Scheduled workflow suppressed by hook", task_id=task_id, room_id=workflow.room_id)
                    return False
                message_text = context.message_text

            content = await _build_workflow_message_content(
                workflow,
                target,
                config,
                runtime_paths,
                message_text,
                conversation_cache,
            )
            if workflow.created_by:
                content[ORIGINAL_SENDER_KEY] = workflow.created_by
            content["com.mindroom.source_kind"] = "scheduled"
            delivered = await send_message_result(client, workflow.room_id, content)
            if delivered is None:
                _raise_scheduled_workflow_send_error()
            conversation_cache.notify_outbound_message(
                workflow.room_id,
                delivered.event_id,
                delivered.content_sent,
            )
            logger.info(
                "Executed scheduled workflow",
                description=workflow.description,
                thread_id=target.resolved_thread_id,
                new_thread=workflow.new_thread,
                event_id=delivered.event_id,
            )
        except Exception as e:
            logger.exception("Failed to execute scheduled workflow")
            await _notify_scheduled_workflow_failure(
                client,
                workflow,
                target,
                e,
                conversation_cache,
            )
            return False
        else:
            return True


async def _run_cron_task(  # noqa: C901, PLR0911, PLR0912, PLR0915
    client: nio.AsyncClient,
    task_id: str,
    workflow: ScheduledWorkflow,
    running_tasks: dict[str, asyncio.Task],
    config: Config,
    runtime_paths: RuntimePaths,
    conversation_cache: ConversationCacheProtocol,
    matrix_admin: HookMatrixAdmin | None = None,
) -> None:
    """Run a recurring task based on cron schedule."""
    if not workflow.room_id:
        logger.error("No room_id provided for recurring task", task_id=task_id)
        return

    current_target = MessageTarget.for_scheduled_task(workflow)
    trusted_sender_ids = _scheduled_task_restore_sender_ids(config, runtime_paths)
    try:
        while True:
            latest_task = await _get_pending_task_record_with_retry(
                client=client,
                room_id=workflow.room_id,
                task_id=task_id,
                trusted_sender_ids=trusted_sender_ids,
            )
            if not latest_task:
                with bound_log_context(**current_target.log_context):
                    logger.info("Recurring task is no longer pending, stopping", task_id=task_id)
                return

            latest_workflow = latest_task.workflow
            workflow = latest_workflow
            current_target = MessageTarget.for_scheduled_task(workflow)
            with bound_log_context(**current_target.log_context):
                cron_schedule = latest_workflow.cron_schedule
                if not cron_schedule:
                    logger.error("No cron schedule provided for recurring task", task_id=task_id)
                    return

                cron_string = cron_schedule.to_cron_string()
                next_run = croniter(cron_string, datetime.now(UTC)).get_next(datetime)
                workflow_changed = False

                while True:
                    delay = (next_run - datetime.now(UTC)).total_seconds()
                    if delay <= 0:
                        break
                    await asyncio.sleep(min(delay, _TASK_STATE_POLL_INTERVAL_SECONDS))

                    refreshed_task = await _get_pending_task_record_with_retry(
                        client=client,
                        room_id=workflow.room_id,
                        task_id=task_id,
                        trusted_sender_ids=trusted_sender_ids,
                    )
                    if not refreshed_task:
                        logger.info("Recurring task cancelled while waiting, stopping", task_id=task_id)
                        return

                    refreshed_workflow = refreshed_task.workflow
                    if not refreshed_workflow.cron_schedule:
                        logger.error("No cron schedule provided for recurring task", task_id=task_id)
                        return

                    if _workflows_differ(workflow, refreshed_workflow):
                        workflow = refreshed_workflow
                        current_target = MessageTarget.for_scheduled_task(workflow)
                        workflow_changed = True
                        break

                if workflow_changed:
                    continue

                latest_before_execute = await _get_pending_task_record_with_retry(
                    client=client,
                    room_id=workflow.room_id,
                    task_id=task_id,
                    trusted_sender_ids=trusted_sender_ids,
                )
                if not latest_before_execute:
                    logger.info("Recurring task cancelled before execution, stopping", task_id=task_id)
                    return

                latest_workflow = latest_before_execute.workflow
                if not latest_workflow.cron_schedule:
                    logger.error("No cron schedule provided for recurring task", task_id=task_id)
                    return
                if _workflows_differ(workflow, latest_workflow):
                    workflow = latest_workflow
                    current_target = MessageTarget.for_scheduled_task(workflow)
                    continue

                await _execute_scheduled_workflow(
                    client,
                    workflow,
                    config,
                    runtime_paths,
                    conversation_cache,
                    task_id,
                    matrix_admin,
                )
                if task_id not in running_tasks:
                    logger.info("scheduled_task_missing_from_running_tasks", task_id=task_id)
                    return
    except asyncio.CancelledError:
        with bound_log_context(**current_target.log_context):
            logger.info("cron_task_cancelled", task_id=task_id)
        raise
    except Exception as e:
        with bound_log_context(**current_target.log_context):
            logger.exception("cron_task_failed", task_id=task_id)
            if workflow.room_id:
                error_message = f"❌ Recurring task failed: {workflow.description}\nTask ID: {task_id}\nError: {e!s}"
                error_content = await _build_scheduled_failure_content(
                    workflow,
                    current_target,
                    error_message,
                    conversation_cache,
                )
                delivered = await send_message_result(client, workflow.room_id, error_content)
                if delivered is not None:
                    conversation_cache.notify_outbound_message(
                        workflow.room_id,
                        delivered.event_id,
                        delivered.content_sent,
                    )
    finally:
        _cleanup_task_if_current(task_id, running_tasks)


async def _run_once_task(  # noqa: C901, PLR0912, PLR0915
    client: nio.AsyncClient,
    task_id: str,
    workflow: ScheduledWorkflow,
    config: Config,
    runtime_paths: RuntimePaths,
    _event_cache: ConversationEventCache,
    conversation_cache: ConversationCacheProtocol,
    matrix_admin: HookMatrixAdmin | None = None,
) -> None:
    """Run a one-time scheduled task."""
    if not workflow.room_id:
        logger.error("No room_id provided for one-time task", task_id=task_id)
        return

    current_target = MessageTarget.for_scheduled_task(workflow)
    latest_pending_task: ScheduledTaskRecord | None = None
    trusted_sender_ids = _scheduled_task_restore_sender_ids(config, runtime_paths)
    try:
        while True:
            latest_task = await _get_pending_task_record_with_retry(
                client=client,
                room_id=workflow.room_id,
                task_id=task_id,
                trusted_sender_ids=trusted_sender_ids,
            )
            if not latest_task:
                with bound_log_context(**current_target.log_context):
                    logger.info("One-time task is no longer pending, stopping", task_id=task_id)
                return

            latest_workflow = latest_task.workflow
            workflow = latest_workflow
            current_target = MessageTarget.for_scheduled_task(workflow)
            with bound_log_context(**current_target.log_context):
                execute_at = latest_workflow.execute_at
                if not execute_at:
                    logger.error("No execution time provided for one-time task", task_id=task_id)
                    return

                delay = (execute_at - datetime.now(UTC)).total_seconds()
                if delay <= 0:
                    break
                await asyncio.sleep(min(delay, _TASK_STATE_POLL_INTERVAL_SECONDS))

        latest_before_execute = await _get_pending_task_record_with_retry(
            client=client,
            room_id=workflow.room_id,
            task_id=task_id,
            trusted_sender_ids=trusted_sender_ids,
        )
        if not latest_before_execute:
            with bound_log_context(**current_target.log_context):
                logger.info("One-time task was cancelled before execution, stopping", task_id=task_id)
            return

        latest_workflow = latest_before_execute.workflow
        latest_pending_task = latest_before_execute
        workflow = latest_workflow
        current_target = MessageTarget.for_scheduled_task(workflow)
        with bound_log_context(**current_target.log_context):
            if not latest_workflow.execute_at:
                logger.error("No execution time provided for one-time task", task_id=task_id)
                return

            execution_succeeded = await _execute_scheduled_workflow(
                client,
                latest_workflow,
                config,
                runtime_paths,
                conversation_cache,
                task_id,
                matrix_admin,
            )
            final_status = "completed" if execution_succeeded else "failed"

            try:
                await _save_one_time_task_status(
                    client=client,
                    task=latest_pending_task,
                    status=final_status,
                )
            except Exception:
                logger.exception(
                    "Failed to persist one-time task final state",
                    task_id=task_id,
                    status=final_status,
                )
    except asyncio.CancelledError:
        if latest_pending_task is not None and latest_pending_task.workflow is not workflow:
            workflow = latest_pending_task.workflow
            current_target = MessageTarget.for_scheduled_task(workflow)
        with bound_log_context(**current_target.log_context):
            logger.info("one_time_task_cancelled", task_id=task_id)
        raise
    except Exception as e:
        if latest_pending_task is not None and latest_pending_task.workflow is not workflow:
            workflow = latest_pending_task.workflow
            current_target = MessageTarget.for_scheduled_task(workflow)
        with bound_log_context(**current_target.log_context):
            logger.exception("one_time_task_failed", task_id=task_id)
            if workflow.room_id:
                error_message = f"❌ One-time task failed: {workflow.description}\nTask ID: {task_id}\nError: {e!s}"
                error_content = await _build_scheduled_failure_content(
                    workflow,
                    current_target,
                    error_message,
                    conversation_cache,
                )
                delivered = await send_message_result(client, workflow.room_id, error_content)
                if delivered is not None:
                    conversation_cache.notify_outbound_message(
                        workflow.room_id,
                        delivered.event_id,
                        delivered.content_sent,
                    )
            if latest_pending_task is not None:
                try:
                    await _save_one_time_task_status(
                        client=client,
                        task=latest_pending_task,
                        status="failed",
                    )
                except Exception:
                    logger.exception("Failed to mark one-time task as failed", task_id=task_id)
    finally:
        _cleanup_task_if_current(task_id, _running_tasks)


async def _validate_agent_mentions(
    message: str,
    allowed_agents: list[MatrixID],
    config: Config,
    runtime_paths: RuntimePaths,
) -> _AgentValidationResult:
    """Validate that all mentioned agents are accessible.

    Args:
        message: The message that may contain @agent mentions
        allowed_agents: Agents the sender may target in this room
        config: Application configuration
        runtime_paths: Explicit runtime context for mention resolution

    Returns:
        _AgentValidationResult with validation status and agent lists

    """
    mentioned_agents = _extract_mentioned_agents_from_text(message, config, runtime_paths)
    if not mentioned_agents:
        return _AgentValidationResult(all_valid=True, valid_agents=[], invalid_agents=[])

    valid_agents: list[MatrixID] = []
    invalid_agents: list[MatrixID] = []

    for mid in mentioned_agents:
        if mid in allowed_agents:
            valid_agents.append(mid)
        else:
            invalid_agents.append(mid)

    return _AgentValidationResult(
        all_valid=len(invalid_agents) == 0,
        valid_agents=valid_agents,
        invalid_agents=invalid_agents,
    )


def _format_scheduled_time(dt: datetime, timezone_str: str) -> str:
    """Format a datetime with timezone and relative time delta.

    Args:
        dt: Datetime in UTC
        timezone_str: Timezone string (e.g., 'America/New_York')

    Returns:
        Formatted string like "2024-01-15 3:30 PM EST (in 2 hours)"

    """
    # Convert UTC to target timezone
    tz = ZoneInfo(timezone_str)
    local_dt = dt.astimezone(tz)

    # Get human-readable relative time using humanize
    now = datetime.now(UTC)
    relative_str = humanize.naturaltime(dt, when=now)

    # Format the datetime string with 24-hour time
    time_str = local_dt.strftime("%Y-%m-%d %H:%M %Z")
    return f"{time_str} ({relative_str})"


def _extract_mentioned_agents_from_text(
    full_text: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> list[MatrixID]:
    """Extract valid agent mentions from scheduling text."""
    _, mentioned_user_ids, _ = parse_mentions_in_text(
        full_text,
        config.get_domain(runtime_paths),
        config,
        runtime_paths,
    )
    mentioned_agents: list[MatrixID] = []

    for user_id in mentioned_user_ids:
        matrix_id = MatrixID.parse(user_id)
        if matrix_id.agent_name(config, runtime_paths) and matrix_id not in mentioned_agents:
            mentioned_agents.append(matrix_id)

    return mentioned_agents


async def schedule_task(  # noqa: C901, PLR0911, PLR0912, PLR0915
    runtime: SchedulingRuntime,
    room_id: str,
    thread_id: str | None,
    scheduled_by: str,
    full_text: str,
    new_thread: bool = False,
    mentioned_agents: list[MatrixID] | None = None,
    task_id: str | None = None,
    existing_task: ScheduledTaskRecord | None = None,
    writer_client: nio.AsyncClient | None = None,
    re_resolve_writer: typing.Callable[[], typing.Awaitable[nio.AsyncClient]] | None = None,
) -> tuple[str | None, str]:
    """Schedule a workflow from natural language request.

    Returns:
        Tuple of (task_id, response_message)

    """
    client = runtime.client
    config = runtime.config
    runtime_paths = runtime.runtime_paths
    room = runtime.room
    conversation_cache = runtime.conversation_cache

    if mentioned_agents is None:
        mentioned_agents = _extract_mentioned_agents_from_text(full_text, config, runtime_paths)

    sender_visible_room_agents = get_available_agents_for_sender(room, scheduled_by, config, runtime_paths)

    available_agents: list[MatrixID] = []
    if new_thread:
        available_agents = list(sender_visible_room_agents)
    else:
        if thread_id:
            thread_history = list(await conversation_cache.get_thread_history(room_id, thread_id))
            thread_agents = get_agents_in_thread(thread_history, config, runtime_paths)
            available_agents = [agent for agent in thread_agents if agent in sender_visible_room_agents]

        if mentioned_agents:
            for mid in mentioned_agents:
                if mid not in available_agents and mid in sender_visible_room_agents:
                    available_agents.append(mid)

        if not available_agents:
            available_agents = list(sender_visible_room_agents)

    if not available_agents:
        return (None, "❌ No agents in this room are allowed to reply to you.")

    # Parse the workflow request with available agents
    workflow_result = await _parse_workflow_schedule(full_text, config, runtime_paths, available_agents)

    if isinstance(workflow_result, _WorkflowParseError):
        error_msg = f"❌ {workflow_result.error}"
        if workflow_result.suggestion:
            error_msg += f"\n\n💡 {workflow_result.suggestion}"
        return (None, error_msg)

    # Handle workflow task
    # Validate workflow before proceeding
    if workflow_result.schedule_type == "once" and not workflow_result.execute_at:
        return (None, "❌ Failed to schedule: One-time task missing execution time")
    if workflow_result.schedule_type == "cron" and not workflow_result.cron_schedule:
        return (None, "❌ Failed to schedule: Recurring task missing cron schedule")

    # Validate that all mentioned agents are accessible
    validation_result = await _validate_agent_mentions(
        workflow_result.message,
        sender_visible_room_agents,
        config,
        runtime_paths,
    )

    if not validation_result.all_valid:
        scope = "room" if new_thread or not thread_id else "thread"
        error_msg = "❌ Failed to schedule: The following agents are not available in this "
        error_msg += scope
        error_msg += f": {', '.join(agent.full_id for agent in validation_result.invalid_agents)}"

        # Provide helpful suggestions
        suggestions: list[str] = []
        for agent in validation_result.invalid_agents:
            agent_name = agent.agent_name(config, runtime_paths)
            if agent_name:
                # Agent exists but not available in this room/thread
                suggestions.append(f"{agent.full_id} is not available in this {scope}")
            else:
                suggestions.append(f"{agent.full_id} does not exist")

        if suggestions:
            error_msg += "\n\n💡 " + "\n💡 ".join(suggestions)

        return (None, error_msg)

    # Add metadata to workflow
    workflow_result.created_by = scheduled_by
    workflow_result.thread_id = None if new_thread else thread_id
    workflow_result.room_id = room_id
    workflow_result.new_thread = new_thread

    # Create task ID for new tasks (or reuse existing ID when editing)
    task_id = task_id or (existing_task.task_id if existing_task else str(uuid.uuid4())[:8])

    logger.info(
        "Storing workflow task in Matrix state",
        task_id=task_id,
        room_id=room_id,
        thread_id=workflow_result.thread_id,
        new_thread=new_thread,
        schedule_type=workflow_result.schedule_type,
    )

    try:
        resolved_writer_client = writer_client
        if resolved_writer_client is None:
            resolved_writer_client, re_resolve_writer = await _resolve_scheduled_task_writer_with_retry(
                room_id=room_id,
                current_client=client,
                router_client=runtime.router_client,
            )
        if resolved_writer_client is not client:
            logger.info(
                "Using delegated scheduled-task writer",
                task_id=task_id,
                room_id=room_id,
                requested_by_client=client.user_id,
                writer_client=resolved_writer_client.user_id,
            )
        if existing_task:
            await save_edited_scheduled_task(
                client=resolved_writer_client,
                room_id=room_id,
                task_id=task_id,
                workflow=workflow_result,
                existing_task=existing_task,
                re_resolve_writer=re_resolve_writer,
            )
        else:
            await _save_pending_scheduled_task(
                writer_client=resolved_writer_client,
                room_id=room_id,
                task_id=task_id,
                workflow=workflow_result,
                runtime=runtime,
                created_at=datetime.now(UTC).isoformat(),
                re_resolve_writer=re_resolve_writer,
            )
    except ScheduledTaskOperationError as error:
        logger.warning(
            "scheduled_task_schedule_failed",
            room_id=room_id,
            task_id=task_id,
            reason=error.reason,
            error=error.diagnostic_message,
        )
        return (None, _format_scheduled_task_failure("schedule", error.public_message))

    # Build success message
    if workflow_result.schedule_type == "once" and workflow_result.execute_at:
        # Format time with timezone and relative delta
        formatted_time = _format_scheduled_time(workflow_result.execute_at, config.timezone)
        success_msg = f"✅ Scheduled for {formatted_time}\n"
    elif workflow_result.cron_schedule:
        # Show both natural language and cron syntax
        natural_desc = workflow_result.cron_schedule.to_natural_language()
        cron_str = workflow_result.cron_schedule.to_cron_string()
        success_msg = f"✅ Scheduled recurring task: **{natural_desc}**\n"
        success_msg += f"   _(Cron: `{cron_str}`)_\n"
    else:
        success_msg = "✅ Task scheduled\n"

    success_msg += f"\n**Task:** {workflow_result.description}\n"
    success_msg += f"**Will post:** {workflow_result.message}\n"
    if new_thread:
        success_msg += "**Delivery:** New room-level thread root\n"
    success_msg += f"\n**Task ID:** `{task_id}`"

    return (task_id, success_msg)


async def edit_scheduled_task(
    runtime: SchedulingRuntime,
    room_id: str,
    task_id: str,
    full_text: str,
    scheduled_by: str,
    thread_id: str | None = None,
) -> str:
    """Edit an existing scheduled task by replacing its workflow details."""
    mutation_context: ScheduledTaskMutationContext | None = None
    try:
        mutation_context = await resolve_existing_scheduled_task_mutation(
            reader_client=runtime.client,
            room_id=room_id,
            task_id=task_id,
            config=runtime.config,
            runtime_paths=runtime.runtime_paths,
            router_client=runtime.router_client,
            requester_id=scheduled_by,
            requester_thread_id=thread_id,
        )
        existing_task = mutation_context.task
        if existing_task.status != "pending":
            _raise_scheduled_task_operation_error(
                reason="invalid_state",
                public_message=f"Task `{task_id}` cannot be edited because it is `{existing_task.status}`.",
            )
    except ScheduledTaskOperationError as error:
        return f"❌ {error.public_message}"

    try:
        target_new_thread = existing_task.workflow.new_thread
        target_thread_id = None if target_new_thread else existing_task.workflow.thread_id or thread_id

        edited_task_id, response_text = await schedule_task(
            runtime=runtime,
            room_id=room_id,
            thread_id=target_thread_id,
            scheduled_by=scheduled_by,
            full_text=full_text,
            new_thread=target_new_thread,
            task_id=task_id,
            existing_task=existing_task,
            writer_client=mutation_context.writer_client if mutation_context is not None else None,
            re_resolve_writer=mutation_context.re_resolve_writer if mutation_context is not None else None,
        )
    finally:
        if mutation_context is not None:
            await mutation_context.close()

    if edited_task_id is None:
        return f"❌ Failed to edit task `{task_id}`.\n\n{response_text}"

    return f"✅ Updated task `{task_id}`.\n\n{response_text}"


async def list_scheduled_tasks(  # noqa: C901, PLR0912
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str | None = None,
    config: Config | None = None,
) -> str:
    """List scheduled tasks in human-readable format."""
    try:
        state_response = await _read_scheduled_tasks_room_state(client, room_id)
    except ScheduledTaskOperationError as error:
        read_error = error
    else:
        task_records = _parse_task_records_from_state(room_id, state_response, include_non_pending=False)

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

        if not tasks and tasks_in_other_threads and not new_thread_tasks:
            return f"No scheduled tasks in this thread.\n\n📌 {len(tasks_in_other_threads)} task(s) scheduled in other threads. Use !list_schedules in those threads to see details."

        # Sort by execution time (one-time tasks) or put recurring tasks at the end
        def _sort_key(r: ScheduledTaskRecord) -> tuple[bool, datetime]:
            t = r.workflow.execute_at if r.workflow.schedule_type == "once" else None
            return (t is None, t or datetime.max.replace(tzinfo=UTC))

        tasks.sort(key=_sort_key)
        new_thread_tasks.sort(key=_sort_key)

        def _append_task_lines(lines: list[str], records: list[ScheduledTaskRecord]) -> None:
            for record in records:
                workflow = record.workflow
                if workflow.schedule_type == "once" and workflow.execute_at:
                    timezone = config.timezone if config else "UTC"
                    time_str = _format_scheduled_time(workflow.execute_at, timezone)
                else:
                    time_str = workflow.cron_schedule.to_natural_language() if workflow.cron_schedule else "recurring"

                msg_preview = workflow.message[:_MESSAGE_PREVIEW_LENGTH] + (
                    "..." if len(workflow.message) > _MESSAGE_PREVIEW_LENGTH else ""
                )
                lines.append(f'• `{record.task_id}` - {time_str}\n  {workflow.description}\n  Message: "{msg_preview}"')

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
                f"📌 {len(tasks_in_other_threads)} task(s) scheduled in other threads. Use !list_schedules in those threads to see details.",
            )

        return "\n".join(lines)

    logger.error(
        "scheduled_task_state_read_failed",
        room_id=room_id,
        thread_id=thread_id,
        error=read_error.diagnostic_message,
    )
    return read_error.public_message


async def cancel_scheduled_task(
    client: nio.AsyncClient,
    room_id: str,
    task_id: str,
    cancel_in_memory: bool = True,
    router_client: nio.AsyncClient | None = None,
    config: Config | None = None,
    runtime_paths: RuntimePaths | None = None,
    writer_client: nio.AsyncClient | None = None,
    re_resolve_writer: typing.Callable[[], typing.Awaitable[nio.AsyncClient]] | None = None,
    requester_id: str | None = None,
    requester_thread_id: str | None = None,
) -> str:
    """Cancel a scheduled task."""
    await _cancel_one_scheduled_task(
        client=client,
        room_id=room_id,
        task_id=task_id,
        cancel_in_memory=cancel_in_memory,
        router_client=router_client,
        config=config,
        runtime_paths=runtime_paths,
        writer_client=writer_client,
        re_resolve_writer=re_resolve_writer,
        requester_id=requester_id,
        requester_thread_id=requester_thread_id,
    )

    return f"✅ Cancelled task `{task_id}`"


def _status_only_scheduled_task_record(
    *,
    task_id: str,
    room_id: str,
    existing_content: dict[str, object] | None,
    requester_id: str | None,
) -> ScheduledTaskRecord | None:
    """Build one minimal task record for legacy status-only state when allowed."""
    if requester_id is not None or existing_content is None:
        return None
    status_value = existing_content.get("status")
    if not isinstance(status_value, str) or not status_value:
        return None
    return ScheduledTaskRecord(
        task_id=task_id,
        room_id=room_id,
        status=status_value,
        created_at=None,
        workflow=ScheduledWorkflow(
            schedule_type="once",
            execute_at=None,
            message="",
            description="",
            room_id=room_id,
        ),
    )


async def _load_cancellable_task_without_mutation_context(
    *,
    client: nio.AsyncClient,
    room_id: str,
    task_id: str,
    requester_id: str | None,
    requester_thread_id: str | None,
) -> tuple[ScheduledTaskRecord, dict[str, object] | None]:
    """Load one cancellable task directly from room state when no config context is available."""
    response: nio.RoomGetStateEventResponse | None = None
    with suppress(ScheduledTaskOperationError):
        response = await _read_scheduled_task_state_event(client, room_id, task_id)

    existing_content = response.content if response is not None and isinstance(response.content, dict) else None
    existing_task = (
        _parse_scheduled_task_record(room_id, task_id, existing_content) if existing_content is not None else None
    )
    if existing_task is None:
        existing_task = _status_only_scheduled_task_record(
            task_id=task_id,
            room_id=room_id,
            existing_content=existing_content,
            requester_id=requester_id,
        )

    if existing_task is None or existing_content is None:
        fetched_task, _task_sender_id, fetched_content = await get_scheduled_task_with_sender_and_content(
            client=client,
            room_id=room_id,
            task_id=task_id,
        )
        if fetched_content is not None:
            existing_content = fetched_content
        if fetched_task is not None:
            existing_task = fetched_task
        elif existing_task is None:
            existing_task = _status_only_scheduled_task_record(
                task_id=task_id,
                room_id=room_id,
                existing_content=existing_content,
                requester_id=requester_id,
            )

    if existing_task is None:
        _raise_scheduled_task_not_found(task_id)

    await _ensure_requester_can_mutate_scheduled_task(
        reader_client=client,
        room_id=room_id,
        task=existing_task,
        requester_id=requester_id,
        requester_thread_id=requester_thread_id,
    )
    return existing_task, existing_content


async def _cancel_one_scheduled_task(
    *,
    client: nio.AsyncClient,
    room_id: str,
    task_id: str,
    cancel_in_memory: bool,
    router_client: nio.AsyncClient | None = None,
    config: Config | None = None,
    runtime_paths: RuntimePaths | None = None,
    writer_client: nio.AsyncClient | None = None,
    re_resolve_writer: typing.Callable[[], typing.Awaitable[nio.AsyncClient]] | None = None,
    requester_id: str | None = None,
    requester_thread_id: str | None = None,
    skip_non_pending: bool = False,
) -> bool:
    """Cancel one task from fresh state and return whether a cancellation was persisted."""
    mutation_context: ScheduledTaskMutationContext | None = None
    existing_task: ScheduledTaskRecord | None = None
    existing_content: dict[str, object] | None = None

    try:
        if config is not None and runtime_paths is not None:
            mutation_context = await resolve_existing_scheduled_task_mutation(
                reader_client=client,
                room_id=room_id,
                task_id=task_id,
                config=config,
                runtime_paths=runtime_paths,
                router_client=router_client,
                requester_id=requester_id,
                requester_thread_id=requester_thread_id,
            )
            existing_task = mutation_context.task
            existing_content = mutation_context.task_content
            if writer_client is None:
                writer_client = mutation_context.writer_client
            if re_resolve_writer is None:
                re_resolve_writer = mutation_context.re_resolve_writer
        else:
            existing_task, existing_content = await _load_cancellable_task_without_mutation_context(
                client=client,
                room_id=room_id,
                task_id=task_id,
                requester_id=requester_id,
                requester_thread_id=requester_thread_id,
            )

        if existing_task.status != "pending":
            if skip_non_pending:
                return False
            _raise_scheduled_task_operation_error(
                reason="invalid_state",
                public_message=f"Task `{task_id}` cannot be cancelled because it is `{existing_task.status}`.",
            )

        if writer_client is None:
            writer_client, re_resolve_writer = await _resolve_scheduled_task_writer_with_retry(
                room_id=room_id,
                current_client=client,
                router_client=router_client,
            )

        await _persist_cancelled_scheduled_task_state(
            client=writer_client,
            room_id=room_id,
            task_id=task_id,
            existing_content=existing_content,
            re_resolve_writer=re_resolve_writer,
        )
    finally:
        if mutation_context is not None:
            await mutation_context.close()

    if cancel_in_memory:
        _cancel_running_task(task_id)

    return True


async def cancel_all_scheduled_tasks(
    client: nio.AsyncClient,
    room_id: str,
    router_client: nio.AsyncClient | None = None,
    config: Config | None = None,
    runtime_paths: RuntimePaths | None = None,
    requester_id: str | None = None,
    requester_thread_id: str | None = None,
) -> str:
    """Cancel all scheduled tasks in a room."""
    response = await _read_scheduled_tasks_room_state(client, room_id)

    cancelled_count = 0
    pending_task_ids = [
        event["state_key"]
        for event in response.events
        if event["type"] == SCHEDULED_TASK_EVENT_TYPE
        and isinstance(event.get("content"), dict)
        and event["content"].get("status") == "pending"
    ]

    if not pending_task_ids:
        return "No scheduled tasks to cancel."

    for task_id in pending_task_ids:
        try:
            cancelled = await _cancel_one_scheduled_task(
                client=client,
                room_id=room_id,
                task_id=task_id,
                cancel_in_memory=True,
                router_client=router_client,
                config=config,
                runtime_paths=runtime_paths,
                requester_id=requester_id,
                requester_thread_id=requester_thread_id,
                skip_non_pending=True,
            )
            if not cancelled:
                continue
            cancelled_count += 1
            logger.info("scheduled_task_cancelled", task_id=task_id)
        except ScheduledTaskOperationError as error:
            logger.warning("scheduled_task_cancel_failed", task_id=task_id, error=error.diagnostic_message)
            if cancelled_count == 0:
                raise _scheduled_task_operation_error(
                    reason=error.reason,
                    public_message=f"Failed to cancel scheduled tasks: {error.public_message}",
                    diagnostic_message=error.diagnostic_message,
                ) from error
            raise _scheduled_task_operation_error(
                reason=error.reason,
                public_message=f"Cancelled {cancelled_count} scheduled task(s) before failing: {error.public_message}",
                diagnostic_message=error.diagnostic_message,
            ) from error

    if cancelled_count == 0:
        return "No scheduled tasks to cancel."

    return f"✅ Cancelled {cancelled_count} scheduled task(s)"


async def restore_scheduled_tasks(  # noqa: C901, PLR0912, PLR0915
    client: nio.AsyncClient,
    room_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    event_cache: ConversationEventCache,
    conversation_cache: ConversationCacheProtocol,
    additional_writer_clients: typing.Sequence[nio.AsyncClient] = (),
) -> int:
    """Restore scheduled tasks from Matrix state after bot restart.

    Returns:
        Number of tasks restored

    """
    try:
        response = await _read_scheduled_tasks_room_state(client, room_id)
    except ScheduledTaskOperationError as error:
        logger.warning("scheduled_task_restore_state_read_failed", room_id=room_id, error=error.diagnostic_message)
        return 0

    restored_count = 0
    known_sender_ids = _scheduled_task_restore_sender_ids(config, runtime_paths)
    for event in response.events:
        if event["type"] != SCHEDULED_TASK_EVENT_TYPE:
            continue

        content = event["content"]
        if content.get("status") != "pending":
            continue

        try:
            task_id: str = event["state_key"]
            sender = event.get("sender")
            if not isinstance(sender, str) or sender not in known_sender_ids:
                logger.warning(
                    "Skipping scheduled task restore with untrusted sender",
                    room_id=room_id,
                    task_id=task_id,
                    sender=sender,
                )
                continue

            # Parse the workflow
            workflow_data = json.loads(content["workflow"])
            workflow = ScheduledWorkflow(**workflow_data)
            writer_client = await _resolve_restore_writer_client(
                room_id=room_id,
                current_client=client,
                additional_writer_clients=additional_writer_clients,
            )
            if writer_client is None:
                continue

            # Validate workflow has required fields
            if workflow.schedule_type == "once":
                if not workflow.execute_at:
                    logger.warning("skipping_one_time_task_without_execution_time", task_id=task_id)
                    continue
                # Handle past one-time tasks: execute if within grace period, fail if too old
                if workflow.execute_at <= datetime.now(UTC):
                    missed_by = (datetime.now(UTC) - workflow.execute_at).total_seconds()
                    if missed_by > _MISSED_TASK_MAX_AGE_SECONDS:
                        logger.warning(
                            "Skipping ancient missed task",
                            task_id=task_id,
                            missed_by_seconds=missed_by,
                        )
                        try:
                            await _persist_scheduled_task_state(
                                client=writer_client,
                                room_id=room_id,
                                task_id=task_id,
                                workflow=workflow,
                                status="failed",
                                created_at=content.get("created_at"),
                            )
                        except Exception:
                            logger.exception("Failed to mark ancient task as failed", task_id=task_id)
                        continue
                    if _queue_deferred_overdue_task(task_id, workflow):
                        logger.warning(
                            "Queued missed one-time task until sync is ready",
                            task_id=task_id,
                            missed_by_seconds=missed_by,
                        )
                        restored_count += 1
                    continue
            elif workflow.schedule_type == "cron":
                if not workflow.cron_schedule:
                    logger.warning("skipping_recurring_task_without_cron_schedule", task_id=task_id)
                    continue
            else:
                logger.warning("unknown_schedule_type", task_id=task_id, schedule_type=workflow.schedule_type)
                continue

            # Start the appropriate task
            if _start_scheduled_task(
                writer_client,
                task_id,
                workflow,
                config,
                runtime_paths,
                event_cache,
                conversation_cache,
                matrix_admin=build_hook_matrix_admin(writer_client, runtime_paths),
            ):
                restored_count += 1

        except (KeyError, ValueError, json.JSONDecodeError):
            logger.exception("Failed to restore task")
            continue

    if restored_count > 0:
        logger.info("Restored scheduled tasks in room", room_id=room_id, restored_count=restored_count)

    return restored_count
