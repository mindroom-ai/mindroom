"""Persisted response-lifecycle facts for terminal repair."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from mindroom.hooks import MessageEnvelope
from mindroom.interactive import InteractiveMetadata
from mindroom.message_target import MessageTarget
from mindroom.turn_origin import SenderKind, TurnIntent, TurnOrigin, TurnTrust


@dataclass(frozen=True, slots=True)
class TerminalDeliveryLifecycleFacts:
    """JSON-safe response facts needed by success-only lifecycle effects."""

    response_kind: str
    correlation_id: str
    response_envelope: MessageEnvelope
    interactive_metadata: InteractiveMetadata | None
    thread_summary_message_count_hint: int | None
    thread_summary_entity_name: str

    def to_record(self) -> dict[str, Any]:
        """Return persisted lifecycle facts without runtime collaborators."""
        envelope = self.response_envelope
        origin = envelope.origin
        return {
            "response_kind": self.response_kind,
            "correlation_id": self.correlation_id,
            "response_envelope": {
                "source_event_id": envelope.source_event_id,
                "target": dict(envelope.target.to_metadata()),
                "body": envelope.body,
                "attachment_ids": list(envelope.attachment_ids),
                "mentioned_agents": list(envelope.mentioned_agents),
                "agent_name": envelope.agent_name,
                "origin": {
                    "transport_sender_id": origin.transport_sender_id,
                    "requester_id": origin.requester_id,
                    "sender_entity_name": origin.sender_entity_name,
                    "requester_entity_name": origin.requester_entity_name,
                    "sender_kind": origin.sender_kind.value,
                    "requester_kind": origin.requester_kind.value,
                    "intent": origin.intent.value,
                    "source_kind": origin.source_kind,
                    "trust": origin.trust.value,
                },
                "hook_source": envelope.hook_source,
                "message_received_depth": envelope.message_received_depth,
                "dispatch_policy_source_kind": envelope.dispatch_policy_source_kind,
            },
            "interactive_metadata": _interactive_to_record(self.interactive_metadata),
            "thread_summary_message_count_hint": self.thread_summary_message_count_hint,
            "thread_summary_entity_name": self.thread_summary_entity_name,
        }

    @classmethod
    def from_record(cls, raw_record: object) -> TerminalDeliveryLifecycleFacts | None:
        """Parse persisted lifecycle facts, rejecting incomplete records."""
        record = _mapping(raw_record)
        if record is None:
            return None
        response_kind = _required_string(record.get("response_kind"))
        correlation_id = _required_string(record.get("correlation_id"))
        response_envelope = _envelope_from_record(record.get("response_envelope"))
        thread_summary_entity_name = _required_string(record.get("thread_summary_entity_name"))
        message_count_hint = record.get("thread_summary_message_count_hint")
        if (
            response_kind is None
            or correlation_id is None
            or response_envelope is None
            or thread_summary_entity_name is None
            or not _is_optional_nonnegative_int(message_count_hint)
        ):
            return None
        interactive_metadata = _interactive_from_record(record.get("interactive_metadata"))
        if record.get("interactive_metadata") is not None and interactive_metadata is None:
            return None
        return cls(
            response_kind=response_kind,
            correlation_id=correlation_id,
            response_envelope=response_envelope,
            interactive_metadata=interactive_metadata,
            thread_summary_message_count_hint=cast("int | None", message_count_hint),
            thread_summary_entity_name=thread_summary_entity_name,
        )


def _envelope_from_record(raw_envelope: object) -> MessageEnvelope | None:
    envelope = _mapping(raw_envelope)
    if envelope is None:
        return None
    source_event_id = _required_string(envelope.get("source_event_id"))
    target = MessageTarget.from_metadata(envelope.get("target"))
    body = envelope.get("body")
    agent_name = _required_string(envelope.get("agent_name"))
    origin = _origin_from_record(envelope.get("origin"))
    attachment_ids = _string_tuple(envelope.get("attachment_ids"))
    mentioned_agents = _string_tuple(envelope.get("mentioned_agents"))
    message_received_depth = envelope.get("message_received_depth")
    if (
        source_event_id is None
        or target is None
        or not isinstance(body, str)
        or agent_name is None
        or origin is None
        or attachment_ids is None
        or mentioned_agents is None
        or not isinstance(message_received_depth, int)
        or isinstance(message_received_depth, bool)
        or message_received_depth < 0
    ):
        return None
    hook_source = _optional_string(envelope.get("hook_source"))
    dispatch_policy_source_kind = _optional_string(envelope.get("dispatch_policy_source_kind"))
    if (envelope.get("hook_source") is not None and hook_source is None) or (
        envelope.get("dispatch_policy_source_kind") is not None and dispatch_policy_source_kind is None
    ):
        return None
    return MessageEnvelope(
        source_event_id=source_event_id,
        target=target,
        body=body,
        attachment_ids=attachment_ids,
        mentioned_agents=mentioned_agents,
        agent_name=agent_name,
        origin=origin,
        hook_source=hook_source,
        message_received_depth=message_received_depth,
        dispatch_policy_source_kind=dispatch_policy_source_kind,
    )


def _origin_from_record(raw_origin: object) -> TurnOrigin | None:
    origin = _mapping(raw_origin)
    if origin is None:
        return None
    required_strings = {
        key: _required_string(origin.get(key))
        for key in (
            "transport_sender_id",
            "requester_id",
            "sender_kind",
            "requester_kind",
            "intent",
            "source_kind",
            "trust",
        )
    }
    if any(value is None for value in required_strings.values()):
        return None
    sender_kind = required_strings["sender_kind"]
    requester_kind = required_strings["requester_kind"]
    intent = required_strings["intent"]
    trust = required_strings["trust"]
    if (
        sender_kind not in {value.value for value in SenderKind}
        or requester_kind not in {value.value for value in SenderKind}
        or intent not in {value.value for value in TurnIntent}
        or trust not in {value.value for value in TurnTrust}
    ):
        return None
    sender_entity_name = _optional_string(origin.get("sender_entity_name"))
    requester_entity_name = _optional_string(origin.get("requester_entity_name"))
    if (origin.get("sender_entity_name") is not None and sender_entity_name is None) or (
        origin.get("requester_entity_name") is not None and requester_entity_name is None
    ):
        return None
    return TurnOrigin(
        transport_sender_id=cast("str", required_strings["transport_sender_id"]),
        requester_id=cast("str", required_strings["requester_id"]),
        sender_entity_name=sender_entity_name,
        requester_entity_name=requester_entity_name,
        sender_kind=SenderKind(sender_kind),
        requester_kind=SenderKind(requester_kind),
        intent=TurnIntent(intent),
        source_kind=cast("str", required_strings["source_kind"]),
        trust=TurnTrust(trust),
    )


def _interactive_to_record(metadata: InteractiveMetadata | None) -> dict[str, Any] | None:
    if metadata is None:
        return None
    return {
        "question_text": metadata.question_text,
        "option_map": dict(metadata.option_map),
        "option_labels": dict(metadata.option_labels),
        "options_list": [dict(item) for item in metadata.options_list],
    }


def _interactive_from_record(raw_metadata: object) -> InteractiveMetadata | None:
    if raw_metadata is None:
        return None
    metadata = _mapping(raw_metadata)
    if metadata is None or not isinstance(metadata.get("question_text"), str):
        return None
    option_map = _string_mapping(metadata.get("option_map"))
    option_labels = _string_mapping(metadata.get("option_labels"))
    raw_options = metadata.get("options_list")
    if option_map is None or option_labels is None or not isinstance(raw_options, list):
        return None
    options = [_string_mapping(item) for item in raw_options]
    if any(item is None for item in options):
        return None
    return InteractiveMetadata.from_parts(
        option_map,
        cast("list[dict[str, str]]", options),
        question_text=cast("str", metadata["question_text"]),
        option_labels=option_labels,
    )


def _mapping(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        return None
    return cast("Mapping[str, object]", value)


def _string_mapping(value: object) -> dict[str, str] | None:
    mapping = _mapping(value)
    if mapping is None or not all(isinstance(item, str) for item in mapping.values()):
        return None
    return cast("dict[str, str]", dict(mapping))


def _string_tuple(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        return None
    return cast("tuple[str, ...]", tuple(dict.fromkeys(value)))


def _required_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _is_optional_nonnegative_int(value: object) -> bool:
    return value is None or (isinstance(value, int) and not isinstance(value, bool) and value >= 0)


__all__ = ["TerminalDeliveryLifecycleFacts"]
