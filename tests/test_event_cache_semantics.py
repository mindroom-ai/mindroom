"""Focused tests for backend-neutral Matrix event-cache semantics."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from mindroom.matrix.cache import ThreadRevision
from mindroom.matrix.cache.event_cache_events import validated_cached_edit_row
from mindroom.matrix.cache.event_normalization import normalize_event_source_for_cache
from mindroom.matrix.cache.thread_cache_state import thread_cache_state_row, thread_revision_row
from mindroom.matrix.event_info import latest_valid_replacement, room_message_content_is_renderable

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


def test_validated_cached_edit_row_rejects_index_timestamp_mismatch() -> None:
    """Latest-edit ordering must not trust an index timestamp that disagrees with its event."""
    event = {
        "event_id": "$edit",
        "origin_server_ts": 2000,
        "sender": "@alice:localhost",
        "type": "m.room.message",
        "content": {
            "body": "* Edited",
            "msgtype": "m.text",
            "m.new_content": {"body": "Edited", "msgtype": "m.text"},
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$original"},
        },
    }

    assert (
        validated_cached_edit_row(
            json.dumps(event),
            None,
            "$edit",
            3000,
            room_id="!room:localhost",
            original_event_id="$original",
            sender="@alice:localhost",
            event_type="m.room.message",
        )
        is None
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

    latest = latest_valid_replacement(original, [edit("$a", explicit_timestamp)])

    assert latest is not None
    assert latest["event_id"] == expected_event_id


_VALID_ENCRYPTED_FILE = {
    "url": "mxc://localhost/media",
    "v": "v2",
    "key": {
        "alg": "A256CTR",
        "ext": True,
        "key_ops": ["encrypt", "decrypt"],
        "kty": "oct",
        "k": "a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s",
    },
    "iv": "aWlpaWlpaWlpaWlpaWlpaQ",
    "hashes": {"sha256": "aGhoaGhoaGhoaGhoaGhoaGhoaGhoaGhoaGhoaGhoaGg"},
}


def test_encrypted_media_file_v2_is_renderable() -> None:
    """A complete Matrix encrypted-file v2 envelope remains visible."""
    assert room_message_content_is_renderable(
        {
            "body": "image.png",
            "msgtype": "m.image",
            "file": _VALID_ENCRYPTED_FILE,
        },
    )


@pytest.mark.parametrize(
    ("path", "invalid_value"),
    [
        (("url",), "https://localhost/media"),
        (("v",), "v1"),
        (("key", "alg"), "wrong"),
        (("key", "ext"), False),
        (("key", "key_ops"), ["decrypt"]),
        (("key", "kty"), "RSA"),
        (("key", "k"), "not-base64"),
        (("key", "k"), "+/v7+/v7+/v7+/v7+/v7+/v7+/v7+/v7+/v7+/v7+/s"),
        (("iv",), "not-base64"),
        (("hashes",), {}),
        (("hashes", "sha256"), "not-base64"),
    ],
)
def test_encrypted_media_file_rejects_invalid_v2_fields(
    path: tuple[str, ...],
    invalid_value: object,
) -> None:
    """Every Matrix encrypted-file v2 and JWK requirement is enforced."""
    encrypted_file = json.loads(json.dumps(_VALID_ENCRYPTED_FILE))
    target = encrypted_file
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = invalid_value

    assert not room_message_content_is_renderable(
        {
            "body": "image.png",
            "msgtype": "m.image",
            "file": encrypted_file,
        },
    )


def test_validated_cached_edit_row_rejects_self_replacement() -> None:
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
        validated_cached_edit_row(
            json.dumps(event),
            None,
            "$self",
            2000,
            room_id="!room:localhost",
            original_event_id="$self",
            sender="@alice:localhost",
            event_type="m.room.message",
        )
        is None
    )
