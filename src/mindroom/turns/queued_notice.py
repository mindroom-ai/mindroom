"""Tell a human their message waits for a pending approval: the approval-wait reaction.

Text, media, and voice ingress share it, so it lives apart from every ingress owner.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from mindroom.background_tasks import create_background_task

if TYPE_CHECKING:
    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.delivery_gateway import DeliveryGateway
    from mindroom.hooks import MessageEnvelope
    from mindroom.message_target import MessageTarget
    from mindroom.response_runner import ResponseRunner


class _ApprovalWaitSupport(Protocol):
    """Collaborators the approval-wait reaction needs."""

    @property
    def runtime(self) -> BotRuntimeView: ...

    @property
    def response_runner(self) -> ResponseRunner: ...

    @property
    def delivery_gateway(self) -> DeliveryGateway: ...


def mark_waiting_for_approval(
    support: _ApprovalWaitSupport,
    target: MessageTarget,
    envelope: MessageEnvelope,
) -> None:
    """React ⏳ to a human message that waits because a pending approval holds its conversation.

    Ingress calls it only for a message this agent may answer (`TurnPolicy.not_for_this_agent`).
    """
    holds = support.response_runner.lifecycle_coordinator.approval_holds(target.room_id, target.resolved_thread_id)
    if not envelope.origin.may_answer_interactive_prompt or not holds:
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
