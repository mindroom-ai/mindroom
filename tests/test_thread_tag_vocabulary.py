"""Tests for room-scoped daily thread-tag vocabulary snapshots."""

from __future__ import annotations

import json
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from weakref import WeakValueDictionary

import nio
import pytest

from mindroom import thread_tag_vocabulary
from mindroom.config.main import Config
from mindroom.thread_tag_vocabulary import (
    _MAX_TRACKED_ROOM_SCOPES,
    _REBUILD_BOUNDARY_HOUR,
    _REBUILD_FAILURE_RETRY_DELAY,
    _VOCABULARY_DESCRIPTION_TAG_LIMIT,
    _most_recent_rebuild_boundary,
    _remember_boundary,
    _snapshot_is_stale,
    _TagUsage,
    _TagVocabularySnapshot,
    claim_vocabulary_check,
    format_tag_vocabulary_for_description,
    format_tag_vocabulary_with_counts,
    load_tag_vocabulary_snapshot,
    maybe_rebuild_tag_vocabulary,
)
from mindroom.thread_tags import THREAD_TAGS_EVENT_TYPE, ThreadTagsError
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

_ROOM_A = "!a:localhost"
_ROOM_B = "!b:localhost"


@pytest.fixture(autouse=True)
def _reset_vocabulary_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate module-level freshness, reservation, retry, and lock state."""
    monkeypatch.setattr(thread_tag_vocabulary, "_last_confirmed_fresh_boundaries", OrderedDict())
    monkeypatch.setattr(thread_tag_vocabulary, "_reserved_rebuild_boundaries", OrderedDict())
    monkeypatch.setattr(thread_tag_vocabulary, "_rebuild_retry_not_before", OrderedDict())
    monkeypatch.setattr(thread_tag_vocabulary, "_rebuild_locks", WeakValueDictionary())


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return test_runtime_paths(tmp_path)


def _snapshot_file(runtime_paths: RuntimePaths, room_id: str) -> Path:
    return thread_tag_vocabulary._snapshot_path(runtime_paths, room_id)


def _write_snapshot_file(
    runtime_paths: RuntimePaths,
    room_id: str,
    *,
    built_at: datetime,
    tags: list[dict[str, object]],
    version: int = 1,
    persisted_room_id: str | None = None,
) -> None:
    path = _snapshot_file(runtime_paths, room_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": version,
                "room_id": persisted_room_id or room_id,
                "built_at": built_at.isoformat(),
                "tags": tags,
            },
        ),
        encoding="utf-8",
    )


def _tag_state_event(thread_root_id: str, tag: str) -> dict[str, object]:
    return {
        "type": THREAD_TAGS_EVENT_TYPE,
        "state_key": json.dumps([thread_root_id, tag], separators=(",", ":")),
        "content": {
            "set_by": "@user:localhost",
            "set_at": "2026-07-01T00:00:00+00:00",
            "data": {},
        },
    }


def _client_with_room_state(room_states: dict[str, list[dict[str, object]]]) -> AsyncMock:
    client = AsyncMock()

    async def room_get_state(room_id: str) -> nio.RoomGetStateResponse:
        return nio.RoomGetStateResponse(events=room_states[room_id], room_id=room_id)

    client.room_get_state = AsyncMock(side_effect=room_get_state)
    return client


def _config(timezone: str = "UTC") -> Config:
    return Config(timezone=timezone)


class TestMostRecentRebuildBoundary:
    """The daily rebuild boundary is local midnight."""

    def test_midday_returns_today_at_midnight(self) -> None:
        """During the day, the boundary is today's midnight."""
        now = datetime(2026, 7, 12, 15, 30, tzinfo=UTC)

        boundary = _most_recent_rebuild_boundary(now, "UTC")

        assert boundary == datetime(2026, 7, 12, _REBUILD_BOUNDARY_HOUR, 0, tzinfo=UTC)

    def test_exact_midnight_returns_itself(self) -> None:
        """At midnight, the new day's boundary is already active."""
        now = datetime(2026, 7, 12, 0, 0, tzinfo=UTC)

        boundary = _most_recent_rebuild_boundary(now, "UTC")

        assert boundary == now

    def test_boundary_uses_local_timezone(self) -> None:
        """The boundary is computed in the configured local timezone."""
        now = datetime(2026, 7, 12, 6, 0, tzinfo=UTC)

        boundary = _most_recent_rebuild_boundary(now, "America/Los_Angeles")

        assert boundary.hour == _REBUILD_BOUNDARY_HOUR
        assert (boundary.month, boundary.day) == (7, 11)


