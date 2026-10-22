"""Opt-in heap type probe behavior."""

from __future__ import annotations

import asyncio
import gc
from pathlib import Path
from types import SimpleNamespace

import pytest
from structlog.testing import capture_logs

from mindroom import heap_probe
from mindroom.constants import RuntimePaths
from mindroom.heap_probe import (
    _HEAP_PROBE_INTERVAL_ENV,
    _HEAP_REFREEZE_INTERVAL_ENV,
    _heap_probe_interval_seconds,
    _heap_refreeze_interval_seconds,
    _log_heap_type_probe,
    _raise_full_collection_threshold,
    _refreeze_runtime_heap,
    _run_heap_refreeze,
    _run_heap_type_probe,
    freeze_startup_heap,
    start_heap_tasks,
)


class _ProbeMarker:
    """One distinctly named type for histogram assertions."""


def _fake_runtime_paths(**env_overrides: str) -> RuntimePaths:
    fake = Path("/var/empty/mindroom-test")
    return RuntimePaths(
        config_path=fake / "config.yaml",
        config_dir=fake,
        env_path=fake / ".env",
        storage_root=fake / "data",
        process_env={**env_overrides},
    )


def _probe_logs(logs: list[dict[str, object]]) -> list[dict[str, object]]:
    return [entry for entry in logs if entry["event"] == "heap_type_probe"]


def test_interval_is_disabled_by_default_and_parses_opt_in_values() -> None:
    """Unset, blank, and zero disable the probe; other values are seconds."""
    assert _heap_probe_interval_seconds(_fake_runtime_paths()) is None
    assert _heap_probe_interval_seconds(_fake_runtime_paths(**{_HEAP_PROBE_INTERVAL_ENV: " "})) is None
    assert _heap_probe_interval_seconds(_fake_runtime_paths(**{_HEAP_PROBE_INTERVAL_ENV: "0"})) is None
    assert _heap_probe_interval_seconds(_fake_runtime_paths(**{_HEAP_PROBE_INTERVAL_ENV: "60"})) == 60.0
    assert _heap_probe_interval_seconds(_fake_runtime_paths(**{_HEAP_PROBE_INTERVAL_ENV: " 900.5 "})) == 900.5


@pytest.mark.parametrize("raw", ["59.9", "1", "-60", "inf", "nan", "hourly"])
def test_interval_rejects_short_negative_nonfinite_or_non_numeric_values(raw: str) -> None:
    """A probe interval that is not 0 or at least one minute fails loudly instead of being clamped."""
    with pytest.raises(ValueError, match=_HEAP_PROBE_INTERVAL_ENV):
        _heap_probe_interval_seconds(_fake_runtime_paths(**{_HEAP_PROBE_INTERVAL_ENV: raw}))


async def _never_ready() -> None:
    await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_start_is_a_no_op_when_both_heap_tasks_are_disabled() -> None:
    """Disabling the refreeze and leaving the probe off starts no task at all."""
    tasks_before = asyncio.all_tasks()

    assert start_heap_tasks(_fake_runtime_paths(**{_HEAP_REFREEZE_INTERVAL_ENV: "0"}), runtime_ready=_never_ready) == []
    assert (
        start_heap_tasks(
            _fake_runtime_paths(**{_HEAP_REFREEZE_INTERVAL_ENV: "0", _HEAP_PROBE_INTERVAL_ENV: "0"}),
            runtime_ready=_never_ready,
        )
        == []
    )

    assert asyncio.all_tasks() == tasks_before


def test_refreeze_interval_defaults_to_one_hour_and_zero_disables_it() -> None:
    """Unset and blank keep the hourly refreeze; zero disables it; other values are seconds."""
    assert _heap_refreeze_interval_seconds(_fake_runtime_paths()) == 3600.0
    assert _heap_refreeze_interval_seconds(_fake_runtime_paths(**{_HEAP_REFREEZE_INTERVAL_ENV: " "})) == 3600.0
    assert _heap_refreeze_interval_seconds(_fake_runtime_paths(**{_HEAP_REFREEZE_INTERVAL_ENV: "0"})) is None
    assert _heap_refreeze_interval_seconds(_fake_runtime_paths(**{_HEAP_REFREEZE_INTERVAL_ENV: " 900.5 "})) == 900.5


