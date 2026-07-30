"""Bounded thread claims for locally delivered Matrix response events."""

from __future__ import annotations

import time
import typing
from dataclasses import dataclass, field

_DEFAULT_TTL_SECONDS = 6 * 60 * 60
_DEFAULT_MAX_RESERVATIONS = 4096
_DEFAULT_MAX_ALIASES_PER_RESERVATION = 32


@dataclass(frozen=True, slots=True)
class OutboundThreadReservationKey:
    """Isolate one response identity by cache principal and Matrix room."""

    principal_id: str
    room_id: str
    event_id: str


@dataclass(slots=True)
class _OutboundThreadReservation:
    stable_key: OutboundThreadReservationKey
    thread_id: str
    expires_at: float
    aliases: dict[OutboundThreadReservationKey, None] = field(default_factory=dict)


@dataclass
class OutboundThreadReservations:
    """Retain certified outbound thread identities until terminal delivery."""

    ttl_seconds: float = _DEFAULT_TTL_SECONDS
    max_reservations: int = _DEFAULT_MAX_RESERVATIONS
    max_aliases_per_reservation: int = _DEFAULT_MAX_ALIASES_PER_RESERVATION
    clock: typing.Callable[[], float] = time.monotonic
    _reservations: dict[OutboundThreadReservationKey, _OutboundThreadReservation] = field(
        default_factory=dict,
        init=False,
    )
    _by_event: dict[OutboundThreadReservationKey, _OutboundThreadReservation] = field(
        default_factory=dict,
        init=False,
    )

    def _key(self, principal_id: str, room_id: str, event_id: str) -> OutboundThreadReservationKey:
        return OutboundThreadReservationKey(principal_id, room_id, event_id)

    def _drop(self, reservation: _OutboundThreadReservation) -> None:
        self._reservations.pop(reservation.stable_key, None)
        for alias in reservation.aliases:
            if self._by_event.get(alias) is reservation:
                self._by_event.pop(alias, None)

    def _prune_expired(self, now: float) -> None:
        for reservation in tuple(self._reservations.values()):
            if reservation.expires_at <= now:
                self._drop(reservation)

    def _refresh(self, reservation: _OutboundThreadReservation, now: float) -> None:
        reservation.expires_at = now + self.ttl_seconds

    def _bind_alias(
        self,
        reservation: _OutboundThreadReservation,
        alias: OutboundThreadReservationKey,
    ) -> None:
        existing = self._by_event.get(alias)
        if existing is reservation:
            return
        if existing is not None:
            return
        if len(reservation.aliases) >= self.max_aliases_per_reservation:
            removable_alias = next(
                (key for key in reservation.aliases if key != reservation.stable_key),
                None,
            )
            if removable_alias is None:
                return
            reservation.aliases.pop(removable_alias)
            self._by_event.pop(removable_alias, None)
        reservation.aliases[alias] = None
        self._by_event[alias] = reservation

    def reserve(
        self,
        principal_id: str,
        room_id: str,
        event_id: str,
        thread_id: str,
    ) -> None:
        """Reserve one stable response identity with certified thread scope."""
        now = self.clock()
        self._prune_expired(now)
        stable_key = self._key(principal_id, room_id, event_id)
        existing = self._by_event.get(stable_key)
        if existing is not None and existing.thread_id == thread_id:
            self._refresh(existing, now)
            return
        if existing is not None:
            self._drop(existing)
        if len(self._reservations) >= self.max_reservations:
            self._drop(min(self._reservations.values(), key=lambda item: item.expires_at))
        reservation = _OutboundThreadReservation(
            stable_key=stable_key,
            thread_id=thread_id,
            expires_at=now + self.ttl_seconds,
        )
        self._reservations[stable_key] = reservation
        self._bind_alias(reservation, stable_key)

    def resolve(
        self,
        principal_id: str,
        room_id: str,
        event_id: str,
        *,
        transitioned_event_id: str | None = None,
    ) -> str | None:
        """Resolve one event alias and retain a returned event-ID transition."""
        now = self.clock()
        self._prune_expired(now)
        reservation = self._by_event.get(self._key(principal_id, room_id, event_id))
        if reservation is None:
            return None
        self._refresh(reservation, now)
        if transitioned_event_id is not None:
            self._bind_alias(
                reservation,
                self._key(principal_id, room_id, transitioned_event_id),
            )
        return reservation.thread_id

    def release(self, principal_id: str, room_id: str, event_id: str) -> None:
        """Release one reservation and every event-ID alias retained with it."""
        now = self.clock()
        self._prune_expired(now)
        reservation = self._by_event.get(self._key(principal_id, room_id, event_id))
        if reservation is not None:
            self._drop(reservation)

    @property
    def active_count(self) -> int:
        """Return live reservation count after lazy TTL cleanup."""
        self._prune_expired(self.clock())
        return len(self._reservations)


__all__ = ["OutboundThreadReservationKey", "OutboundThreadReservations"]
