"""Tests for process-local reuse of resolved thread history across durable-cache reads."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch
from weakref import WeakKeyDictionary

import pytest

import mindroom.matrix.client_thread_history as matrix_client_module
from mindroom.matrix.cache import ThreadCacheState, ThreadRevision
from mindroom.matrix.client_thread_history import fetch_thread_history
from mindroom.matrix.thread_resolution_reuse import (
    ThreadResolutionReuseCache,
    build_thread_resolution_snapshot,
    reusable_event_source_suffix,
)
from tests.conftest import make_event_cache_mock
from tests.event_cache_test_support import replace_thread_unconditionally

if TYPE_CHECKING:
    from pathlib import Path

    import nio

    from mindroom.matrix.client_thread_history import _ResolvedThreadEventSources
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.matrix.thread_resolution_reuse import ThreadResolutionSnapshot

ROOM = "!room:localhost"
THREAD = "$root"
EPOCH = 1


@dataclass(slots=True)
class _SyntheticDurableState:
    """Durable revision state used by the unit-test cache simulation."""

    rows: list[dict[str, Any]] | None = None
    write_seq: int = 0


_SYNTHETIC_DURABLE_STATES: WeakKeyDictionary[ThreadResolutionReuseCache, _SyntheticDurableState] = WeakKeyDictionary()


def _message_row(
    event_id: str,
    timestamp: int,
    body: str,
    *,
    sender: str = "@user:localhost",
) -> dict[str, Any]:
    content: dict[str, Any] = {"msgtype": "m.text", "body": body}
    if event_id != THREAD:
        content["m.relates_to"] = {"rel_type": "m.thread", "event_id": THREAD}
    return {
        "event_id": event_id,
        "origin_server_ts": timestamp,
        "type": "m.room.message",
        "sender": sender,
        "content": content,
    }


def _edit_row(
    event_id: str,
    timestamp: int,
    *,
    target: str,
    body: str,
    sender: str = "@user:localhost",
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "origin_server_ts": timestamp,
        "type": "m.room.message",
        "sender": sender,
        "content": {
            "msgtype": "m.text",
            "body": f"* {body}",
            "m.new_content": {
                "msgtype": "m.text",
                "body": body,
                "m.relates_to": {"rel_type": "m.thread", "event_id": THREAD},
            },
            "m.relates_to": {"rel_type": "m.replace", "event_id": target},
        },
    }


def _sidecar_row(event_id: str, timestamp: int) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "origin_server_ts": timestamp,
        "type": "m.room.message",
        "sender": "@agent:localhost",
        "content": {
            "msgtype": "m.file",
            "body": "Preview",
            "io.mindroom.long_text": {"version": 2, "encoding": "matrix_event_content_json"},
            "url": f"mxc://server/sidecar-{event_id.lstrip('$')}",
            "m.relates_to": {"rel_type": "m.thread", "event_id": THREAD},
        },
    }


async def _resolve(
    rows: list[dict[str, Any]],
    *,
    reuse: ThreadResolutionReuseCache | None,
    epoch: int = EPOCH,
    trusted: tuple[str, ...] = (),
    hydrate_sidecars: bool = True,
    event_cache: AsyncMock | None = None,
    thread_id: str = THREAD,
) -> tuple[list[ResolvedVisibleMessage], str]:
    cache = event_cache if event_cache is not None else make_event_cache_mock()
    synthetic_state = (
        _SYNTHETIC_DURABLE_STATES.setdefault(reuse, _SyntheticDurableState())
        if reuse is not None
        else _SyntheticDurableState()
    )
    previous_rows = synthetic_state.rows
    previous_write_seq = synthetic_state.write_seq
    if previous_rows == rows:
        max_write_seq = previous_write_seq
        suffix: list[dict[str, Any]] = []
    elif (
        isinstance(previous_rows, list)
        and len(rows) > len(previous_rows)
        and rows[: len(previous_rows)] == previous_rows
    ):
        suffix = rows[len(previous_rows) :]
        max_write_seq = previous_write_seq + len(suffix)
    else:
        suffix = []
        max_write_seq = previous_write_seq + max(len(rows), 1)
    if reuse is not None:
        synthetic_state.rows = list(rows)
        synthetic_state.write_seq = max_write_seq

    cache.room_membership_epoch.return_value = epoch
    cache.get_thread_cache_state.return_value = ThreadCacheState(
        validated_at=1.0,
        invalidated_at=None,
        invalidation_reason=None,
        room_invalidated_at=None,
        room_invalidation_reason=None,
    )
    cache.get_thread_revision.return_value = ThreadRevision(
        event_count=len(rows),
        max_write_seq=max_write_seq,
        max_thread_write_seq=max_write_seq,
        max_origin_server_ts=max(int(row["origin_server_ts"]) for row in rows),
    )
    cache.get_thread_events.return_value = rows
    cache.get_thread_events_written_between.return_value = suffix
    result, rejection = await matrix_client_module._load_cached_thread_history_if_usable(
        AsyncMock(),
        room_id=ROOM,
        thread_id=thread_id,
        event_cache=cache,
        hydrate_sidecars=hydrate_sidecars,
        trusted_sender_ids=trusted,
        resolution_reuse=reuse,
    )
    assert rejection is None
    assert result is not None
    return list(result), str(result.diagnostics["thread_resolution_reuse"])


def _counting_resolver() -> tuple[Any, list[int]]:
    """Wrap the raw-row resolver to record how many rows each invocation resolves."""
    original = matrix_client_module._resolve_thread_history_from_event_sources_timed
    resolved_row_counts: list[int] = []

    async def wrapper(client: nio.AsyncClient, **kwargs: Any) -> _ResolvedThreadEventSources:  # noqa: ANN401
        resolved_row_counts.append(len(kwargs["event_sources"]))
        return await original(client, **kwargs)

    return wrapper, resolved_row_counts


def _guard_suffix(
    snapshot: ThreadResolutionSnapshot,
    suffix: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    """Apply the append-only guard with a matching synthetic durable revision."""
    return reusable_event_source_suffix(
        snapshot,
        suffix,
        trusted_sender_ids=frozenset(),
        membership_epoch=EPOCH,
        revision=ThreadRevision(
            event_count=snapshot.revision.event_count + len(suffix),
            max_write_seq=snapshot.revision.max_write_seq + len(suffix),
            max_thread_write_seq=snapshot.revision.max_thread_write_seq + len(suffix),
            max_origin_server_ts=max(
                snapshot.revision.max_origin_server_ts,
                *(int(row["origin_server_ts"]) for row in suffix),
            ),
        ),
    )


class TestSnapshotReuse:
    """Reuse and incremental resolution through `_resolve_cached_thread_history`."""

    @pytest.mark.asyncio
    async def test_unchanged_thread_serves_snapshot_without_reresolution(self) -> None:
        """An identical second read serves the snapshot without re-resolving any rows."""
        rows = [_message_row(THREAD, 1000, "root"), _message_row("$m1", 2000, "first reply")]
        reuse = ThreadResolutionReuseCache()
        wrapper, resolved_row_counts = _counting_resolver()

        with patch.object(matrix_client_module, "_resolve_thread_history_from_event_sources_timed", wrapper):
            first, first_kind = await _resolve(rows, reuse=reuse)
            second, second_kind = await _resolve(rows, reuse=reuse)

        assert first_kind == "full"
        assert second_kind == "reuse"
        assert second == first
        assert resolved_row_counts == [2]

    @pytest.mark.asyncio
    async def test_reused_messages_are_caller_owned_copies(self) -> None:
        """Mutating a returned history must not corrupt the stored snapshot."""
        rows = [_message_row(THREAD, 1000, "root"), _message_row("$m1", 2000, "reply")]
        reuse = ThreadResolutionReuseCache()

        first, _ = await _resolve(rows, reuse=reuse)
        first[0].body = "mutated"
        first[0].content["body"] = "mutated"
        first[1].content["m.relates_to"]["event_id"] = "$mutated"

        second, kind = await _resolve(rows, reuse=reuse)
        assert kind == "reuse"
        assert second[0].body == "root"
        assert second[0].content["body"] == "root"
        assert second[1].content["m.relates_to"]["event_id"] == THREAD

    @pytest.mark.asyncio
    async def test_appended_message_resolves_only_suffix(self) -> None:
        """One appended message re-resolves only the suffix and matches a full resolve."""
        rows = [_message_row(THREAD, 1000, "root"), _message_row("$m1", 2000, "first")]
        grown = [*rows, _message_row("$m2", 3000, "second", sender="@agent:localhost")]
        reuse = ThreadResolutionReuseCache()
        wrapper, resolved_row_counts = _counting_resolver()

        with patch.object(matrix_client_module, "_resolve_thread_history_from_event_sources_timed", wrapper):
            await _resolve(rows, reuse=reuse)
            incremental, kind = await _resolve(grown, reuse=reuse)
        full, _ = await _resolve(grown, reuse=None)

        assert kind == "incremental"
        assert incremental == full
        assert [message.event_id for message in incremental] == [THREAD, "$m1", "$m2"]
        assert resolved_row_counts == [2, 1]

    @pytest.mark.asyncio
    async def test_appended_edit_of_existing_message_forces_full_and_applies(self) -> None:
        """An edit targeting a snapshot message falls back to full resolution and applies."""
        rows = [_message_row(THREAD, 1000, "root"), _message_row("$m1", 2000, "draft")]
        grown = [*rows, _edit_row("$e1", 3000, target="$m1", body="final")]
        reuse = ThreadResolutionReuseCache()

        await _resolve(rows, reuse=reuse)
        edited, kind = await _resolve(grown, reuse=reuse)
        full, _ = await _resolve(grown, reuse=None)

        assert kind == "full"
        assert edited == full
        assert [message.body for message in edited] == ["root", "final"]

    @pytest.mark.asyncio
    async def test_redaction_pruned_row_forces_full(self) -> None:
        """A row removed from the durable cache (redaction) invalidates the snapshot."""
        rows = [
            _message_row(THREAD, 1000, "root"),
            _message_row("$m1", 2000, "kept"),
            _message_row("$m2", 3000, "redacted later"),
        ]
        pruned = [rows[0], rows[1]]
        reuse = ThreadResolutionReuseCache()

        await _resolve(rows, reuse=reuse)
        after, kind = await _resolve(pruned, reuse=reuse)

        assert kind == "full"
        assert [message.event_id for message in after] == [THREAD, "$m1"]

    @pytest.mark.asyncio
    async def test_in_place_row_change_forces_full(self) -> None:
        """A changed prefix row breaks prefix equality and forces full resolution."""
        rows = [_message_row(THREAD, 1000, "root"), _message_row("$m1", 2000, "original")]
        changed = [rows[0], _message_row("$m1", 2000, "rewritten")]
        reuse = ThreadResolutionReuseCache()

        await _resolve(rows, reuse=reuse)
        after, kind = await _resolve(changed, reuse=reuse)

        assert kind == "full"
        assert after[1].body == "rewritten"

    @pytest.mark.asyncio
    async def test_membership_epoch_change_forces_full(self) -> None:
        """A membership-epoch change invalidates any prior snapshot."""
        rows = [_message_row(THREAD, 1000, "root")]
        reuse = ThreadResolutionReuseCache()

        await _resolve(rows, reuse=reuse, epoch=1)
        _, kind = await _resolve(rows, reuse=reuse, epoch=2)

        assert kind == "full"

    @pytest.mark.asyncio
    async def test_trusted_sender_change_forces_full(self) -> None:
        """A different trusted-sender set (identity change) invalidates the snapshot."""
        rows = [_message_row(THREAD, 1000, "root")]
        reuse = ThreadResolutionReuseCache()

        await _resolve(rows, reuse=reuse, trusted=("@mindroom_a:localhost",))
        _, kind = await _resolve(rows, reuse=reuse, trusted=("@mindroom_b:localhost",))

        assert kind == "full"

    @pytest.mark.asyncio
    async def test_threads_do_not_share_snapshots(self) -> None:
        """Snapshots are keyed per thread; a different thread never reuses them."""
        rows = [_message_row(THREAD, 1000, "root")]
        reuse = ThreadResolutionReuseCache()

        await _resolve(rows, reuse=reuse, thread_id=THREAD)
        other_rows = [{**rows[0], "event_id": "$other_root"}]
        _, kind = await _resolve(other_rows, reuse=reuse, thread_id="$other_root")

        assert kind == "full"

    @pytest.mark.asyncio
    async def test_snapshot_mode_read_never_uses_or_stores_reuse(self) -> None:
        """Reads with ``hydrate_sidecars=False`` neither use nor populate the reuse cache."""
        rows = [_message_row(THREAD, 1000, "root")]
        reuse = ThreadResolutionReuseCache()

        _, first_kind = await _resolve(rows, reuse=reuse, hydrate_sidecars=False)
        _, second_kind = await _resolve(rows, reuse=reuse, hydrate_sidecars=False)
        _, hydrated_kind = await _resolve(rows, reuse=reuse, hydrate_sidecars=True)

        assert first_kind == "full"
        assert second_kind == "full"
        assert hydrated_kind == "full"

    @pytest.mark.asyncio
    async def test_incomplete_sidecar_hydration_is_never_frozen(self) -> None:
        """Histories with unhydrated sidecar references are never stored for reuse."""
        rows = [_message_row(THREAD, 1000, "root"), _sidecar_row("$m1", 2000)]
        reuse = ThreadResolutionReuseCache()
        event_cache = make_event_cache_mock()
        event_cache.get_mxc_texts.return_value = {}

        _, first_kind = await _resolve(rows, reuse=reuse, event_cache=event_cache)
        _, second_kind = await _resolve(rows, reuse=reuse, event_cache=event_cache)

        assert first_kind == "full"
        assert second_kind == "full"

    @pytest.mark.asyncio
    async def test_changed_sidecar_text_forces_full_resolution(self) -> None:
        """Exact reuse verifies sidecar plaintext dependencies before serving a snapshot."""
        rows = [_message_row(THREAD, 1000, "root"), _sidecar_row("$m1", 2000)]
        reuse = ThreadResolutionReuseCache()
        event_cache = make_event_cache_mock()
        reference = ("$m1", "mxc://server/sidecar-m1")
        event_cache.get_mxc_texts.return_value = {
            reference: json.dumps({"msgtype": "m.text", "body": "first body"}),
        }

        first, _first_kind = await _resolve(rows, reuse=reuse, event_cache=event_cache)
        event_cache.get_mxc_texts.return_value = {
            reference: json.dumps({"msgtype": "m.text", "body": "second body"}),
        }
        second, second_kind = await _resolve(rows, reuse=reuse, event_cache=event_cache)

        assert first[1].body == "first body"
        assert second[1].body == "second body"
        assert second_kind == "full"

    @pytest.mark.asyncio
    async def test_non_growing_revision_skips_preliminary_sidecar_check(self) -> None:
        """A known full fallback should hydrate sidecars only during full resolution."""
        rows = [_message_row(THREAD, 1000, "root"), _sidecar_row("$m1", 2000)]
        changed = [_message_row(THREAD, 1000, "rewritten"), rows[1]]
        reuse = ThreadResolutionReuseCache()
        event_cache = make_event_cache_mock()
        reference = ("$m1", "mxc://server/sidecar-m1")
        event_cache.get_mxc_texts.return_value = {
            reference: json.dumps({"msgtype": "m.text", "body": "sidecar body"}),
        }

        await _resolve(rows, reuse=reuse, event_cache=event_cache)
        event_cache.get_mxc_texts.reset_mock()
        second, second_kind = await _resolve(changed, reuse=reuse, event_cache=event_cache)

        assert second_kind == "full"
        assert second[0].body == "rewritten"
        event_cache.get_mxc_texts.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_resolution_failure_discards_snapshot_and_invalidates(self) -> None:
        """A resolution failure drops the snapshot alongside the durable cache entry."""
        rows = [_message_row(THREAD, 1000, "root")]
        reuse = ThreadResolutionReuseCache()
        event_cache = make_event_cache_mock()

        await _resolve(rows, reuse=reuse, event_cache=event_cache)
        assert reuse.get(ROOM, THREAD) is not None

        with patch.object(
            matrix_client_module,
            "_resolve_thread_history_from_event_sources_timed",
            AsyncMock(side_effect=ValueError("corrupt cached payload")),
        ):
            messages, _sidecar_ms = await matrix_client_module._resolve_cached_thread_history(
                AsyncMock(),
                room_id=ROOM,
                thread_id=THREAD,
                event_cache=event_cache,
                cached_event_sources=rows,
                hydrate_sidecars=True,
                expected_membership_epoch=EPOCH + 1,
                trusted_sender_ids=(),
                resolution_reuse=reuse,
                revision=ThreadRevision(
                    event_count=1,
                    max_write_seq=2,
                    max_thread_write_seq=2,
                    max_origin_server_ts=1000,
                ),
            )

        assert messages is None
        assert reuse.get(ROOM, THREAD) is None
        event_cache.invalidate_thread.assert_awaited_with(ROOM, THREAD)

    @pytest.mark.asyncio
    async def test_concurrent_same_thread_reads_return_identical_histories(self) -> None:
        """Concurrent same-thread reads all match a full resolve, before and after growth."""
        rows = [_message_row(THREAD, 1000, "root")]
        rows += [_message_row(f"$m{index}", 1001 + index, f"body {index}") for index in range(19)]
        reuse = ThreadResolutionReuseCache()

        results = await asyncio.gather(*(_resolve(rows, reuse=reuse) for _ in range(4)))
        full, _ = await _resolve(rows, reuse=None)

        for messages, _kind in results:
            assert messages == full

        grown = [*rows, _message_row("$late", 9000, "late reply")]
        after, _kind = await _resolve(grown, reuse=reuse)
        full_after, _ = await _resolve(grown, reuse=None)
        assert after == full_after

    @pytest.mark.asyncio
    async def test_incremental_reuse_matches_full_resolution_across_growth(self) -> None:
        """Every growth step (replies, streaming edits, redaction) matches a fresh full resolve."""
        reuse = ThreadResolutionReuseCache()
        rows: list[dict[str, Any]] = [_message_row(THREAD, 1000, "root")]
        steps: list[list[dict[str, Any]]] = [list(rows)]
        rows.append(_message_row("$a1", 2000, "thinking...", sender="@agent:localhost"))
        steps.append(list(rows))
        for index in range(3):
            rows.append(
                _edit_row(
                    f"$a1-edit-{index}",
                    2100 + index,
                    target="$a1",
                    body=f"draft {index}",
                    sender="@agent:localhost",
                ),
            )
            steps.append(list(rows))
        rows.append(_message_row("$u2", 3000, "user follow-up"))
        steps.append(list(rows))
        rows = [row for row in rows if row["event_id"] != "$u2"]  # redaction pruning
        steps.append(list(rows))
        rows.append(_message_row("$a2", 4000, "second answer", sender="@agent:localhost"))
        steps.append(list(rows))

        for step_rows in steps:
            with_reuse, _kind = await _resolve(step_rows, reuse=reuse)
            without_reuse, _ = await _resolve(step_rows, reuse=None)
            assert with_reuse == without_reuse


class TestSuffixSafetyGuards:
    """Direct guard checks on `reusable_event_source_suffix`."""

    async def _snapshot(self, rows: list[dict[str, Any]]) -> ThreadResolutionSnapshot:
        reuse = ThreadResolutionReuseCache()
        await _resolve(rows, reuse=reuse)
        snapshot = reuse.get(ROOM, THREAD)
        assert snapshot is not None
        return snapshot

    @pytest.mark.asyncio
    async def test_rejects_non_message_suffix_row(self) -> None:
        """Any non-``m.room.message`` suffix row rejects reuse."""
        rows = [_message_row(THREAD, 1000, "root")]
        snapshot = await self._snapshot(rows)
        redaction = {
            "event_id": "$r1",
            "origin_server_ts": 2000,
            "type": "m.room.redaction",
            "sender": "@user:localhost",
            "redacts": THREAD,
            "content": {},
        }

        suffix = _guard_suffix(snapshot, [redaction])
        assert suffix is None

    @pytest.mark.asyncio
    async def test_rejects_duplicate_and_known_suffix_event_ids(self) -> None:
        """Suffix rows replaying known IDs or duplicating each other reject reuse."""
        rows = [_message_row(THREAD, 1000, "root"), _message_row("$m1", 2000, "reply")]
        snapshot = await self._snapshot(rows)

        replayed_known = _guard_suffix(snapshot, [_message_row("$m1", 3000, "replayed")])
        duplicated_new = _guard_suffix(
            snapshot,
            [_message_row("$m2", 3000, "one"), _message_row("$m2", 3100, "two")],
        )
        assert replayed_known is None
        assert duplicated_new is None

    @pytest.mark.asyncio
    async def test_rejects_suffix_row_reusing_synthesized_edit_target(self) -> None:
        """A suffix row must not reuse the original ID of a prefix edit whose original was missing."""
        rows = [
            _message_row(THREAD, 1000, "root"),
            _edit_row("$e1", 2000, target="$missing_original", body="synthesized"),
        ]
        snapshot = await self._snapshot(rows)

        suffix = _guard_suffix(snapshot, [_message_row("$missing_original", 3000, "late arrival")])
        assert suffix is None

    @pytest.mark.asyncio
    async def test_rejects_bundled_replacement_targeting_prefix_message(self) -> None:
        """A bundled ``m.replace`` aggregation targeting a prefix message rejects reuse."""
        rows = [_message_row(THREAD, 1000, "root"), _message_row("$m1", 2000, "draft")]
        snapshot = await self._snapshot(rows)
        bundled = _message_row("$m2", 3000, "new message")
        bundled["unsigned"] = {
            "m.relations": {
                "m.replace": {
                    "event": {
                        "event_id": "$agg-edit",
                        "type": "m.room.message",
                        "sender": "@user:localhost",
                        "origin_server_ts": 3100,
                        "content": {
                            "msgtype": "m.text",
                            "body": "* rewritten",
                            "m.new_content": {"msgtype": "m.text", "body": "rewritten"},
                            "m.relates_to": {"rel_type": "m.replace", "event_id": "$m1"},
                        },
                    },
                },
            },
        }

        suffix = _guard_suffix(snapshot, [bundled])
        assert suffix is None

    def test_rejects_rows_without_string_event_id(self) -> None:
        """Suffix rows lacking a string event ID reject reuse."""
        snapshot = build_thread_resolution_snapshot(
            event_sources=[],
            messages=[],
            input_order_by_event_id={},
            related_event_id_by_event_id={},
            trusted_sender_ids=frozenset(),
            membership_epoch=EPOCH,
            revision=ThreadRevision(
                event_count=0,
                max_write_seq=0,
                max_thread_write_seq=0,
                max_origin_server_ts=0,
            ),
            sidecar_texts={},
        )
        missing_id = {"origin_server_ts": 1000, "type": "m.room.message", "content": {}}

        suffix = _guard_suffix(snapshot, [missing_id])
        assert suffix is None


class TestReuseCacheBounds:
    """Single-snapshot behavior of the per-bot reuse cache."""

    def _snapshot(self) -> ThreadResolutionSnapshot:
        return build_thread_resolution_snapshot(
            event_sources=[],
            messages=[],
            input_order_by_event_id={},
            related_event_id_by_event_id={},
            trusted_sender_ids=frozenset(),
            membership_epoch=EPOCH,
            revision=ThreadRevision(
                event_count=1,
                max_write_seq=1,
                max_thread_write_seq=1,
                max_origin_server_ts=1,
            ),
            sidecar_texts={},
        )

    def test_new_thread_replaces_previous_snapshot(self) -> None:
        """The cache retains only the bot's latest resolved thread."""
        cache = ThreadResolutionReuseCache()
        cache.store(ROOM, "$t1", self._snapshot())
        cache.store(ROOM, "$t2", self._snapshot())

        assert cache.get(ROOM, "$t1") is None
        assert cache.get(ROOM, "$t2") is not None

    def test_discard_removes_entry(self) -> None:
        """``discard`` removes a stored snapshot."""
        cache = ThreadResolutionReuseCache()
        cache.store(ROOM, "$t1", self._snapshot())
        cache.discard(ROOM, "$t1")
        assert cache.get(ROOM, "$t1") is None


