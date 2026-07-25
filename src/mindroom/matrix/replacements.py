"""Canonical Matrix replacement validation and projection."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from mindroom.matrix.event_info import EventInfo, event_source_is_state_event, event_source_matches_room

type ReplacementValidator = Callable[[dict[str, Any]], bool]


def bundled_replacement_candidates(event: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Flatten all bundled replacement shapes without trusting their order."""
    unsigned = event.get("unsigned")
    relations = unsigned.get("m.relations") if isinstance(unsigned, Mapping) else None
    bundled = relations.get("m.replace") if isinstance(relations, Mapping) else None
    if not isinstance(bundled, Mapping):
        return []
    return [
        dict(candidate)
        for candidate in (bundled.get("latest_event"), bundled.get("event"), bundled)
        if isinstance(candidate, Mapping)
    ]


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
) -> list[dict[str, Any]]:
    """Return valid replacements in Matrix latest-first order."""
    original_id, sender, event_type = (original.get(key) for key in ("event_id", "sender", "type"))
    original_room_id = original.get("room_id")
    if (
        not all(isinstance(value, str) and value for value in (original_id, sender, event_type))
        or ("room_id" in original and not (isinstance(original_room_id, str) and original_room_id))
        or event_source_is_state_event(original)
        or EventInfo.from_event(dict(original)).is_edit
        or (room_id is not None and not event_source_matches_room(original, room_id))
    ):
        return []

    def valid(candidate: dict[str, Any]) -> bool:
        event_id, timestamp = (candidate.get(key) for key in ("event_id", "origin_server_ts"))
        content = candidate.get("content")
        relation = content.get("m.relates_to") if isinstance(content, Mapping) else None
        candidate_room_id = candidate.get("room_id")
        return (
            isinstance(event_id, str)
            and event_id not in ("", original_id)
            and (candidate.get("sender"), candidate.get("type")) == (sender, event_type)
            and type(timestamp) is int
            and not event_source_is_state_event(candidate)
            and (
                "room_id" not in candidate
                or (
                    isinstance(candidate_room_id, str)
                    and bool(candidate_room_id)
                    and ("room_id" not in original or candidate_room_id == original_room_id)
                )
            )
            and (room_id is None or event_source_matches_room(candidate, room_id))
            and isinstance(relation, Mapping)
            and (relation.get("rel_type"), relation.get("event_id")) == ("m.replace", original_id)
            and validator(candidate)
        )

    flattened = [dict(candidate) for candidate in candidates] + bundled_replacement_candidates(original)
    return sorted(
        (candidate for candidate in flattened if valid(candidate)),
        key=lambda candidate: (candidate["origin_server_ts"], candidate["event_id"]),
        reverse=True,
    )
