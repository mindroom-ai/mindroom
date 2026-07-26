"""Immutable Matrix event representation reconciliation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

PROVISIONAL_OUTBOUND_KEY = "io.mindroom.provisional_outbound"
type _EventRepresentationTransition = Literal["accept", "ignore", "conflict"]


def _replacement_target(event: Mapping[str, Any]) -> object | None:
    """Return an exposed replacement target, if this representation carries one."""
    content = event.get("content")
    relates_to = content.get("m.relates_to") if isinstance(content, Mapping) else None
    if not isinstance(relates_to, Mapping) or relates_to.get("rel_type") != "m.replace":
        return None
    return relates_to.get("event_id")


def _provisional_transition(
    existing: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> _EventRepresentationTransition | None:
    """Return a trusted provisional transition, or ``None`` for canonical pairs."""
    existing_is_provisional = existing.get(PROVISIONAL_OUTBOUND_KEY) is True
    candidate_is_provisional = candidate.get(PROVISIONAL_OUTBOUND_KEY) is True
    if not existing_is_provisional and not candidate_is_provisional:
        return None
    if existing.get("type") != candidate.get("type"):
        return "conflict"
    if existing_is_provisional:
        compatible_content = not candidate_is_provisional or existing.get("content") == candidate.get("content")
        return "accept" if compatible_content else "conflict"
    return "ignore"


def _same_scoped_event_identity(
    existing: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> bool:
    """Return whether two representations agree on event identity and available scope."""
    same_envelope = all(existing.get(key) == candidate.get(key) for key in ("event_id", "sender")) and (
        "state_key" in existing,
        existing.get("state_key"),
    ) == ("state_key" in candidate, candidate.get("state_key"))
    rooms_conflict = (
        "room_id" in existing and "room_id" in candidate and existing.get("room_id") != candidate.get("room_id")
    )
    existing_replacement_target = _replacement_target(existing)
    candidate_replacement_target = _replacement_target(candidate)
    replacement_targets_conflict = (
        existing_replacement_target is not None
        and candidate_replacement_target is not None
        and existing_replacement_target != candidate_replacement_target
    )
    return same_envelope and not rooms_conflict and not replacement_targets_conflict


def event_representation_identity_matches(
    existing: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> bool:
    """Return whether immutable envelope metadata identifies one Matrix event."""
    valid_required_strings = all(
        isinstance(event.get(key), str) and bool(event.get(key))
        for event in (existing, candidate)
        for key in ("event_id", "sender", "type")
    )
    valid_optional_rooms = all(
        key not in event or (isinstance(event.get(key), str) and bool(event.get(key)))
        for event in (existing, candidate)
        for key in ("room_id",)
    )
    valid_optional_state_keys = all(
        "state_key" not in event or isinstance(event.get("state_key"), str) for event in (existing, candidate)
    )
    if not (
        valid_required_strings
        and valid_optional_rooms
        and valid_optional_state_keys
        and _same_scoped_event_identity(existing, candidate)
    ):
        return False
    existing_timestamp = existing.get("origin_server_ts")
    candidate_timestamp = candidate.get("origin_server_ts")
    if type(existing_timestamp) is not int or type(candidate_timestamp) is not int:
        return False
    if existing_timestamp != candidate_timestamp:
        return False
    existing_type = existing["type"]
    candidate_type = candidate["type"]
    return existing_type == candidate_type or "m.room.encrypted" in {existing_type, candidate_type}


def event_representation_transition(
    existing: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> _EventRepresentationTransition:
    """Choose a legal richer view of one immutable event or reject a contradiction."""
    if not _same_scoped_event_identity(existing, candidate):
        return "conflict"

    existing_is_encrypted = existing.get("type") == "m.room.encrypted"
    candidate_is_encrypted = candidate.get("type") == "m.room.encrypted"
    if existing_is_encrypted != candidate_is_encrypted:
        has_provisional_view = (
            existing.get(PROVISIONAL_OUTBOUND_KEY) is True or candidate.get(PROVISIONAL_OUTBOUND_KEY) is True
        )
        return (
            "conflict"
            if not has_provisional_view and existing.get("origin_server_ts") != candidate.get("origin_server_ts")
            else ("accept" if existing_is_encrypted else "ignore")
        )
    provisional_transition = _provisional_transition(existing, candidate)
    if provisional_transition is not None:
        return provisional_transition
    if existing.get("origin_server_ts") != candidate.get("origin_server_ts"):
        return "conflict"
    if (existing.get("type"), existing.get("content")) != (candidate.get("type"), candidate.get("content")):
        return "conflict"
    return "accept"
