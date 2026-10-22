"""Heap freezes and an opt-in periodic type histogram of the primary process's Python heap.

Call audio shares the event loop with full garbage collections, so the primary
freezes its import-time heap once before any bot exists. A full collection
walks every live object, so a large runtime pauses for seconds. Once the
runtime is ready, and then every ``MINDROOM_HEAP_REFREEZE_INTERVAL_SECONDS``,
the primary unfreezes, collects once, and freezes the survivors again. Routine
full collections then only walk objects created since the last refreeze, and
bots a config reload replaced are collected at the next refreeze instead of
staying frozen. After the first refreeze the primary also raises CPython's
full-collection threshold, because each routine full collection walks the whole
unfrozen heap to find little garbage; how much rarer they get depends on the
workload.

A long-running primary can grow for hours without saying what it holds, and
external memory profilers cannot attach inside hardened containers. When
``MINDROOM_HEAP_PROBE_INTERVAL_SECONDS`` is set, one background task counts
every object tracked by the garbage collector by type and logs the most common
types, so successive records show which kinds of objects accumulate.

Only GC-tracked containers are counted; strings, bytes, and numbers are not.
Frozen objects are left out of the histogram and only counted, so the types
show what the runtime created since the last freeze; disable the refreeze to
see accumulation over the whole process lifetime.
Each record also carries glibc's malloc totals and Python's allocated block
count. Live malloc bytes are ``arena_in_use_bytes`` plus ``mmap_bytes``;
Python's small-object arenas come straight from mmap, so malloc does not count
them and ``python_allocated_blocks`` covers them instead. Resident memory that
grows while both stay flat suggests freed memory the allocator keeps; growth in
either with a flat type histogram suggests untracked objects or native
libraries.
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
_HEAP_REFREEZE_INTERVAL_ENV = "MINDROOM_HEAP_REFREEZE_INTERVAL_SECONDS"
_DEFAULT_HEAP_REFREEZE_INTERVAL_SECONDS = 3600.0
# A probe walk or a refreeze costs about one full collection; shorter intervals would make it the main source of pauses.
_MIN_HEAP_TASK_INTERVAL_SECONDS = 60.0
# CPython runs a full collection on the 11th middle pass once the objects promoted since the last full pass
# exceed a quarter of the old generation as counted at that pass. gc.freeze() keeps that count, so frozen objects
# raise the bar only until the first full pass after a refreeze; from then on it covers objects since the refreeze.
# Turn state that outlives two young passes and is later freed by refcount crosses it quickly, so under load a
# full pass ran every few seconds, walked every object since the refreeze and found little garbage.
_FROZEN_HEAP_FULL_COLLECTION_THRESHOLD = 100
_TOP_TYPE_COUNT = 25


def _interval_seconds(runtime_paths: RuntimePaths, env_name: str, *, default: float | None, task: str) -> float | None:
    """Return one heap task interval, ``default`` when unset, or ``None`` when zero disables the task."""
    raw = (runtime_paths.env_value(env_name) or "").strip()
    if not raw:
        return default
    try:
        interval_seconds = float(raw)
    except ValueError:
        interval_seconds = math.nan
    if interval_seconds == 0:
        return None
    if not math.isfinite(interval_seconds) or interval_seconds < _MIN_HEAP_TASK_INTERVAL_SECONDS:
        msg = (
            f"{env_name} must be 0 to disable the {task} or at least "
            f"{_MIN_HEAP_TASK_INTERVAL_SECONDS:g} seconds, got {raw!r}"
        )
        raise ValueError(msg)
    return interval_seconds


def _heap_probe_interval_seconds(runtime_paths: RuntimePaths) -> float | None:
    """Return the probe interval, or ``None`` when unset or zero disables the probe."""
    return _interval_seconds(runtime_paths, _HEAP_PROBE_INTERVAL_ENV, default=None, task="heap probe")


def _heap_refreeze_interval_seconds(runtime_paths: RuntimePaths) -> float | None:
    """Return the refreeze interval, hourly by default, or ``None`` when zero disables refreezing."""
    return _interval_seconds(
        runtime_paths,
        _HEAP_REFREEZE_INTERVAL_ENV,
        default=_DEFAULT_HEAP_REFREEZE_INTERVAL_SECONDS,
        task="heap refreeze",
    )


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
    """Return malloc's arena and direct-mmap byte totals across all arenas, when glibc provides them."""
    mallinfo2 = _mallinfo2()
    if mallinfo2 is None:
        return None
    info = mallinfo2()
    return {
        "arena_bytes": info.arena,
        "arena_in_use_bytes": info.uordblks,
        "arena_free_bytes": info.fordblks,
        "mmap_bytes": info.hblkhd,
    }


