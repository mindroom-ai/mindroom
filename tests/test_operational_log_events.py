"""Operators alert on these log events, so their names and fields change only together with their docs page."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from mindroom.agent_loop.run import run_agent
from mindroom.event_loop_stall import EventLoopStallDetector
from mindroom.matrix.sync_diagnostics import SyncStallDiagnostics
from tests.agent_loop.helpers import collect, context, spec, toolkit
from tests.fakes.scripted_model import ScriptedModel, calls, tool_call

_DOCS_PAGE = Path(__file__).resolve().parents[1] / "docs" / "deployment" / "operational-log-events.md"

# Log-based metrics and alerts match these event names and extract these fields.
# A rename silently disables them, so update docs/deployment/operational-log-events.md in the same change.
_STABLE_EVENTS = {
    "event_loop_scheduler_lag_summary": {
        "sample_count",
        "p50_ms",
        "p95_ms",
        "p99_ms",
        "max_ms",
        "max_lag_scheduled_at",
        "max_lag_observed_at",
        "gc_collections_total",
    },
    "matrix_sync_stall_diagnostics": {"agent", "no_progress_seconds", "sync_age", "generation", "snapshots"},
    "tool_call_limit_reached": {"entity", "budget", "model_requests"},
}


def _emitted_fields(logs: list[dict[str, object]], event: str) -> set[str]:
    [entry] = [entry for entry in logs if entry["event"] == event]
    return set(entry) - {"event", "log_level"}


def test_docs_page_documents_exactly_the_stable_events_and_fields() -> None:
    """Each stable event has a docs section whose field table lists exactly its pinned fields."""
    sections = re.split(r"^### `([a-z_]+)`$", _DOCS_PAGE.read_text(encoding="utf-8"), flags=re.MULTILINE)
    documented = {
        name: set(re.findall(r"^\| `([a-z0-9_]+)` \|", body, flags=re.MULTILINE))
        for name, body in zip(sections[1::2], sections[2::2], strict=True)
    }
    assert documented == _STABLE_EVENTS


def test_scheduler_lag_summary_keeps_its_name_and_fields() -> None:
    """A completed 60-second window logs the pinned summary."""
    detector = EventLoopStallDetector()
    detector._scheduler_lag_samples.append((0.002, 0.0, 0.0))

    with capture_logs() as logs:
        detector._report_scheduler_lag(60.0)

    assert (
        _emitted_fields(logs, "event_loop_scheduler_lag_summary") == _STABLE_EVENTS["event_loop_scheduler_lag_summary"]
    )


@pytest.mark.asyncio
async def test_matrix_sync_stall_diagnostics_keeps_its_name_and_fields() -> None:
    """Ninety seconds without receive-loop progress logs the pinned diagnostics."""
    diagnostics = SyncStallDiagnostics("router", last_progress_monotonic=0.0, generation=None)

    with capture_logs() as logs:
        diagnostics.observe(now=90.0, sync_age=None, generation=None)

    assert _emitted_fields(logs, "matrix_sync_stall_diagnostics") == _STABLE_EVENTS["matrix_sync_stall_diagnostics"]


@pytest.mark.asyncio
async def test_tool_call_limit_reached_keeps_its_name_and_fields() -> None:
    """The model request refused after budget plus two requests logs the pinned warning."""

    def probe() -> str:
        """Record one call."""
        return "probed"

    model = ScriptedModel([calls(tool_call("probe", f"call_{index}")) for index in range(3)])

    with capture_logs() as logs:
        await collect(run_agent(spec(model, toolkit(probe), max_tool_calls_per_turn=0), [], ctx=context()))

    assert len(model.requests) == 2
    assert _emitted_fields(logs, "tool_call_limit_reached") == _STABLE_EVENTS["tool_call_limit_reached"]
