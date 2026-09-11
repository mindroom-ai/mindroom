"""Run each native knowledge read in a bounded, short-lived child process."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from contextlib import contextmanager
from threading import BoundedSemaphore
from typing import TYPE_CHECKING

from mindroom.knowledge.read_protocol import (
    MAX_FRAME_BYTES,
    ReadRequest,
    ReadResult,
    request_adapter,
    result_adapter,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator

# TODO: Remove this read subprocess workaround after pinning a Chroma release containing
# https://github.com/chroma-core/chroma/pull/7692 and verifying lock waits no longer stall the application.

# Avoid turning simultaneous searches into unbounded native index copies.
_read_slots = BoundedSemaphore(4)
_CHILD_ENV_KEYS = (
    "PATH",
    "HOME",
    "SYSTEMROOT",
    "WINDIR",
    "LD_LIBRARY_PATH",
    "DYLD_LIBRARY_PATH",
    "NIX_LD_LIBRARY_PATH",
    "TMPDIR",
    "TEMP",
    "TMP",
)


def _encode_request(request: ReadRequest) -> bytes:
    payload = request_adapter.dump_json(request)
    if len(payload) > MAX_FRAME_BYTES:
        message = "Knowledge read request exceeds transport size limit"
        raise ValueError(message)
    return payload


def _decode_result(payload: bytes) -> ReadResult:
    if len(payload) > MAX_FRAME_BYTES:
        message = "Knowledge read result exceeds transport size limit"
        raise RuntimeError(message)
    result = result_adapter.validate_json(payload)
    if result.error_type is not None:
        message = f"Knowledge read failed ({result.error_type})"
        raise RuntimeError(message)
    return result


def _child_environment() -> dict[str, str]:
    env = {name: os.environ[name] for name in _CHILD_ENV_KEYS if name in os.environ}
    env.update(OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", TOKIO_WORKER_THREADS="2")
    return env


@contextmanager
def _read_slot() -> Iterator[None]:
    # Waiting here would occupy the shared executor and starve unrelated I/O.
    if not _read_slots.acquire(blocking=False):
        message = "Knowledge reader is busy; try again shortly"
        raise RuntimeError(message)
    try:
        yield
    finally:
        _read_slots.release()


def read_chroma(request: ReadRequest, *, timeout: float = 30.0) -> ReadResult:
    """Run off the event loop; subprocess.run kills and reaps a timed-out child."""
    payload = _encode_request(request)
    with _read_slot():
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "mindroom.knowledge.read_worker"],
                input=payload,
                stdout=subprocess.PIPE,
                env=_child_environment(),
                timeout=timeout,
                check=True,
            )
        except subprocess.TimeoutExpired as exc:
            message = "Knowledge read timed out"
            raise TimeoutError(message) from exc
        return _decode_result(completed.stdout)


async def read_chroma_async(
    prepare_request: Callable[[], Awaitable[ReadRequest]],
    *,
    timeout: float = 30.0,  # noqa: ASYNC109 - The transport owns the child's bounded lifetime.
) -> ReadResult:
    """Overlap child imports with parent preparation under one request deadline."""
    with _read_slot():
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "mindroom.knowledge.read_worker",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            env=_child_environment(),
        )
        try:
            async with asyncio.timeout(timeout):
                request = await prepare_request()
                output, _ = await process.communicate(_encode_request(request))
            if process.returncode:
                raise subprocess.CalledProcessError(process.returncode, "knowledge read worker", output=output)
            return _decode_result(output)
        except TimeoutError as exc:
            message = "Knowledge read timed out"
            raise TimeoutError(message) from exc
        finally:
            await _cleanup_read_process(process)


async def _cleanup_read_process(process: asyncio.subprocess.Process) -> None:
    """Drain pipes and reap before releasing capacity, even under repeated cancellation."""
    if process.returncode is None:
        process.kill()
    cleanup = asyncio.create_task(process.communicate())
    cancelled = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cancelled = True
    cleanup.result()
    if cancelled:
        raise asyncio.CancelledError