def freeze_startup_heap() -> None:
    """Collect garbage, then exclude every surviving object from later collections.

    Call audio is pumped on the event loop, and a full collection that walks every
    imported module pauses agent speech for hundreds of milliseconds. Run this
    before any bot exists, so runtime objects stay collectable until the runtime
    heap refreeze takes them over.
    """
    started = time.perf_counter()
    gc.collect()
    collect_seconds = time.perf_counter() - started
    gc.freeze()
    logger.info("startup_heap_frozen", frozen_objects=gc.get_freeze_count(), collect_seconds=round(collect_seconds, 3))


def _log_heap_type_probe() -> None:
    """Walk the GC-tracked heap once and log its most common object types."""
    started = time.perf_counter()
    counts = Counter(map(type, gc.get_objects()))
    walk_seconds = time.perf_counter() - started
    logger.info(
        "heap_type_probe",
        tracked_objects=counts.total(),
        frozen_objects=gc.get_freeze_count(),
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


def _refreeze_runtime_heap() -> None:
    """Collect what the frozen heap released, then freeze the survivors again.

    Unfreezing first makes objects frozen earlier collectable, such as bots a
    config reload replaced; collecting before freezing keeps garbage out of
    the frozen set. Objects still alive here that later become cyclic garbage,
    such as a reply in flight, wait for the next refreeze together with
    everything they reference. The collection walks the whole heap once,
    including the import-time heap frozen at startup, so it pauses the loop a
    little longer than one full collection did before.
    """
    started = time.perf_counter()
    gc.unfreeze()
    collected = gc.collect()
    collect_seconds = time.perf_counter() - started
    gc.freeze()
    logger.info(
        "runtime_heap_refrozen",
        frozen_objects=gc.get_freeze_count(),
        collected=collected,
        collect_seconds=round(collect_seconds, 3),
    )


async def _run_heap_refreeze(
    interval_seconds: float,
    *,
    ready: Callable[[], Awaitable[object]],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Refreeze once the runtime is ready, then after each interval until cancelled."""
    await ready()
    _refreeze_runtime_heap()
    _raise_full_collection_threshold()
    while True:
        await sleep(interval_seconds)
        _refreeze_runtime_heap()


def _raise_full_collection_threshold() -> None:
    """Make routine full collections rarer once the runtime heap is frozen, never lowering a higher setting."""
    young, middle, full = gc.get_threshold()
    if full >= _FROZEN_HEAP_FULL_COLLECTION_THRESHOLD:
        return
    gc.set_threshold(young, middle, _FROZEN_HEAP_FULL_COLLECTION_THRESHOLD)
    logger.info("gc_full_collection_threshold_raised", previous=full, threshold=_FROZEN_HEAP_FULL_COLLECTION_THRESHOLD)


def start_heap_tasks(
    runtime_paths: RuntimePaths,
    *,
    runtime_ready: Callable[[], Awaitable[object]],
) -> list[asyncio.Task[None]]:
    """Start the heap refreeze and the opt-in probe; the caller cancels them at shutdown.

    Both intervals are validated before either task exists, so an invalid value
    fails startup without leaving a task that shutdown would never cancel.
    """
    refreeze_interval_seconds = _heap_refreeze_interval_seconds(runtime_paths)
    probe_interval_seconds = _heap_probe_interval_seconds(runtime_paths)
    tasks: list[asyncio.Task[None]] = []
    if refreeze_interval_seconds is not None:
        logger.info("heap_refreeze_started", interval_seconds=refreeze_interval_seconds)
        tasks.append(
            create_background_task(
                _run_heap_refreeze(refreeze_interval_seconds, ready=runtime_ready),
                name="heap_refreeze",
            ),
        )
    if probe_interval_seconds is not None:
        logger.info("heap_type_probe_started", interval_seconds=probe_interval_seconds)
        tasks.append(create_background_task(_run_heap_type_probe(probe_interval_seconds), name="heap_type_probe"))
    return tasks
