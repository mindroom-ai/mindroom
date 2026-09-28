"""Sleep toolkit that waits on the event loop for a bounded duration."""

from __future__ import annotations

import asyncio
from typing import Any

from agno.tools import Toolkit

_MAX_SLEEP_SECONDS = 300


# AGNO_COMPAT: SleepTools blocks a thread in time.sleep for any requested duration.
# Reason: Agno 3.0.9 SleepTools.sleep is synchronous, so each call holds a default-executor thread of the
# primary process for as long as the model asks, and parallel calls can pin every thread it offloads to.
# Upstream issue: Tracking gap; no matching issue has been verified.
# Upstream PR: No matching fix has been verified.
# Remove when: Agno's SleepTools awaits asyncio.sleep; the 300-second cap is MindRoom policy for the shared
# primary and remains, for example as a bounded override of the upstream method.
# Coverage: tests/test_sleep_tool.py::test_sleep_waits_on_the_event_loop;
# tests/test_sleep_tool.py::test_sleep_rejects_durations_outside_the_cap;
# tests/test_sleep_tool.py::test_sleep_config_fields_select_the_function.
class SleepTools(Toolkit):
    """Agno-compatible sleep toolkit that holds no thread and caps each pause.

    Agno's own toolkit blocks a thread in ``time.sleep`` for any requested duration.
    Here the pause runs on the event loop of the process shared by every agent.
    """

    def __init__(
        self,
        enable_sleep: bool = True,
        all: bool = False,  # noqa: A002 - mirrors Agno's SleepTools config field.
        **kwargs: Any,  # noqa: ANN401 - mirrors Agno Toolkit passthrough kwargs.
    ) -> None:
        tools = [self.sleep] if all or enable_sleep else []
        super().__init__(name="sleep", tools=tools, **kwargs)

    async def sleep(self, seconds: int) -> str:
        """Use this function to sleep for a given number of seconds, at most 300."""
        if not 0 <= seconds <= _MAX_SLEEP_SECONDS:
            return f"Sleep duration must be between 0 and {_MAX_SLEEP_SECONDS} seconds."
        await asyncio.sleep(seconds)
        return f"Slept for {seconds} seconds"
