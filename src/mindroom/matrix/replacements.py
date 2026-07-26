"""Canonical Matrix replacement validation and projection."""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping
from operator import itemgetter
from typing import Any

from mindroom.matrix.event_identity import event_representation_transition
from mindroom.matrix.event_info import EventInfo, event_source_is_timeline_in_room

type ReplacementValidator = Callable[[dict[str, Any]], bool]


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


def _replacement_candidates_by_identity(
    candidates: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    """Group replacement representations and identify immutable conflicts."""
    candidates_by_event_id: dict[str, dict[str, Any]] = {}
    conflicting_event_ids: set[str] = set()

    for candidate in candidates:
        event_id = candidate.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            continue
        existing = candidates_by_event_id.get(event_id)
        if existing is None:
            candidates_by_event_id[event_id] = dict(candidate)
            continue
        transition = event_representation_transition(existing, candidate)
        if transition == "conflict":
            conflicting_event_ids.add(event_id)
            continue
        if transition == "accept":
            candidates_by_event_id[event_id] = dict(candidate)

    return candidates_by_event_id, conflicting_event_ids


def conflicting_replacement_event_ids(event_sources: Iterable[Mapping[str, Any]]) -> frozenset[str]:
    """Return edit IDs whose explicit and bundled representations conflict globally."""
    observations = [
        candidate
        for event_source in event_sources
        for candidate in (event_source, *bundled_replacement_candidates(event_source))
        if candidate is event_source or candidate.get("event_id") != event_source.get("event_id")
    ]
    _, conflicting_event_ids = _replacement_candidates_by_identity(observations)
    return frozenset(conflicting_event_ids)


def _deduplicated_replacement_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate one immutable event identity and reject conflicting representations."""
    candidates_by_event_id, conflicting_event_ids = _replacement_candidates_by_identity(candidates)

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

    return sorted(
        filter(valid, _deduplicated_replacement_candidates(candidates)),
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
