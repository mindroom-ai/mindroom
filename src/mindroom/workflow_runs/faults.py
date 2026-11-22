"""What live tests of workflow runs read and hold: barriers where a call waits to be killed, and what a run showed."""

from __future__ import annotations

import asyncio
import json
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


def capture(name: str, shown: object) -> None:
    """Keep what a run showed a room, as one JSON line of `<dir>/<name>.jsonl`; nothing when no fault directory is set.

    A live test reads there what it cannot read from an encrypted room: the frames and the card as they were sent.
    """
    if directory := os.environ.get(_FAULT_DIR_ENV):
        with Path(directory, f"{name}.jsonl").open("a", encoding="utf-8") as lines:
            lines.write(json.dumps(shown) + "\n")