class TestSnapshotIsStale:
    """A snapshot is stale once a new daily boundary passes."""

    def test_missing_snapshot_is_stale(self) -> None:
        """A missing snapshot always counts as stale."""
        assert _snapshot_is_stale(
            None,
            now=datetime(2026, 7, 12, 12, 0, tzinfo=UTC),
            timezone_name="UTC",
        )

    def test_snapshot_before_boundary_is_stale(self) -> None:
        """A snapshot built before the current boundary is stale."""
        snapshot = _TagVocabularySnapshot(
            built_at=datetime(2026, 7, 11, 23, 59, tzinfo=UTC),
            tags=(),
        )

        assert _snapshot_is_stale(
            snapshot,
            now=datetime(2026, 7, 12, 12, 0, tzinfo=UTC),
            timezone_name="UTC",
        )

    def test_snapshot_after_boundary_is_fresh(self) -> None:
        """A snapshot built after the current boundary is fresh."""
        snapshot = _TagVocabularySnapshot(
            built_at=datetime(2026, 7, 12, 0, 1, tzinfo=UTC),
            tags=(),
        )

        assert not _snapshot_is_stale(
            snapshot,
            now=datetime(2026, 7, 12, 12, 0, tzinfo=UTC),
            timezone_name="UTC",
        )


def test_claim_vocabulary_check_reserves_each_room_once(tmp_path: Path) -> None:
    """The pre-queue claim suppresses duplicate checks without crossing rooms."""
    runtime_paths = _runtime_paths(tmp_path)
    now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)

    assert claim_vocabulary_check(_ROOM_A, _config(), runtime_paths, now=now)
    assert not claim_vocabulary_check(_ROOM_A, _config(), runtime_paths, now=now)
    assert claim_vocabulary_check(_ROOM_B, _config(), runtime_paths, now=now)


def test_claim_vocabulary_check_opens_at_next_boundary(tmp_path: Path) -> None:
    """A confirmed room becomes due again only after the next boundary."""
    runtime_paths = _runtime_paths(tmp_path)
    now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    scope_key = thread_tag_vocabulary._scope_key(runtime_paths, _ROOM_A)
    thread_tag_vocabulary._last_confirmed_fresh_boundaries[scope_key] = _most_recent_rebuild_boundary(now, "UTC")

    assert not claim_vocabulary_check(_ROOM_A, _config(), runtime_paths, now=now)
    assert claim_vocabulary_check(
        _ROOM_A,
        _config(),
        runtime_paths,
        now=datetime(2026, 7, 13, 12, 0, tzinfo=UTC),
    )


def test_room_scope_boundary_maps_are_lru_bounded(tmp_path: Path) -> None:
    """Room-scoped freshness state evicts its oldest entry at the shared bound."""
    runtime_paths = _runtime_paths(tmp_path)
    boundaries: OrderedDict[tuple[Path, str], datetime] = OrderedDict()
    boundary = datetime(2026, 7, 12, 0, 0, tzinfo=UTC)

    for index in range(_MAX_TRACKED_ROOM_SCOPES + 1):
        _remember_boundary(boundaries, (runtime_paths.storage_root, f"!room{index}:localhost"), boundary)

    assert len(boundaries) == _MAX_TRACKED_ROOM_SCOPES
    assert (runtime_paths.storage_root, "!room0:localhost") not in boundaries
    assert (runtime_paths.storage_root, f"!room{_MAX_TRACKED_ROOM_SCOPES}:localhost") in boundaries


def test_load_snapshot_missing_file_returns_none(tmp_path: Path) -> None:
    """A missing room snapshot loads as None."""
    assert load_tag_vocabulary_snapshot(_runtime_paths(tmp_path), _ROOM_A) is None


