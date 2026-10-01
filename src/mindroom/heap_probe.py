"""Opt-in periodic type histogram of the primary process's Python heap.

A long-running primary can grow for hours without saying what it holds, and
external memory profilers cannot attach inside hardened containers. When
``MINDROOM_HEAP_PROBE_INTERVAL_SECONDS`` is set, one background task counts
every object tracked by the garbage collector by type and logs the most common
types, so successive records show which kinds of objects accumulate.

Only GC-tracked containers are counted; strings, bytes, and numbers are not.
Each record also carries glibc's malloc totals and Python's allocated block
count. Resident memory that grows while live malloc bytes stay flat is freed
memory the allocator keeps; growing live bytes with a flat type histogram point
at untracked objects or native libraries.
The walk runs inline on the event loop: ``gc.get_objects()`` and the type count
both run in C while holding the GIL, so a worker thread would not free the loop
any sooner. Each walk therefore pauses the loop about as long as a full garbage
collection and briefly holds a list with one pointer per tracked object.
"""

from __future__ import annotations

import asyncio
import ctypes
import functools
import gc
import math
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

from mindroom.background_tasks import create_background_task
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

_HEAP_PROBE_INTERVAL_ENV = "MINDROOM_HEAP_PROBE_INTERVAL_SECONDS"
# A walk costs about one full collection; shorter intervals would make the probe the main source of pauses.
_MIN_HEAP_PROBE_INTERVAL_SECONDS = 60.0
_TOP_TYPE_COUNT = 25


def _heap_probe_interval_seconds(runtime_paths: RuntimePaths) -> float | None:
    """Return the probe interval, or ``None`` when unset or zero disables the probe."""
    raw = (runtime_paths.env_value(_HEAP_PROBE_INTERVAL_ENV) or "").strip()
    if not raw:
        return None
    try:
        interval_seconds = float(raw)
    except ValueError:
        interval_seconds = math.nan
    if interval_seconds == 0:
        return None
    if not math.isfinite(interval_seconds) or interval_seconds < _MIN_HEAP_PROBE_INTERVAL_SECONDS:
        msg = (
            f"{_HEAP_PROBE_INTERVAL_ENV} must be 0 to disable the heap probe or at least "
            f"{_MIN_HEAP_PROBE_INTERVAL_SECONDS:g} seconds, got {raw!r}"
        )
        raise ValueError(msg)
    return interval_seconds


def _rss_bytes() -> int | None:
    """Return current resident memory where ``/proc`` exposes it, else ``None``."""
    try:
        resident_pages = int(Path("/proc/self/statm").read_bytes().split()[1])
    except (OSError, IndexError, ValueError):
        return None
    return resident_pages * os.sysconf("SC_PAGE_SIZE")


class _MallInfo2(ctypes.Structure):
    """glibc's ``struct mallinfo2``, totals across all malloc arenas."""

    _fields_ = [
        (name, ctypes.c_size_t)
        for name in (
            "arena",
            "ordblks",
            "smblks",
            "hblks",
            "hblkhd",
            "usmblks",
            "fsmblks",
            "uordblks",
            "fordblks",
            "keepcost",
        )
    ]


@functools.cache
def _mallinfo2() -> Callable[[], _MallInfo2] | None:
    """Return glibc's ``mallinfo2`` (2.33+), or ``None`` on other C libraries."""
    function = getattr(ctypes.CDLL(None), "mallinfo2", None)
    if function is None:
        return None
    function.restype = _MallInfo2
    function.argtypes = []
    return function


def _malloc_stats() -> dict[str, int] | None:
    """Return malloc's live, free, and releasable bytes across all arenas, when glibc provides them."""
    mallinfo2 = _mallinfo2()
    if mallinfo2 is None:
        return None
    info = mallinfo2()
    return {
        "arena_bytes": info.arena,
        "mmap_bytes": info.hblkhd,
        "in_use_bytes": info.uordblks,
        "free_bytes": info.fordblks,
        "releasable_bytes": info.keepcost,
    }


def _log_heap_type_probe() -> None:
    """Walk the GC-tracked heap once and log its most common object types."""
    started = time.perf_counter()
    counts = Counter(map(type, gc.get_objects()))
    walk_seconds = time.perf_counter() - started
    logger.info(
        "heap_type_probe",
        tracked_objects=counts.total(),
        top_types=[
            {"type": f"{object_type.__module__}.{object_type.__qualname__}", "count": count}
            for object_type, count in counts.most_common(_TOP_TYPE_COUNT)
        ],
        rss_bytes=_rss_bytes(),
        malloc=_malloc_stats(),
        python_allocated_blocks=sys.getallocatedblocks(),
        walk_seconds=round(walk_seconds, 3),
    )


async def _run_heap_type_probe(
    interval_seconds: float,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Log one heap type histogram after each interval until cancelled."""
    while True:
        await sleep(interval_seconds)
        _log_heap_type_probe()


def start_heap_type_probe(runtime_paths: RuntimePaths) -> asyncio.Task[None] | None:
    """Start the probe task when the env knob opts in; the caller cancels it at shutdown."""
    interval_seconds = _heap_probe_interval_seconds(runtime_paths)
    if interval_seconds is None:
        return None
    logger.info("heap_type_probe_started", interval_seconds=interval_seconds)
    return create_background_task(_run_heap_type_probe(interval_seconds), name="heap_type_probe")
