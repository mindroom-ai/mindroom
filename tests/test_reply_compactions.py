"""Tests for the per-reply compaction recorder and its lifecycle adapter."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Literal, cast

import nio
import pytest

from mindroom.constants import COMPACTIONS_CONTENT_KEY
from mindroom.history.reply_compactions import ReplyCompactionLifecycle, ReplyCompactions
from mindroom.history.types import (
    CompactionLifecycleFailure,
    CompactionLifecycleProgress,
    CompactionLifecycleStart,
    CompactionMode,
    CompactionOutcome,
)
from mindroom.matrix.large_messages import prepare_large_message
from tests.history_helpers import RecordingCompactionLifecycle
from tests.test_large_messages import _UploadClient

if TYPE_CHECKING:
    from mindroom.delivery_gateway import MatrixCompactionLifecycle


def _start(mode: CompactionMode = "auto") -> CompactionLifecycleStart:
    return CompactionLifecycleStart(
        mode=mode,
        session_id="session-1",
        scope="agent:test_agent",
        summary_model="haiku",
        before_tokens=366_804,
        history_budget_tokens=351_792,
        runs_before=24,
        phase="mid_turn",
    )


def _progress(entry_id: str | None, mode: CompactionMode = "auto") -> CompactionLifecycleProgress:
    return CompactionLifecycleProgress(
        mode=mode,
        session_id="session-1",
        scope="agent:test_agent",
        summary_model="haiku",
        before_tokens=366_804,
        after_tokens=9_000,
        history_budget_tokens=351_792,
        runs_before=24,
        compacted_run_count=9,
        runs_remaining=15,
        duration_ms=4_000,
        notice_event_id=entry_id,
    )


def _outcome(entry_id: str | None, mode: CompactionMode = "auto") -> CompactionOutcome:
    return CompactionOutcome(
        mode=mode,
        session_id="session-1",
        scope="agent:test_agent",
        summary="summary",
        summary_model="haiku",
        before_tokens=366_804,
        after_tokens=6_336,
        window_tokens=400_000,
        threshold_tokens=0,
        runs_before=24,
        runs_after=1,
        compacted_run_count=24,
        compacted_at="2026-11-17T00:00:00Z",
        history_budget_tokens=351_792,
        lifecycle_notice_event_id=entry_id,
        duration_ms=8_400,
    )


def _failure(entry_id: str | None, status: Literal["failed", "timeout"] = "timeout") -> CompactionLifecycleFailure:
    return CompactionLifecycleFailure(
        notice_event_id=entry_id,
        mode="auto",
        session_id="session-1",
        scope="agent:test_agent",
        summary_model="haiku-fallback",
        status=status,
        duration_ms=30_000,
        failure_reason="Summary request timed out.",
        history_budget_tokens=351_792,
    )


def _lifecycle(
    recorder: ReplyCompactions,
    notice: RecordingCompactionLifecycle | None = None,
) -> ReplyCompactionLifecycle:
    double = notice if notice is not None else RecordingCompactionLifecycle()
    return ReplyCompactionLifecycle(recorder, cast("MatrixCompactionLifecycle", double))


@pytest.mark.asyncio
async def test_start_progress_success_updates_one_entry_in_place() -> None:
    """One compaction stays one entry, edited by id from running to success."""
    recorder = ReplyCompactions()
    lifecycle = _lifecycle(recorder)

    entry_id = await lifecycle.start(_start())
    assert entry_id is not None
    await lifecycle.progress(_progress(entry_id))
    running = recorder.content()
    assert running is not None
    assert running[0]["status"] == "running"
    assert running[0]["runs_remaining"] == 15
    assert running[0]["after_tokens"] == 9_000
    await lifecycle.complete_success(_outcome(entry_id))

    content = recorder.content()
    assert content is not None
    [entry] = content
    assert entry["id"] == entry_id
    assert entry["status"] == "success"
    assert entry["phase"] == "mid_turn"
    assert entry["mode"] == "auto"
    assert entry["after_tokens"] == 6_336
    assert entry["compacted_run_count"] == 24
    assert entry["duration_ms"] == 8_400
    assert entry["before_tokens"] == 366_804
    assert entry["history_budget_tokens"] == 351_792
    assert entry["runs_before"] == 24
    assert entry["summary_model"] == "haiku"
    assert isinstance(entry["started_at"], int)
    assert "runs_remaining" not in entry
    assert "failure_reason" not in entry


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "timeout"])
async def test_failure_sets_status_and_reason(status: Literal["failed", "timeout"]) -> None:
    """A failed compaction ends in its failure status with the reason and without remaining runs."""
    recorder = ReplyCompactions()
    lifecycle = _lifecycle(recorder)

    entry_id = await lifecycle.start(_start())
    await lifecycle.progress(_progress(entry_id))
    await lifecycle.complete_failure(_failure(entry_id, status))

    content = recorder.content()
    assert content is not None
    [entry] = content
    assert entry["status"] == status
    assert entry["failure_reason"] == "Summary request timed out."
    assert entry["summary_model"] == "haiku-fallback"
    assert entry["duration_ms"] == 30_000
    assert "runs_remaining" not in entry


@pytest.mark.asyncio
async def test_a_verbose_failure_reason_is_capped_so_a_short_answer_edit_still_fits() -> None:
    """A long provider error is cut to a short reason, so every later edit of the reply stays a normal edit."""
    recorder = ReplyCompactions()
    lifecycle = _lifecycle(recorder)
    entry_id = await lifecycle.start(_start())
    await lifecycle.complete_failure(
        replace(_failure(entry_id, "failed"), failure_reason="provider said: " + "x" * 40_000),
    )

    content = recorder.content()
    assert content is not None
    reason = content[0]["failure_reason"]
    assert isinstance(reason, str)
    assert len(reason) == 301
    assert reason.startswith("provider said: xxx")
    assert reason.endswith("…")
    edit = {
        "msgtype": "m.text",
        "body": "* Short answer.",
        "m.new_content": {"msgtype": "m.text", "body": "Short answer.", COMPACTIONS_CONTENT_KEY: content},
        COMPACTIONS_CONTENT_KEY: content,
        "m.relates_to": {"rel_type": "m.replace", "event_id": "$reply"},
    }
    prepared = await prepare_large_message(
        _UploadClient(nio.UploadResponse("mxc://server/sidecar")),
        "!room:server",
        edit,
        room_encrypted=False,
        sidecar_required=True,
    )
    assert prepared["m.new_content"]["body"] == "Short answer."


@pytest.mark.asyncio
async def test_a_short_failure_reason_is_kept_whole() -> None:
    """A reason within the cap is stored unchanged."""
    recorder = ReplyCompactions()
    lifecycle = _lifecycle(recorder)
    entry_id = await lifecycle.start(_start())
    await lifecycle.complete_failure(replace(_failure(entry_id, "failed"), failure_reason="y" * 300))

    content = recorder.content()
    assert content is not None
    assert content[0]["failure_reason"] == "y" * 300


@pytest.mark.asyncio
async def test_manual_goes_to_notice_and_routes_back() -> None:
    """A requested compaction keeps its separate notice, and its later events reach that notice."""
    recorder = ReplyCompactions()
    notice = RecordingCompactionLifecycle(start_event_id="$notice")
    lifecycle = _lifecycle(recorder, notice)

    notice_id = await lifecycle.start(_start(mode="manual"))
    assert notice_id == "$notice"
    await lifecycle.progress(_progress(notice_id, mode="manual"))
    await lifecycle.complete_success(_outcome(notice_id, mode="manual"))

    assert [type(event) for event in notice.events] == [
        CompactionLifecycleStart,
        CompactionLifecycleProgress,
        CompactionOutcome,
    ]
    assert recorder.content() is None


@pytest.mark.asyncio
async def test_two_compactions_completed_out_of_order_stay_in_start_order() -> None:
    """Each entry keeps its own state by id, and the list stays in start order whatever order they finish in."""
    recorder = ReplyCompactions()
    lifecycle = _lifecycle(recorder)

    first_id = await lifecycle.start(_start())
    second_id = await lifecycle.start(_start())
    assert first_id != second_id
    await lifecycle.complete_success(_outcome(second_id))
    running = recorder.content()
    assert running is not None
    assert [(entry["id"], entry["status"]) for entry in running] == [(first_id, "running"), (second_id, "success")]
    await lifecycle.complete_failure(_failure(first_id))

    content = recorder.content()
    assert content is not None
    assert [(entry["id"], entry["status"]) for entry in content] == [(first_id, "timeout"), (second_id, "success")]
    assert "failure_reason" not in content[1]
    assert "after_tokens" not in content[0]


def test_after_tool_read_once_at_start() -> None:
    """The entry anchors at the tool count when it began, not at a later count."""
    recorder = ReplyCompactions()
    count = 2
    recorder.bind(tool_count=lambda: count, changed=lambda: None)

    entry_id = recorder.start(_start())
    count = 5
    recorder.update(entry_id, status="success", after_tokens=6_336)

    content = recorder.content()
    assert content is not None
    assert content[0]["after_tool"] == 2


def test_unbound_recorder_anchors_at_zero() -> None:
    """Without a bound delivery, an entry anchors above every tool call."""
    recorder = ReplyCompactions()
    recorder.start(_start())

    content = recorder.content()
    assert content is not None
    assert content[0]["after_tool"] == 0


def test_unbind_stops_reading_the_delivery() -> None:
    """An unbound recorder no longer reads or nudges the delivery it was bound to."""
    recorder = ReplyCompactions()
    calls: list[None] = []
    recorder.bind(tool_count=lambda: 3, changed=lambda: calls.append(None))
    recorder.unbind()

    recorder.start(_start())

    content = recorder.content()
    assert content is not None
    assert content[0]["after_tool"] == 0
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "failed"])
async def test_a_failing_nudge_keeps_the_entry_and_its_lifecycle(outcome: str) -> None:
    """A delivery nudge that raises loses only that refresh: start returns the id and later updates still land."""
    recorder = ReplyCompactions()
    nudges: list[None] = []

    def failing_nudge() -> None:
        nudges.append(None)
        msg = "delivery queue gone"
        raise RuntimeError(msg)

    recorder.bind(tool_count=lambda: 0, changed=failing_nudge)
    lifecycle = _lifecycle(recorder)

    entry_id = await lifecycle.start(_start())
    assert entry_id is not None
    await lifecycle.progress(_progress(entry_id))
    if outcome == "success":
        await lifecycle.complete_success(_outcome(entry_id))
    else:
        await lifecycle.complete_failure(_failure(entry_id, "failed"))

    content = recorder.content()
    assert content is not None
    [entry] = content
    assert entry["id"] == entry_id
    assert entry["status"] == outcome
    assert len(nudges) == 3
    assert recorder.version == 3


@pytest.mark.asyncio
async def test_changed_called_on_every_change() -> None:
    """Every recorded change nudges the bound delivery and bumps the version."""
    recorder = ReplyCompactions()
    calls: list[None] = []
    recorder.bind(tool_count=lambda: 0, changed=lambda: calls.append(None))
    lifecycle = _lifecycle(recorder)
    assert recorder.version == 0

    entry_id = await lifecycle.start(_start())
    await lifecycle.progress(_progress(entry_id))
    await lifecycle.complete_success(_outcome(entry_id))

    assert len(calls) == 3
    assert recorder.version == 3


def test_content_is_a_copy() -> None:
    """Callers cannot change the recorded entries through what content() returns."""
    recorder = ReplyCompactions()
    assert recorder.content() is None
    recorder.start(_start())

    content = recorder.content()
    assert content is not None
    content[0]["status"] = "tampered"
    content.clear()

    again = recorder.content()
    assert again is not None
    assert again[0]["status"] == "running"


def test_optional_keys_are_omitted_not_null() -> None:
    """An entry without a budget leaves the key out rather than writing null."""
    recorder = ReplyCompactions()
    start = CompactionLifecycleStart(
        mode="auto",
        session_id="session-1",
        scope="agent:test_agent",
        summary_model="haiku",
        before_tokens=1_000,
        history_budget_tokens=None,
        runs_before=3,
    )
    recorder.start(start)

    content = recorder.content()
    assert content is not None
    [entry] = content
    assert "history_budget_tokens" not in entry
    assert entry["phase"] == "pre_turn"
    assert None not in entry.values()
