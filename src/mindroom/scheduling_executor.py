"""Fire one scheduled task: hook emission, message construction, Matrix delivery, failure notices."""

from __future__ import annotations

import typing
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.constants import ORIGINAL_SENDER_KEY, SOURCE_KIND_KEY
from mindroom.dispatch_source import SCHEDULED_SOURCE_KIND
from mindroom.hooks import (
    EVENT_SCHEDULE_FIRED,
    HookRegistry,
    ScheduleFiredContext,
    build_hook_message_sender,
    build_hook_room_state_putter,
    build_hook_room_state_querier,
    emit,
    send_and_track_message,
)
from mindroom.logging_config import bound_log_context, get_logger
from mindroom.matrix.mentions import format_message_with_mentions
from mindroom.matrix.message_builder import build_message_content
from mindroom.message_target import MessageTarget

if TYPE_CHECKING:
    import nio

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.hooks import HookMatrixAdmin
    from mindroom.matrix.conversation_cache import ConversationCacheProtocol
    from mindroom.scheduling import ScheduledWorkflow

logger = get_logger(__name__)

_ACTIVE_HOOK_REGISTRY: HookRegistry = HookRegistry.empty()


def set_scheduling_hook_registry(hook_registry: HookRegistry) -> None:
    """Update the immutable hook snapshot used by scheduled task runners."""
    global _ACTIVE_HOOK_REGISTRY
    _ACTIVE_HOOK_REGISTRY = hook_registry


@dataclass(frozen=True)
class ScheduledWorkflowOutcome:
    """Typed result of firing one scheduled workflow."""

    delivered: bool
    failure_reason: str | None = None


def _raise_scheduled_workflow_send_error() -> typing.NoReturn:
    """Raise when a scheduled workflow message cannot be sent."""
    msg = "Failed to send scheduled workflow message to Matrix"
    raise RuntimeError(msg)


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
            caller_label="scheduled_workflow_message",
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
    conversation_cache: ConversationCacheProtocol,
) -> dict[str, typing.Any]:
    """Build a failure message that follows the scheduled workflow target."""
    latest_thread_event_id = None
    if target.resolved_thread_id is not None:
        assert workflow.room_id is not None
        latest_thread_event_id = await conversation_cache.get_latest_thread_event_id_if_needed(
            workflow.room_id,
            target.resolved_thread_id,
            caller_label="scheduled_workflow_failure",
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
    config: Config,
    conversation_cache: ConversationCacheProtocol,
) -> None:
    """Send a visible failure notice that follows the scheduled workflow target."""
    assert workflow.room_id is not None  # Callers guard on room_id before notifying
    error_content = await _build_scheduled_failure_content(
        workflow,
        target,
        error_message,
        conversation_cache,
    )
    await send_and_track_message(client, workflow.room_id, error_content, config, conversation_cache)


async def _notify_scheduled_workflow_failure(
    client: nio.AsyncClient,
    workflow: ScheduledWorkflow,
    target: MessageTarget,
    config: Config,
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
        await send_and_track_message(client, workflow.room_id, error_content, config, conversation_cache)
    except Exception:
        logger.exception("Failed to send scheduled workflow failure message")


async def execute_scheduled_workflow(
    client: nio.AsyncClient,
    workflow: ScheduledWorkflow,
    config: Config,
    runtime_paths: RuntimePaths,
    conversation_cache: ConversationCacheProtocol,
    task_id: str = "scheduled-task",
    matrix_admin: HookMatrixAdmin | None = None,
) -> ScheduledWorkflowOutcome:
    """Execute a scheduled workflow by posting its message to the thread."""
    if not workflow.room_id:
        logger.error("Cannot execute workflow without room_id")
        return ScheduledWorkflowOutcome(delivered=False, failure_reason="missing room_id")

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
                    return ScheduledWorkflowOutcome(delivered=False, failure_reason="suppressed by hook")
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
            content[SOURCE_KIND_KEY] = SCHEDULED_SOURCE_KIND
            delivered = await send_and_track_message(client, workflow.room_id, content, config, conversation_cache)
            if delivered is None:
                _raise_scheduled_workflow_send_error()
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
                config,
                e,
                conversation_cache,
            )
            return ScheduledWorkflowOutcome(delivered=False, failure_reason=str(e))
        else:
            return ScheduledWorkflowOutcome(delivered=True)