class TestFetchPathIntegration:
    """End-to-end plumbing through the public fetch helpers and a real durable cache."""

    @pytest.mark.asyncio
    async def test_fetch_thread_history_reports_reuse_on_unchanged_thread(self, tmp_path: Path) -> None:
        """A second unchanged fetch through the public helper reports snapshot reuse."""
        from mindroom.matrix.cache.sqlite_event_cache import SqliteEventCache  # noqa: PLC0415

        cache = SqliteEventCache(tmp_path / "event_cache.db")
        await cache.initialize()
        rows = [_message_row(THREAD, 1000, "root"), _message_row("$m1", 2000, "reply")]
        await replace_thread_unconditionally(cache, ROOM, THREAD, rows)

        client = MagicMock()
        client.user_id = "@mindroom_general:localhost"
        client.room_messages = AsyncMock(side_effect=AssertionError("should not refetch fresh cache"))
        reuse = ThreadResolutionReuseCache()
        try:
            first = await fetch_thread_history(client, ROOM, THREAD, event_cache=cache, resolution_reuse=reuse)
            second = await fetch_thread_history(client, ROOM, THREAD, event_cache=cache, resolution_reuse=reuse)
        finally:
            await cache.close()

        assert list(second) == list(first)
        assert first.diagnostics["thread_resolution_reuse"] == "full"
        assert second.diagnostics["thread_resolution_reuse"] == "reuse"

    @pytest.mark.asyncio
    async def test_fetch_thread_history_reads_only_new_rows_for_safe_append(self, tmp_path: Path) -> None:
        """A fresh append is merged from the durable write-sequence delta."""
        from mindroom.matrix.cache.sqlite_event_cache import SqliteEventCache  # noqa: PLC0415

        cache = SqliteEventCache(tmp_path / "event_cache.db")
        await cache.initialize()
        rows = [_message_row(THREAD, 1000, "root"), _message_row("$m1", 2000, "reply")]
        await replace_thread_unconditionally(cache, ROOM, THREAD, rows)

        client = MagicMock()
        client.user_id = "@mindroom_general:localhost"
        client.room_messages = AsyncMock(side_effect=AssertionError("should not refetch fresh cache"))
        reuse = ThreadResolutionReuseCache()
        try:
            first = await fetch_thread_history(client, ROOM, THREAD, event_cache=cache, resolution_reuse=reuse)
            appended = _message_row("$m2", 3000, "new reply")
            await cache.mark_thread_stale(ROOM, THREAD, reason="live_thread_mutation")
            assert await cache.append_event(ROOM, THREAD, appended)
            assert await cache.revalidate_thread_after_incremental_update(ROOM, THREAD)
            with patch.object(
                cache,
                "get_thread_events",
                AsyncMock(side_effect=AssertionError("incremental reuse should not read the full thread")),
            ):
                second = await fetch_thread_history(client, ROOM, THREAD, event_cache=cache, resolution_reuse=reuse)
        finally:
            await cache.close()

        assert [message.event_id for message in first] == [THREAD, "$m1"]
        assert [message.event_id for message in second] == [THREAD, "$m1", "$m2"]
        assert second.diagnostics["thread_resolution_reuse"] == "incremental"

    @pytest.mark.asyncio
    async def test_point_payload_upgrade_forces_full_resolution(self, tmp_path: Path) -> None:
        """A changed lookup payload is detected even when its thread-index row is unchanged."""
        from mindroom.matrix.cache.sqlite_event_cache import SqliteEventCache  # noqa: PLC0415

        cache = SqliteEventCache(tmp_path / "event_cache.db")
        await cache.initialize()
        rows = [_message_row(THREAD, 1000, "root"), _message_row("$m1", 2000, "original")]
        await replace_thread_unconditionally(cache, ROOM, THREAD, rows)

        client = MagicMock()
        client.user_id = "@mindroom_general:localhost"
        client.room_messages = AsyncMock(side_effect=AssertionError("should not refetch fresh cache"))
        reuse = ThreadResolutionReuseCache()
        try:
            await fetch_thread_history(client, ROOM, THREAD, event_cache=cache, resolution_reuse=reuse)
            updated = _message_row("$m1", 2000, "updated")
            await cache.store_event("$m1", ROOM, updated, expected_membership_epoch=0)
            second = await fetch_thread_history(client, ROOM, THREAD, event_cache=cache, resolution_reuse=reuse)
        finally:
            await cache.close()

        assert [message.body for message in second] == ["root", "updated"]
        assert second.diagnostics["thread_resolution_reuse"] == "full"

    @pytest.mark.asyncio
    async def test_thread_reindex_forces_full_resolution_when_payload_revision_is_unchanged(
        self,
        tmp_path: Path,
    ) -> None:
        """A replaced thread index cannot masquerade as an unchanged payload revision."""
        from mindroom.matrix.cache.sqlite_event_cache import SqliteEventCache  # noqa: PLC0415

        cache = SqliteEventCache(tmp_path / "event_cache.db")
        await cache.initialize()
        root = _message_row(THREAD, 1000, "root")
        old_reply = _message_row("$old", 2000, "old")
        shared_reply = _message_row("$shared", 3000, "shared")
        new_reply = _message_row("$new", 2000, "new")
        await cache.store_event("$new", ROOM, new_reply)
        await replace_thread_unconditionally(cache, ROOM, THREAD, [root, old_reply, shared_reply])

        client = MagicMock()
        client.user_id = "@mindroom_general:localhost"
        client.room_messages = AsyncMock(side_effect=AssertionError("should not refetch fresh cache"))
        reuse = ThreadResolutionReuseCache()
        try:
            await fetch_thread_history(client, ROOM, THREAD, event_cache=cache, resolution_reuse=reuse)
            before = await cache.get_thread_revision(ROOM, THREAD)
            opaque_rows = [
                {
                    "event_id": row["event_id"],
                    "origin_server_ts": row["origin_server_ts"],
                    "type": "m.room.encrypted",
                    "sender": row["sender"],
                    "content": {"algorithm": "m.megolm.v1.aes-sha2", "ciphertext": "opaque"},
                }
                for row in (root, new_reply, shared_reply)
            ]
            await replace_thread_unconditionally(cache, ROOM, THREAD, opaque_rows)
            after = await cache.get_thread_revision(ROOM, THREAD)
            second = await fetch_thread_history(
                client,
                ROOM,
                THREAD,
                event_cache=cache,
                resolution_reuse=reuse,
            )
        finally:
            await cache.close()

        assert before is not None
        assert after is not None
        assert before.event_count == after.event_count == 3
        assert before.max_write_seq == after.max_write_seq
        assert after.max_thread_write_seq > before.max_thread_write_seq
        assert [message.event_id for message in second] == [THREAD, "$new", "$shared"]
        assert second.diagnostics["thread_resolution_reuse"] == "full"
