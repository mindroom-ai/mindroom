"""Linux MCP subprocess supervisor, including detached Chromium descendants.

Runs only in its own spawned process. The primary and worker ASGI processes
never become subreapers or change their signal handlers.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import FrameType

_PR_SET_CHILD_SUBREAPER = 36


def _children() -> list[int]:
    """Read only this supervisor's current direct, waitable children."""
    path = Path(f"/proc/self/task/{os.getpid()}/children")
    return [int(value) for value in path.read_text().split()]


def _reap_descendants() -> None:
    """Kill and reap adopted child roots repeatedly until the owned tree is empty."""
    deadline = time.monotonic() + 1.5
    while True:
        while True:
            try:
                pid, _status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return
            if pid == 0:
                break
        for pid in _children():
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        if time.monotonic() >= deadline:
            msg = "MCP descendant cleanup exceeded its deadline."
            raise RuntimeError(msg)
        time.sleep(0.01)


def _main() -> int:
    """Forward stdio to MCP and reap its complete Linux descendant tree on exit."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        msg = "MCP child supervisor could not enable descendant ownership."
        raise RuntimeError(msg)
    stopping = False

    def stop(_signum: int, _frame: FrameType | None) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        # Arguments come from trusted desktop/worker launch configuration, never tools.
        process = subprocess.Popen(sys.argv[1:])
        while not stopping and process.poll() is None:
            time.sleep(0.01)
        return process.returncode or 0
    finally:
        _reap_descendants()


if __name__ == "__main__":
    sys.exit(_main())