def test_load_snapshot_omits_reserved_tags_from_model_vocabulary(tmp_path: Path) -> None:
    """Existing snapshots must not advertise user-controlled lifecycle tags."""
    runtime_paths = _runtime_paths(tmp_path)
    _write_snapshot_file(
        runtime_paths,
        _ROOM_A,
        built_at=datetime(2026, 7, 12, 5, 0, tzinfo=UTC),
        tags=[{"tag": "resolved", "count": 5}, {"tag": "bug", "count": 3}],
    )

    snapshot = load_tag_vocabulary_snapshot(runtime_paths, _ROOM_A)

    assert snapshot is not None
    assert snapshot.tags == (_TagUsage(tag="bug", count=3),)
    assert "resolved" not in format_tag_vocabulary_for_description(snapshot)
    assert "resolved" not in format_tag_vocabulary_with_counts(snapshot)


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        json.dumps(["wrong shape"]),
        json.dumps({"version": 1, "room_id": _ROOM_A, "built_at": "not-a-date", "tags": []}),
        json.dumps(
            {
                "version": 1,
                "room_id": _ROOM_A,
                "built_at": "2026-07-12T05:00:00",
                "tags": [],
            },
        ),
        json.dumps(
            {
                "version": 1,
                "room_id": _ROOM_A,
                "built_at": "2026-07-12T05:00:00+00:00",
                "tags": [{"tag": "bug"}],
            },
        ),
        json.dumps(
            {
                "version": 2,
                "room_id": _ROOM_A,
                "built_at": "2026-07-12T05:00:00+00:00",
                "tags": [],
            },
        ),
        json.dumps(
            {
                "version": 1,
                "room_id": _ROOM_B,
                "built_at": "2026-07-12T05:00:00+00:00",
                "tags": [],
            },
        ),
    ],
)
def test_load_snapshot_malformed_payload_returns_none(
    tmp_path: Path,
    payload: str,
) -> None:
    """Malformed room snapshots load as None instead of raising."""
    runtime_paths = _runtime_paths(tmp_path)
    path = _snapshot_file(runtime_paths, _ROOM_A)
    path.parent.mkdir(parents=True)
    path.write_text(payload, encoding="utf-8")

    assert load_tag_vocabulary_snapshot(runtime_paths, _ROOM_A) is None


def test_load_snapshot_invalid_utf8_returns_none(tmp_path: Path) -> None:
    """Invalid UTF-8 is treated as a malformed absent snapshot."""
    runtime_paths = _runtime_paths(tmp_path)
    path = _snapshot_file(runtime_paths, _ROOM_A)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe")

    assert load_tag_vocabulary_snapshot(runtime_paths, _ROOM_A) is None


@pytest.mark.asyncio
async def test_maybe_rebuild_counts_ranks_and_persists_one_room(tmp_path: Path) -> None:
    """A stale room snapshot is rebuilt from only that room's tag state."""
    client = _client_with_room_state(
        {
            _ROOM_A: [
                _tag_state_event("$t1", "resolved"),
                _tag_state_event("$t1", "bug"),
                _tag_state_event("$t1", "docs"),
                _tag_state_event("$t2", "bug"),
                _tag_state_event("$t2", "resolved"),
            ],
            _ROOM_B: [_tag_state_event("$t3", "private")],
        },
    )
    runtime_paths = _runtime_paths(tmp_path)
    now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)

    rebuilt = await maybe_rebuild_tag_vocabulary(
        client,
        _ROOM_A,
        _config(),
        runtime_paths,
        now=now,
    )

    assert rebuilt is not None
    client.room_get_state.assert_awaited_once_with(_ROOM_A)
    snapshot = load_tag_vocabulary_snapshot(runtime_paths, _ROOM_A)
    assert snapshot is not None
    assert snapshot.built_at == now
    assert snapshot.tags == (
        _TagUsage(tag="bug", count=2),
        _TagUsage(tag="docs", count=1),
    )
    assert load_tag_vocabulary_snapshot(runtime_paths, _ROOM_B) is None


