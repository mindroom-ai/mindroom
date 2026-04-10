"""Tests for decorator-based timing instrumentation."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

import mindroom.timing as timing_module
from mindroom.timing import timed, timing_scope

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
