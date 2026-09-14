"""Explicit source ownership for one response attempt."""

from __future__ import annotations

from dataclasses import dataclass


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
    edit_receipt_order: int | None = None

    def __post_init__(self) -> None:
        """Validate immutable source identity and ordering inputs."""
        _validate_event_ids("pending_event_ids", self.pending_event_ids, required=True)
        _validate_event_ids("logical_source_event_ids", self.logical_source_event_ids, required=True)
        _validate_event_ids("discovery_event_ids", self.discovery_event_ids, required=False)
        if self.edit_receipt_order is not None and (
            not isinstance(self.edit_receipt_order, int)
            or isinstance(self.edit_receipt_order, bool)
            or self.edit_receipt_order <= 0
        ):
            message = "edit_receipt_order must be None or a positive integer"
            raise ValueError(message)


@dataclass(frozen=True, slots=True)
class ResponseAttempt:
    """Immutable response identity carried separately from delivery results."""

    entity_name: str
    sources: ResponseSources

    def __post_init__(self) -> None:
        """Require an exact entity and validated source value."""
        if not isinstance(self.entity_name, str) or not self.entity_name:
            message = "entity_name must be a non-empty string"
            raise ValueError(message)
        if not isinstance(self.sources, ResponseSources):
            message = "sources must be ResponseSources"
            raise TypeError(message)
