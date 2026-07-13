"""Tests for the daily thread-tag vocabulary snapshot."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom import thread_tag_vocabulary
from mindroom.config.main import Config
from mindroom.thread_tag_vocabulary import (
    _REBUILD_BOUNDARY_HOUR,
    _VOCABULARY_DESCRIPTION_TAG_LIMIT,
    _most_recent_rebuild_boundary,
    _snapshot_is_stale,
    _TagUsage,
    _TagVocabularySnapshot,
    format_tag_vocabulary_for_description,
    format_tag_vocabulary_with_counts,
    load_tag_vocabulary_snapshot,
    maybe_rebuild_tag_vocabulary,
    vocabulary_check_due,
)
from mindroom.thread_tags import THREAD_TAGS_EVENT_TYPE

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _reset_vocabulary_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the module-level freshness memo between tests."""
    monkeypatch.setattr(thread_tag_vocabulary, "_last_confirmed_fresh_boundary", None)


def _runtime_paths(tmp_path: Path) -> MagicMock:
    rp = MagicMock()
    rp.storage_root = tmp_path
    return rp


def _snapshot_file(tmp_path: Path) -> Path:
    return tmp_path / "tracking" / "thread_tag_vocabulary.json"


def _write_snapshot_file(tmp_path: Path, *, built_at: datetime, tags: list[dict[str, object]]) -> None:
    path = _snapshot_file(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "built_at": built_at.isoformat(), "tags": tags}))


def _tag_state_event(thread_root_id: str, tag: str) -> dict[str, object]:
    return {
        "type": THREAD_TAGS_EVENT_TYPE,
        "state_key": json.dumps([thread_root_id, tag], separators=(",", ":")),
        "content": {"set_by": "@user:localhost", "set_at": "2026-07-01T00:00:00+00:00", "data": {}},
    }


def _client_with_room_state(room_states: dict[str, list[dict[str, object]]]) -> AsyncMock:
    client = AsyncMock()
    client.rooms = dict.fromkeys(room_states)

    async def room_get_state(room_id: str) -> nio.RoomGetStateResponse:
        return nio.RoomGetStateResponse(events=room_states[room_id], room_id=room_id)

    client.room_get_state = AsyncMock(side_effect=room_get_state)
    return client


def _config(timezone: str = "UTC") -> Config:
    return Config(timezone=timezone)


# -- daily boundary arithmetic --


class TestMostRecentRebuildBoundary:
    """The daily rebuild boundary is the fixed early-morning hour in local time."""

    def test_after_boundary_hour_returns_today(self) -> None:
        """Past the boundary hour, the boundary is today's."""
        now = datetime(2026, 7, 12, 15, 30, tzinfo=UTC)

        boundary = _most_recent_rebuild_boundary(now, "UTC")

        assert boundary == datetime(2026, 7, 12, _REBUILD_BOUNDARY_HOUR, 0, tzinfo=UTC)

    def test_before_boundary_hour_returns_yesterday(self) -> None:
        """Before the boundary hour, the boundary is yesterday's."""
        now = datetime(2026, 7, 12, 3, 59, tzinfo=UTC)

        boundary = _most_recent_rebuild_boundary(now, "UTC")

        assert boundary == datetime(2026, 7, 11, _REBUILD_BOUNDARY_HOUR, 0, tzinfo=UTC)

    def test_boundary_uses_local_timezone(self) -> None:
        """The boundary is computed in the configured local timezone."""
        # 08:00 UTC on 2026-07-12 is 01:00 in Los Angeles (UTC-7): the LA
        # boundary is still yesterday's 04:00 local.
        now = datetime(2026, 7, 12, 8, 0, tzinfo=UTC)

        boundary = _most_recent_rebuild_boundary(now, "America/Los_Angeles")

        assert boundary.hour == _REBUILD_BOUNDARY_HOUR
        assert (boundary.month, boundary.day) == (7, 11)


