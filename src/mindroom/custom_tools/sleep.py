"""Sleep toolkit that waits on the event loop for a bounded duration."""

from __future__ import annotations

import asyncio
from typing import Any

from agno.tools import Toolkit

_MAX_SLEEP_SECONDS = 300


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
