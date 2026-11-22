"""Barriers for the crash tests of workflow runs: places where a call waits so that a test can kill MindRoom there."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

_FAULT_DIR_ENV = "MINDROOM_WORKFLOW_FAULT_DIR"
_POLL_SECONDS = 0.05


async def hold(name: str) -> None:
    """Wait at the barrier *name* while a crash test holds it; return at once when no fault directory is set.

    The test finds `<dir>/<name>.reached` and lets the call go on by writing `<dir>/<name>.release`.
    """
    directory = os.environ.get(_FAULT_DIR_ENV)
    if not directory:
        return
    Path(directory, f"{name}.reached").touch()
    while not Path(directory, f"{name}.release").exists():  # noqa: ASYNC110 - a file another process writes.
        await asyncio.sleep(_POLL_SECONDS)
