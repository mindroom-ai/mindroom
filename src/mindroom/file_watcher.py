"""Simple file watcher utility without external dependencies."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = structlog.get_logger(__name__)


async def watch_file(
    file_path: Path | str,
    callback: Callable[[], Awaitable[None]],
    stop_event: asyncio.Event | None = None,
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

        try:
            if file_path.exists():
                current_mtime = file_path.stat().st_mtime
                if current_mtime != last_mtime:
                    last_mtime = current_mtime
                    await callback()
        except (OSError, PermissionError):
            # File might have been deleted or become unreadable
            # Reset mtime so we detect when it comes back
            last_mtime = 0
        except Exception:
            # Don't let callback errors stop the watcher
            # The callback should handle its own errors
            logger.exception("Exception during file watcher callback - continuing to watch")
