"""Transport-neutral facts describing incomplete room history."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class HistoryRecoveryState(StrEnum):
    """Whether a room's unknown missing interval still needs a proof walk."""

    REPAIRABLE = "repairable"
    TRUNCATED = "truncated"


@dataclass(frozen=True, slots=True)
class RoomHistoryRecovery:
    """One room's durable obligation to account for unknown missing history."""

    room_id: str
    state: HistoryRecoveryState
    revision: int = 0


class HistoryRecoveryOutcome(StrEnum):
    """What one attempted settlement did to a recovery obligation."""

    REPAIRED = "repaired"
    TRUNCATED = "truncated"
    SUPERSEDED = "superseded"
