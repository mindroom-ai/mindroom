"""Fire one scheduled task: hook emission, message construction, Matrix delivery, failure notices."""

from __future__ import annotations

import typing
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from mindroom.constants import (
    ORIGINAL_SENDER_KEY,
    PER_FIRE_THREAD_ROOT_KEY,
    SCHEDULED_HISTORY_LIMIT_KEY,
    SILENT_SCHEDULE_EVENT_TYPE,
    SOURCE_KIND_KEY,
)
from mindroom.dispatch_source import SCHEDULED_SOURCE_KIND, SILENT_SCHEDULE_SOURCE_KIND
from mindroom.hooks import (
    EVENT_SCHEDULE_FIRED,
    HookRegistry,
    HookRegistryState,
    ScheduleFiredContext,
    build_hook_message_sender,
    build_hook_room_state_putter,
    build_hook_room_state_querier,
    emit,
)
from mindroom.logging_config import bound_log_context, get_logger
from mindroom.matrix import client_delivery
from mindroom.matrix.mentions import format_message_with_mentions
from mindroom.matrix.message_builder import build_message_content
from mindroom.message_target import MessageTarget
from mindroom.recurring_schedule import (
    RecurringDeliveryHeldError,
    prepare_recurring_delivery,
    recurring_delivery_content,
)

if TYPE_CHECKING:
    import nio

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.hooks import HookMatrixAdmin
    from mindroom.matrix.conversation_reads import ConversationReader
    from mindroom.recurring_schedule import RecurringOccurrence
    from mindroom.scheduling import ScheduledWorkflow

logger = get_logger(__name__)

_SCHEDULING_HOOK_REGISTRY_STATE = HookRegistryState(HookRegistry.empty())


def set_scheduling_hook_registry(hook_registry: HookRegistry) -> None:
    """Update the immutable hook snapshot used by scheduled task runners."""
    _SCHEDULING_HOOK_REGISTRY_STATE.registry = hook_registry


@dataclass(frozen=True)
class ScheduledWorkflowOutcome:
    """Typed result of firing one scheduled workflow."""

    status: Literal["delivered", "suppressed", "failed", "retry", "held"]
    failure_reason: str | None = None


class _InvalidScheduledTriggerError(ValueError):
    """The authored trigger cannot be delivered for this occurrence."""


def _validate_scheduled_workflow_message(message_text: str) -> None:
    """Reject an empty trigger body before Matrix accepts it as delivered."""
    if not message_text.strip():
        msg = "Scheduled workflow message is empty after hooks"
        raise _InvalidScheduledTriggerError(msg)


async def _build_workflow_message_content(
    workflow: ScheduledWorkflow,
    target: MessageTarget,
    config: Config,
    runtime_paths: RuntimePaths,
    message_text: str,
    conversation_reader: ConversationReader,
) -> dict[str, typing.Any]:
    """Build Matrix message content for a scheduled workflow."""
    if workflow.new_thread:
        return format_message_with_mentions(
            config,
            runtime_paths,
            message_text,
            thread_event_id=None,
        )
    automated_message = (
        f"⏰ [Automated Task]\n{message_text}\n\n_Note: Automated task - follow-up expected when complete._"
    )
    assert workflow.room_id is not None  # Caller checks this
    latest_thread_event_id = None
    if target.resolved_thread_id is not None:
        latest_thread_event_id = await conversation_reader.latest_thread_event_id(
            room_id=workflow.room_id,
            thread_id=target.resolved_thread_id,
        )
    return format_message_with_mentions(
        config,
        runtime_paths,
        automated_message,
        thread_event_id=target.resolved_thread_id,
        latest_thread_event_id=latest_thread_event_id,
    )


async def _build_scheduled_failure_content(
    workflow: ScheduledWorkflow,
    target: MessageTarget,
    error_message: str,
    conversation_reader: ConversationReader,
) -> dict[str, typing.Any]:
    """Build a failure message that follows the scheduled workflow target."""
    latest_thread_event_id = None
    if target.resolved_thread_id is not None:
        assert workflow.room_id is not None
        latest_thread_event_id = await conversation_reader.latest_thread_event_id(
            room_id=workflow.room_id,
            thread_id=target.resolved_thread_id,
        )
    return build_message_content(
        body=error_message,
        thread_event_id=target.resolved_thread_id,
        latest_thread_event_id=latest_thread_event_id,
    )