@pytest.mark.asyncio
async def test_maybe_rebuild_keeps_disjoint_room_vocabularies(tmp_path: Path) -> None:
    """Disjoint room snapshots never expose one room's tags to another."""
    client = _client_with_room_state(
        {
            _ROOM_A: [_tag_state_event("$a", "alpha")],
            _ROOM_B: [_tag_state_event("$b", "secret-beta")],
        },
    )
    runtime_paths = _runtime_paths(tmp_path)
    now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)

    assert await maybe_rebuild_tag_vocabulary(client, _ROOM_A, _config(), runtime_paths, now=now) is not None
    assert await maybe_rebuild_tag_vocabulary(client, _ROOM_B, _config(), runtime_paths, now=now) is not None

    room_a = load_tag_vocabulary_snapshot(runtime_paths, _ROOM_A)
    room_b = load_tag_vocabulary_snapshot(runtime_paths, _ROOM_B)
    assert room_a is not None
    assert room_a.tags == (_TagUsage(tag="alpha", count=1),)
    assert room_b is not None
    assert room_b.tags == (_TagUsage(tag="secret-beta", count=1),)


@pytest.mark.asyncio
async def test_maybe_rebuild_persists_only_model_reproducible_top_tags(
    tmp_path: Path,
) -> None:
    """Snapshots keep only the top 20 tags that model output can preserve."""
    events = [_tag_state_event(f"$thread{i}", f"tag{i:02d}") for i in range(_VOCABULARY_DESCRIPTION_TAG_LIMIT + 5)]
    events.append(_tag_state_event("$long", "x" * 26))
    client = _client_with_room_state({_ROOM_A: events})
    runtime_paths = _runtime_paths(tmp_path)

    assert (
        await maybe_rebuild_tag_vocabulary(
            client,
            _ROOM_A,
            _config(),
            runtime_paths,
            now=datetime(2026, 7, 12, 12, 0, tzinfo=UTC),
        )
        is not None
    )

    snapshot = load_tag_vocabulary_snapshot(runtime_paths, _ROOM_A)
    assert snapshot is not None
    assert len(snapshot.tags) == _VOCABULARY_DESCRIPTION_TAG_LIMIT
    assert snapshot.tags[-1].tag == "tag19"
    assert all(len(usage.tag) <= 25 for usage in snapshot.tags)


@pytest.mark.asyncio
async def test_maybe_rebuild_runs_once_per_room_boundary(tmp_path: Path) -> None:
    """A second same-day check for one room never touches Matrix."""
    client = _client_with_room_state({_ROOM_A: [_tag_state_event("$t1", "bug")]})
    runtime_paths = _runtime_paths(tmp_path)
    now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)

    assert await maybe_rebuild_tag_vocabulary(client, _ROOM_A, _config(), runtime_paths, now=now) is not None
    client.room_get_state.reset_mock()

    assert await maybe_rebuild_tag_vocabulary(client, _ROOM_A, _config(), runtime_paths, now=now) is None
    client.room_get_state.assert_not_awaited()

    next_day = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
    assert (
        await maybe_rebuild_tag_vocabulary(
            client,
            _ROOM_A,
            _config(),
            runtime_paths,
            now=next_day,
        )
        is not None
    )
    client.room_get_state.assert_awaited_once_with(_ROOM_A)


@pytest.mark.asyncio
async def test_maybe_rebuild_trusts_fresh_room_snapshot_on_disk(tmp_path: Path) -> None:
    """A fresh room snapshot after restart suppresses Matrix aggregation."""
    client = _client_with_room_state({_ROOM_A: []})
    runtime_paths = _runtime_paths(tmp_path)
    now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    _write_snapshot_file(
        runtime_paths,
        _ROOM_A,
        built_at=datetime(2026, 7, 12, 5, 0, tzinfo=UTC),
        tags=[{"tag": "bug", "count": 2}],
    )

    refresh = await maybe_rebuild_tag_vocabulary(
        client,
        _ROOM_A,
        _config(),
        runtime_paths,
        now=now,
    )
    assert refresh is not None
    client.room_get_state.assert_not_awaited()
    snapshot = load_tag_vocabulary_snapshot(runtime_paths, _ROOM_A)
    assert snapshot is not None
    assert snapshot.tags == (_TagUsage(tag="bug", count=2),)


