"""Opt-in heap type probe behavior."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from mindroom import handled_turns, heap_probe
from mindroom.constants import RuntimePaths
from mindroom.heap_probe import (
    _HEAP_PROBE_INTERVAL_ENV,
    _heap_probe_interval_seconds,
    _log_heap_type_probe,
    _run_heap_type_probe,
    start_heap_type_probe,
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


@pytest.mark.asyncio
async def test_start_is_a_no_op_when_disabled() -> None:
    """The default configuration starts no task at all."""
    tasks_before = asyncio.all_tasks()

    assert start_heap_type_probe(_fake_runtime_paths()) is None
    assert start_heap_type_probe(_fake_runtime_paths(**{_HEAP_PROBE_INTERVAL_ENV: "0"})) is None

    assert asyncio.all_tasks() == tasks_before


def test_probe_logs_bounded_type_histogram_rss_and_ledger_sizes(monkeypatch: pytest.MonkeyPatch) -> None:
    """One probe logs total tracked objects, the most common types, RSS, walk time, and ledger sizes."""
    heap = [_ProbeMarker(), _ProbeMarker(), _ProbeMarker(), {}, {}, []]
    monkeypatch.setattr(heap_probe.gc, "get_objects", lambda: list(heap))
    monkeypatch.setattr(heap_probe, "_TOP_TYPE_COUNT", 2)
    ledger_state = handled_turns._LedgerState()
    ledger_state.responses.update({"$first": object(), "$second": object()})
    monkeypatch.setattr(handled_turns, "_LEDGER_STATES", {"store\x00general": ledger_state})

    with capture_logs() as logs:
        _log_heap_type_probe()

    [probe] = _probe_logs(logs)
    assert probe["log_level"] == "info"
    assert probe["tracked_objects"] == 6
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
    assert probe["handled_turn_ledger_states"] == 1
    assert probe["handled_turn_ledger_responses"] == 2


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
        task = start_heap_type_probe(_fake_runtime_paths(**{_HEAP_PROBE_INTERVAL_ENV: "60"}))
        assert task is not None
        assert task.get_name() == "heap_type_probe"
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert task.cancelled()
    assert _probe_logs(logs) == []
    [started] = [entry for entry in logs if entry["event"] == "heap_type_probe_started"]
    assert started["interval_seconds"] == 60.0
