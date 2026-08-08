"""Measure the event journal before deciding to cut production over to it.

The plan this implements requires numbers, not impressions, before the old
owners are deleted: how fast admission and bounded reads are, whether the
single-writer design actually eliminates SQLite lock failures under load, and
whether reads stay indexed as a conversation grows.

Run it with::

    uv run python tests/manual/event_journal_measurements.py

Timings depend on the host, so the output records the host alongside them.
"""

from __future__ import annotations

import asyncio
import json
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from mindroom.event_journal import (
    EventClass,
    EventJournalStore,
    EventKind,
    InboundEvent,
    ProjectedEvent,
)
from mindroom.event_journal.sqlite_backend import _BUSY_TIMEOUT_MILLISECONDS

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.event_journal import PrincipalStore

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ROOM = "!measure:example.org"
SENDER = "@alice:example.org"
PRINCIPAL = "measurement"

# The conversation size the plan's read targets are stated against. Deep
# paging over a small table would measure the page cache, not the index.
CONVERSATION_SIZE = 100_000
_ADMISSION_SAMPLE = 2_000

# The plan's initial targets, recorded here so a regression is visible rather
# than merely slower.
TARGET_ADMISSION_P95_MS = 50.0
TARGET_READ_P95_MS = 50.0
TARGET_WRITER_QUEUE_P95_MS = 100.0


@dataclass
class Report:
    """Everything the feasibility decision needs."""

    host: str
    measurements: dict[str, float | int | str | bool] = field(default_factory=dict)

    def record(self, name: str, value: float | str | bool) -> None:
        """Record one measurement."""
        self.measurements[name] = value
        print(f"{name}: {value}")


def _event(index: int, *, thread_id: str | None = None) -> tuple[InboundEvent, ProjectedEvent]:
    event_id = f"$measure{index:07d}"
    content: dict[str, object] = {"msgtype": "m.text", "body": f"message {index}"}
    inbound = InboundEvent(
        event_id=event_id,
        room_id=ROOM,
        thread_id=thread_id,
        kind=EventKind.MESSAGE,
        event_class=EventClass.ACTIONABLE,
        sender=SENDER,
        origin_server_ts=1_000_000 + index,
        source={"event_id": event_id, "content": content},
    )
    projected = ProjectedEvent(
        event_id=event_id,
        room_id=ROOM,
        thread_id=thread_id,
        sender=SENDER,
        origin_server_ts=1_000_000 + index,
        content=content,
        replaces_event_id=None,
        redacts_event_id=None,
    )
    return inbound, projected


def _p95(samples: Sequence[float]) -> float:
    ordered = sorted(samples)
    return round(ordered[min(int(len(ordered) * 0.95), len(ordered) - 1)] * 1000, 3)


async def measure_admission(store: PrincipalStore, report: Report, *, count: int = _ADMISSION_SAMPLE) -> None:
    """Time durable admission, which is on the sync callback's critical path."""
    samples: list[float] = []
    for index in range(count):
        inbound, projected = _event(index)
        started = time.perf_counter()
        await store.admit(inbound, projected)
        samples.append(time.perf_counter() - started)
    report.record("admission_p95_ms", _p95(samples))
    report.record("admission_mean_ms", round(statistics.mean(samples) * 1000, 3))
    report.record("admission_target_met", _p95(samples) < TARGET_ADMISSION_P95_MS)


async def grow_conversation(store: PrincipalStore, report: Report, *, total: int = CONVERSATION_SIZE) -> None:
    """Fill the room conversation to the scale the read targets are about.

    Reads are measured against this, not against the admission sample. An
    index that looks fine over a couple of thousand rows says nothing about a
    room a bot has actually lived in, which is the case deep paging exists for.
    """
    started = time.perf_counter()
    for index in range(_ADMISSION_SAMPLE, total):
        inbound, projected = _event(index)
        await store.admit(inbound, projected)
    report.record("conversation_messages", total)
    report.record("conversation_seed_seconds", round(time.perf_counter() - started, 1))


async def measure_bounded_reads(store: PrincipalStore, report: Report, *, pages: int = 200) -> None:
    """Time bounded reads over a large conversation."""
    samples: list[float] = []
    for _ in range(pages):
        started = time.perf_counter()
        await store.read_conversation(room_id=ROOM, thread_id=None, limit=50)
        samples.append(time.perf_counter() - started)
    report.record("bounded_read_p95_ms", _p95(samples))
    report.record("bounded_read_target_met", _p95(samples) < TARGET_READ_P95_MS)


