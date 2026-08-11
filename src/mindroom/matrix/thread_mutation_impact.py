"""Which thread an outbound Matrix mutation lands in, decided before it is sent.

Ownership map:
- canonical resolution: `mindroom.matrix.thread_membership`
- outbound mutation impact: this module
- tool-facing root normalization: `mindroom.custom_tools.attachment_helpers`

A tool that sends or redacts a Matrix event (see `mindroom.custom_tools.matrix_api`)
has to know whether that event belongs to a thread before it goes out, because
the answer decides what it sends, and no later correction is possible. It asks
here, through `resolve_event_thread_impact_for_client` or
`resolve_redaction_thread_impact_for_client`, and refuses the operation when the
answer is UNKNOWN.

The mapping is total and UNKNOWN fails closed. THREADED names the thread,
ROOM_LEVEL says there is none, and UNKNOWN says membership could not be proven --
which a caller must treat as a refusal rather than as "no thread", because
sending threaded work at room level is not recoverable.

Redactions are thread-affecting only when the target identifies a plaintext or
encrypted room message. Reactions and non-message targets are ROOM_LEVEL,
because removing them cannot move a message between conversations.
"""

from __future__ import annotations

from enum import Enum, auto
from typing import TYPE_CHECKING

from mindroom.matrix.event_info import EventInfo, event_type_supports_thread_relations
from mindroom.matrix.thread_membership import (
    ThreadResolution,
    ThreadResolutionState,
    resolve_event_thread_membership,
    resolve_related_event_thread_membership,
)
from mindroom.matrix.thread_room_scan import (
    RoomScanRelations,
    room_scan_membership_access_for_client,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    import nio


def _redaction_can_affect_thread_membership(event_info: EventInfo) -> bool:
    """Return whether redacting one related event can change a thread's visible messages."""
    return event_type_supports_thread_relations(event_info.event_type) and not event_info.is_reaction


class MutationThreadImpactState(Enum):
    """Mutation outcomes for one event relation."""

    THREADED = auto()
    ROOM_LEVEL = auto()
    UNKNOWN = auto()


def _mutation_thread_impact_from_resolution(
    resolution: ThreadResolution,
) -> MutationThreadImpactState:
    """Map canonical membership results onto mutation behavior."""
    if resolution.state is ThreadResolutionState.THREADED:
        return MutationThreadImpactState.THREADED
    if resolution.state is ThreadResolutionState.ROOM_LEVEL:
        return MutationThreadImpactState.ROOM_LEVEL
    return MutationThreadImpactState.UNKNOWN


async def resolve_event_thread_impact_for_client(
    client: nio.AsyncClient,
    room_id: str,
    *,
    event_type: str,
    content: Mapping[str, object],
    relations: RoomScanRelations,
) -> MutationThreadImpactState:
    """Return the mutation impact for one outbound client-side event payload."""
    if event_type != "m.room.message":
        return MutationThreadImpactState.ROOM_LEVEL
    event_info = EventInfo.from_event({"type": event_type, "content": dict(content)})
    try:
        resolution = await resolve_event_thread_membership(
            room_id,
            event_info,
            access=room_scan_membership_access_for_client(
                client,
                relations=relations,
            ),
        )
    except Exception:
        # Two different failures, one correct answer. An ancestor that cannot
        # carry thread membership -- a sticker, a reaction -- proves nothing;
        # a lookup that simply failed proves nothing either. Both must report
        # UNKNOWN rather than "no thread", because the caller turns UNKNOWN
        # into an explicit error and would otherwise send threaded work at
        # room level. The strict lookup exists so this path sees the failure
        # at all instead of a silently degraded None.
        return MutationThreadImpactState.UNKNOWN
    return _mutation_thread_impact_from_resolution(resolution)


async def resolve_redaction_thread_impact_for_client(
    client: nio.AsyncClient,
    room_id: str,
    *,
    event_id: str,
    relations: RoomScanRelations,
) -> MutationThreadImpactState:
    """Return the mutation impact for one client-side redaction target."""
    target_event_info = await relations.event_info(room_id, event_id)
    if target_event_info is not None and not _redaction_can_affect_thread_membership(target_event_info):
        return MutationThreadImpactState.ROOM_LEVEL
    try:
        resolution = await resolve_related_event_thread_membership(
            room_id,
            event_id,
            access=room_scan_membership_access_for_client(
                client,
                relations=relations,
            ),
        )
    except Exception:
        return MutationThreadImpactState.UNKNOWN
    return _mutation_thread_impact_from_resolution(resolution)
