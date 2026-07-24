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
import statistics
import time
from typing import Any
from unittest.mock import AsyncMock

from mindroom.matrix.client_thread_history import _resolve_cached_thread_history
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


def _appended_user_message(rows: list[dict[str, Any]], turn: int) -> dict[str, Any]:
    return {
        "event_id": f"$turn-{turn}",
        "origin_server_ts": rows[-1]["origin_server_ts"] + 10_000,
        "type": "m.room.message",
        "sender": "@user:localhost",
        "content": {
            "msgtype": "m.text",
            "body": f"follow-up question {turn}",
            "m.relates_to": {"rel_type": "m.thread", "event_id": THREAD},
        },
    }


def _event_cache() -> AsyncMock:
    event_cache = AsyncMock()
    event_cache.get_mxc_texts.return_value = {}
    return event_cache


async def _timed_resolve(
    rows: list[dict[str, Any]],
    *,
    reuse: ThreadResolutionReuseCache | None,
) -> tuple[float, int, str]:
    started = time.perf_counter()
    messages, _sidecar_ms, kind = await _resolve_cached_thread_history(
        AsyncMock(),
        room_id=ROOM,
        thread_id=THREAD,
        event_cache=_event_cache(),
        cached_event_sources=rows,
        expected_membership_epoch=0,
        trusted_sender_ids=TRUSTED_SENDER_IDS,
        resolution_reuse=reuse,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    assert messages is not None
    return elapsed_ms, len(messages), kind


def _report(label: str, timings: list[float], *, raw_rows: int, visible: int, kind: str) -> None:
    print(
        f"{label:<18} raw_rows={raw_rows:>6} visible={visible:>5} kind={kind:<11} "
        f"median={statistics.median(timings):8.1f} ms min={min(timings):8.1f} ms max={max(timings):8.1f} ms",
    )


async def run_benchmark(*, visible_messages: int, edits_per_agent_message: int, turns: int) -> None:
    """Measure cold full resolution, warm snapshot reuse, and warm incremental merges."""
    rows = make_streaming_thread(
        visible_messages=visible_messages,
        edits_per_agent_message=edits_per_agent_message,
        final_body_chars=4000,
    )

    cold_timings: list[float] = []
    for _ in range(turns):
        elapsed_ms, visible, kind = await _timed_resolve(rows, reuse=None)
        cold_timings.append(elapsed_ms)
    _report("cold_full", cold_timings, raw_rows=len(rows), visible=visible, kind=kind)

    reuse = ThreadResolutionReuseCache()
    await _timed_resolve(rows, reuse=reuse)  # populate the snapshot once
    warm_timings: list[float] = []
    for _ in range(turns):
        elapsed_ms, visible, kind = await _timed_resolve(rows, reuse=reuse)
        warm_timings.append(elapsed_ms)
    _report("warm_reuse", warm_timings, raw_rows=len(rows), visible=visible, kind=kind)

    incremental_timings: list[float] = []
    grown = list(rows)
    for turn in range(turns):
        grown = [*grown, _appended_user_message(grown, turn)]
        elapsed_ms, visible, kind = await _timed_resolve(grown, reuse=reuse)
        incremental_timings.append(elapsed_ms)
    _report("warm_incremental", incremental_timings, raw_rows=len(grown), visible=visible, kind=kind)


def main() -> None:
    """Parse benchmark sizing arguments and run the benchmark."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--visible-messages", type=int, default=400)
    parser.add_argument("--edits-per-agent-message", type=int, default=40)
    parser.add_argument("--turns", type=int, default=5)
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
