"""Canonical Matrix replacement validation and projection."""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping
from operator import itemgetter
from typing import Any, Literal

from mindroom.matrix.event_identity import event_representation_transition
from mindroom.matrix.event_info import EventInfo, event_source_is_timeline_in_room, event_source_matches_room

type ReplacementValidator = Callable[[dict[str, Any]], bool]
type _EventRepresentationObservation = Literal["accept", "ignore", "conflict"]
_REPLACEMENT_IDENTITY_EVENT_TYPES = frozenset(
    {"m.room.encrypted", "m.room.message", "io.mindroom.tool_approval"},
)


def _valid_explicit_room(event: Mapping[str, Any], expected: str | None = None) -> bool:
    """Return whether an event's optional ``room_id`` is non-empty and agrees with ``expected``.

    Absent room evidence is acceptable; authoritative scope is ``event_source_is_timeline_in_room``.
    """
    if "room_id" not in event:
        return True
    room = event.get("room_id")
    return isinstance(room, str) and bool(room) and expected in (None, room)


def bundled_replacement_candidates(event: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Flatten all bundled replacement shapes without trusting their order."""
    unsigned = event.get("unsigned")
    relations = unsigned.get("m.relations") if isinstance(unsigned, Mapping) else None
    bundled = relations.get("m.replace") if isinstance(relations, Mapping) else None
    if not isinstance(bundled, Mapping):
        return []
    nested_candidates = bundled.get("latest_event"), bundled.get("event")
    candidates = (
        nested_candidates if any(isinstance(candidate, Mapping) for candidate in nested_candidates) else (bundled,)
    )
    return [dict(candidate) for candidate in candidates if isinstance(candidate, Mapping)]


def replacement_content(original: Mapping[str, object], new: Mapping[str, object]) -> dict[str, object]:
    """Replace content while preserving only the original relation."""
    content = {key: value for key, value in new.items() if key != "m.relates_to"}
    if "m.relates_to" in original:
        content["m.relates_to"] = original["m.relates_to"]
    return content


def _compatible_representation_types(original_type: object, candidate_type: object) -> bool:
    """Return whether two event types can be encrypted and clear views of one event."""
    return (
        isinstance(original_type, str)
        and isinstance(candidate_type, str)
        and (original_type == candidate_type or "m.room.encrypted" in {original_type, candidate_type})
    )


def _valid_bundled_identity_observation(
    container: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    room_id: str | None,
) -> bool:
    """Return whether a bundle may contribute immutable replacement identity evidence."""
    original_id, original_sender, original_type = (container.get(key) for key in ("event_id", "sender", "type"))
    candidate_type = candidate.get("type")
    content = candidate.get("content")
    relation = content.get("m.relates_to") if isinstance(content, Mapping) else None
    event_id, timestamp = (candidate.get(key) for key in ("event_id", "origin_server_ts"))
    return (
        all(isinstance(value, str) and value for value in (original_id, original_sender, original_type))
        and original_type in _REPLACEMENT_IDENTITY_EVENT_TYPES
        and not EventInfo.from_event(dict(container)).is_edit
        and event_source_is_timeline_in_room(container, room_id)
        and isinstance(event_id, str)
        and bool(event_id)
        and event_id != original_id
        and candidate.get("sender") == original_sender
        and _compatible_representation_types(original_type, candidate_type)
        and type(timestamp) is int
        and event_source_is_timeline_in_room(candidate, room_id)
        and isinstance(relation, Mapping)
        and (relation.get("rel_type"), relation.get("event_id")) == ("m.replace", original_id)
    )


def observe_event_representation(
    observed: dict[str, dict[str, Any]],
    conflicting_event_ids: set[str],
    candidate: Mapping[str, Any],
    *,
    room_id: str | None,
    container: Mapping[str, Any] | None = None,
    require_timeline: bool = True,
) -> _EventRepresentationObservation:
    """Record one room-scoped immutable event view and reject true contradictions."""
    event_id = candidate.get("event_id")
    if (
        not isinstance(event_id, str)
        or not event_id
        or (
            not event_source_is_timeline_in_room(candidate, room_id)
            if require_timeline
            else room_id is not None and not event_source_matches_room(candidate, room_id)
        )
        or (container is not None and not _valid_bundled_identity_observation(container, candidate, room_id=room_id))
    ):
        return "ignore"
    if event_id in conflicting_event_ids:
        return "conflict"
    existing = observed.get(event_id)
    transition: _EventRepresentationObservation = (
        "accept" if existing is None else event_representation_transition(existing, candidate)
    )
    if transition == "conflict":
        conflicting_event_ids.add(event_id)
    elif transition == "accept":
        observed[event_id] = dict(candidate)
    return transition


def _replacement_candidates_by_identity(
    candidates: Iterable[Mapping[str, Any]],
    *,
    room_id: str | None,
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    """Group replacement representations and identify immutable conflicts."""
    candidates_by_event_id: dict[str, dict[str, Any]] = {}
    conflicting_event_ids: set[str] = set()

    for candidate in candidates:
        observe_event_representation(
            candidates_by_event_id,
            conflicting_event_ids,
            candidate,
            room_id=room_id,
        )

    return candidates_by_event_id, conflicting_event_ids


def conflicting_replacement_event_ids(
    event_sources: Iterable[Mapping[str, Any]],
    *,
    room_id: str | None,
) -> frozenset[str]:
    """Return edit IDs whose explicit and bundled representations conflict globally."""
    observed: dict[str, dict[str, Any]] = {}
    conflicting_event_ids: set[str] = set()
    for event_source in event_sources:
        observe_event_representation(
            observed,
            conflicting_event_ids,
            event_source,
            room_id=room_id,
        )
        for bundled in bundled_replacement_candidates(event_source):
            observe_event_representation(
                observed,
                conflicting_event_ids,
                bundled,
                room_id=room_id,
                container=event_source,
            )
    return frozenset(conflicting_event_ids)


def _deduplicated_replacement_candidates(
    candidates: list[dict[str, Any]],
    *,
    room_id: str | None,
) -> list[dict[str, Any]]:
    """Deduplicate one immutable event identity and reject conflicting representations."""
    candidates_by_event_id, conflicting_event_ids = _replacement_candidates_by_identity(candidates, room_id=room_id)

    return [
        candidate for event_id, candidate in candidates_by_event_id.items() if event_id not in conflicting_event_ids
    ]


def _ordered_valid_replacements(
    original: Mapping[str, Any],
    candidates: list[dict[str, Any]],
    *,
    room_id: str | None,
    validator: ReplacementValidator,
    excluded_event_ids: Collection[str],
) -> list[dict[str, Any]]:
    """Return the candidates that may replace ``original``, Matrix latest-first."""
    original_id, sender, event_type = (original.get(key) for key in ("event_id", "sender", "type"))
    if (
        not all(isinstance(value, str) and value for value in (original_id, sender, event_type))
        or not _valid_explicit_room(original)
        or not event_source_is_timeline_in_room(original, room_id)
        or EventInfo.from_event(dict(original)).is_edit
    ):
        return []

    def valid(candidate: dict[str, Any]) -> bool:
        event_id, timestamp = (candidate.get(key) for key in ("event_id", "origin_server_ts"))
        content = candidate.get("content")
        relation = content.get("m.relates_to") if isinstance(content, Mapping) else None
        return (
            isinstance(event_id, str)
            and event_id not in ("", original_id)
            and event_id not in excluded_event_ids
            and (candidate.get("sender"), candidate.get("type")) == (sender, event_type)
            and type(timestamp) is int
            and event_source_is_timeline_in_room(candidate, room_id)
            and _valid_explicit_room(candidate, original.get("room_id"))
            and isinstance(relation, Mapping)
            and (relation.get("rel_type"), relation.get("event_id")) == ("m.replace", original_id)
            and validator(candidate)
        )

    valid_candidates = [candidate for candidate in candidates if valid(candidate)]
    return sorted(
        _deduplicated_replacement_candidates(valid_candidates, room_id=room_id),
        key=itemgetter("origin_server_ts", "event_id"),
        reverse=True,
    )


def ordered_replacements(
    original: Mapping[str, Any],
    candidates: Iterable[Mapping[str, Any]] = (),
    *,
    room_id: str | None,
    validator: ReplacementValidator,
    excluded_event_ids: Collection[str] = (),
) -> list[dict[str, Any]]:
    """Return valid replacements of ``original``, explicit and bundled, in one latest-first order."""
    return _ordered_valid_replacements(
        original,
        [dict(candidate) for candidate in candidates] + bundled_replacement_candidates(original),
        room_id=room_id,
        validator=validator,
        excluded_event_ids=excluded_event_ids,
    )


def is_valid_replacement(
    original: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    room_id: str | None,
    validator: ReplacementValidator,
    excluded_event_ids: Collection[str] = (),
) -> bool:
    """Return whether one specific candidate may replace ``original``.

    Bundled aggregation on ``original`` is deliberately not consulted, so a caller validating one
    cached row is never told "valid" because some other candidate is.
    """
    return bool(
        _ordered_valid_replacements(
            original,
            [dict(candidate)],
            room_id=room_id,
            validator=validator,
            excluded_event_ids=excluded_event_ids,
        ),
    )
