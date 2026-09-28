"""Tests for the bounded sleep toolkit."""

from __future__ import annotations

import inspect
import time
from typing import NoReturn

import pytest

from mindroom.tools.sleep import sleep_tools


def _refuse_thread_sleep(_seconds: float) -> NoReturn:
    msg = "sleep must not block a thread"
    raise AssertionError(msg)


@pytest.mark.asyncio
async def test_sleep_waits_on_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pause awaits the event loop instead of pinning a default-executor thread."""
    monkeypatch.setattr(time, "sleep", _refuse_thread_sleep)
    tools = sleep_tools()()

    assert inspect.iscoroutinefunction(tools.sleep)
    assert await tools.sleep(0) == "Slept for 0 seconds"


@pytest.mark.asyncio
@pytest.mark.parametrize("seconds", [301, 999_999_999, -1])
async def test_sleep_rejects_durations_outside_the_cap(monkeypatch: pytest.MonkeyPatch, seconds: int) -> None:
    """Durations beyond five minutes, or negative ones, return immediately with an error."""
    monkeypatch.setattr(time, "sleep", _refuse_thread_sleep)
    tools = sleep_tools()()

    assert await tools.sleep(seconds) == "Sleep duration must be between 0 and 300 seconds."


def test_sleep_config_fields_select_the_function() -> None:
    """The Agno config fields still decide whether sleep is exposed."""
    assert set(sleep_tools()().get_async_functions()) == {"sleep"}
    assert set(sleep_tools()(enable_sleep=False).get_async_functions()) == set()
    assert set(sleep_tools()(enable_sleep=False, all=True).get_async_functions()) == {"sleep"}
