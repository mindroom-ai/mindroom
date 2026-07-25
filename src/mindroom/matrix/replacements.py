"""Canonical Matrix replacement validation and projection."""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from operator import itemgetter
from typing import Any, cast

from mindroom.matrix.event_info import EventInfo, event_source_is_timeline_in_room

type ReplacementValidator = Callable[[dict[str, Any]], bool]


def _valid_explicit_room(event: Mapping[str, Any], expected: str | None = None) -> bool:
    """Return whether one event's optional explicit room evidence is usable and consistent.

    An event without ``room_id`` carries no evidence and stays acceptable; authoritative room
    scope is enforced separately by ``event_source_is_timeline_in_room``. When the event does
    carry ``room_id`` it must be a non-empty string, and it must equal ``expected`` whenever the
    caller has explicit room evidence of its own to compare against.
    """
    if "room_id" not in event:
        return True
    room = event.get("room_id")
    if not isinstance(room, str) or not room:
        return False
    return expected is None or expected == room


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


@dataclass(frozen=True, slots=True)
class _ReplaceableOriginal:
    """The identity one original imposes on every replacement that may apply to it."""

    event_id: str
    sender: str
    event_type: str
    explicit_room_id: str | None


def _replaceable_original(original: Mapping[str, Any], *, room_id: str | None) -> _ReplaceableOriginal | None:
    """Return the replacement identity of one original, or ``None`` if it cannot be replaced."""
    event_id, sender, event_type = (original.get(key) for key in ("event_id", "sender", "type"))
    if (
        not all(isinstance(value, str) and value for value in (event_id, sender, event_type))
        or not _valid_explicit_room(original)
        or not event_source_is_timeline_in_room(original, room_id)
        or EventInfo.from_event(dict(original)).is_edit
    ):
        return None
    explicit_room_id = original.get("room_id")
    return _ReplaceableOriginal(
        event_id=cast("str", event_id),
        sender=cast("str", sender),
        event_type=cast("str", event_type),
        explicit_room_id=explicit_room_id if isinstance(explicit_room_id, str) else None,
    )


def _candidate_replaces_original(
    candidate: Mapping[str, Any],
    original: _ReplaceableOriginal,
    *,
    room_id: str | None,
    validator: ReplacementValidator,
    excluded_event_ids: Collection[str],
) -> bool:
    """Return whether one candidate is an eligible replacement of one replaceable original."""
    event_id, timestamp = (candidate.get(key) for key in ("event_id", "origin_server_ts"))
    content = candidate.get("content")
    relation = content.get("m.relates_to") if isinstance(content, Mapping) else None
    return (
        isinstance(event_id, str)
        and event_id not in ("", original.event_id)
        and event_id not in excluded_event_ids
        and (candidate.get("sender"), candidate.get("type")) == (original.sender, original.event_type)
        and type(timestamp) is int
        and event_source_is_timeline_in_room(candidate, room_id)
        and _valid_explicit_room(candidate, original.explicit_room_id)
        and isinstance(relation, Mapping)
        and (relation.get("rel_type"), relation.get("event_id")) == ("m.replace", original.event_id)
        and validator(dict(candidate))
    )


def ordered_replacements(
    original: Mapping[str, Any],
    candidates: Iterable[Mapping[str, Any]] = (),
    *,
    room_id: str | None,
    validator: ReplacementValidator,
    excluded_event_ids: Collection[str] = (),
) -> list[dict[str, Any]]:
    """Return valid replacements of ``original`` in Matrix latest-first order.

    Explicit ``candidates`` and any bundled aggregation carried by ``original`` compete in one
    ordering: newest ``origin_server_ts`` first, then lexicographically greatest event ID.
    """
    replaceable = _replaceable_original(original, room_id=room_id)
    if replaceable is None:
        return []
    flattened = [dict(candidate) for candidate in candidates] + bundled_replacement_candidates(original)
    valid = [
        candidate
        for candidate in flattened
        if _candidate_replaces_original(
            candidate,
            replaceable,
            room_id=room_id,
            validator=validator,
            excluded_event_ids=excluded_event_ids,
        )
    ]
    return sorted(valid, key=itemgetter("origin_server_ts", "event_id"), reverse=True)


def is_valid_replacement(
    original: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    room_id: str | None,
    validator: ReplacementValidator,
    excluded_event_ids: Collection[str] = (),
) -> bool:
    """Return whether one specific candidate may replace one original.

    Unlike ``ordered_replacements`` this ignores any bundled aggregation on ``original``, so a
    caller validating one cached row is never told "valid" because a different candidate is.
    """
    replaceable = _replaceable_original(original, room_id=room_id)
    return replaceable is not None and _candidate_replaces_original(
        candidate,
        replaceable,
        room_id=room_id,
        validator=validator,
        excluded_event_ids=excluded_event_ids,
    )
