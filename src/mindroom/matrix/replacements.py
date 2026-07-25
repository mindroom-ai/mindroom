"""Canonical Matrix replacement validation and projection."""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping
from operator import itemgetter
from typing import Any

from mindroom.matrix.event_info import EventInfo, event_source_is_timeline_in_room

type ReplacementValidator = Callable[[dict[str, Any]], bool]


def _valid_explicit_room(event: Mapping[str, Any], expected: object = None) -> bool:
    room = event.get("room_id")
    return "room_id" not in event or all((isinstance(room, str), bool(room), expected in (None, room)))


def bundled_replacement_candidates(event: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Flatten all bundled replacement shapes without trusting their order."""
    unsigned = event.get("unsigned")
    relations = unsigned.get("m.relations") if isinstance(unsigned, Mapping) else None
    bundled = relations.get("m.replace") if isinstance(relations, Mapping) else None
    if not isinstance(bundled, Mapping):
        return []
    candidates = bundled.get("latest_event"), bundled.get("event"), bundled
    return [dict(candidate) for candidate in candidates if isinstance(candidate, Mapping)]


def replacement_content(original: Mapping[str, object], new: Mapping[str, object]) -> dict[str, object]:
    """Replace content while preserving only the original relation."""
    content = {key: value for key, value in new.items() if key != "m.relates_to"}
    if "m.relates_to" in original:
        content["m.relates_to"] = original["m.relates_to"]
    return content


def ordered_replacements(
    original: Mapping[str, Any],
    candidates: Iterable[Mapping[str, Any]] = (),
    *,
    room_id: str | None,
    validator: ReplacementValidator,
    excluded_event_ids: Collection[str] = (),
) -> list[dict[str, Any]]:
    """Return valid replacements in Matrix latest-first order."""
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

    flattened = [dict(candidate) for candidate in candidates] + bundled_replacement_candidates(original)
    return sorted(filter(valid, flattened), key=itemgetter("origin_server_ts", "event_id"), reverse=True)
