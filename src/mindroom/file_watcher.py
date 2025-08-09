"""Simple file watcher utility without external dependencies."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path


async def watch_file(
    file_path: Path | str, callback: Callable[[], Awaitable[None]], stop_event: asyncio.Event | None = None
) -> None:
    """Watch a file for changes and call callback when modified.

    Args:
        file_path: Path to the file to watch
        callback: Async function to call when file changes
        stop_event: Optional event to signal when to stop watching
    """
    file_path = Path(file_path)
    last_mtime = file_path.stat().st_mtime if file_path.exists() else 0

    while stop_event is None or not stop_event.is_set():
        await asyncio.sleep(1.0)  # Check every second

        if file_path.exists():
            current_mtime = file_path.stat().st_mtime
            if current_mtime != last_mtime:
                last_mtime = current_mtime
                await callback()
