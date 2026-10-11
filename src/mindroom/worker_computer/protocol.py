"""Narrow interfaces for worker computer resource ownership."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, TypedDict


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
