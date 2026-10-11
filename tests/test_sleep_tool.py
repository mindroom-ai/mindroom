"""Sleep pauses stay within their cap and hold no thread."""

from __future__ import annotations

import asyncio
import inspect
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from mindroom.custom_tools import sleep as sleep_module
from mindroom.custom_tools.sleep import SleepTools


@pytest.mark.asyncio
async def test_sleep_refuses_durations_outside_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Durations past 300 seconds or below zero return an error without waiting."""
    slept: list[float] = []

    async def record_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(sleep_module, "asyncio", SimpleNamespace(sleep=record_sleep))
    tools = SleepTools()

    refused = [await tools.sleep(10**9), await tools.sleep(-1)]
    allowed = await tools.sleep(300)

    assert all("between 0 and 300" in result for result in refused)
    assert allowed == "Slept for 300 seconds"
    assert slept == [300]


@pytest.mark.asyncio
async def test_parallel_sleeps_hold_no_executor_thread() -> None:
    """Agno awaits the registered sleep on the event loop, so parallel calls leave the default executor free."""
    entrypoint = SleepTools().get_async_functions()["sleep"].entrypoint
    assert entrypoint is not None
    assert inspect.iscoroutinefunction(entrypoint)
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=1))

    sleeps = [asyncio.create_task(entrypoint(seconds=300)) for _ in range(8)]
    await asyncio.sleep(0)

    assert await asyncio.wait_for(asyncio.to_thread(lambda: "free"), timeout=5) == "free"
    for task in sleeps:
        task.cancel()
    results = await asyncio.gather(*sleeps, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)
