"""The stream `mindroom-agent workers run` reads, for tests that answer it without a server."""

from __future__ import annotations

import base64

__all__ = ["command_frames"]


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