class TestSnapshotIsStale:
    """A snapshot is stale once a new daily boundary has passed."""

    def test_missing_snapshot_is_stale(self) -> None:
        """A missing snapshot always counts as stale."""
        assert _snapshot_is_stale(None, now=datetime(2026, 7, 12, 12, 0, tzinfo=UTC), timezone_name="UTC")

    def test_snapshot_before_boundary_is_stale(self) -> None:
        """A snapshot built before the current boundary is stale."""
        snapshot = _TagVocabularySnapshot(built_at=datetime(2026, 7, 12, 3, 0, tzinfo=UTC), tags=())

        assert _snapshot_is_stale(snapshot, now=datetime(2026, 7, 12, 12, 0, tzinfo=UTC), timezone_name="UTC")

    def test_snapshot_after_boundary_is_fresh(self) -> None:
        """A snapshot built after the current boundary is fresh."""
        snapshot = _TagVocabularySnapshot(built_at=datetime(2026, 7, 12, 5, 0, tzinfo=UTC), tags=())

        assert not _snapshot_is_stale(snapshot, now=datetime(2026, 7, 12, 12, 0, tzinfo=UTC), timezone_name="UTC")


def test_vocabulary_check_due_transitions_with_confirmed_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    """The in-memory pre-queue gate opens only when a new boundary passes."""
    config = _config()
    now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)

    assert vocabulary_check_due(config, now=now)

    monkeypatch.setattr(
        thread_tag_vocabulary,
        "_last_confirmed_fresh_boundary",
        _most_recent_rebuild_boundary(now, "UTC"),
    )
    assert not vocabulary_check_due(config, now=now)
    assert vocabulary_check_due(config, now=datetime(2026, 7, 13, 12, 0, tzinfo=UTC))


# -- snapshot persistence --


def test_load_snapshot_missing_file_returns_none(tmp_path: Path) -> None:
    """A missing snapshot file loads as None."""
    assert load_tag_vocabulary_snapshot(_runtime_paths(tmp_path)) is None


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        json.dumps(["wrong shape"]),
        json.dumps({"built_at": "not-a-date", "tags": []}),
        json.dumps({"built_at": "2026-07-12T05:00:00", "tags": []}),  # naive timestamp
        json.dumps({"built_at": "2026-07-12T05:00:00+00:00", "tags": [{"tag": "bug"}]}),
    ],
)
def test_load_snapshot_malformed_payload_returns_none(tmp_path: Path, payload: str) -> None:
    """Malformed snapshot payloads load as None instead of raising."""
    path = _snapshot_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(payload)

    assert load_tag_vocabulary_snapshot(_runtime_paths(tmp_path)) is None


# -- rebuild --


@pytest.mark.asyncio
async def test_maybe_rebuild_counts_ranks_and_persists(tmp_path: Path) -> None:
    """A stale snapshot triggers one aggregation ranked by usage, ties alphabetical."""
    client = _client_with_room_state(
        {
            "!a:localhost": [
                _tag_state_event("$t1", "bug"),
                _tag_state_event("$t1", "docs"),
                _tag_state_event("$t2", "bug"),
            ],
            "!b:localhost": [
                _tag_state_event("$t3", "bug"),
                _tag_state_event("$t3", "billing"),
                _tag_state_event("$t4", "docs"),
            ],
        },
    )
    rp = _runtime_paths(tmp_path)
    now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)

    rebuilt = await maybe_rebuild_tag_vocabulary(client, _config(), rp, now=now)

    assert rebuilt
    snapshot = load_tag_vocabulary_snapshot(rp)
    assert snapshot is not None
    assert snapshot.built_at == now
    assert snapshot.tags == (
        _TagUsage(tag="bug", count=3),
        _TagUsage(tag="docs", count=2),
        _TagUsage(tag="billing", count=1),
    )


@pytest.mark.asyncio
async def test_maybe_rebuild_runs_once_per_boundary(tmp_path: Path) -> None:
    """A second check on the same day is a no-op that never touches Matrix."""
    client = _client_with_room_state({"!a:localhost": [_tag_state_event("$t1", "bug")]})
    rp = _runtime_paths(tmp_path)
    now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)

    assert await maybe_rebuild_tag_vocabulary(client, _config(), rp, now=now)
    client.room_get_state.reset_mock()

    assert not await maybe_rebuild_tag_vocabulary(client, _config(), rp, now=now)
    client.room_get_state.assert_not_awaited()

    # The next day's boundary triggers a fresh aggregation.
    assert await maybe_rebuild_tag_vocabulary(client, _config(), rp, now=datetime(2026, 7, 13, 12, 0, tzinfo=UTC))
    client.room_get_state.assert_awaited()


