"""Configurable EventSource test doubles shared across seam, Slack, and room-API tests."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from mindroom.event_sources import EventSource, EventSourceAccessDenial, EventSourceContext, EventSourceDescriptor
from mindroom.reactive_triggers import (
    ExternalEventSourceSpec,
    ReactiveTriggerRecord,
    TriggerEvent,
    matches_external_event,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.matrix.room_reconciliation import RoomStateSnapshot


def room_active_members(state: RoomStateSnapshot | None) -> set[str] | None:
    """Return joined/invited user IDs from a room state snapshot, or None when state is unavailable."""
    if state is None:
        return None
    return {
        state_key
        for (etype, state_key), content in state.events.items()
        if etype == "m.room.member" and content.get("membership") in {"join", "invite"}
    }


def owner_only_room_check(ctx: EventSourceContext, rule: ReactiveTriggerRecord) -> EventSourceAccessDenial | None:
    """Deny delivery when room state is missing or contains any joined/invited member other than the rule owner."""
    members = room_active_members(ctx.target_room_state)
    if members is None:
        return EventSourceAccessDenial(reason="Cannot verify private room state")
    if members - {rule.owner_user_id}:
        return EventSourceAccessDenial(reason="Room has another member")
    return None


@dataclass
class FakeEventSource(EventSource):
    """Configurable EventSource for testing registration, matching, privacy, pause reasons, and hook faults."""

    source_id: str
    description: str = ""
    allowed_kinds: tuple[str, ...] = ("message", "mention")
    match_fields: tuple[str, ...] = ()
    private_room_only: bool = False
    descriptor_private_room_only: bool | None = None
    delivery_decision: EventSourceAccessDenial | None = None
    delivery_check: Callable[[EventSourceContext, ReactiveTriggerRecord], EventSourceAccessDenial | None] | None = None
    require_audience_member: bool = False
    describe_error: Exception | None = None
    delivery_error: Exception | None = None
    match_calls: list[tuple[str, object, bool]] = field(default_factory=list, init=False)
    delivery_contexts: list[EventSourceContext] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        """Default description from source_id when omitted."""
        if not self.description:
            self.description = f"Fake {self.source_id}"

    async def describe_for_user(self, ctx: EventSourceContext) -> EventSourceDescriptor | None:
        """Return descriptor (optionally overriding private_room_only) or raise configured error."""
        if self.describe_error is not None:
            raise self.describe_error
        desc = await super().describe_for_user(ctx)
        if desc is not None and self.descriptor_private_room_only is not None:
            return replace(desc, private_room_only=self.descriptor_private_room_only)
        return desc

    def matches_rule(
        self,
        rule: ReactiveTriggerRecord,
        event: TriggerEvent,
        *,
        audience: object = None,
        has_thread_binding: bool = False,
    ) -> bool:
        """Record call args and evaluate audience, bound thread, and external match filters."""
        self.match_calls.append((rule.trigger_id, audience, has_thread_binding))
        if not isinstance(rule.source, ExternalEventSourceSpec):
            return False
        if self.require_audience_member:
            expected_user = rule.source.binding.get("ext_user_id")
            if not isinstance(audience, set) or expected_user not in audience:
                return False
        if rule.source.kind == "mention" and has_thread_binding:
            return True
        return matches_external_event(rule.source, event)

    async def check_delivery_access(
        self,
        ctx: EventSourceContext,
        *,
        rule: ReactiveTriggerRecord,
        event: TriggerEvent | None = None,
    ) -> EventSourceAccessDenial | None:
        """Record context and return configured delivery denial, check result, or raise configured error."""
        del event
        self.delivery_contexts.append(ctx)
        if self.delivery_error is not None:
            raise self.delivery_error
        if self.delivery_check is not None:
            return self.delivery_check(ctx, rule)
        return self.delivery_decision
