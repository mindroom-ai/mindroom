"""Sleep toolkit with a bounded pause."""

from __future__ import annotations

import asyncio

from agno.tools.sleep import SleepTools as AgnoSleepTools

_MAX_SLEEP_SECONDS = 300


# AGNO_COMPAT: SleepTools blocks a thread in time.sleep for any requested duration.
# Reason: Agno 3.0.9 SleepTools.sleep is synchronous and unbounded, so each call holds a thread of the
# primary process's shared default executor for as long as the model asks, and parallel calls exhaust it.
# Upstream issue: Tracking gap; no matching issue has been verified.
# Upstream PR: No matching fix has been verified.
# Remove when: Agno awaits its sleep instead of blocking a thread; the 300-second cap is MindRoom policy for
# the shared primary and stays if Agno picks another bound.
# Coverage: tests/test_sleep_tool.py::test_sleep_refuses_durations_outside_the_cap;
# tests/test_sleep_tool.py::test_parallel_sleeps_hold_no_executor_thread.
class SleepTools(AgnoSleepTools):
    """Agno sleep toolkit whose pauses are capped and awaited, because it runs in the process shared by every agent."""

    async def sleep(self, seconds: int) -> str:  # ty: ignore[invalid-method-override]  # Agno awaits async tools
        """Use this function to sleep for a given number of seconds, at most 300."""
        if not 0 <= seconds <= _MAX_SLEEP_SECONDS:
            return f"Sleep duration must be between 0 and {_MAX_SLEEP_SECONDS} seconds."
        await asyncio.sleep(seconds)
        return f"Slept for {seconds} seconds"