async def measure_deep_paging(store: PrincipalStore, report: Report) -> None:
    """Confirm a read stays bounded when it is deep into a conversation.

    An index that only helps the newest page is not an index; it is a scan with
    a fast first result.
    """
    cursor = None
    pages = 0
    samples: list[float] = []
    # Walk the whole conversation rather than a prefix of it: the point is
    # what the last page costs, not the first.
    while pages < CONVERSATION_SIZE // 50 + 1:
        started = time.perf_counter()
        page = await store.read_conversation(room_id=ROOM, thread_id=None, limit=50, before=cursor)
        samples.append(time.perf_counter() - started)
        pages += 1
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    report.record("deep_page_count", pages)
    report.record("deep_page_p95_ms", _p95(samples))
    report.record("deep_page_last_ms", round(samples[-1] * 1000, 3))


async def measure_concurrency(store: PrincipalStore, report: Report, *, conversations: int = 50) -> None:
    """Run the stress case that used to produce ``database is locked``."""
    failures = 0
    queue_waits: list[float] = []

    async def conversation(index: int) -> None:
        nonlocal failures
        for step in range(20):
            inbound, projected = _event(1_000_000 + index * 100 + step, thread_id=f"$thread{index}")
            started = time.perf_counter()
            try:
                await store.admit(inbound, projected)
            except Exception as error:
                failures += 1
                print(f"  lock failure: {error}")
            queue_waits.append(time.perf_counter() - started)

    started = time.perf_counter()
    await asyncio.gather(*(conversation(index) for index in range(conversations)))
    elapsed = time.perf_counter() - started

    report.record("concurrent_conversations", conversations)
    report.record("concurrent_admissions", conversations * 20)
    report.record("sqlite_lock_failures", failures)
    report.record("writer_queue_p95_ms", _p95(queue_waits))
    report.record("writer_queue_target_met", _p95(queue_waits) < TARGET_WRITER_QUEUE_P95_MS)
    report.record("concurrent_throughput_per_second", round(conversations * 20 / elapsed, 1))
    report.record("sqlite_busy_timeout_ms", _BUSY_TIMEOUT_MILLISECONDS)


def measure_query_plans(database_path: Path, report: Report) -> None:
    """Confirm the conversation read uses its index rather than scanning."""
    import sqlite3  # noqa: PLC0415 - only needed for this inspection

    with sqlite3.connect(database_path) as db:
        plan = db.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT logical_event_id FROM visible_messages
            WHERE principal_id = ? AND room_id = ? AND thread_id = ?
              AND (created_ts < ? OR (created_ts = ? AND logical_event_id < ?))
            ORDER BY created_ts DESC, logical_event_id DESC
            LIMIT 50
            """,
            (PRINCIPAL, ROOM, "", 0, 0, ""),
        ).fetchall()
        pending_plan = db.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT event_id FROM journal_events
            WHERE principal_id = ? AND state = 'pending' ORDER BY receipt_order LIMIT 128
            """,
            (PRINCIPAL,),
        ).fetchall()
    detail = " | ".join(row[-1] for row in plan)
    pending_detail = " | ".join(row[-1] for row in pending_plan)
    report.record("conversation_read_plan", detail)
    report.record("conversation_read_uses_index", "visible_messages_page" in detail)
    report.record("pending_replay_plan", pending_detail)
    report.record("pending_replay_uses_index", "journal_events_pending" in pending_detail)


async def run(report: Report) -> None:
    """Run every measurement against a fresh SQLite store."""
    with tempfile.TemporaryDirectory(prefix="journal-measure-") as directory:
        database_path = Path(directory) / "journal.db"
        store_root = EventJournalStore.open_sqlite(database_path)
        store = store_root.principal(PRINCIPAL)
        try:
            await measure_admission(store, report)
            await grow_conversation(store, report)
            await measure_bounded_reads(store, report)
            await measure_deep_paging(store, report)
            await measure_concurrency(store, report)
            measure_query_plans(database_path, report)
            report.record("database_bytes", database_path.stat().st_size)
            report.record(
                "database_bytes_per_message",
                round(database_path.stat().st_size / (CONVERSATION_SIZE + 50 * 20), 1),
            )
        finally:
            await store_root.close()


def main() -> int:
    """Measure and print the feasibility report."""
    commit = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
    ).stdout.strip()
    report = Report(
        host=f"{platform.platform()} python-{platform.python_version()} commit-{commit}",
    )
    print(f"host: {report.host}\n")
    asyncio.run(run(report))
    print("\n--- machine readable ---")
    print(json.dumps({"host": report.host, **report.measurements}, indent=2, sort_keys=True))
    targets_met = all(value for key, value in report.measurements.items() if key.endswith("_target_met"))
    indexed = all(value for key, value in report.measurements.items() if key.endswith("_uses_index"))
    no_locks = report.measurements.get("sqlite_lock_failures") == 0
    return 0 if targets_met and indexed and no_locks else 1


if __name__ == "__main__":
    sys.exit(main())
