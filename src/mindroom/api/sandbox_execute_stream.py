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
from mindroom.tool_system.progress import cap_progress, tool_progress_reporter

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
        # Tool threads report too; the stream's state belongs to its loop.
        self._loop.call_soon_threadsafe(self._set, cap_progress(text))

    def _set(self, text: str) -> None:
        self._text = text
        self.changed.set()

    def take(self) -> str | None:
        text, self._text = self._text, None
        self.changed.clear()
        return text


async def stream_execution(
    request_id: str,
    execute: Callable[[], Awaitable[BaseModel]],
    *,
    keepalive_seconds: float,
    failure: Callable[[Exception], BaseModel],
) -> AsyncIterator[bytes]:
    """Run ``execute`` as the request ``request_id``, streaming its progress and a keep-alive whenever it is quiet.

    ``failure`` turns an error raised after the stream started into the result line.
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
            changed = asyncio.ensure_future(progress.changed.wait())
            try:
                await asyncio.wait({call, changed}, timeout=keepalive_seconds, return_when=asyncio.FIRST_COMPLETED)
            finally:
                changed.cancel()
            text = progress.take()
            if text is not None:
                yield json.dumps({"progress": text}).encode() + b"\n"
            if call.done():
                yield call.result().model_dump_json().encode() + b"\n"
                return
            if text is None:
                yield _KEEPALIVE_LINE
    finally:
        if not call.done():
            # The primary hung up. Stopping is synchronous, so a repeated cancellation cannot skip it.
            sandbox_request_cancellation.cancel_request(request_id)