@pytest.mark.asyncio
async def test_maybe_rebuild_trusts_fresh_snapshot_on_disk(tmp_path: Path) -> None:
    """A fresh on-disk snapshot (e.g. after restart) suppresses re-aggregation."""
    client = _client_with_room_state({"!a:localhost": []})
    rp = _runtime_paths(tmp_path)
    now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    _write_snapshot_file(tmp_path, built_at=datetime(2026, 7, 12, 5, 0, tzinfo=UTC), tags=[{"tag": "bug", "count": 2}])

    assert not await maybe_rebuild_tag_vocabulary(client, _config(), rp, now=now)
    client.room_get_state.assert_not_awaited()
    snapshot = load_tag_vocabulary_snapshot(rp)
    assert snapshot is not None
    assert snapshot.tags == (_TagUsage(tag="bug", count=2),)


@pytest.mark.asyncio
async def test_maybe_rebuild_skips_unreadable_room(tmp_path: Path) -> None:
    """One room failing its state fetch must not lose the other rooms' counts."""
    client = _client_with_room_state({"!good:localhost": [_tag_state_event("$t1", "bug")]})
    room_states: dict[str, object] = {
        "!bad:localhost": object(),  # anything but RoomGetStateResponse fails the fetch
        "!good:localhost": nio.RoomGetStateResponse(events=[_tag_state_event("$t1", "bug")], room_id="!good:localhost"),
    }
    client.rooms = dict.fromkeys(room_states)
    client.room_get_state = AsyncMock(side_effect=lambda room_id: room_states[room_id])
    rp = _runtime_paths(tmp_path)

    assert await maybe_rebuild_tag_vocabulary(client, _config(), rp, now=datetime(2026, 7, 12, 12, 0, tzinfo=UTC))

    snapshot = load_tag_vocabulary_snapshot(rp)
    assert snapshot is not None
    assert snapshot.tags == (_TagUsage(tag="bug", count=1),)


# -- formatting --


def _snapshot_with_n_tags(count: int) -> _TagVocabularySnapshot:
    return _TagVocabularySnapshot(
        built_at=datetime(2026, 7, 12, 5, 0, tzinfo=UTC),
        tags=tuple(_TagUsage(tag=f"tag{i:02d}", count=count - i) for i in range(count)),
    )


def test_format_for_description_caps_at_limit_without_counts() -> None:
    """The description format ranks tags, caps at the limit, and omits counts."""
    snapshot = _snapshot_with_n_tags(_VOCABULARY_DESCRIPTION_TAG_LIMIT + 5)

    text = format_tag_vocabulary_for_description(snapshot)

    assert "rebuilt once a day" in text
    assert text.endswith(", ".join(f"tag{i:02d}" for i in range(_VOCABULARY_DESCRIPTION_TAG_LIMIT)))
    assert f"tag{_VOCABULARY_DESCRIPTION_TAG_LIMIT:02d}" not in text
    ranked_list = text.partition("): ")[2]
    assert "(" not in ranked_list  # no usage counts


@pytest.mark.parametrize(
    "snapshot",
    [None, _TagVocabularySnapshot(built_at=datetime(2026, 7, 12, tzinfo=UTC), tags=())],
)
def test_format_for_description_without_tags_falls_back(snapshot: _TagVocabularySnapshot | None) -> None:
    """An absent or empty snapshot formats as the coin-new-tags fallback."""
    assert format_tag_vocabulary_for_description(snapshot) == "No tags are in use yet; coin sensible new ones."


def test_format_with_counts_lists_ranked_tags() -> None:
    """The sidecar prompt format lists ranked tags with usage counts."""
    snapshot = _TagVocabularySnapshot(
        built_at=datetime(2026, 7, 12, 5, 0, tzinfo=UTC),
        tags=(_TagUsage(tag="bug", count=3), _TagUsage(tag="docs", count=1)),
    )

    assert format_tag_vocabulary_with_counts(snapshot) == "- bug (3)\n- docs (1)"


def test_format_with_counts_without_tags_falls_back() -> None:
    """An absent snapshot formats as the empty-vocabulary placeholder."""
    assert format_tag_vocabulary_with_counts(None) == "(no tags in use yet)"
