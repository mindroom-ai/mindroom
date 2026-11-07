"""Narrow interfaces for worker computer resource ownership, and the limits of its streams."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, TypedDict

if TYPE_CHECKING:
    from playwright.async_api import Page

# The WebSocket subprotocol of a screencast of the agent's browser page, on both the public and the worker hop.
SCREENCAST_PROTOCOL = "mindroom-screencast"
# A worker stream message is a chunk of the desktop's RFB stream or one JPEG frame of a screencast.
MAX_WORKER_STREAM_MESSAGE_BYTES = 4 * 1024 * 1024
# Viewer messages are JSON input events; inserted text is the largest.
MAX_SCREENCAST_MESSAGE_BYTES = 64 * 1024


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


def _no_page() -> None:
    return None


@dataclass(frozen=True)
class BrowserSession:
    """Validated browser entrypoint, its asynchronous resource closer, and the page it shows.

    ``page`` returns the page a screencast shows while the browser runs in this process; a browser in another
    process, such as ``browser_mcp``'s, has none.
    """

    execute: Callable[..., Awaitable[object]]
    close: Callable[[], Awaitable[None]]
    page: Callable[[], "Page | None"] = _no_page
