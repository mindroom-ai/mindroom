"""Tests for decorator-based timing instrumentation."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

import pytest

from mindroom.timing import timed, timing_scope

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def _assert_timing_logged(caplog: pytest.LogCaptureFixture, label: str, *, scope: str | None = None) -> None:
    prefix = f"\\[{scope}\\] " if scope is not None else ""
    assert re.fullmatch(rf"TIMING {prefix}{label}: \d+\.\d{{3}}s", caplog.messages[-1]) is not None


def test_timed_sync_logs_when_enabled(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Sync functions should emit a timing log when timing is enabled."""
    monkeypatch.setenv("MINDROOM_TIMING", "1")

    @timed("sync_label")
    def add(left: int, right: int) -> int:
        return left + right

    caplog.set_level(logging.INFO, logger="mindroom.timing")

    assert add(2, 3) == 5
    _assert_timing_logged(caplog, "sync_label")


def test_timed_sync_logs_on_exception(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Sync functions should still emit a timing log when they raise."""
    monkeypatch.setenv("MINDROOM_TIMING", "1")

    @timed("sync_error_label")
    def fail() -> None:
        msg = "boom"
        raise RuntimeError(msg)

    caplog.set_level(logging.INFO, logger="mindroom.timing")

    with pytest.raises(RuntimeError, match="boom"):
        fail()

    _assert_timing_logged(caplog, "sync_error_label")


@pytest.mark.asyncio
async def test_timed_async_logs_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Async functions should emit a timing log when timing is enabled."""
    monkeypatch.setenv("MINDROOM_TIMING", "1")

    @timed("async_label")
    async def compute() -> str:
        return "done"

    caplog.set_level(logging.INFO, logger="mindroom.timing")

    assert await compute() == "done"
    _assert_timing_logged(caplog, "async_label")


@pytest.mark.asyncio
async def test_timed_async_logs_on_exception(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Async functions should still emit a timing log when they raise."""
    monkeypatch.setenv("MINDROOM_TIMING", "1")

    @timed("async_error_label")
    async def fail() -> None:
        msg = "boom"
        raise RuntimeError(msg)

    caplog.set_level(logging.INFO, logger="mindroom.timing")

    with pytest.raises(RuntimeError, match="boom"):
        await fail()

    _assert_timing_logged(caplog, "async_error_label")


@pytest.mark.asyncio
async def test_timed_async_generator_logs_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Async generators should emit a timing log after iteration completes."""
    monkeypatch.setenv("MINDROOM_TIMING", "1")

    @timed("async_generator_label")
    async def generate() -> AsyncIterator[str]:
        yield "a"
        yield "b"

    caplog.set_level(logging.INFO, logger="mindroom.timing")

    assert [item async for item in generate()] == ["a", "b"]
    _assert_timing_logged(caplog, "async_generator_label")


@pytest.mark.asyncio
async def test_timed_async_generator_logs_on_early_close(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Async generators should emit a timing log when iteration stops early."""
    monkeypatch.setenv("MINDROOM_TIMING", "1")

    @timed("async_generator_early_close_label")
    async def generate() -> AsyncIterator[str]:
        yield "a"
        yield "b"

    caplog.set_level(logging.INFO, logger="mindroom.timing")

    generator = generate()
    assert await anext(generator) == "a"
    await generator.aclose()

    _assert_timing_logged(caplog, "async_generator_early_close_label")


@pytest.mark.asyncio
async def test_timed_async_generator_uses_explicit_scope_on_cross_task_close(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Explicit timing_scope kwargs should survive async-generator close in another task."""
    monkeypatch.setenv("MINDROOM_TIMING", "1")

    @timed("async_generator_cross_task_close_label")
    async def generate(*, timing_scope: str | None = None) -> AsyncIterator[str]:
        del timing_scope
        yield "a"
        yield "b"

    caplog.set_level(logging.INFO, logger="mindroom.timing")

    generator = generate(timing_scope="scope-123")
    assert await anext(generator) == "a"
    await asyncio.create_task(generator.aclose())

    _assert_timing_logged(caplog, "async_generator_cross_task_close_label", scope="scope-123")


@pytest.mark.asyncio
async def test_timed_async_generator_logs_on_exception(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Async generators should emit a timing log when iteration raises."""
    monkeypatch.setenv("MINDROOM_TIMING", "1")

    @timed("async_generator_error_label")
    async def generate() -> AsyncIterator[str]:
        yield "a"
        msg = "boom"
        raise RuntimeError(msg)

    caplog.set_level(logging.INFO, logger="mindroom.timing")

    generator = generate()
    assert await anext(generator) == "a"

    with pytest.raises(RuntimeError, match="boom"):
        await anext(generator)

    _assert_timing_logged(caplog, "async_generator_error_label")


def test_timed_returns_original_function_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disabled timing should leave the original callable untouched."""
    monkeypatch.delenv("MINDROOM_TIMING", raising=False)

    def original() -> str:
        return "ok"

    wrapped = timed("disabled_label")(original)

    assert wrapped is original


def test_timed_includes_scope_when_set(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The current timing scope should be rendered in the log line."""
    monkeypatch.setenv("MINDROOM_TIMING", "1")

    @timed("scoped_label")
    def run() -> None:
        return None

    caplog.set_level(logging.INFO, logger="mindroom.timing")

    token = timing_scope.set("scope-123")
    try:
        run()
    finally:
        timing_scope.reset(token)

    _assert_timing_logged(caplog, "scoped_label", scope="scope-123")


def test_timed_log_format_omits_scope_when_unset(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unscoped timing lines should not render an empty scope prefix."""
    monkeypatch.setenv("MINDROOM_TIMING", "1")

    @timed("plain_label")
    def run() -> None:
        return None

    caplog.set_level(logging.INFO, logger="mindroom.timing")

    run()

    assert caplog.messages[-1].startswith("TIMING plain_label: ")
    assert "[scope" not in caplog.messages[-1]