@pytest.mark.parametrize("raw", ["59.9", "-60", "inf", "nan", "hourly"])
def test_refreeze_interval_rejects_short_negative_nonfinite_or_non_numeric_values(raw: str) -> None:
    """A refreeze interval that is not 0 or at least one minute fails loudly instead of being clamped."""
    with pytest.raises(ValueError, match=_HEAP_REFREEZE_INTERVAL_ENV):
        _heap_refreeze_interval_seconds(_fake_runtime_paths(**{_HEAP_REFREEZE_INTERVAL_ENV: raw}))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "env",
    [
        {_HEAP_REFREEZE_INTERVAL_ENV: "1", _HEAP_PROBE_INTERVAL_ENV: "60"},
        {_HEAP_REFREEZE_INTERVAL_ENV: "60", _HEAP_PROBE_INTERVAL_ENV: "1"},
    ],
)
async def test_an_invalid_interval_fails_before_any_heap_task_exists(env: dict[str, str]) -> None:
    """Shutdown cannot cancel a task that startup created before failing, so validation comes first."""
    tasks_before = asyncio.all_tasks()

    with pytest.raises(ValueError, match="at least 60 seconds"):
        start_heap_tasks(_fake_runtime_paths(**env), runtime_ready=_never_ready)

    assert asyncio.all_tasks() == tasks_before


