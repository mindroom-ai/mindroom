"""Whether an outbound Matrix mutation's thread membership is known before it is sent.

Ownership map:
- canonical resolution: `mindroom.matrix.thread_membership`
- outbound mutation impact: this module
- tool-facing root normalization: `mindroom.custom_tools.attachment_helpers`

A tool that sends or redacts a Matrix event (see `mindroom.custom_tools.matrix_api`)
asks here, through `event_thread_membership_known` or
`redaction_thread_membership_known`, before the mutation goes out, and refuses
the operation when the answer is False. Root proofs read the conversation
projection, the same reader turns use.

The answer is True when membership is proven threaded or room level, or when
the mutation cannot affect thread membership at all. It is False when
membership could not be proven, which fails closed: a mutation whose thread
cannot be decided is refused rather than treated as room level. This module
logs why, because the refusal the caller returns cannot say.

Redactions are thread-affecting only when the target identifies a plaintext or
encrypted room message. Removing a reaction or a non-message event cannot move
a message between conversations, so those redactions are always known.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger
from mindroom.matrix.event_info import EventInfo, event_type_supports_thread_relations
from mindroom.matrix.projected_thread_membership import projected_thread_membership_access
from mindroom.matrix.thread_membership import (
    ThreadResolution,
    ThreadResolutionState,
    resolve_event_thread_membership,
    resolve_related_event_thread_membership,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.matrix.conversation_reads import ConversationReader
    from mindroom.matrix.relation_lookup import RelationLookup

logger = get_logger(__name__)


def _redaction_can_affect_thread_membership(event_info: EventInfo) -> bool:
    """Return whether redacting one related event can change a thread's visible messages."""
    return event_type_supports_thread_relations(event_info.event_type) and not event_info.is_reaction


async def event_thread_membership_known(
    reader: ConversationReader,
    relations: RelationLookup,
    room_id: str,
    *,
    event_type: str,
    content: Mapping[str, object],
) -> bool:
    """Return whether one outbound client-side event payload has known thread membership."""
    if event_type != "m.room.message":
        return True
    event_info = EventInfo.from_event({"type": event_type, "content": dict(content)})
    try:
        resolution = await resolve_event_thread_membership(
            room_id,
            event_info,
            access=projected_thread_membership_access(reader, relations),
        )
    except Exception as exc:
        # Two different failures, one correct answer. An ancestor that cannot
        # carry thread membership -- a sticker, a reaction -- proves nothing;
        # a lookup that simply failed proves nothing either. Both must report
        # unknown rather than "no thread", because the caller turns unknown
        # into an explicit error and would otherwise send threaded work at
        # room level. The strict lookup exists so this path sees the failure
        # at all instead of a silently degraded None.
        resolution = ThreadResolution(ThreadResolutionState.INDETERMINATE, error=exc)
    return _membership_known(resolution, room_id=room_id, mutation="send_event")


async def redaction_thread_membership_known(
    reader: ConversationReader,
    relations: RelationLookup,
    room_id: str,
    *,
    event_id: str,
) -> bool:
    """Return whether one client-side redaction target has known thread membership."""
    try:
        target_event_info = await relations.event_info(room_id, event_id)
        if target_event_info is not None and not _redaction_can_affect_thread_membership(target_event_info):
            return True
        resolution = await resolve_related_event_thread_membership(
            room_id,
            event_id,
            access=projected_thread_membership_access(reader, relations),
        )
    except Exception as exc:
        resolution = ThreadResolution(ThreadResolutionState.INDETERMINATE, error=exc)
    return _membership_known(resolution, room_id=room_id, mutation="redact", target_event_id=event_id)


def _membership_known(
    resolution: ThreadResolution,
    *,
    room_id: str,
    mutation: str,
    target_event_id: str | None = None,
) -> bool:
    """Return whether one resolution proves membership, logging why it does not, since callers only refuse."""
    if resolution.state is not ThreadResolutionState.INDETERMINATE:
        return True
    logger.warning(
        "Refusing a Matrix mutation whose thread membership is unknown",
        room_id=room_id,
        mutation=mutation,
        target_event_id=target_event_id,
        error=str(resolution.error),
    )
    return False
