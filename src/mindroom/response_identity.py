"""Leaf identity for one response delivery lifecycle."""

from dataclasses import dataclass

from mindroom.hooks import MessageEnvelope


@dataclass(frozen=True)
class ResponseIdentity:
    """Identify which visible response a delivery or hook call belongs to."""

    response_kind: str
    response_envelope: MessageEnvelope
    correlation_id: str
    source_event_ids: tuple[str, ...] = ()


__all__ = ["ResponseIdentity"]