def test_refreeze_collects_released_objects_before_freezing_the_survivors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replaced bots must be collectable again, and garbage must be gone before the survivors are frozen."""
    gc_calls: list[str] = []
    frozen = [12_000]

    def unfreeze() -> None:
        gc_calls.append("unfreeze")
        frozen[0] = 0

    def collect() -> int:
        gc_calls.append("collect")
        return 4_321

    def freeze() -> None:
        gc_calls.append("freeze")
        frozen[0] = 3_000_000

    fake_gc = SimpleNamespace(unfreeze=unfreeze, collect=collect, freeze=freeze, get_freeze_count=lambda: frozen[0])
    monkeypatch.setattr(heap_probe, "gc", fake_gc)

    with capture_logs() as logs:
        _refreeze_runtime_heap()

    assert gc_calls == ["unfreeze", "collect", "freeze"]
    [record] = [entry for entry in logs if entry["event"] == "runtime_heap_refrozen"]
    assert record["frozen_objects"] == 3_000_000
    assert record["collected"] == 4_321
    assert record["collect_seconds"] >= 0


@pytest.mark.asyncio
async def test_refreeze_task_waits_for_runtime_readiness_then_refreezes_every_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bots built during startup are frozen as soon as the runtime is ready, then again after each interval."""
    steps: list[object] = []
    ready = asyncio.Event()
    parked = asyncio.Event()
    monkeypatch.setattr(
        heap_probe,
        "gc",
        SimpleNamespace(
            unfreeze=lambda: steps.append("unfreeze"),
            collect=lambda: 0,
            freeze=lambda: steps.append("freeze"),
            get_freeze_count=lambda: 0,
            get_threshold=lambda: (2000, 10, 10),
            set_threshold=lambda *thresholds: steps.append(thresholds),
        ),
    )

    async def _sleep(seconds: float) -> None:
        steps.append(seconds)
        if steps.count(seconds) == 2:
            parked.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(_run_heap_refreeze(600.0, ready=ready.wait, sleep=_sleep))
    await asyncio.sleep(0)
    assert steps == []

    ready.set()
    await asyncio.wait_for(parked.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert steps == ["unfreeze", "freeze", (2000, 10, 100), 600.0, "unfreeze", "freeze", 600.0]


@pytest.mark.parametrize(("configured", "expected"), [(10, 100), (500, 500)])
def test_frozen_heap_raises_the_full_collection_threshold_without_lowering_it(configured: int, expected: int) -> None:
    """Full passes walk everything since the refreeze yet find little garbage, so they run ten times less often."""
    original = gc.get_threshold()
    try:
        gc.set_threshold(original[0], original[1], configured)
        with capture_logs() as logs:
            _raise_full_collection_threshold()
        assert gc.get_threshold() == (original[0], original[1], expected)
    finally:
        gc.set_threshold(*original)
    [record] = [entry for entry in logs if entry["event"] == "gc_full_collection_threshold_raised"]
    assert (record["previous"], record["threshold"]) == (configured, expected)


@pytest.mark.asyncio
async def test_default_start_runs_only_the_named_refreeze_task() -> None:
    """Without a probe interval the primary gets the refreeze task alone, and shutdown can cancel it."""
    with capture_logs() as logs:
        tasks = start_heap_tasks(_fake_runtime_paths(), runtime_ready=_never_ready)
        assert [task.get_name() for task in tasks] == ["heap_refreeze"]
        await asyncio.sleep(0)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert all(task.cancelled() for task in tasks)
    [started] = [entry for entry in logs if entry["event"] == "heap_refreeze_started"]
    assert started["interval_seconds"] == 3600.0
    assert [entry for entry in logs if entry["event"] == "runtime_heap_refrozen"] == []


def test_probe_logs_bounded_type_histogram_and_rss(monkeypatch: pytest.MonkeyPatch) -> None:
    """One probe logs unfrozen and frozen object totals, the most common types, RSS, and walk time."""
    heap = [_ProbeMarker(), _ProbeMarker(), _ProbeMarker(), {}, {}, []]
    monkeypatch.setattr(heap_probe.gc, "get_objects", lambda: list(heap))
    monkeypatch.setattr(heap_probe.gc, "get_freeze_count", lambda: 747_738)
    monkeypatch.setattr(heap_probe, "_TOP_TYPE_COUNT", 2)

    with capture_logs() as logs:
        _log_heap_type_probe()

    [probe] = _probe_logs(logs)
    assert probe["log_level"] == "info"
    assert probe["tracked_objects"] == 6
    assert probe["frozen_objects"] == 747_738
    assert probe["top_types"] == [
        {"type": f"{__name__}._ProbeMarker", "count": 3},
        {"type": "builtins.dict", "count": 2},
    ]
    assert isinstance(probe["walk_seconds"], float)
    assert probe["walk_seconds"] >= 0
    if Path("/proc/self/statm").exists():
        assert isinstance(probe["rss_bytes"], int)
        assert probe["rss_bytes"] > 0
    else:
        assert probe["rss_bytes"] is None


def test_probe_logs_allocator_totals_that_separate_live_from_retained_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    """Live and freed-but-retained malloc bytes tell a leak apart from fragmentation."""
    monkeypatch.setattr(heap_probe.gc, "get_objects", list)

    with capture_logs() as logs:
        _log_heap_type_probe()

    [probe] = _probe_logs(logs)
    assert isinstance(probe["python_allocated_blocks"], int)
    assert probe["python_allocated_blocks"] >= 0
    malloc = probe["malloc"]
    if heap_probe._mallinfo2() is None:
        assert malloc is None
    else:
        assert isinstance(malloc, dict)
        assert set(malloc) == {"arena_bytes", "arena_in_use_bytes", "arena_free_bytes", "mmap_bytes"}
        assert all(isinstance(value, int) and value >= 0 for value in malloc.values())
        assert malloc["arena_in_use_bytes"] > 0


def test_allocator_totals_are_none_without_glibc_mallinfo2(monkeypatch: pytest.MonkeyPatch) -> None:
    """Other C libraries have no mallinfo2, so the probe reports none instead of failing."""
    monkeypatch.setattr(heap_probe, "_mallinfo2", lambda: None)

    assert heap_probe._malloc_stats() is None


@pytest.mark.asyncio
async def test_probe_task_logs_once_per_interval_and_stops_on_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    """The loop waits one interval before each walk and ends cleanly when shutdown cancels it."""
    monkeypatch.setattr(heap_probe.gc, "get_objects", lambda: [_ProbeMarker()])
    sleeps: list[float] = []
    parked = asyncio.Event()

    async def _sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 3:
            parked.set()
            await asyncio.Event().wait()

    with capture_logs() as logs:
        task = asyncio.create_task(_run_heap_type_probe(120.0, sleep=_sleep))
        await asyncio.wait_for(parked.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert task.cancelled()
    assert sleeps == [120.0, 120.0, 120.0]
    assert len(_probe_logs(logs)) == 2


@pytest.mark.asyncio
async def test_start_helper_returns_named_task_that_cancels_cleanly() -> None:
    """An opted-in primary gets one named background task that shutdown can cancel before any walk."""
    with capture_logs() as logs:
        [task] = start_heap_tasks(
            _fake_runtime_paths(**{_HEAP_PROBE_INTERVAL_ENV: "60", _HEAP_REFREEZE_INTERVAL_ENV: "0"}),
            runtime_ready=_never_ready,
        )
        assert task.get_name() == "heap_type_probe"
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert task.cancelled()
    assert _probe_logs(logs) == []
    [started] = [entry for entry in logs if entry["event"] == "heap_type_probe_started"]
    assert started["interval_seconds"] == 60.0


def test_freeze_startup_heap_collects_then_freezes_and_logs_the_frozen_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Garbage is collected before the survivors are frozen, and one record reports both."""
    frozen = [0]
    gc_calls: list[str] = []

    def freeze() -> None:
        gc_calls.append("freeze")
        frozen[0] = 469_281

    fake_gc = SimpleNamespace(
        collect=lambda: gc_calls.append("collect"),
        freeze=freeze,
        get_freeze_count=lambda: frozen[0],
    )
    monkeypatch.setattr(heap_probe, "gc", fake_gc)

    with capture_logs() as logs:
        freeze_startup_heap()

    assert gc_calls == ["collect", "freeze"]
    [record] = [entry for entry in logs if entry["event"] == "startup_heap_frozen"]
    assert record["frozen_objects"] == 469_281
    assert record["collect_seconds"] >= 0
