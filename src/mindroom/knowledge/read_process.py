"""Run each native knowledge read in a bounded, single-use child process.

Every child serves one read and exits, which releases all native memory. One
idle child is kept warm, so a read does not wait for interpreter startup and
native imports; each read starts its successor once it is over.
"""

from __future__ import annotations

import asyncio
import atexit
import os
import signal
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from threading import BoundedSemaphore, Lock
from typing import TYPE_CHECKING

from mindroom.knowledge.read_protocol import (
    MAX_FRAME_BYTES,
    ReadRequest,
    ReadResult,
    request_adapter,
    result_adapter,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator

# TODO: Remove this read subprocess workaround after pinning a Chroma release containing
# https://github.com/chroma-core/chroma/pull/7692 and verifying lock waits no longer stall the application.

_READ_CAPACITY = 4
# Avoid turning simultaneous searches into unbounded native index copies.
_read_slots = BoundedSemaphore(_READ_CAPACITY)
# Async reads wait on child pipes here, never in the shared default executor.
_exchange_threads = ThreadPoolExecutor(_READ_CAPACITY, thread_name_prefix="knowledge-read")
_warm_reader_lock = Lock()
_warm_reader: subprocess.Popen[bytes] | None = None
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


def _encode_request(request: ReadRequest, timeout: float) -> bytes:
    """Frame the child's deadline as the first line, ahead of the JSON request."""
    payload = request_adapter.dump_json(request)
    if len(payload) > MAX_FRAME_BYTES:
        message = "Knowledge read request exceeds transport size limit"
        raise ValueError(message)
    return f"{timeout!r}\n".encode() + payload


def _decode_result(output: bytes, returncode: int) -> ReadResult:
    if returncode == -signal.SIGALRM:
        message = "Knowledge read timed out"
        raise TimeoutError(message)
    if returncode:
        raise subprocess.CalledProcessError(returncode, "knowledge read worker", output=output)
    if len(output) > MAX_FRAME_BYTES:
        message = "Knowledge read result exceeds transport size limit"
        raise RuntimeError(message)
    result = result_adapter.validate_json(output)
    if result.error_type is not None:
        message = f"Knowledge read failed ({result.error_type})"
        raise RuntimeError(message)
    return result


def _child_environment() -> dict[str, str]:
    env = {name: os.environ[name] for name in _CHILD_ENV_KEYS if name in os.environ}
    env.update(OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", TOKIO_WORKER_THREADS="2")
    return env


def _spawn_reader() -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-m", "mindroom.knowledge.read_worker"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env=_child_environment(),
    )


@contextmanager
def _reader() -> Iterator[subprocess.Popen[bytes]]:
    """Hand out the warm child, or a new one, then warm the next read's child.

    Warming starts only after this read, so the successor's imports never compete with it for CPU.
    """
    global _warm_reader
    with _warm_reader_lock:
        reader, _warm_reader = _warm_reader, None
    if reader is None or reader.poll() is not None:
        if reader is not None:
            _exchange(reader, None)  # Close the pipes of a warm child that died while idle.
        reader = _spawn_reader()
    try:
        yield reader
    finally:
        with _warm_reader_lock:
            if _warm_reader is None:
                _warm_reader = _spawn_reader()


def _exchange(
    reader: subprocess.Popen[bytes],
    payload: bytes | None,
    timeout: float | None = None,
) -> tuple[bytes, int]:
    """Send one framed request, or none to discard the child; return only after reaping it."""
    with reader:
        if payload is None:
            reader.kill()
        try:
            output, _ = reader.communicate(payload, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            reader.kill()
            message = "Knowledge read timed out"
            raise TimeoutError(message) from exc
    return output, reader.returncode


def _discard_warm_reader() -> None:
    """Kill and reap the idle child; it holds no request, lease, or native index."""
    global _warm_reader
    with _warm_reader_lock:
        reader, _warm_reader = _warm_reader, None
    if reader is not None:
        _exchange(reader, None)


atexit.register(_discard_warm_reader)


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
    """Run off the event loop; a child that outlives the deadline is killed and reaped."""
    payload = _encode_request(request, timeout)
    with _read_slot(), _reader() as reader:
        return _decode_result(*_exchange(reader, payload, timeout))


@asynccontextmanager
async def _read_slot_async() -> AsyncIterator[None]:
    # Share sync capacity without parking an executor worker or binding to one loop.
    while not _read_slots.acquire(blocking=False):  # noqa: ASYNC110 - Capacity is shared across threads/loops.
        await asyncio.sleep(0.01)
    try:
        yield
    finally:
        _read_slots.release()


async def read_chroma_async(
    prepare_request: Callable[[], Awaitable[ReadRequest]],
    *,
    timeout: float = 30.0,  # noqa: ASYNC109 - The transport owns the child's bounded lifetime.
) -> ReadResult:
    """Let a still-starting child finish its imports during parent preparation, under one deadline."""
    try:
        async with asyncio.timeout(timeout), _read_slot_async():
            with _reader() as reader:
                try:
                    payload = _encode_request(await prepare_request(), timeout)
                except BaseException:
                    await _exchange_async(reader, None)
                    raise
                return _decode_result(*await _exchange_async(reader, payload))
    except TimeoutError as exc:
        message = "Knowledge read timed out"
        raise TimeoutError(message) from exc


async def _exchange_async(reader: subprocess.Popen[bytes], payload: bytes | None) -> tuple[bytes, int]:
    """Exchange off-loop; cancellation kills the child but keeps capacity until it is reaped."""
    exchange = asyncio.wrap_future(_exchange_threads.submit(_exchange, reader, payload))
    cancelled = False
    while not exchange.done():
        try:
            await asyncio.wait([exchange])
        except asyncio.CancelledError:
            cancelled = True
            reader.kill()
    if cancelled:
        raise asyncio.CancelledError
    return exchange.result()
