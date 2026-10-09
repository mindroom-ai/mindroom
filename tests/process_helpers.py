"""Process liveness assertions shared by shell and worker cancellation tests."""

from __future__ import annotations

import asyncio
from pathlib import Path


async def assert_linux_pid_not_running(pid: int) -> None:
    """Wait until a Linux process exits, allowing an unreaped zombie."""
    stat_path = Path(f"/proc/{pid}/stat")
    for _ in range(40):
        try:
            state = stat_path.read_text(encoding="utf-8").split()[2]
        except (FileNotFoundError, ProcessLookupError):
            return
        if state == "Z":
            return
        await asyncio.sleep(0.05)
    message = f"Process {pid} is still running"
    raise AssertionError(message)
