"""Ingress boundary validation: trust, effective requester, dedup, and command detection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

import nio

from mindroom.authorization import get_effective_sender_id_for_reply_permissions, is_authorized_sender
from mindroom.commands.parsing import command_parser
from mindroom.constants import ORIGINAL_SENDER_KEY, ROUTER_AGENT_NAME
from mindroom.dispatch_handoff import PreparedTextEvent
from mindroom.dispatch_source import (
    HOOK_DISPATCH_SOURCE_KIND,
    IMAGE_SOURCE_KIND,
    MEDIA_SOURCE_KIND,
    TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
    VOICE_SOURCE_KIND,
    is_visible_router_voice_echo_content,
    is_voice_event,
    source_kind_allows_trusted_original_sender,
    source_kind_bypasses_coalescing,
    source_kind_from_content,
)
from mindroom.entity_resolution import entity_identity_registry
from mindroom.handled_turns import HandledTurnState
from mindroom.matrix.media import is_audio_message_event
from mindroom.turn_origin import requester_id_from_trusted_original_sender

if TYPE_CHECKING:
    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.commands.parsing import Command
    from mindroom.constants import RuntimePaths
    from mindroom.dispatch_handoff import (
        DispatchEvent,
        DispatchIngressMetadata,
        DispatchPayloadMetadata,
        TextDispatchEvent,
    )
    from mindroom.matrix.identity import MatrixID
    from mindroom.matrix.media import MatrixMediaEvent
    from mindroom.turn_store import TurnStore


class _SenderReplyPolicy(Protocol):
    """Minimal reply-permission surface needed at the ingress boundary."""

    def can_reply_to_sender(self, sender_id: str) -> bool:
        """Return whether this agent may reply to one effective requester."""
        ...


@dataclass(frozen=True)
class IngressValidatorDeps:
    """Explicit collaborators for ingress boundary validation."""

    runtime: BotRuntimeView
    runtime_paths: RuntimePaths
    matrix_id: MatrixID
    turn_store: TurnStore
    turn_policy: _SenderReplyPolicy


@dataclass(frozen=True)
class IngressValidator:
    """Validate one inbound event at the ingress boundary before any batching."""

    deps: IngressValidatorDeps

    def requester_user_id(
        self,
        *,
        sender: str,
        source: object,
    ) -> str:
        """Return the effective requester for reply-permission checks."""
        source_dict = cast("dict[str, Any] | None", source if isinstance(source, dict) else None)
        content = source_dict.get("content") if source_dict is not None else None
        if isinstance(content, dict):
            original_sender = content.get(ORIGINAL_SENDER_KEY)
            if not isinstance(original_sender, str):
                return get_effective_sender_id_for_reply_permissions(
                    sender,
                    source_dict,
                    self.deps.runtime.config,
                    self.deps.runtime_paths,
                )
            source_kind = source_kind_from_content(content)
            trusted_requester = requester_id_from_trusted_original_sender(
                original_sender=original_sender,
                original_sender_entity_name=self.managed_entity_name_for_sender(original_sender),
                source_kind=source_kind,
                sender_trusts_original_sender=self.should_trust_original_sender_metadata(
                    sender=sender,
                    source_kind=source_kind,
                ),
            )
            if trusted_requester is not None:
                return trusted_requester
            return sender
        return get_effective_sender_id_for_reply_permissions(
            sender,
            source_dict,
            self.deps.runtime.config,
            self.deps.runtime_paths,
        )

    def sender_is_trusted_for_ingress_metadata(self, sender_id: str) -> bool:
        """Return whether one sender may supply trusted ingress metadata overrides."""
        return self.managed_entity_name_for_sender(sender_id) is not None

    def managed_entity_name_for_sender(self, sender_id: str, *, include_router: bool = True) -> str | None:
        """Return the configured entity alias for an exact current Matrix user ID."""
        registry = entity_identity_registry(self.deps.runtime.config, self.deps.runtime_paths)
        return registry.current_entity_name_for_user_id(sender_id, include_router=include_router)

    def should_trust_original_sender_metadata(
        self,
        *,
        sender: str,
        source_kind: str | None,
    ) -> bool:
        """Return whether original-sender metadata represents a trusted relay for this event."""
        sender_is_own_entity = sender == self.deps.matrix_id.full_id
        sender_agent_name = self.managed_entity_name_for_sender(sender)
        if sender_agent_name is None and not sender_is_own_entity:
            return False
        return source_kind_allows_trusted_original_sender(source_kind)

    @staticmethod
    def event_source_kind(event: DispatchEvent, content: dict[str, Any]) -> str | None:
        """Return canonical source-kind metadata for one dispatch event."""
        source_kind = event.source_kind_override if isinstance(event, PreparedTextEvent) else None
        return source_kind if source_kind is not None else source_kind_from_content(content)

    def trusted_human_original_sender_for_event(self, event: DispatchEvent) -> str | None:
        """Return trusted human original-sender metadata from one dispatch event."""
        if not isinstance(event, nio.RoomMessageText | PreparedTextEvent):
            return None
        if not self.sender_is_trusted_for_ingress_metadata(event.sender):
            return None
        content = event.source.get("content") if isinstance(event.source, dict) else None
        if not isinstance(content, dict):
            return None
        source_kind = self.event_source_kind(event, content)
        return self.trusted_human_original_sender(
            sender=event.sender,
            content=content,
            source_kind=source_kind,
        )

    def trusted_human_original_sender(
        self,
        *,
        sender: str,
        content: dict[str, Any],
        source_kind: str | None,
    ) -> str | None:
        """Return trusted original-sender metadata only when it names a human requester."""
        original_sender = content.get(ORIGINAL_SENDER_KEY)
        if not isinstance(original_sender, str) or not original_sender:
            return None
        if self.managed_entity_name_for_sender(original_sender) is not None:
            return None
        if not self.should_trust_original_sender_metadata(
            sender=sender,
            source_kind=source_kind,
        ):
            return None
        return original_sender

    def should_trust_internal_payload_metadata(self, event: DispatchEvent) -> bool:
        """Return whether internal payload keys on one event should be treated as authoritative."""
        return self.sender_is_trusted_for_ingress_metadata(event.sender)

    def is_trusted_internal_relay_event(self, event: DispatchEvent) -> bool:
        """Return whether one agent-authored relay should bypass user-turn coalescing."""
        if not isinstance(event, nio.RoomMessageText | PreparedTextEvent):
            return False
        content = event.source.get("content") if isinstance(event.source, dict) else None
        if not isinstance(content, dict):
            return False
        if self.event_source_kind(event, content) != TRUSTED_INTERNAL_RELAY_SOURCE_KIND:
            return False
        return self.trusted_human_original_sender_for_event(event) is not None

    def is_trusted_router_relay_event(self, event: DispatchEvent) -> bool:
        """Return whether one trusted internal relay originated from the router."""
        if not self.is_trusted_internal_relay_event(event):
            return False
        sender_agent_name = self.managed_entity_name_for_sender(event.sender)
        return sender_agent_name == ROUTER_AGENT_NAME

    def is_trusted_router_visible_voice_echo_content(self, sender: str, content: object) -> bool:
        """Return whether replay history content is a display-only router voice echo."""
        if self.managed_entity_name_for_sender(sender) != ROUTER_AGENT_NAME:
            return False
        if not is_visible_router_voice_echo_content(content) or not isinstance(content, dict):
            return False
        content_dict = cast("dict[str, Any]", content)
        return (
            self.trusted_human_original_sender(
                sender=sender,
                content=content_dict,
                source_kind=source_kind_from_content(content_dict),
            )
            is not None
        )

    def is_display_only_router_voice_echo(self, event: DispatchEvent) -> bool:
        """Return whether one ingress event is the router's display-only voice transcript echo."""
        content = event.source.get("content") if isinstance(event.source, dict) else None
        return is_visible_router_voice_echo_content(content) and self.is_trusted_router_relay_event(event)

    def should_use_trusted_router_relay_context(
        self,
        event: DispatchEvent,
        *,
        ingress_metadata: DispatchIngressMetadata | None,
        payload_metadata: DispatchPayloadMetadata | None,
    ) -> bool:
        """Return whether dispatch context should use trusted router relay semantics."""
        if ingress_metadata is None:
            return self.is_trusted_router_relay_event(event)
        if ingress_metadata.source_kind != TRUSTED_INTERNAL_RELAY_SOURCE_KIND:
            return False
        sender_agent_name = self.managed_entity_name_for_sender(event.sender)
        if sender_agent_name != ROUTER_AGENT_NAME:
            return False
        if payload_metadata is not None:
            original_sender = payload_metadata.original_sender
            return (
                original_sender is not None
                and original_sender != ""
                and self.managed_entity_name_for_sender(original_sender) is None
            )
        return self.is_trusted_internal_relay_event(event)

    def precheck_event(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent | MatrixMediaEvent,
        *,
        is_edit: bool = False,
    ) -> str | None:
        """Run shared early-exit checks for inbound text and media events."""
        content = event.source.get("content") if isinstance(event.source, dict) else None
        source_kind = source_kind_from_content(content) if isinstance(content, dict) else None
        requester_user_id = self.requester_user_id(
            sender=event.sender,
            source=event.source,
        )

        if requester_user_id == self.deps.matrix_id.full_id and source_kind != HOOK_DISPATCH_SOURCE_KIND:
            return None

        if not is_edit and self.deps.turn_store.is_handled(event.event_id):
            return None

        if not is_authorized_sender(
            requester_user_id,
            self.deps.runtime.config,
            room.room_id,
            self.deps.runtime_paths,
        ):
            self.deps.turn_store.record_turn(HandledTurnState.from_source_event_id(event.event_id))
            return None

        if not self.deps.turn_policy.can_reply_to_sender(requester_user_id):
            self.deps.turn_store.record_turn(HandledTurnState.from_source_event_id(event.event_id))
            return None

        return requester_user_id

    def command_control_input(self, event: TextDispatchEvent, *, source_kind: str) -> Command | None:
        """Return the parsed command when one text event is a control input, not conversation."""
        if source_kind_bypasses_coalescing(source_kind):
            return None
        if source_kind in {VOICE_SOURCE_KIND, IMAGE_SOURCE_KIND, MEDIA_SOURCE_KIND}:
            return None
        if is_audio_message_event(event) or is_voice_event(
            event,
            sender_is_trusted=self.sender_is_trusted_for_ingress_metadata,
        ):
            return None
        return command_parser.parse(event.body)
