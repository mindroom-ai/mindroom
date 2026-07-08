"""Shell command execution core with timeout-to-handle background support.

This module owns the process spawning, output buffering, and background
handle registry shared by the shell toolkit (local execution) and the
worker-local shell supervisor process. It must stay importable without the
heavy `mindroom.tools` package so the supervisor process starts fast.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import os
import signal
import time
import uuid
from collections import deque
from dataclasses import dataclass, field

DEFAULT_RUN_TIMEOUT_SECONDS = 120

_STALE_RECORD_SECONDS = 600  # 10 minutes
_MAX_BACKGROUNDED = 16
_MAX_OUTPUT_LINES = 10_000
_MAX_OUTPUT_BYTES = 50 * 1024
_STREAM_READ_CHUNK_BYTES = 8192
_PROCESS_EXIT_POLL_INTERVAL_SECONDS = 0.05
_POST_EXIT_READER_GRACE_SECONDS = 0.5


@dataclass
class _OutputBuffer:
    """Bound shell output by both line count and encoded byte size."""

    max_lines: int = _MAX_OUTPUT_LINES
    max_bytes: int = _MAX_OUTPUT_BYTES
    chunks: deque[str] = field(default_factory=deque)
    byte_count: int = 0
    truncated: bool = False

    def append(self, text: str) -> None:
        if not text:
            return

        encoded = text.encode("utf-8", errors="replace")
        if len(encoded) > self.max_bytes:
            text = encoded[-self.max_bytes :].decode("utf-8", errors="ignore")
            encoded = text.encode("utf-8", errors="replace")
            self.truncated = True

        self.chunks.append(text)
        self.byte_count += len(encoded)

        while self.chunks and self.byte_count > self.max_bytes:
            removed = self.chunks.popleft()
            self.byte_count -= len(removed.encode("utf-8", errors="replace"))
            self.truncated = True

        self._trim_to_max_lines()

    def render(self, *, tail: int | None = None) -> str:
        output = self._rendered_text()
        output_lines = output.split("\n") if output else []
        if tail is not None:
            output_lines = output_lines[-tail:]
        output = "\n".join(output_lines)
        if not self.truncated:
            return output

        notice = (
            f"[Output truncated to the last {self.max_bytes} bytes. "
            "Redirect command output to a file for complete results.]"
        )
        return f"{notice}\n{output}" if output else notice

    def __len__(self) -> int:
        output = self._rendered_text()
        return len(output.split("\n")) if output else 0

    def _rendered_text(self) -> str:
        return "".join(self.chunks).rstrip("\n")

    def _trim_to_max_lines(self) -> None:
        output = self._rendered_text()
        lines = output.split("\n")
        if len(lines) <= self.max_lines:
            return

        output = "\n".join(lines[-self.max_lines :])
        self.chunks = deque([output])
        self.byte_count = len(output.encode("utf-8", errors="replace"))
        self.truncated = True


@dataclass
class ProcessRecord:
    """Tracks a backgrounded shell process."""

    namespace: str
    handle: str
    pid: int
    args: list[str]
    process: asyncio.subprocess.Process
    stdout_buf: _OutputBuffer = field(default_factory=_OutputBuffer)
    stderr_buf: _OutputBuffer = field(default_factory=_OutputBuffer)
    started_at: float = field(default_factory=time.monotonic)
    tail: int = 100
    finished: bool = False
    finished_at: float | None = None
    return_code: int | None = None
    _monitor_task: asyncio.Task[None] | None = field(default=None, repr=False)


async def run_command(
    registry: dict[str, ProcessRecord],
    *,
    namespace: str,
    argv: list[str],
    env: dict[str, str],
    cwd: str | None,
    tail: int,
    timeout: float,  # noqa: ASYNC109
) -> str:
    """Run one shell command; return output, an error message, or a background handle.

    When the command completes within ``timeout`` seconds the last ``tail``
    lines of stdout are returned (or the stderr on non-zero exit). When the
    timeout is exceeded the process keeps running under *registry* and a
    handle string is returned for ``check_command``/``kill_command``.
    Cancellation terminates the process group and drops any registered handle.
    """
    _sweep_stale_records(registry)

    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )
    except Exception as exc:
        return f"Error: {exc}"

    stdout_buf = _OutputBuffer()
    stderr_buf = _OutputBuffer()
    background_handle: str | None = None
    background_monitor_task: asyncio.Task[None] | None = None

    stdout_reader = asyncio.create_task(_read_stream(process.stdout, stdout_buf))
    stderr_reader = asyncio.create_task(_read_stream(process.stderr, stderr_buf))

    try:
        try:
            await _await_foreground_process_exit(process, timeout_seconds=timeout)
        except TimeoutError:
            active = sum(1 for r in registry.values() if not r.finished)
            if active >= _MAX_BACKGROUNDED:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(process.pid, signal.SIGKILL)
                await _cancel_pending_tasks(stdout_reader, stderr_reader)
                return (
                    f"Error: Too many backgrounded processes ({active}/{_MAX_BACKGROUNDED}). "
                    "Kill or wait for existing ones before running more."
                )
            handle = f"shell:{uuid.uuid4().hex[:8]}"
            record = ProcessRecord(
                namespace=namespace,
                handle=handle,
                pid=process.pid,
                args=argv,
                process=process,
                stdout_buf=stdout_buf,
                stderr_buf=stderr_buf,
                tail=tail,
            )
            record._monitor_task = asyncio.create_task(
                _monitor_process(registry, handle, process, stdout_reader, stderr_reader),
            )
            registry[handle] = record
            background_handle = handle
            background_monitor_task = record._monitor_task
            await asyncio.sleep(0)
            return (
                f"Command timed out after {timeout}s. Still running (PID {process.pid}).\n"
                f"Handle: {handle}\n"
                f"Use check_shell_command('{handle}') to poll or "
                f"kill_shell_command('{handle}') to stop."
            )

        await _await_reader_tasks_with_grace(
            stdout_reader,
            stderr_reader,
            grace_seconds=_POST_EXIT_READER_GRACE_SECONDS,
        )
    except asyncio.CancelledError:
        if background_handle is not None:
            registry.pop(background_handle, None)
        if background_monitor_task is not None:
            background_monitor_task.cancel()
        if process.returncode is None:
            await _terminate_process_group(process)
        await _cancel_pending_tasks(stdout_reader, stderr_reader)
        if background_monitor_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await background_monitor_task
        raise

    if process.returncode != 0:
        return f"Error: {stderr_buf.render()}"
    return stdout_buf.render(tail=tail)


def check_command(registry: dict[str, ProcessRecord], *, namespace: str, handle: str) -> str:
    """Poll the status of a backgrounded shell command in *registry*."""
    record = registry.get(handle)
    if record is None or record.namespace != namespace:
        return f"Error: Unknown handle '{handle}'"

    elapsed = time.monotonic() - record.started_at

    if record.finished:
        output = record.stdout_buf.render(tail=record.tail)
        errors = record.stderr_buf.render()
        result = f"Status: FINISHED (exit code {record.return_code}, ran for {elapsed:.1f}s)\n"
        if record.return_code != 0 and errors:
            result += f"Stderr:\n{errors}\n"
        result += f"Output:\n{output}"
        return result

    partial = record.stdout_buf.render(tail=50)
    return (
        f"Status: RUNNING (PID {record.pid}, elapsed {elapsed:.1f}s)\n"
        f"Partial output ({len(record.stdout_buf)} lines buffered):\n{partial}"
    )


def kill_command(registry: dict[str, ProcessRecord], *, namespace: str, handle: str, force: bool = False) -> str:
    """Kill a backgrounded shell command tracked in *registry*."""
    record = registry.get(handle)
    if record is None or record.namespace != namespace:
        return f"Error: Unknown handle '{handle}'"

    if record.finished:
        return f"Process already finished (exit code {record.return_code})"

    sig = signal.SIGKILL if force else signal.SIGTERM
    sig_name = "SIGKILL" if force else "SIGTERM"
    try:
        os.killpg(record.pid, sig)
    except (ProcessLookupError, PermissionError):
        return f"Process {record.pid} already exited"

    action = "Force-killed" if force else "Terminated"
    return f"{action} process {record.pid} ({sig_name} sent). Use check_shell_command('{handle}') to confirm exit."


def kill_all_records(registry: dict[str, ProcessRecord]) -> None:
    """Kill every unfinished process group in *registry* and clear it."""
    for record in registry.values():
        if record._monitor_task is not None:
            record._monitor_task.cancel()
        if not record.finished:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(record.pid, signal.SIGKILL)
    registry.clear()


def _sweep_stale_records(registry: dict[str, ProcessRecord]) -> None:
    """Remove records that finished more than 10 minutes ago."""
    now = time.monotonic()
    stale = [
        h
        for h, r in registry.items()
        if r.finished and r.finished_at is not None and (now - r.finished_at) > _STALE_RECORD_SECONDS
    ]
    for h in stale:
        registry.pop(h, None)


async def _read_stream(stream: asyncio.StreamReader | None, buf: _OutputBuffer) -> None:
    """Read bounded chunks from an async stream into *buf* until EOF."""
    if stream is None:
        return

    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    while True:
        chunk = await stream.read(_STREAM_READ_CHUNK_BYTES)
        if not chunk:
            break
        buf.append(decoder.decode(chunk))

    buf.append(decoder.decode(b"", final=True))


async def _cancel_pending_tasks(*tasks: asyncio.Task[None]) -> None:
    """Cancel any pending tasks and wait for them to finish."""
    pending_tasks = [task for task in tasks if not task.done()]
    for task in pending_tasks:
        task.cancel()
    for task in pending_tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _await_foreground_process_exit(
    process: asyncio.subprocess.Process,
    *,
    timeout_seconds: float,
) -> None:
    """Wait for the foreground process to exit without depending on pipe EOF."""
    if process.returncode is not None:
        return
    if timeout_seconds <= 0:
        raise TimeoutError

    wait_task = asyncio.create_task(process.wait())
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    try:
        while process.returncode is None:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError
            done_tasks, _pending_tasks = await asyncio.wait(
                (wait_task,),
                timeout=min(_PROCESS_EXIT_POLL_INTERVAL_SECONDS, remaining),
            )
            if wait_task in done_tasks:
                await wait_task
                return
    finally:
        if not wait_task.done():
            wait_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await wait_task


async def _await_reader_tasks_with_grace(
    stdout_reader: asyncio.Task[None],
    stderr_reader: asyncio.Task[None],
    *,
    grace_seconds: float,
) -> None:
    """Drain reader tasks briefly after foreground exit, then stop waiting for EOF."""
    reader_tasks = (stdout_reader, stderr_reader)
    done_tasks, pending_tasks = await asyncio.wait(reader_tasks, timeout=grace_seconds)

    try:
        for task in done_tasks:
            await task
    finally:
        for task in pending_tasks:
            task.cancel()
        for task in pending_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def _terminate_process_group(
    process: asyncio.subprocess.Process,
    *,
    grace_period: float = 0.5,
) -> None:
    """Terminate a subprocess process group, escalating to SIGKILL if needed."""
    if process.returncode is not None:
        return

    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGTERM)

    try:
        await asyncio.wait_for(process.wait(), timeout=grace_period)
    except TimeoutError:
        pass
    else:
        return

    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=grace_period)


async def _monitor_process(
    registry: dict[str, ProcessRecord],
    handle: str,
    process: asyncio.subprocess.Process,
    stdout_reader: asyncio.Task[None],
    stderr_reader: asyncio.Task[None],
) -> None:
    """Wait for a backgrounded process to exit and update its record."""
    try:
        await process.wait()
    finally:
        await asyncio.wait([stdout_reader, stderr_reader], timeout=2.0)
        await _cancel_pending_tasks(stdout_reader, stderr_reader)
        record = registry.get(handle)
        if record is not None:
            record.finished = True
            record.finished_at = time.monotonic()
            record.return_code = process.returncode
