"""Leaf models shared by Matrix membership parsing and journal fencing."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReportedDeparture:
    """One durable observation that a room membership ended."""

    room_id: str
    observation_id: str | None = None
    rejoined_after: bool = False
