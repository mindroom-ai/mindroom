"""Tests for decorator-based timing instrumentation."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

import mindroom.timing as timing_module
from mindroom.timing import DispatchPipelineTiming, emit_timing_event, timed, timing_enabled, timing_scope

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def _mock_timing_logger(monkeypatch: pytest.MonkeyPatch) -> Mock:
    logger = Mock()
    monkeypatch.setattr(timing_module, "logger", logger)
    return logger


def _assert_timing_logged(logger: Mock, label: str, *, scope: str | None = None) -> None:
    logger.info.assert_called_once()
    assert logger.info.call_args.args == ("timing_elapsed",)
    assert logger.info.call_args.kwargs["label"] == label
    assert isinstance(logger.info.call_args.kwargs["duration_ms"], float)
    assert logger.info.call_args.kwargs["duration_ms"] >= 0
    if scope is None:
        assert "timing_scope" not in logger.info.call_args.kwargs
    else:
        assert logger.info.call_args.kwargs["timing_scope"] == scope


def test_timed_sync_logs_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sync functions should emit a timing log when timing is enabled."""
    monkeypatch.setenv("MINDROOM_TIMING", "1")
    logger = _mock_timing_logger(monkeypatch)

    @timed("sync_label")
    def add(left: int, right: int) -> int:
        return left + right

    assert add(2, 3) == 5
    _assert_timing_logged(logger, "sync_label")


def test_timed_sync_logs_on_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sync functions should still emit a timing log when they raise."""
    monkeypatch.setenv("MINDROOM_TIMING", "1")
    logger = _mock_timing_logger(monkeypatch)

    @timed("sync_error_label")
    def fail() -> None:
        msg = "boom"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="boom"):
        fail()

    _assert_timing_logged(logger, "sync_error_label")


@pytest.mark.asyncio
async def test_timed_async_logs_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Async functions should emit a timing log when timing is enabled."""
    monkeypatch.setenv("MINDROOM_TIMING", "1")
    logger = _mock_timing_logger(monkeypatch)

    @timed("async_label")
    async def compute() -> str:
        return "done"

    assert await compute() == "done"
    _assert_timing_logged(logger, "async_label")


@pytest.mark.asyncio
async def test_timed_async_logs_on_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Async functions should still emit a timing log when they raise."""
    monkeypatch.setenv("MINDROOM_TIMING", "1")
    logger = _mock_timing_logger(monkeypatch)

    @timed("async_error_label")
    async def fail() -> None:
        msg = "boom"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="boom"):
        await fail()

    _assert_timing_logged(logger, "async_error_label")


@pytest.mark.asyncio
async def test_timed_async_generator_logs_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Async generators should emit a timing log after iteration completes."""
    monkeypatch.setenv("MINDROOM_TIMING", "1")
    logger = _mock_timing_logger(monkeypatch)

    @timed("async_generator_label")
    async def generate() -> AsyncIterator[str]:
        yield "a"
        yield "b"

    assert [item async for item in generate()] == ["a", "b"]
    _assert_timing_logged(logger, "async_generator_label")


@pytest.mark.asyncio
async def test_timed_async_generator_logs_on_early_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Async generators should emit a timing log when iteration stops early."""
    monkeypatch.setenv("MINDROOM_TIMING", "1")
    logger = _mock_timing_logger(monkeypatch)

    @timed("async_generator_early_close_label")
    async def generate() -> AsyncIterator[str]:
        yield "a"
        yield "b"

    generator = generate()
    assert await anext(generator) == "a"
    await generator.aclose()

    _assert_timing_logged(logger, "async_generator_early_close_label")


@pytest.mark.asyncio
async def test_timed_async_generator_uses_explicit_scope_on_cross_task_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit timing_scope kwargs should survive async-generator close in another task."""
    monkeypatch.setenv("MINDROOM_TIMING", "1")
    logger = _mock_timing_logger(monkeypatch)

    @timed("async_generator_cross_task_close_label")
    async def generate(*, timing_scope: str | None = None) -> AsyncIterator[str]:
        del timing_scope
        yield "a"
        yield "b"

    generator = generate(timing_scope="scope-123")
    assert await anext(generator) == "a"
    await asyncio.create_task(generator.aclose())

    _assert_timing_logged(logger, "async_generator_cross_task_close_label", scope="scope-123")


@pytest.mark.asyncio
async def test_timed_async_generator_logs_on_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Async generators should emit a timing log when iteration raises."""
    monkeypatch.setenv("MINDROOM_TIMING", "1")
    logger = _mock_timing_logger(monkeypatch)

    @timed("async_generator_error_label")
    async def generate() -> AsyncIterator[str]:
        yield "a"
        msg = "boom"
        raise RuntimeError(msg)

    generator = generate()
    assert await anext(generator) == "a"

    with pytest.raises(RuntimeError, match="boom"):
        await anext(generator)

    _assert_timing_logged(logger, "async_generator_error_label")


def test_timed_returns_original_function_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disabled timing should leave the original callable untouched."""
    monkeypatch.delenv("MINDROOM_TIMING", raising=False)

    def original() -> str:
        return "ok"

    wrapped = timed("disabled_label")(original)

    assert wrapped is original


