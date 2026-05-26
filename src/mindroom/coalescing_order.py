"""Receive-order reservation bookkeeping for coalescing ingress."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from .coalescing_batch import CoalescingKey


@dataclass
class IngressOrderReservation:
    """Receive-order placeholder for ingress that must resolve its canonical key first."""

    room_id: str
    requester_user_id: str
    received_order: int
    receipt_time: float
    released: bool = False
    settled: asyncio.Event = field(default_factory=asyncio.Event, repr=False, compare=False)
    _release: Callable[[IngressOrderReservation], None] | None = field(default=None, repr=False, compare=False)

    def release(self) -> None:
        """Release this reservation if it will not be admitted."""
        if self._release is None:
            if self.released:
                return
            self.released = True
            self.settled.set()
            return
        self._release(self)


class CoalescingOrderBook:
    """Own receive-order reservation state for one coalescing gate."""

    def __init__(self) -> None:
        self._reservations: list[IngressOrderReservation] = []
        self._next_received_order = 0

    @property
    def next_received_order(self) -> int:
        """Return the highest receive order assigned so far."""
        return self._next_received_order

    def next_order(self) -> int:
        """Return the next monotonic receive order."""
        self._next_received_order += 1
        return self._next_received_order

    def reserve(
        self,
        *,
        room_id: str,
        requester_user_id: str,
        receipt_time: float | None,
        release: Callable[[IngressOrderReservation], None],
        released: bool = False,
    ) -> IngressOrderReservation:
        """Create a reservation and track it until release unless already closed."""
        reservation = IngressOrderReservation(
            room_id=room_id,
            requester_user_id=requester_user_id,
            received_order=self.next_order(),
            receipt_time=receipt_time if receipt_time is not None else time.monotonic(),
            released=released,
            _release=release,
        )
        if released:
            reservation.settled.set()
        else:
            self._reservations.append(reservation)
        return reservation

    @staticmethod
    def reservation_matches_key(reservation: IngressOrderReservation, key: CoalescingKey) -> bool:
        """Return whether a reservation belongs to the same room requester as a key."""
        return reservation.room_id == key.room_id and reservation.requester_user_id == key.requester_user_id

    def release(self, reservation: IngressOrderReservation) -> bool:
        """Release a tracked reservation, returning whether it changed state."""
        if reservation.released:
            return False
        reservation.released = True
        for index, current_reservation in enumerate(self._reservations):
            if current_reservation is reservation:
                del self._reservations[index]
                break
        reservation.settled.set()
        return True

    def unsettled(self) -> list[IngressOrderReservation]:
        """Return all currently unresolved reservations."""
        return list(self._reservations)

    def all_settled(self) -> bool:
        """Return whether there are no unresolved reservations."""
        return not self._reservations

    def older_owner_reservations(
        self,
        key: CoalescingKey,
        *,
        before_order: int,
    ) -> list[IngressOrderReservation]:
        """Return unresolved same-owner reservations older than a receive order."""
        return [
            reservation
            for reservation in self._reservations
            if self.reservation_matches_key(reservation, key) and reservation.received_order < before_order
        ]

    def has_older_unresolved_owner_reservation(self, key: CoalescingKey, received_order: int) -> bool:
        """Return whether older unresolved same-owner reservation blocks this order."""
        return any(
            self.reservation_matches_key(reservation, key) and reservation.received_order < received_order
            for reservation in self._reservations
        )

    def unsettled_owner_reservations_in_window(
        self,
        key: CoalescingKey,
        *,
        after_order: int,
        before_order: int | None,
        before_or_at_receipt_time: float,
    ) -> list[IngressOrderReservation]:
        """Return unresolved same-owner reservations that belong to a claim window."""
        return [
            reservation
            for reservation in self._reservations
            if self.reservation_matches_key(reservation, key)
            and reservation.received_order > after_order
            and (before_order is None or reservation.received_order < before_order)
            and reservation.receipt_time <= before_or_at_receipt_time
        ]
