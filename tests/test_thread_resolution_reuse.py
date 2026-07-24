"""Tests for process-local reuse of resolved thread history across durable-cache reads."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import mindroom.matrix.client_thread_history as matrix_client_module
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
    messages, _sidecar_ms, kind = await matrix_client_module._resolve_cached_thread_history(
        AsyncMock(),
        room_id=ROOM,
        thread_id=thread_id,
        event_cache=event_cache if event_cache is not None else make_event_cache_mock(),
        cached_event_sources=rows,
        hydrate_sidecars=hydrate_sidecars,
        expected_membership_epoch=epoch,
        trusted_sender_ids=trusted,
        resolution_reuse=reuse,
    )
    assert messages is not None
    return messages, kind


def _counting_resolver() -> tuple[Any, list[int]]:
    """Wrap the raw-row resolver to record how many rows each invocation resolves."""
    original = matrix_client_module._resolve_thread_history_from_event_sources_timed
    resolved_row_counts: list[int] = []

    async def wrapper(client: nio.AsyncClient, **kwargs: Any) -> _ResolvedThreadEventSources:  # noqa: ANN401
        resolved_row_counts.append(len(kwargs["event_sources"]))
        return await original(client, **kwargs)

    return wrapper, resolved_row_counts


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
        rows = [_message_row(THREAD, 1000, "root")]
        reuse = ThreadResolutionReuseCache()

        first, _ = await _resolve(rows, reuse=reuse)
        first[0].body = "mutated"
        first[0].content["body"] = "mutated"

        second, kind = await _resolve(rows, reuse=reuse)
        assert kind == "reuse"
        assert second[0].body == "root"
        assert second[0].content["body"] == "root"

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
        _, kind = await _resolve(rows, reuse=reuse, thread_id="$other_root")

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
            messages, _sidecar_ms, kind = await matrix_client_module._resolve_cached_thread_history(
                AsyncMock(),
                room_id=ROOM,
                thread_id=THREAD,
                event_cache=event_cache,
                cached_event_sources=rows,
                hydrate_sidecars=True,
                expected_membership_epoch=EPOCH + 1,
                trusted_sender_ids=(),
                resolution_reuse=reuse,
            )

        assert messages is None
        assert kind == "full"
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

        suffix = reusable_event_source_suffix(
            snapshot,
            [*rows, redaction],
            trusted_sender_ids=frozenset(),
            membership_epoch=EPOCH,
        )
        assert suffix is None

    @pytest.mark.asyncio
    async def test_rejects_duplicate_and_known_suffix_event_ids(self) -> None:
        """Suffix rows replaying known IDs or duplicating each other reject reuse."""
        rows = [_message_row(THREAD, 1000, "root"), _message_row("$m1", 2000, "reply")]
        snapshot = await self._snapshot(rows)

        replayed_known = reusable_event_source_suffix(
            snapshot,
            [*rows, _message_row("$m1", 3000, "replayed")],
            trusted_sender_ids=frozenset(),
            membership_epoch=EPOCH,
        )
        duplicated_new = reusable_event_source_suffix(
            snapshot,
            [*rows, _message_row("$m2", 3000, "one"), _message_row("$m2", 3100, "two")],
            trusted_sender_ids=frozenset(),
            membership_epoch=EPOCH,
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

        suffix = reusable_event_source_suffix(
            snapshot,
            [*rows, _message_row("$missing_original", 3000, "late arrival")],
            trusted_sender_ids=frozenset(),
            membership_epoch=EPOCH,
        )
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

        suffix = reusable_event_source_suffix(
            snapshot,
            [*rows, bundled],
            trusted_sender_ids=frozenset(),
            membership_epoch=EPOCH,
        )
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
        )
        missing_id = {"origin_server_ts": 1000, "type": "m.room.message", "content": {}}

        suffix = reusable_event_source_suffix(
            snapshot,
            [missing_id],
            trusted_sender_ids=frozenset(),
            membership_epoch=EPOCH,
        )
        assert suffix is None


class TestReuseCacheBounds:
    """LRU behavior of the per-bot reuse cache."""

    def _snapshot(self) -> ThreadResolutionSnapshot:
        return build_thread_resolution_snapshot(
            event_sources=[],
            messages=[],
            input_order_by_event_id={},
            related_event_id_by_event_id={},
            trusted_sender_ids=frozenset(),
            membership_epoch=EPOCH,
        )

    def test_evicts_least_recently_used_beyond_cap(self) -> None:
        """The cache keeps at most ``max_entries`` snapshots, evicting the least recent."""
        cache = ThreadResolutionReuseCache(max_entries=2)
        cache.store(ROOM, "$t1", self._snapshot())
        cache.store(ROOM, "$t2", self._snapshot())
        assert cache.get(ROOM, "$t1") is not None  # refresh recency
        cache.store(ROOM, "$t3", self._snapshot())

        assert cache.get(ROOM, "$t2") is None
        assert cache.get(ROOM, "$t1") is not None
        assert cache.get(ROOM, "$t3") is not None

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
