"""Explicit source ownership for one response attempt, and what the response did with its sources."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mindroom.turn_store import SourcePreparation


def _validate_event_ids(field_name: str, event_ids: tuple[str, ...], *, required: bool) -> None:
    """Reject mutable, empty, or duplicate event identity collections."""
    if not isinstance(event_ids, tuple):
        message = f"{field_name} must be a tuple"
        raise TypeError(message)
    if required and not event_ids:
        message = f"{field_name} must not be empty"
        raise ValueError(message)
    if any(not isinstance(event_id, str) or not event_id for event_id in event_ids):
        message = f"{field_name} must contain non-empty event IDs"
        raise ValueError(message)
    if len(set(event_ids)) != len(event_ids):
        message = f"{field_name} must not contain duplicate event IDs"
        raise ValueError(message)


@dataclass(frozen=True, slots=True)
class ResponseSources:
    """Source identities selected for one response execution."""

    pending_event_ids: tuple[str, ...]
    logical_source_event_ids: tuple[str, ...]
    discovery_event_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Validate immutable source identities."""
        _validate_event_ids("pending_event_ids", self.pending_event_ids, required=True)
        _validate_event_ids("logical_source_event_ids", self.logical_source_event_ids, required=True)
        _validate_event_ids("discovery_event_ids", self.discovery_event_ids, required=False)


class UnclaimedReason(StrEnum):
    """Why no reply took a response's sources."""

    # The requester may no longer drive this entity here.
    REVOKED = "revoked"
    # A source was deleted or answered elsewhere while the response waited.
    TERMINAL_SOURCES = "terminal_sources"
    # The participation decision declined the turn before any reply existed.
    DECLINED = "declined"
    # The reply already answered these sources, or a Stop, deletion, or departure ended it.
    NOTHING_TO_RUN = "nothing_to_run"
    # A job follow-up found nothing left to report.
    NOT_DUE = "not_due"


@dataclass(frozen=True, slots=True)
class Owned:
    """A reply's records, a refused claim, or an approval hold the response's exact pending sources.

    They settle them, keep them for a retry, or replay them; the response's caller does nothing.
    """


@dataclass(frozen=True, slots=True)
class Unclaimed:
    """No reply took the response's sources: its caller settles them.

    ``preparation`` names, for terminal sources before any reply existed, which settle and which survive to answer as
    their own turn.
    """

    reason: UnclaimedReason
    preparation: SourcePreparation | None = None


type SourceFate = Owned | Unclaimed