async def send_scheduled_failure_notice(
    client: nio.AsyncClient,
    workflow: ScheduledWorkflow,
    target: MessageTarget,
    error_message: str,
    conversation_reader: ConversationReader,
) -> None:
    """Send a visible failure notice that follows the scheduled workflow target."""
    assert workflow.room_id is not None  # Callers guard on room_id before notifying
    error_content = await _build_scheduled_failure_content(
        workflow,
        target,
        error_message,
        conversation_reader,
    )
    await client_delivery.send_message_outcome(client, workflow.room_id, error_content)


async def _notify_scheduled_workflow_failure(
    client: nio.AsyncClient,
    workflow: ScheduledWorkflow,
    target: MessageTarget,
    error: str,
    conversation_reader: ConversationReader,
) -> None:
    """Send the visible failure notice for one scheduled workflow when possible."""
    if not workflow.room_id:
        return
    error_message = f"❌ Scheduled task failed: {workflow.description}\nError: {error!s}"
    error_content = await _build_scheduled_failure_content(
        workflow,
        target,
        error_message,
        conversation_reader,
    )
    try:
        await client_delivery.send_message_outcome(client, workflow.room_id, error_content)
    except Exception:
        logger.exception("Failed to send scheduled workflow failure message")


async def _prepare_scheduled_trigger(
    client: nio.AsyncClient,
    workflow: ScheduledWorkflow,
    config: Config,
    runtime_paths: RuntimePaths,
    conversation_reader: ConversationReader,
    task_id: str,
    matrix_admin: HookMatrixAdmin | None,
    target: MessageTarget,
    correlation_id: str,
) -> dict[str, typing.Any] | None:
    """Run hooks and build content before a recurring delivery is frozen."""
    assert workflow.room_id is not None
    message_text = workflow.message
    hook_registry = _SCHEDULING_HOOK_REGISTRY_STATE.registry
    if hook_registry.has_hooks(EVENT_SCHEDULE_FIRED):
        context = ScheduleFiredContext(
            event_name=EVENT_SCHEDULE_FIRED,
            plugin_name="",
            settings={},
            config=config,
            runtime_paths=runtime_paths,
            logger=logger.bind(event_name=EVENT_SCHEDULE_FIRED),
            correlation_id=correlation_id,
            message_sender=build_hook_message_sender(
                client,
                config,
                runtime_paths,
                conversation_reader=conversation_reader,
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
            _hook_registry_state=_SCHEDULING_HOOK_REGISTRY_STATE,
        )
        await emit(hook_registry, EVENT_SCHEDULE_FIRED, context)
        if context.suppress:
            logger.info("Scheduled workflow suppressed by hook", task_id=task_id, room_id=workflow.room_id)
            return None
        message_text = context.message_text

    _validate_scheduled_workflow_message(message_text)
    content = await _build_workflow_message_content(
        workflow,
        target,
        config,
        runtime_paths,
        message_text,
        conversation_reader,
    )
    if workflow.created_by:
        content[ORIGINAL_SENDER_KEY] = workflow.created_by
    content[SOURCE_KIND_KEY] = SILENT_SCHEDULE_SOURCE_KIND if workflow.silent else SCHEDULED_SOURCE_KIND
    if workflow.new_thread and not workflow.silent:
        content[PER_FIRE_THREAD_ROOT_KEY] = True
    if workflow.history_limit is not None:
        content[SCHEDULED_HISTORY_LIMIT_KEY] = workflow.history_limit
    return content


async def _deliver_scheduled_trigger(
    client: nio.AsyncClient,
    workflow: ScheduledWorkflow,
    content: dict[str, typing.Any],
    occurrence: RecurringOccurrence | None,
) -> ScheduledWorkflowOutcome:
    """Freeze recurring content before sending and classify the typed Matrix result."""
    assert workflow.room_id is not None
    if occurrence is not None and occurrence.checkpoint.prepared is None:
        prepared = await client_delivery.prepare_message_content(client, workflow.room_id, content)
        if isinstance(prepared, client_delivery.MatrixDeliveryFailure):
            status = (
                "failed" if prepared.kind is client_delivery.MatrixDeliveryFailureKind.PAYLOAD_TOO_LARGE else "retry"
            )
            return ScheduledWorkflowOutcome(status=status, failure_reason=prepared.detail)
        occurrence = await prepare_recurring_delivery(occurrence, prepared, client.device_id)
        assert occurrence.checkpoint.prepared is not None
        content = occurrence.checkpoint.prepared.content

    delivered = await client_delivery.send_message_outcome(
        client,
        workflow.room_id,
        content,
        message_type=SILENT_SCHEDULE_EVENT_TYPE if workflow.silent else "m.room.message",
        transaction_id=occurrence.transaction_id if occurrence is not None else None,
        content_is_prepared=occurrence is not None,
    )
    if isinstance(delivered, client_delivery.MatrixDeliveryFailure):
        # Once frozen, keep the trigger even if a later send rejects its content.
        return ScheduledWorkflowOutcome(
            status="retry" if occurrence is not None else "failed",
            failure_reason=delivered.detail,
        )
    logger.info(
        "Executed scheduled workflow",
        description=workflow.description,
        thread_id=MessageTarget.for_scheduled_task(workflow).resolved_thread_id,
        new_thread=workflow.new_thread,
        event_id=delivered.event_id,
    )
    return ScheduledWorkflowOutcome(status="delivered")


async def execute_scheduled_workflow(
    client: nio.AsyncClient,
    workflow: ScheduledWorkflow,
    config: Config,
    runtime_paths: RuntimePaths,
    conversation_reader: ConversationReader,
    task_id: str = "scheduled-task",
    matrix_admin: HookMatrixAdmin | None = None,
    *,
    occurrence: RecurringOccurrence | None = None,
) -> ScheduledWorkflowOutcome:
    """Execute a scheduled workflow by posting its message to the thread."""
    if not workflow.room_id:
        logger.error("Cannot execute workflow without room_id")
        return ScheduledWorkflowOutcome(status="failed", failure_reason="missing room_id")

    target = MessageTarget.for_scheduled_task(
        workflow,
    )

    with bound_log_context(**target.log_context):
        try:
            content = recurring_delivery_content(occurrence, client.device_id) if occurrence is not None else None
            if content is None:
                content = await _prepare_scheduled_trigger(
                    client,
                    workflow,
                    config,
                    runtime_paths,
                    conversation_reader,
                    task_id,
                    matrix_admin,
                    target,
                    f"{EVENT_SCHEDULE_FIRED}:{occurrence.transaction_id if occurrence is not None else task_id}",
                )
                if content is None:
                    return ScheduledWorkflowOutcome(status="suppressed", failure_reason="suppressed by hook")
            outcome = await _deliver_scheduled_trigger(client, workflow, content, occurrence)
        except _InvalidScheduledTriggerError as error:
            outcome = ScheduledWorkflowOutcome(status="failed", failure_reason=str(error))
        except RecurringDeliveryHeldError as error:
            outcome = ScheduledWorkflowOutcome(status="held", failure_reason=str(error))
        except Exception as error:
            logger.exception("Failed to execute scheduled workflow")
            outcome = ScheduledWorkflowOutcome(
                status="retry" if occurrence is not None else "failed",
                failure_reason=str(error),
            )
        if outcome.status in {"retry", "held"}:
            logger.warning(
                "Recurring delivery remains pending",
                task_id=task_id,
                status=outcome.status,
                reason=outcome.failure_reason,
            )
        if outcome.status == "failed":
            assert outcome.failure_reason is not None
            await _notify_scheduled_workflow_failure(
                client,
                workflow,
                target,
                outcome.failure_reason,
                conversation_reader,
            )
        return outcome
