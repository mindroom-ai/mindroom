"""Tell a human their message waits: the running response's queued notice and the approval-wait reaction.

Text, media, and voice ingress share these, so they live apart from every ingress owner.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from mindroom.background_tasks import create_background_task
from mindroom.mid_turn import MidTurnMode

if TYPE_CHECKING:
    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.delivery_gateway import DeliveryGateway
    from mindroom.hooks import MessageEnvelope
    from mindroom.message_target import MessageTarget
    from mindroom.response_lifecycle import QueuedHumanNoticeReservation
    from mindroom.response_runner import ResponseRunner


class _ApprovalWaitSupport(Protocol):
    """Collaborators the approval-wait reaction needs."""

    runtime: BotRuntimeView
    response_runner: ResponseRunner
    delivery_gateway: DeliveryGateway


def queued_notice_reservation_if_busy(
    response_runner: ResponseRunner,
    *,
    target: MessageTarget,
    envelope: MessageEnvelope,
    mentions_nobody: bool = False,
    transcript: str | None = None,
    mode: MidTurnMode = MidTurnMode.AUTO,
) -> QueuedHumanNoticeReservation | None:
    """Reserve the mid-turn queued notice when this conversation has a running response.

    ``mentions_nobody`` stays False while whom the message mentions is unknown, as for voice without a transcript.
    The judge reads a voice message's ``transcript`` as its text; ``mode`` is the one its content names.
    """
    if not envelope.origin.may_answer_interactive_prompt:
        return None
    if not response_runner.has_active_response_for_target(target):
        return None
    return response_runner.reserve_waiting_human_message(
        target=target,
        response_envelope=envelope,
        mentions_nobody=mentions_nobody,
        transcript=transcript,
        mode=mode,
    )


async def arriving_message_notice_reservation(
    response_runner: ResponseRunner,
    *,
    target: MessageTarget,
    envelope: MessageEnvelope,
    mentions_nobody: bool = False,
    transcript: str | None = None,
    mode: MidTurnMode = MidTurnMode.AUTO,
) -> QueuedHumanNoticeReservation | None:
    """Reserve the queued notice for a message as ingress takes it in, unless a reply to it is already under way.

    A restart replays the message a cut-short reply answers. Nobody sent anything new, so the turn that holds its
    conversation meanwhile, such as a job follow-up, is not told that a newer message waits.
    """
    if response_runner.has_active_response_for_target(target) and await response_runner.is_answering(
        (envelope.source_event_id,),
    ):
        return None
    return queued_notice_reservation_if_busy(
        response_runner,
        target=target,
        envelope=envelope,
        mentions_nobody=mentions_nobody,
        transcript=transcript,
        mode=mode,
    )


def mark_waiting_for_approval(
    support: _ApprovalWaitSupport,
    target: MessageTarget,
    envelope: MessageEnvelope,
) -> None:
    """React ⏳ to a human message that waits because a pending approval holds its conversation.

    Ingress calls it only for a message this agent may answer (`TurnPolicy.not_for_this_agent`).
    """
    if not envelope.origin.may_answer_interactive_prompt or not support.response_runner.is_held_for_approval(target):
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
