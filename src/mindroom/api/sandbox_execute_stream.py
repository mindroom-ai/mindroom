"""Stream one sandbox tool call as NDJSON lines: its latest progress and keep-alives, then its result.

The result line is the call's whole response. A primary that hangs up stops the call, and nothing more is
written to it.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

from mindroom.api import sandbox_request_cancellation
from mindroom.background_tasks import create_background_task
from mindroom.tool_system.progress import tool_progress_reporter

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from pydantic import BaseModel

_KEEPALIVE_LINE = b'{"keepalive": true}\n'


class _LatestProgress:
    """The newest progress a call reported; a newer report replaces one the stream has not sent yet."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._text: str | None = None
        self.changed = asyncio.Event()

    def report(self, text: str) -> None:
        # Tool threads report too; the stream's state belongs to its loop. The primary masks and cuts what it shows.
        self._loop.call_soon_threadsafe(self._set, text)

    def _set(self, text: str) -> None:
        self._text = text
        self.changed.set()

    def take(self) -> str | None:
        text, self._text = self._text, None
        self.changed.clear()
        return text


def _queued(frames: asyncio.Queue[bytes] | None) -> list[bytes]:
    """Return the lines a call queued that were not sent yet."""
    lines = []
    while frames is not None and not frames.empty():
        lines.append(frames.get_nowait())
    return lines


async def stream_execution(
    request_id: str,
    execute: Callable[[], Awaitable[BaseModel]],
    *,
    keepalive_seconds: float,
    failure: Callable[[Exception], BaseModel],
    frames: asyncio.Queue[bytes] | None = None,
) -> AsyncIterator[bytes]:
    """Run ``execute`` as the request ``request_id``, streaming its progress and a keep-alive whenever it is quiet.

    ``failure`` turns an error raised after the stream started into the result line. A call that streams its
    command's output puts each line into ``frames``, which are sent in order before the result line; the queue is
    short, so the call waits while the primary reads slowly.
    """
    progress = _LatestProgress(asyncio.get_running_loop())

    async def run() -> BaseModel:
        with sandbox_request_cancellation.track_request(request_id), tool_progress_reporter(progress.report):
            try:
                return await execute()
            except Exception as error:
                return failure(error)

    call = create_background_task(run(), name="sandbox-execute")
    try:
        while True:
            waits = [call, asyncio.ensure_future(progress.changed.wait())]
            if frames is not None:
                waits.append(asyncio.ensure_future(frames.get()))
            try:
                done, _ = await asyncio.wait(waits, timeout=keepalive_seconds, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for wait in waits[1:]:
                    wait.cancel()
            text = progress.take()
            lines = [] if text is None else [json.dumps({"progress": text}).encode() + b"\n"]
            lines += [wait.result() for wait in waits[2:] if wait in done]
            # The result follows the last line the call queued before it.
            finished = call.done() and not lines
            if finished:
                lines = [*_queued(frames), call.result().model_dump_json().encode() + b"\n"]
            for line in lines or [_KEEPALIVE_LINE]:
                yield line
            if finished:
                return
    finally:
        if not call.done():
            # The primary hung up. Stopping is synchronous, so a repeated cancellation cannot skip it.
            sandbox_request_cancellation.cancel_request(request_id)
