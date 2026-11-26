"""Native sleep toolkit with a bounded async pause."""

from __future__ import annotations

import asyncio

from mindroom.logging_config import get_logger
from mindroom.tool_system.authoring import Toolkit, tool

logger = get_logger(__name__)

_MAX_SLEEP_SECONDS = 300


class SleepTools(Toolkit):
    """Sleep toolkit whose pauses are capped and awaited on the event loop."""

    name = "sleep"

    def __init__(
        self,
        enable_sleep: bool = True,
        all: bool = False,  # noqa: A002
    ) -> None:
        tools = (self.sleep,) if (all or enable_sleep) else ()
        super().__init__(name="sleep", tools=tools)
        if not (all or enable_sleep):
            self.functions.pop("sleep", None)

    @tool
    async def sleep(self, seconds: int) -> str:
        """Use this function to sleep for a given number of seconds, at most 300."""
        if not 0 <= seconds <= _MAX_SLEEP_SECONDS:
            return f"Sleep duration must be between 0 and {_MAX_SLEEP_SECONDS} seconds."
        logger.info("Sleeping", seconds=seconds)
        await asyncio.sleep(seconds)
        logger.info("Awake after sleep", seconds=seconds)
        return f"Slept for {seconds} seconds"
