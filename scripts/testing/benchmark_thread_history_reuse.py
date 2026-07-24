"""Benchmark durable-cache thread resolution with and without process-local reuse.

Generates one synthetic long agent thread where every agent response was streamed via
``m.replace`` edits (the raw-row inflation seen in production), then measures three reads:

- ``cold_full``: full re-parse and re-resolution of every raw row (pre-change behavior).
- ``warm_reuse``: identical rows served from the process-local resolution snapshot.
- ``warm_incremental``: one appended user message resolved as a suffix and merged.

Run with ``uv run python scripts/testing/benchmark_thread_history_reuse.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import statistics
import tempfile
import time
import tracemalloc
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

from mindroom.matrix.cache.sqlite_event_cache import SqliteEventCache
from mindroom.matrix.client_thread_history import _load_cached_thread_history_if_usable
from mindroom.matrix.thread_resolution_reuse import ThreadResolutionReuseCache

ROOM = "!room:localhost"
THREAD = "$root"
TRUSTED_SENDER_IDS = ("@mindroom_agent:localhost",)


def make_streaming_thread(
    *,
    visible_messages: int,
    edits_per_agent_message: int,
    final_body_chars: int,
) -> list[dict[str, Any]]:
    """Build a thread where every other message is an agent response streamed via edits."""
    timestamp = 1_700_000_000_000
    sources: list[dict[str, Any]] = [
        {
            "event_id": THREAD,
            "origin_server_ts": timestamp,
            "type": "m.room.message",
            "sender": "@user:localhost",
            "content": {"msgtype": "m.text", "body": "root message"},
        },
    ]
    for index in range(1, visible_messages):
        timestamp += 10_000
        is_agent = index % 2 == 1
        sender = "@mindroom_agent:localhost" if is_agent else "@user:localhost"
        original_id = f"$msg-{index}"
        body = f"message {index} " + "x" * (final_body_chars if is_agent else 80)
        sources.append(
            {
                "event_id": original_id,
                "origin_server_ts": timestamp,
                "type": "m.room.message",
                "sender": sender,
                "content": {
                    "msgtype": "m.text",
                    "body": body[:200],
                    "m.relates_to": {"rel_type": "m.thread", "event_id": THREAD},
                },
            },
        )
        if not is_agent:
            continue
        for edit_index in range(1, edits_per_agent_message + 1):
            timestamp += 500
            partial = body[: max(200, int(len(body) * edit_index / edits_per_agent_message))]
            sources.append(
                {
                    "event_id": f"$msg-{index}-edit-{edit_index}",
                    "origin_server_ts": timestamp,
                    "type": "m.room.message",
                    "sender": sender,
                    "content": {
                        "msgtype": "m.text",
                        "body": f"* {partial}",
                        "m.new_content": {"msgtype": "m.text", "body": partial},
                        "m.relates_to": {"rel_type": "m.replace", "event_id": original_id},
                    },
                },
            )
    return sources


def _appended_user_message(last_timestamp: int, turn: int) -> dict[str, Any]:
    return {
        "event_id": f"$turn-{turn}",
        "origin_server_ts": last_timestamp + 10_000,
        "type": "m.room.message",
        "sender": "@user:localhost",
        "content": {
            "msgtype": "m.text",
            "body": f"follow-up question {turn}",
            "m.relates_to": {"rel_type": "m.thread", "event_id": THREAD},
        },
    }


async def _timed_resolve(
    event_cache: SqliteEventCache,
    *,
    reuse: ThreadResolutionReuseCache | None,
) -> tuple[float, int, str]:
    started = time.perf_counter()
    result, rejection = await _load_cached_thread_history_if_usable(
        AsyncMock(),
        room_id=ROOM,
        thread_id=THREAD,
        event_cache=event_cache,
        hydrate_sidecars=True,
        trusted_sender_ids=TRUSTED_SENDER_IDS,
        resolution_reuse=reuse,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    assert rejection is None
    assert result is not None
    return elapsed_ms, len(result), str(result.diagnostics["thread_resolution_reuse"])


def _report(label: str, timings: list[float], *, raw_rows: int, visible: int, kind: str) -> None:
    print(
        f"{label:<18} raw_rows={raw_rows:>6} visible={visible:>5} kind={kind:<11} "
        f"median={statistics.median(timings):8.1f} ms min={min(timings):8.1f} ms max={max(timings):8.1f} ms",
    )


async def run_benchmark(*, visible_messages: int, edits_per_agent_message: int, turns: int) -> None:
    """Measure cold full resolution, warm snapshot reuse, and warm incremental merges."""
    with tempfile.TemporaryDirectory() as temp_dir:
        event_cache = SqliteEventCache(Path(temp_dir) / "event_cache.db")
        await event_cache.initialize()
        rows = make_streaming_thread(
            visible_messages=visible_messages,
            edits_per_agent_message=edits_per_agent_message,
            final_body_chars=4000,
        )
        raw_rows = len(rows)
        last_timestamp = int(rows[-1]["origin_server_ts"])
        fetch_started_at = time.time()
        replaced = await event_cache.replace_thread_if_not_newer(
            ROOM,
            THREAD,
            rows,
            expected_membership_epoch=0,
            fetch_started_at=fetch_started_at,
            validated_at=fetch_started_at,
        )
        assert replaced
        del rows
        gc.collect()

        try:
            cold_timings: list[float] = []
            for _ in range(turns):
                elapsed_ms, visible, kind = await _timed_resolve(event_cache, reuse=None)
                cold_timings.append(elapsed_ms)
            _report("cold_full", cold_timings, raw_rows=raw_rows, visible=visible, kind=kind)

            reuse = ThreadResolutionReuseCache()
            tracemalloc.start()
            retained_before, _peak_before = tracemalloc.get_traced_memory()
            await _timed_resolve(event_cache, reuse=reuse)
            gc.collect()
            retained_after, _peak_after = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            print(f"snapshot_retained={(retained_after - retained_before) / (1024 * 1024):.1f} MiB")

            warm_timings: list[float] = []
            for _ in range(turns):
                elapsed_ms, visible, kind = await _timed_resolve(event_cache, reuse=reuse)
                warm_timings.append(elapsed_ms)
            _report("warm_reuse", warm_timings, raw_rows=raw_rows, visible=visible, kind=kind)

            incremental_timings: list[float] = []
            for turn in range(turns):
                appended = _appended_user_message(last_timestamp, turn)
                last_timestamp = int(appended["origin_server_ts"])
                await event_cache.mark_thread_stale(ROOM, THREAD, reason="live_thread_mutation")
                assert await event_cache.append_event(ROOM, THREAD, appended)
                assert await event_cache.revalidate_thread_after_incremental_update(ROOM, THREAD)
                elapsed_ms, visible, kind = await _timed_resolve(event_cache, reuse=reuse)
                incremental_timings.append(elapsed_ms)
            _report(
                "warm_incremental",
                incremental_timings,
                raw_rows=raw_rows + turns,
                visible=visible,
                kind=kind,
            )
        finally:
            await event_cache.close()


def _positive_int(value: str) -> int:
    """Parse a strictly positive benchmark size."""
    parsed = int(value)
    if parsed < 1:
        message = "must be at least 1"
        raise argparse.ArgumentTypeError(message)
    return parsed


def main() -> None:
    """Parse benchmark sizing arguments and run the benchmark."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--visible-messages", type=_positive_int, default=400)
    parser.add_argument("--edits-per-agent-message", type=_positive_int, default=40)
    parser.add_argument("--turns", type=_positive_int, default=5)
    args = parser.parse_args()
    asyncio.run(
        run_benchmark(
            visible_messages=args.visible_messages,
            edits_per_agent_message=args.edits_per_agent_message,
            turns=args.turns,
        ),
    )


if __name__ == "__main__":
    main()
