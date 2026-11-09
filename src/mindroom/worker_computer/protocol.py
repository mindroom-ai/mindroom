"""Narrow interfaces for worker computer resource ownership, and the limits of its streams."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, TypedDict

# A worker stream message is a chunk of the desktop's RFB stream.
MAX_WORKER_STREAM_MESSAGE_BYTES = 4 * 1024 * 1024


class ComputerStatus(TypedDict):
    """Runtime status shared with the authenticated backend."""

    state: Literal["starting", "ready", "stopped"]
    generation: str
    controller_session_id: str | None


class ComputerDisplay(Protocol):
    """Private display lifecycle used by the ASGI owner."""

    display: str
    socket_path: Path

    async def start(self) -> None:
        """Start and verify the display."""

    async def close(self) -> None:
        """Reap all owned processes."""

    def healthy(self) -> bool:
        """Return whether all display processes still run."""


@dataclass(frozen=True)
class BrowserSession:
    """Validated browser entrypoint and its asynchronous resource closer."""

    execute: Callable[..., Awaitable[object]]
    close: Callable[[], Awaitable[None]]
