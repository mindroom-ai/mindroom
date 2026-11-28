"""Sleep pauses stay within their cap and hold no thread."""

from __future__ import annotations

import asyncio
import inspect
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from mindroom.custom_tools import sleep as sleep_module
from mindroom.custom_tools.sleep import SleepTools
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.toolkits import toolkit_functions
from mindroom.tools.sleep import sleep_tools


def test_sleep_toolkit_is_native_and_respects_enable_switches() -> None:
    """The sleep factory returns a native Toolkit honoring enable_sleep and all."""
    tool_class = sleep_tools()

    assert issubclass(tool_class, Toolkit)
    assert tool_class is SleepTools
    assert tool_class().name == "sleep"
    assert list(tool_class().functions) == ["sleep"]
    assert list(tool_class(enable_sleep=False).functions) == []
    assert list(tool_class(enable_sleep=False, all=True).functions) == ["sleep"]


@pytest.mark.asyncio
async def test_sleep_refuses_durations_outside_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Durations past 300 seconds or below zero return an error without waiting."""
    slept: list[float] = []

    async def record_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(sleep_module, "asyncio", SimpleNamespace(sleep=record_sleep))
    tools = SleepTools()

    refused = [await tools.sleep(10**9), await tools.sleep(-1)]
    allowed = [await tools.sleep(0), await tools.sleep(300)]

    assert all("between 0 and 300" in result for result in refused)
    assert allowed == ["Slept for 0 seconds", "Slept for 300 seconds"]
    assert slept == [0, 300]


@pytest.mark.asyncio
async def test_parallel_sleeps_hold_no_executor_thread() -> None:
    """The registered sleep awaits on the event loop, so parallel calls leave the default executor free."""
    entrypoint = toolkit_functions(SleepTools())["sleep"]
    assert inspect.iscoroutinefunction(entrypoint)
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=1))

    sleeps = [asyncio.create_task(entrypoint(seconds=300)) for _ in range(8)]
    await asyncio.sleep(0)

    assert await asyncio.wait_for(asyncio.to_thread(lambda: "free"), timeout=5) == "free"
    for task in sleeps:
        task.cancel()
    results = await asyncio.gather(*sleeps, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)