def test_timed_includes_scope_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The current timing scope should be rendered in the log line."""
    monkeypatch.setenv("MINDROOM_TIMING", "1")
    logger = _mock_timing_logger(monkeypatch)

    @timed("scoped_label")
    def run() -> None:
        return None

    token = timing_scope.set("scope-123")
    try:
        run()
    finally:
        timing_scope.reset(token)

    _assert_timing_logged(logger, "scoped_label", scope="scope-123")


def test_timed_logs_omit_scope_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unscoped timing logs should not include the optional timing_scope field."""
    monkeypatch.setenv("MINDROOM_TIMING", "1")
    logger = _mock_timing_logger(monkeypatch)

    @timed("plain_label")
    def run() -> None:
        return None

    run()

    _assert_timing_logged(logger, "plain_label")


def test_emit_timing_event_logs_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Structured timing events should inherit the current timing scope."""
    monkeypatch.setenv("MINDROOM_TIMING", "1")
    logger = _mock_timing_logger(monkeypatch)

    token = timing_scope.set("scope-123")
    try:
        emit_timing_event("custom_timing_event", value=42, ok=True)
    finally:
        timing_scope.reset(token)

    logger.info.assert_called_once_with(
        "custom_timing_event",
        value=42,
        ok=True,
        timing_scope="scope-123",
    )


def test_timing_enabled_reflects_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The public timing-enabled helper should mirror the environment switch."""
    monkeypatch.delenv("MINDROOM_TIMING", raising=False)
    assert timing_enabled() is False

    monkeypatch.setenv("MINDROOM_TIMING", "1")
    assert timing_enabled() is True


def test_dispatch_pipeline_summary_emits_additive_segments_and_diagnostics() -> None:
    """Pipeline summary should separate additive segments from drill-down diagnostics."""
    logger = Mock()
    timing = DispatchPipelineTiming(source_event_id="$event", room_id="!room")
    timing.metadata["first_visible_kind"] = "stream_update"
    timing.marks.update(
        {
            "message_received": 0.0,
            "ingress_cache_append_start": 0.2,
            "ingress_cache_append_ready": 0.7,
            "ingress_normalize_start": 0.9,
            "ingress_normalize_ready": 1.4,
            "gate_enter": 1.0,
            "gate_exit": 3.0,
            "dispatch_prepare_start": 4.0,
            "dispatch_prepare_ready": 6.0,
            "dispatch_plan_start": 6.5,
            "dispatch_plan_ready": 7.5,
            "response_payload_start": 8.0,
            "response_payload_ready": 9.0,
            "lock_wait_start": 10.0,
            "lock_acquired": 12.0,
            "thread_refresh_ready": 13.0,
            "response_runtime_start": 14.0,
            "response_runtime_ready": 15.0,
            "ai_prepare_start": 15.5,
            "history_ready": 17.0,
            "model_request_sent": 18.0,
            "model_first_token": 19.5,
            "first_visible_reply": 20.0,
            "streaming_complete": 24.0,
            "response_complete": 25.0,
        },
    )

    timing.emit_summary(logger, outcome="edited")

    logger.info.assert_called_once()
    assert logger.info.call_args.args == ("Dispatch pipeline timing",)
    summary = logger.info.call_args.kwargs
    assert summary["first_visible_kind"] == "stream_update"
    assert summary["seg_ingress_ms"] == 1000.0
    assert summary["seg_coalescing_ms"] == 2000.0
    assert summary["seg_dispatch_ms"] == 6000.0
    assert summary["seg_response_queue_ms"] == 3000.0
    assert summary["seg_thread_refresh_ms"] == 1000.0
    assert summary["seg_first_visible_reply_ms"] == 7000.0
    assert summary["seg_after_first_visible_ms"] == 5000.0
    assert summary["time_to_first_visible_reply_ms"] == 20000.0
    assert summary["total_pipeline_ms"] == 25000.0
    assert summary["diag_ingress_cache_append_ms"] == 500.0
    assert summary["diag_ingress_normalize_ms"] == 500.0
    assert summary["diag_dispatch_prepare_ms"] == 2000.0
    assert summary["diag_dispatch_plan_ms"] == 1000.0
    assert summary["diag_response_payload_setup_ms"] == 1000.0
    assert summary["diag_lock_wait_ms"] == 2000.0
    assert summary["diag_runtime_prepare_ms"] == 1000.0
    assert summary["diag_llm_prepare_ms"] == 1500.0
    assert summary["diag_history_ready_to_model_request_ms"] == 1000.0
    assert summary["diag_provider_ttft_ms"] == 1500.0
    assert summary["diag_first_visible_to_stream_complete_ms"] == 4000.0
    assert summary["diag_model_request_to_completion_ms"] == 7000.0
    assert "model_first_token_to_first_visible_stream_update_ms" not in summary
    assert "placeholder_visible_ms" not in summary
    assert "model_request_to_completion_ms" not in summary


def test_dispatch_pipeline_first_visible_reply_is_first_write_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first visible reply mark should preserve the earliest visible milestone."""
    perf_counter = Mock(side_effect=[1.5, 9.0])
    monkeypatch.setattr(timing_module.time, "perf_counter", perf_counter)
    timing = DispatchPipelineTiming(source_event_id="$event", room_id="!room")

    timing.mark_first_visible_reply("placeholder")
    timing.mark_first_visible_reply("final")

    assert timing.marks["first_visible_reply"] == 1.5
    assert timing.metadata["first_visible_kind"] == "placeholder"
