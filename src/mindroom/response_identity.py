"""Leaf identity for one response delivery lifecycle."""

from collections.abc import Mapping
from dataclasses import dataclass

from mindroom.hooks import MessageEnvelope


@dataclass(frozen=True)
class FrozenThreadSummary:
    """Exact Matrix wire payload frozen before a stable-transaction send."""

    wire_content: Mapping[str, object]
    message_count: int


@dataclass(frozen=True)
class ResponseIdentity:
    """Identify which visible response a delivery or hook call belongs to."""

    response_kind: str
    response_envelope: MessageEnvelope
    correlation_id: str
    source_event_ids: tuple[str, ...] = ()
    thread_summary_message_count_hint: int | None = None


__all__ = ["FrozenThreadSummary", "ResponseIdentity"]
