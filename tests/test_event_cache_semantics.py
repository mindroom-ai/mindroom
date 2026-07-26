"""Focused tests for backend-neutral Matrix event-cache semantics."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.matrix.cache import ThreadRevision
from mindroom.matrix.cache.event_cache_events import filter_redacted_events
from mindroom.matrix.cache.event_normalization import normalize_event_source_for_cache
from mindroom.matrix.cache.thread_cache_state import thread_cache_state_row, thread_revision_row
from mindroom.matrix.media import valid_room_message_replacement
from mindroom.matrix.replacements import (
    bundled_replacement_candidates,
    is_valid_replacement,
    ordered_replacements,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


@pytest.mark.parametrize(
    "values",
    [
        (),
        (1.0,),
        (1.0, 2.0, "reason", 3.0),
        (1.0, 2.0, "reason", 3.0, "room_reason", 4.0),
    ],
)
def test_thread_cache_state_row_rejects_malformed_storage_width(
    values: Sequence[float | str | None],
) -> None:
    """Storage rows must match the five-column query contract exactly."""
    with pytest.raises(ValueError, match=r"must contain exactly 5 values, got \d+"):
        thread_cache_state_row(values)


def test_thread_cache_state_row_treats_full_null_row_as_absent() -> None:
    """A complete outer-join miss remains an absent cache-state row."""
    assert thread_cache_state_row((None, None, None, None, None)) is None


@pytest.mark.parametrize("values", [(), (1,), (1, 2, 3), (1, 2, 3, 4, 5)])
def test_thread_revision_row_rejects_malformed_storage_width(
    values: Sequence[float | int | None],
) -> None:
    """Aggregate rows must match the four-column revision query contract exactly."""
    with pytest.raises(ValueError, match=r"must contain exactly 4 values, got \d+"):
        thread_revision_row(values)


@pytest.mark.parametrize("values", [None, (0, None, None, None), (1, None, 2, 3)])
def test_thread_revision_row_treats_empty_thread_as_absent(
    values: Sequence[float | int | None] | None,
) -> None:
    """Empty or partially aggregated threads never produce a revision."""
    assert thread_revision_row(values) is None


def test_thread_revision_row_normalizes_backend_values() -> None:
    """Backend numeric values normalize into one integer revision."""
    assert thread_revision_row((3, 7, 9, 1000)) == ThreadRevision(
        event_count=3,
        max_write_seq=7,
        max_thread_write_seq=9,
        max_origin_server_ts=1000,
    )


def test_cache_normalization_uses_authoritative_event_id() -> None:
    """A cache lookup key must override contradictory payload identity."""
    assert (
        normalize_event_source_for_cache(
            {"event_id": "$payload"},
            event_id="$indexed",
        )["event_id"]
        == "$indexed"
    )


@pytest.mark.parametrize(
    ("bundled_timestamp", "explicit_timestamp", "expected_event_id"),
    [
        (2000, 3000, "$a"),
        (2000, 2000, "$z"),
    ],
)
def test_latest_valid_replacement_orders_bundled_and_explicit_candidates_together(
    bundled_timestamp: int,
    explicit_timestamp: int,
    expected_event_id: str,
) -> None:
    """Cached and bundled candidates share Matrix timestamp and event-ID ordering."""
    original = {
        "event_id": "$original",
        "sender": "@alice:localhost",
        "origin_server_ts": 1000,
        "type": "m.room.message",
        "content": {"body": "Original", "msgtype": "m.text"},
    }

    def edit(event_id: str, timestamp: int) -> dict[str, object]:
        return {
            "event_id": event_id,
            "sender": "@alice:localhost",
            "origin_server_ts": timestamp,
            "type": "m.room.message",
            "content": {
                "body": f"* {event_id}",
                "msgtype": "m.text",
                "m.new_content": {"body": event_id, "msgtype": "m.text"},
                "m.relates_to": {"rel_type": "m.replace", "event_id": "$original"},
            },
        }

    original["unsigned"] = {
        "m.relations": {"m.replace": edit("$z", bundled_timestamp)},
    }

    latest = ordered_replacements(
        original,
        [edit("$a", explicit_timestamp)],
        room_id=None,
        validator=valid_room_message_replacement,
    )[0]

    assert latest is not None
    assert latest["event_id"] == expected_event_id


def test_ordered_replacements_rejects_self_replacement() -> None:
    """A replacement event cannot target its own event ID."""
    event = {
        "event_id": "$self",
        "origin_server_ts": 2000,
        "sender": "@alice:localhost",
        "type": "m.room.message",
        "content": {
            "body": "* Edited",
            "msgtype": "m.text",
            "m.new_content": {"body": "Edited", "msgtype": "m.text"},
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$self"},
        },
    }

    assert (
        ordered_replacements(
            {
                "event_id": "$self",
                "origin_server_ts": 1000,
                "sender": "@alice:localhost",
                "type": "m.room.message",
                "content": {"body": "Original", "msgtype": "m.text"},
            },
            [event],
            room_id="!room:localhost",
            validator=valid_room_message_replacement,
        )
        == []
    )


@pytest.mark.parametrize(
    ("candidate_shape", "original_room_id", "candidate_room_id"),
    [
        ("explicit", [], None),
        ("bundled", {}, None),
        ("explicit", None, []),
        ("bundled", None, {}),
    ],
)
def test_ordered_replacements_rejects_malformed_explicit_room_ids(
    candidate_shape: str,
    original_room_id: object,
    candidate_room_id: object,
) -> None:
    """Untrusted room evidence must fail closed without being hashed."""
    original: dict[str, object] = {
        "event_id": "$original",
        "sender": "@alice:localhost",
        "origin_server_ts": 1000,
        "type": "m.room.message",
        "content": {"body": "Original", "msgtype": "m.text"},
    }
    candidate: dict[str, object] = {
        "event_id": "$edit",
        "sender": "@alice:localhost",
        "origin_server_ts": 2000,
        "type": "m.room.message",
        "content": {
            "body": "* Edited",
            "msgtype": "m.text",
            "m.new_content": {"body": "Edited", "msgtype": "m.text"},
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$original"},
        },
    }
    if original_room_id is not None:
        original["room_id"] = original_room_id
    if candidate_room_id is not None:
        candidate["room_id"] = candidate_room_id
    candidates = [candidate] if candidate_shape == "explicit" else []
    if candidate_shape == "bundled":
        original["unsigned"] = {"m.relations": {"m.replace": candidate}}

    assert (
        ordered_replacements(
            original,
            candidates,
            room_id=None,
            validator=valid_room_message_replacement,
        )
        == []
    )


def _replaceable_original(**extra: object) -> dict[str, object]:
    """Return one plain original message that a same-sender replacement may target."""
    return {
        "event_id": "$original",
        "sender": "@alice:localhost",
        "origin_server_ts": 1000,
        "type": "m.room.message",
        "content": {"body": "Original", "msgtype": "m.text"},
        **extra,
    }


def _replacement(event_id: str, timestamp: int, **extra: object) -> dict[str, object]:
    """Return one well-formed replacement of ``$original``."""
    return {
        "event_id": event_id,
        "sender": "@alice:localhost",
        "origin_server_ts": timestamp,
        "type": "m.room.message",
        "content": {
            "body": f"* {event_id}",
            "msgtype": "m.text",
            "m.new_content": {"body": event_id, "msgtype": "m.text"},
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$original"},
        },
        **extra,
    }


def test_is_valid_replacement_ignores_a_valid_bundled_sibling() -> None:
    """Validating one candidate must not pass because a different candidate is valid.

    Cache backends ask this per joined row while scanning the edit index. Answering from the
    original's bundled aggregation instead of the row under test would admit a forged row
    whenever the original happened to carry any valid bundled replacement.
    """
    original = _replaceable_original(
        unsigned={"m.relations": {"m.replace": _replacement("$bundled", 3000)}},
    )
    forged = _replacement("$forged", 4000) | {"sender": "@mallory:localhost"}

    assert is_valid_replacement(
        original,
        _replacement("$cached", 2000),
        room_id=None,
        validator=valid_room_message_replacement,
    )
    assert not is_valid_replacement(
        original,
        forged,
        room_id=None,
        validator=valid_room_message_replacement,
    )
    # The bundled sibling is still selected by the ordering seam that is allowed to see it.
    assert (
        ordered_replacements(
            original,
            [forged],
            room_id=None,
            validator=valid_room_message_replacement,
        )[0]["event_id"]
        == "$bundled"
    )


def test_is_valid_replacement_honors_excluded_event_ids() -> None:
    """An excluded candidate is not a usable replacement even when it is otherwise valid."""
    original = _replaceable_original()
    candidate = _replacement("$edit", 2000)

    assert is_valid_replacement(original, candidate, room_id=None, validator=valid_room_message_replacement)
    assert not is_valid_replacement(
        original,
        candidate,
        room_id=None,
        validator=valid_room_message_replacement,
        excluded_event_ids={"$edit"},
    )


def test_redacting_one_bundled_shape_keeps_the_surviving_replacement() -> None:
    """A tombstone on one bundled shape must not delete a different surviving replacement.

    ``bundled_replacement_candidates`` treats the nested shapes as candidates that compete on
    their own identity, so redaction has to tombstone them individually. Dropping the whole
    aggregation would hide a replacement that selection would otherwise have chosen.
    """
    room_id = "!room:localhost"
    original = _replaceable_original(room_id=room_id)
    dead, good = _replacement("$dead", 2000, room_id=room_id), _replacement("$good", 3000, room_id=room_id)
    original["unsigned"] = {"m.relations": {"m.replace": {**dead, "latest_event": good}}}

    retained = filter_redacted_events(
        [("$original", original)],
        room_id=room_id,
        redacted_event_ids=frozenset({"$dead"}),
    )

    surviving = [
        event_id
        for candidate in bundled_replacement_candidates(retained[0][1])
        if isinstance(event_id := candidate.get("event_id"), str)
    ]
    assert surviving == ["$good"]


def test_redacting_the_bundled_wrapper_lets_surviving_shapes_compete() -> None:
    """A dead wrapper identity must not pick a winner among the surviving nested shapes.

    Rewriting the aggregation to one surviving shape would silently choose it; stripping only the
    dead wrapper identity leaves both survivors to compete on canonical timestamp ordering.
    """
    room_id = "!room:localhost"
    original = _replaceable_original(room_id=room_id)
    original["unsigned"] = {
        "m.relations": {
            "m.replace": {
                **_replacement("$dead", 2000, room_id=room_id),
                "latest_event": _replacement("$low", 1500, room_id=room_id),
                "event": _replacement("$high", 4000, room_id=room_id),
            },
        },
    }

    retained = filter_redacted_events(
        [("$original", original)],
        room_id=room_id,
        redacted_event_ids=frozenset({"$dead"}),
    )
    sanitized = retained[0][1]
    surviving = sorted(
        event_id
        for candidate in bundled_replacement_candidates(sanitized)
        if isinstance(event_id := candidate.get("event_id"), str)
    )

    assert surviving == ["$high", "$low"]
    selected = ordered_replacements(sanitized, room_id=room_id, validator=valid_room_message_replacement)
    assert selected[0]["event_id"] == "$high"


def test_redacting_every_bundled_shape_drops_the_aggregation() -> None:
    """When no bundled shape survives its tombstones the aggregation is removed outright."""
    room_id = "!room:localhost"
    original = _replaceable_original(room_id=room_id)
    edit = _replacement("$edit", 2000, room_id=room_id)
    original["unsigned"] = {"m.relations": {"m.replace": {**edit, "latest_event": edit}}}

    retained = filter_redacted_events(
        [("$original", original)],
        room_id=room_id,
        redacted_event_ids=frozenset({"$edit"}),
    )

    assert bundled_replacement_candidates(retained[0][1]) == []
