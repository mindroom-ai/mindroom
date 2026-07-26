"""Leaf identity for one response delivery lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mindroom.handled_turns import TurnRecord
    from mindroom.hooks import MessageEnvelope


@dataclass(frozen=True)
class ResponseIdentity:
    """Identify which visible response a delivery or hook call belongs to."""

    response_kind: str
    response_envelope: MessageEnvelope
    correlation_id: str
    source_event_ids: tuple[str, ...] = ()
    regeneration_turn_record: TurnRecord | None = None


__all__ = ["ResponseIdentity"]
