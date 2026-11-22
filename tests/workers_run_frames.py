"""The stream `mindroom-agent workers run` reads, for tests that answer it without a server."""

from __future__ import annotations

import base64
import io
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Buffer

__all__ = ["FakeStream", "command_frames"]


class _Recorded(io.BytesIO):
    def __init__(self, name: str, log: list[tuple[str, bytes]]) -> None:
        super().__init__()
        self._name, self._log = name, log

    def write(self, buffer: Buffer, /) -> int:
        self._log.append((self._name, bytes(buffer)))
        return super().write(buffer)


class FakeStream(io.TextIOWrapper):
    """A stdout or stderr for the CLI that keeps what is written to it, as text or as bytes, and may be a terminal.

    ``log`` takes every write under ``name`` in the order it reached the stream, across the streams that share it.
    """

    def __init__(self, *, is_tty: bool = False, name: str = "", log: list[tuple[str, bytes]] | None = None) -> None:
        super().__init__(_Recorded(name, [] if log is None else log), encoding="utf-8", newline="", write_through=True)
        self._is_tty = is_tty

    def isatty(self) -> bool:
        """Whether the CLI takes this stream for a terminal."""
        return self._is_tty

    def getvalue(self) -> str:
        """Return everything written so far."""
        return self.buffer.getvalue().decode()  # type: ignore[attr-defined]


def command_frames(
    stdout: bytes = b"",
    stderr: bytes = b"",
    *,
    code: int = 0,
    timed_out: bool = False,
    header: str | None = "[worker: w]",
) -> list[dict[str, object]]:
    """Return the frames of a command that ran in a worker: its start, its output, its exit, and its call's receipt."""
    frames: list[dict[str, object]] = [{"t": "started", "header": header}]
    frames += [{"t": "out", "b": base64.b64encode(stdout).decode()}] if stdout else []
    frames += [{"t": "err", "b": base64.b64encode(stderr).decode()}] if stderr else []
    return [*frames, {"t": "exit", "code": code, "timed_out": timed_out}, {"status": "completed", "outcome": header}]