@pytest.mark.asyncio
async def test_failed_room_read_preserves_snapshot_and_backs_off(tmp_path: Path) -> None:
    """A failed room read publishes nothing and suppresses immediate retry storms."""
    client = AsyncMock()
    client.room_get_state = AsyncMock(return_value=object())
    runtime_paths = _runtime_paths(tmp_path)
    stale_time = datetime(2026, 7, 11, 5, 0, tzinfo=UTC)
    now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    _write_snapshot_file(
        runtime_paths,
        _ROOM_A,
        built_at=stale_time,
        tags=[{"tag": "previous", "count": 2}],
    )

    with pytest.raises(ThreadTagsError):
        await maybe_rebuild_tag_vocabulary(
            client,
            _ROOM_A,
            _config(),
            runtime_paths,
            now=now,
        )

    snapshot = load_tag_vocabulary_snapshot(runtime_paths, _ROOM_A)
    assert snapshot is not None
    assert snapshot.built_at == stale_time
    assert snapshot.tags == (_TagUsage(tag="previous", count=2),)
    client.room_get_state.reset_mock()
    retry_refresh = await maybe_rebuild_tag_vocabulary(
        client,
        _ROOM_A,
        _config(),
        runtime_paths,
        now=now + _REBUILD_FAILURE_RETRY_DELAY - timedelta(seconds=1),
    )
    assert retry_refresh is None
    client.room_get_state.assert_not_awaited()

    client.room_get_state = AsyncMock(return_value=nio.RoomGetStateResponse(events=[], room_id=_ROOM_A))
    assert (
        await maybe_rebuild_tag_vocabulary(
            client,
            _ROOM_A,
            _config(),
            runtime_paths,
            now=now + _REBUILD_FAILURE_RETRY_DELAY,
        )
        is not None
    )


def _snapshot_with_n_tags(count: int) -> _TagVocabularySnapshot:
    return _TagVocabularySnapshot(
        built_at=datetime(2026, 7, 12, 5, 0, tzinfo=UTC),
        tags=tuple(_TagUsage(tag=f"tag{i:02d}", count=count - i) for i in range(count)),
    )


def test_format_for_description_caps_at_limit_without_counts() -> None:
    """The description ranks tags, caps at the limit, and omits counts."""
    snapshot = _snapshot_with_n_tags(_VOCABULARY_DESCRIPTION_TAG_LIMIT + 5)

    text = format_tag_vocabulary_for_description(snapshot)

    assert "rebuilt once a day" in text
    assert text.endswith(", ".join(f"tag{i:02d}" for i in range(_VOCABULARY_DESCRIPTION_TAG_LIMIT)))
    assert f"tag{_VOCABULARY_DESCRIPTION_TAG_LIMIT:02d}" not in text
    ranked_list = text.partition("): ")[2]
    assert "(" not in ranked_list


@pytest.mark.parametrize(
    "snapshot",
    [None, _TagVocabularySnapshot(built_at=datetime(2026, 7, 12, tzinfo=UTC), tags=())],
)
def test_format_for_description_without_tags_falls_back(
    snapshot: _TagVocabularySnapshot | None,
) -> None:
    """An absent or empty snapshot formats as the coin-new-tags fallback."""
    assert format_tag_vocabulary_for_description(snapshot) == (
        "No reusable short tags are in use yet; coin sensible new ones."
    )


def test_format_with_counts_lists_ranked_tags() -> None:
    """The initial-enrichment prompt lists ranked tags with usage counts."""
    snapshot = _TagVocabularySnapshot(
        built_at=datetime(2026, 7, 12, 5, 0, tzinfo=UTC),
        tags=(_TagUsage(tag="bug", count=3), _TagUsage(tag="docs", count=1)),
    )

    assert format_tag_vocabulary_with_counts(snapshot) == "- bug (3)\n- docs (1)"


def test_format_with_counts_without_tags_falls_back() -> None:
    """An absent snapshot formats as the empty-vocabulary placeholder."""
    assert format_tag_vocabulary_with_counts(None) == "(no reusable short tags in use yet)"
