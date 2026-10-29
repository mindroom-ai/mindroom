"""Tell a human their message waits: the running response's queued notice and the approval-wait reaction.

Text, media, and voice ingress share these, so they live apart from every ingress owner.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from mindroom.background_tasks import create_background_task
from mindroom.thread_utils import mentions_another_participant

if TYPE_CHECKING:
    import nio

    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.constants import RuntimePaths
    from mindroom.delivery_gateway import DeliveryGateway
    from mindroom.hooks import MessageEnvelope
    from mindroom.matrix.identity import MatrixID
    from mindroom.message_target import MessageTarget
    from mindroom.response_lifecycle import QueuedHumanNoticeReservation
    from mindroom.response_runner import ResponseRunner


class _ApprovalWaitSupport(Protocol):
    """Collaborators the approval-wait reaction needs."""

    runtime: BotRuntimeView
    runtime_paths: RuntimePaths
    matrix_id: MatrixID
    response_runner: ResponseRunner
    delivery_gateway: DeliveryGateway


def queued_notice_reservation_if_busy(
    response_runner: ResponseRunner,
    *,
    target: MessageTarget,
    envelope: MessageEnvelope,
) -> QueuedHumanNoticeReservation | None:
    """Reserve the mid-turn queued notice when this conversation has a running response."""
    if not envelope.origin.may_answer_interactive_prompt:
        return None
    if not response_runner.has_active_response_for_target(target):
        return None
    return response_runner.reserve_waiting_human_message(
        target=target,
        response_envelope=envelope,
    )


def mark_waiting_for_approval(
    support: _ApprovalWaitSupport,
    room: nio.MatrixRoom,
    source: dict[str, Any],
    target: MessageTarget,
    envelope: MessageEnvelope,
) -> None:
    """React ⏳ to a human message that waits because a pending approval holds its conversation."""
    if not envelope.origin.may_answer_interactive_prompt or not support.response_runner.is_held_for_approval(target):
        return
    if mentions_another_participant(
        source,
        support.matrix_id,
        support.runtime.config,
        support.runtime_paths,
        room=room,
    ):
        # A message for another agent or person does not wait for this one.
        return
    create_background_task(
        support.delivery_gateway.send_judgment_reaction(
            turn_id=envelope.source_event_id,
            room_id=target.room_id,
            event_id=envelope.source_event_id,
            key="⏳",
            kind="approval_wait",
        ),
        name=f"approval_wait_reaction_{envelope.source_event_id}",
        owner=support.runtime,
    )
