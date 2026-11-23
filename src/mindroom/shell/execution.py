"""Shell command execution core: foreground runs that stop at their timeout, or background handles.

This module owns the process spawning, output buffering, and background
handle registry shared by the shell toolkit (local execution), the Desktop
shell, and the worker-local shell supervisor process. It must stay importable
without the heavy `mindroom.tools` package so the supervisor process starts fast.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import errno
import functools
import itertools
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, Literal

from mindroom.agent_cli.shell_contract import SHELL_FUNCTION
from mindroom.shell.output_capture import (
    CapturedShellStream,
    ShellOutputCapture,
    ShellOutputDestination,
    format_shell_completion,
    format_shell_timeout,
)
from mindroom.tool_jobs.control import job_owns_execution

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping

_DEFAULT_RUN_TIMEOUT_SECONDS = 120
# Time a worker shell request needs beyond its command's foreground wait to deliver the answer.
_SHELL_REQUEST_GRACE_SECONDS = 30.0

_STALE_RECORD_SECONDS = 600  # 10 minutes
MAX_BACKGROUNDED = 16
_MAX_TOTAL_BACKGROUNDED = 128
_LOG_MAX_BYTES = 4 * 1024 * 1024
_LOG_MAX_AGE_SECONDS = 24 * 3600
_LOG_MAX_FILES = 64
_MAX_OUTPUT_LINES = 10_000
_MAX_OUTPUT_BYTES = 50 * 1024
_STREAM_READ_CHUNK_BYTES = 8192
# The most bytes of a streamed command's output handed on at once.
_STREAM_CHUNK_BYTES = 32 * 1024
# Once a streamed command's process group was killed, only what its pipes still held is read. More bytes than pipes
# hold, or bytes still waited for after this long, come from a process that left the group, and are not read.
_KILLED_GROUP_PIPE_BYTES = 8 * 1024 * 1024
_KILLED_GROUP_READ_SECONDS = 1.0
# How long one chunk of a streamed command's stdin waits to leave before its sender is told to send it again.
_STDIN_WRITE_SECONDS = 20.0
_PROCESS_EXIT_POLL_INTERVAL_SECONDS = 0.05
_POST_EXIT_READER_GRACE_SECONDS = 0.5
# A running command reports the tail of its output at most this often, and only when it changed; replies show it live.
_PROGRESS_INTERVAL_SECONDS = 0.5
_CALLER_HANDLE_RE = re.compile(r"shell:[0-9a-f]{32}")
# Numbers every append to any output buffer, so a reader can tell which stream printed last.
_APPEND_SEQUENCE = itertools.count(1)


@dataclass
class _OutputBuffer:
    """Bound shell output by both line count and encoded byte size."""

    max_lines: int = _MAX_OUTPUT_LINES
    max_bytes: int = _MAX_OUTPUT_BYTES
    chunks: deque[str] = field(default_factory=deque)
    byte_count: int = 0
    # Which caps dropped output, and every line the command printed, so a notice can say what was left out.
    bytes_capped: bool = False
    lines_capped: bool = False
    newlines: int = 0
    open_line: bool = False
    # The number of this buffer's latest append, so a reader can tell new output from a full buffer that rotated to
    # the same size, and which stream printed last.
    last_append: int = 0

    def append(self, text: str) -> None:
        if not text:
            return
        self.last_append = next(_APPEND_SEQUENCE)
        self.newlines += text.count("\n")
        self.open_line = not text.endswith("\n")

        encoded = text.encode("utf-8", errors="replace")
        if len(encoded) > self.max_bytes:
            text = encoded[-self.max_bytes :].decode("utf-8", errors="ignore")
            encoded = text.encode("utf-8", errors="replace")
            self.bytes_capped = True

        self.chunks.append(text)
        self.byte_count += len(encoded)

        while self.chunks and self.byte_count > self.max_bytes:
            removed = self.chunks.popleft()
            self.byte_count -= len(removed.encode("utf-8", errors="replace"))
            self.bytes_capped = True

        self._trim_to_max_lines()

    def lines(self) -> list[str]:
        """Return every retained line; a final newline ends the last line rather than starting another."""
        text = "".join(self.chunks)
        return text.removesuffix("\n").split("\n") if text else []

    def render(self, *, tail: int | None = None) -> str:
        """Return the last ``tail`` retained lines, after one notice of what was left out."""
        return "\n".join(part for part in self.render_parts(tail=tail) if part)

    def render_parts(self, *, tail: int | None = None, stream: str = "") -> tuple[str, str]:
        """Return the notice of what was left out, empty when nothing was, and the last ``tail`` retained lines."""
        lines = self.lines()
        shown = lines if tail is None else lines[-tail:]
        caps = [f"{self.max_bytes} bytes"] if self.bytes_capped else []
        if self.lines_capped:
            caps.append(f"{self.max_lines} lines")
        printed = self.newlines + self.open_line
        if len(shown) == printed and not caps:
            # Whole output ends where its last non-blank line does, as it always has.
            return "", "\n".join(shown).rstrip("\n")
        cuts = []
        if len(shown) < printed:
            cuts.append(f"Showing the last {len(shown)} of {printed} {stream + ' ' if stream else ''}lines")
        if caps:
            noun = stream or "output"
            cuts.append(f"{noun if cuts else noun.capitalize()} truncated to the last {' and '.join(caps)}")
        advice = (
            "Redirect command output to a file for complete results."
            if caps
            else "Pass a larger tail, or narrow the command."
        )
        return f"[{'; '.join(cuts)}. {advice}]", "\n".join(shown)

    def __len__(self) -> int:
        return len(self.lines())

    def _trim_to_max_lines(self) -> None:
        text = "".join(self.chunks)
        lines = text.split("\n")
        # A final newline ends the last line rather than starting another, and stays for the next append.
        keep = self.max_lines + text.endswith("\n")
        if len(lines) <= keep:
            return

        text = "\n".join(lines[-keep:])
        self.chunks = deque([text])
        self.byte_count = len(text.encode("utf-8", errors="replace"))
        self.lines_capped = True


@dataclass(frozen=True)
class BackgroundLogPolicy:
    """Where and how much output an auto-backgrounded shell command writes to its live log."""

    log_dir: str
    display_root: str | None = None
    max_bytes: int = _LOG_MAX_BYTES


class _BackgroundLogFile:
    """Bounded live log file for one auto-backgrounded shell process."""

    def __init__(self, *, path: Path, file: BinaryIO, max_bytes: int) -> None:
        self.path = path
        self._file = file
        self._max_bytes = max_bytes
        self._written_bytes = 0
        self._truncated = False
        self.closed = False

    def write(self, text: str) -> None:
        if self.closed or self._truncated or not text:
            return
        data = text.encode("utf-8", errors="replace")
        remaining = self._max_bytes - self._written_bytes
        try:
            if len(data) < remaining:
                self._file.write(data)
                self._written_bytes += len(data)
            else:
                if remaining > 0:
                    clipped = data[:remaining].decode("utf-8", errors="ignore").encode("utf-8")
                    self._file.write(clipped)
                    self._written_bytes += len(clipped)
                self._file.write(self._truncation_notice().encode("utf-8"))
                self._truncated = True
            self._file.flush()
        except OSError:
            self._truncated = True

    def finish(self, trailer: str | None = None) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            if trailer is not None:
                self._file.write(trailer.encode("utf-8"))
                self._file.flush()
        except OSError:
            pass
        finally:
            with contextlib.suppress(OSError):
                self._file.close()

    def _truncation_notice(self) -> str:
        limit = (
            f"{self._max_bytes // (1024 * 1024)} MiB"
            if self._max_bytes % (1024 * 1024) == 0
            else f"{self._max_bytes} bytes"
        )
        return f"\n[Log truncated at {limit} limit; command output continues without being logged.]\n"


@dataclass
class _LogSinkRef:
    """Mutable indirection letting stream readers attach to a lazily opened log file."""

    sink: _BackgroundLogFile | None = None


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
    output_capture: ShellOutputCapture | None = None
    output_receipt: str | None = None
    log_path: str | None = None
    max_runtime_seconds: float | None = None
    _log_file: _BackgroundLogFile | None = field(default=None, repr=False)
    _stop_reason: Literal["timeout", "scope_ended"] | None = field(default=None, repr=False)
    _monitor_task: asyncio.Task[None] | None = field(default=None, repr=False)


@dataclass
class ShellBackgroundScope:
    """Turn-scoped owner for auto-backgrounded local shell processes."""

    scope_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    records: dict[str, ProcessRecord] = field(default_factory=dict)
    had_backgrounded: bool = False
    closed: bool = False

    async def close(self) -> None:
        """Stop unfinished processes in this scope, flush log trailers, and clear records."""
        self.closed = True
        records = list(self.records.values())
        if not records:
            return
        try:
            to_stop = [r for r in records if not r.finished and r.process.returncode is None]
            for record in to_stop:
                record._stop_reason = record._stop_reason or "scope_ended"
            if to_stop:
                await asyncio.gather(
                    *(_terminate_process_group(r.process, whole_group=True) for r in to_stop),
                    return_exceptions=True,
                )
            monitors = [r._monitor_task for r in records if r._monitor_task is not None and not r._monitor_task.done()]
            if monitors:
                await asyncio.gather(*monitors, return_exceptions=True)
        finally:
            for record in records:
                _kill_record(record, stop_reason="scope_ended")
            self.records.clear()


_SHELL_BACKGROUND_SCOPE: ContextVar[ShellBackgroundScope | None] = ContextVar(
    "shell_background_scope",
    default=None,
)


def current_shell_background_scope() -> ShellBackgroundScope | None:
    """Return the active turn-scoped shell background owner, if any."""
    return _SHELL_BACKGROUND_SCOPE.get()


@contextlib.contextmanager
def bind_shell_background_scope(scope: ShellBackgroundScope | None) -> Iterator[ShellBackgroundScope | None]:
    """Bind *scope* (or ``None`` to clear) for the duration of the context block."""
    token = _SHELL_BACKGROUND_SCOPE.set(scope)
    try:
        yield scope
    finally:
        _SHELL_BACKGROUND_SCOPE.reset(token)


@contextlib.asynccontextmanager
async def shell_background_scope() -> AsyncIterator[ShellBackgroundScope]:
    """Create, bind, and close a fresh turn-scoped shell background owner."""
    scope = ShellBackgroundScope()
    try:
        with bind_shell_background_scope(scope):
            yield scope
    finally:
        close_task = asyncio.create_task(scope.close())
        cancelled: asyncio.CancelledError | None = None
        while not close_task.done():
            try:
                await asyncio.shield(close_task)
            except asyncio.CancelledError as exc:
                cancelled = exc
        await close_task
        if cancelled is not None:
            raise cancelled


def _progress_lines(buffer: _OutputBuffer) -> str:
    """Return what a running command printed so far, each line in the state it was redrawn to last.

    It is reported whole, so whoever shows its tail can mask a credential whose label sits above the tail.
    """
    return "\n".join(line.rstrip("\r").rsplit("\r", 1)[-1] for line in "".join(buffer.chunks).rstrip("\n").split("\n"))


class _OutputProgress:
    """Report a running command's latest output, at most twice a second and only when it changed."""

    def __init__(self, report: Callable[[str], None], stdout_buf: _OutputBuffer, stderr_buf: _OutputBuffer) -> None:
        self._report = report
        self._stdout_buf = stdout_buf
        self._stderr_buf = stderr_buf
        self._sent = ""
        self._last_appends = (0, 0)
        self._due = time.monotonic() + _PROGRESS_INTERVAL_SECONDS

    def poll(self) -> None:
        now = time.monotonic()
        last_appends = (self._stdout_buf.last_append, self._stderr_buf.last_append)
        if now < self._due or last_appends == self._last_appends:
            return
        self._due = now + _PROGRESS_INTERVAL_SECONDS
        self._last_appends = last_appends
        text = _progress_lines(self._stdout_buf)
        if errors := _progress_lines(self._stderr_buf):
            # The stream that printed last ends the report, where the live view shows its tail.
            if not text:
                text = f"Stderr:\n{errors}"
            elif self._stderr_buf.last_append > self._stdout_buf.last_append:
                text = f"{text}\nStderr:\n{errors}"
            else:
                text = f"Stderr:\n{errors}\nStdout:\n{text}"
        if text and text != self._sent:
            self._sent = text
            self._report(text)


@dataclass(frozen=True)
class ShellRunResult:
    """Outcome of one run_command call.

    ``handle`` is set only when the command timed out and was registered as a
    background record, or finished in time and ``register_finished`` kept it as
    a finished record, so callers that could not deliver the message can roll
    the registration back with ``discard_background_record``.
    ``output_file_handled`` means execution owns publication or its error receipt;
    the generic output-file wrapper must not write the returned message again.
    """

    message: str
    handle: str | None = None
    output_file_handled: bool = False
    execution_error: bool = False


def default_run_timeout_seconds() -> float:
    """Return the current foreground wait / auto-background threshold in seconds."""
    return float(_DEFAULT_RUN_TIMEOUT_SECONDS)


def shell_call_timeout(timeout: float | None) -> float | None:
    """Return when a shell call's command is stopped: its own timeout, none for a job's call, else the default."""
    if timeout is not None:
        return timeout
    return None if job_owns_execution() else _DEFAULT_RUN_TIMEOUT_SECONDS


def shell_job_wait_seconds(registered_toolkit: str | None, function_name: str) -> float | None:
    """Return how long a reply waits for a shell command that runs as a job when its call sets no wait, else None.

    The reply waits as long as a command outside a job may run, then the command continues as a job, so a command
    that runs longer than the model expected never holds the conversation longer than one outside a job would.
    """
    if registered_toolkit == "shell" and function_name == SHELL_FUNCTION:
        return float(_DEFAULT_RUN_TIMEOUT_SECONDS)
    return None


def shell_request_budget_seconds(
    tool_name: str,
    function_name: str,
    kwargs: Mapping[str, Any],
    *,
    auto_background: bool = False,
) -> float:
    """Return how long one worker request outside a job may take for this shell call, or 0 when it has no foreground wait.

    Callers give a job's call no budget, as `shell_call_timeout` gives its command no limit.
    With ``auto_background``, foreground waiting is capped at the default auto-background wait.
    """
    if tool_name != "shell" or function_name != SHELL_FUNCTION:
        return 0.0
    raw_timeout = kwargs.get("timeout")
    try:
        timeout = _DEFAULT_RUN_TIMEOUT_SECONDS if raw_timeout is None else max(0.0, float(raw_timeout))
    except (TypeError, ValueError):
        timeout = _DEFAULT_RUN_TIMEOUT_SECONDS
    if auto_background:
        timeout = min(timeout, float(_DEFAULT_RUN_TIMEOUT_SECONDS))
    return timeout + _SHELL_REQUEST_GRACE_SECONDS


async def run_command(
    registry: dict[str, ProcessRecord],
    *,
    namespace: str,
    argv: list[str],
    env: dict[str, str],
    cwd: str | None,
    tail: int,
    timeout: float | None,  # noqa: ASYNC109
    on_timeout: Literal["background", "kill"] = "background",
    on_progress: Callable[[str], None] | None = None,
    handle: str | None = None,
    handle_reservations: set[str] | None = None,
    output_destination: ShellOutputDestination | None = None,
    output_capture: ShellOutputCapture | None = None,
    stdin: int | None = None,
    stderr: int = asyncio.subprocess.PIPE,
    kill_group_after_exit: bool = False,
    register_finished: bool = False,
    launch_failure_exits: bool = False,
    max_runtime_seconds: float | None = None,
    background_log: BackgroundLogPolicy | None = None,
) -> ShellRunResult:
    """Run one shell command; return output, an error message, or a background handle.

    When the command completes within ``timeout`` seconds the last ``tail``
    lines of stdout, then of any stderr, are returned; ``None`` waits however
    long it runs. On non-zero exit, stdout is preserved with all of stderr. When the
    timeout is exceeded, ``on_timeout="kill"`` stops the command's whole
    process group and returns its output so far; otherwise the process keeps
    running under *registry* and a handle string is returned for
    ``check_command``/``kill_command``.
    Cancellation stops a running command's process group and drops any registered handle.
    ``on_progress`` receives the tail of the output while the command runs in the foreground.

    Callers that manage output themselves pass ``output_capture`` instead of
    ``output_destination``. ``stdin`` and ``stderr`` go to the subprocess as
    given, so ``stderr=asyncio.subprocess.STDOUT`` merges both streams. With
    ``kill_group_after_exit``, whatever remains of the process group is killed
    once the foreground process exits or is cancelled. With ``register_finished``,
    a command that finishes within ``timeout`` is also registered, as a finished
    record whose handle the caller can check or discard later. With
    ``launch_failure_exits``, a program that is missing or cannot be executed
    ends the call as a shell ends it: exit code 127 or 126, the reason on stderr.
    """
    _sweep_stale_records(registry)
    if handle is not None:
        if _CALLER_HANDLE_RE.fullmatch(handle) is None:
            return ShellRunResult(message="Error: Invalid caller-supplied shell handle.")
        if handle in registry or (handle_reservations is not None and handle in handle_reservations):
            return ShellRunResult(message=f"Error: Shell handle '{handle}' is already registered.")
        if handle_reservations is None:
            return ShellRunResult(message="Error: Caller-supplied shell handle reservations are unavailable.")
        handle_reservations.add(handle)

    try:
        return await _run_command_after_reservation(
            registry,
            namespace=namespace,
            argv=argv,
            env=env,
            cwd=cwd,
            tail=tail,
            timeout=timeout,
            on_timeout=on_timeout,
            on_progress=on_progress,
            handle=handle,
            output_destination=output_destination,
            output_capture=output_capture,
            stdin=stdin,
            stderr=stderr,
            kill_group_after_exit=kill_group_after_exit,
            register_finished=register_finished,
            launch_failure_exits=launch_failure_exits,
            max_runtime_seconds=max_runtime_seconds,
            background_log=background_log,
        )
    finally:
        if handle is not None and handle_reservations is not None:
            handle_reservations.discard(handle)


async def _start_command(
    argv: list[str],
    env: dict[str, str],
    cwd: str | None,
    *,
    stdin: int | None,
    stderr: int = asyncio.subprocess.PIPE,
) -> asyncio.subprocess.Process:
    """Start one command at idle priority as the leader of its own session, with its stdout on a pipe."""
    spawn_argv = _idle_launch_argv(argv, env, cwd)
    process = await asyncio.create_subprocess_exec(
        *spawn_argv,
        stdin=stdin,
        stdout=asyncio.subprocess.PIPE,
        stderr=stderr,
        cwd=cwd,
        env=env,
        start_new_session=True,
    )
    # A busy command, such as a build, must not take the CPU from the process that runs MindRoom; processes the
    # command starts later inherit the priority.
    with contextlib.suppress(OSError):
        os.setpriority(os.PRIO_PGRP, process.pid, 19)
    if sys.platform == "linux" and spawn_argv is argv:
        _schedule_process_tree_idle(process.pid)
    return process


class CommandStdin:
    """The stdin of a command `stream_command` runs, written in chunks numbered from 0, one at a time."""

    def __init__(self) -> None:
        self._writer: asyncio.StreamWriter | None = None
        self._next = 0
        self._writing = False

    async def write(self, seq: int, data: bytes, *, eof: bool) -> bool:
        """Write chunk ``seq`` once, however often it arrives, and with ``eof`` close stdin after it.

        Return whether stdin takes a next chunk, once this one has left for the command: a command that reads
        slowly slows its writer down. One chunk at most waits to leave, and for a bounded time: TimeoutError says
        this one has not left yet, or that the one before it still waits, and that it is to be sent again.
        """
        writer = self._writer
        if writer is None:
            return False
        if seq > self._next:
            msg = f"stdin chunk {seq} arrived before chunk {self._next}"
            raise ValueError(msg)
        if self._writing:
            raise TimeoutError
        self._writing = True
        try:
            if seq == self._next:
                self._next += 1
                writer.write(data)
                if eof:
                    self._writer = None
                    writer.write_eof()
                    return False
            async with asyncio.timeout(_STDIN_WRITE_SECONDS):
                await writer.drain()
        except TimeoutError:
            raise
        except OSError:
            # The command closed its stdin or ended.
            self._writer = None
        finally:
            self._writing = False
        return self._writer is not None


async def _start_streamed_command(
    argv: list[str],
    env: dict[str, str],
    cwd: str | None,
    on_start: Callable[[], Awaitable[None]],
    on_output: Callable[[Literal["out", "err"], bytes], Awaitable[None]],
) -> asyncio.subprocess.Process | int:
    """Start a streamed command; one whose program is missing or cannot be executed gets its exit code back."""
    try:
        return await _start_command(argv, env, cwd, stdin=asyncio.subprocess.PIPE)
    except OSError as exc:
        if (failure := _launch_failure_exit(argv, env, cwd, exc)) is None:
            raise
        await on_start()
        await on_output("err", f"{failure[1]}\n".encode())
        return failure[0]


class _PipeReaders:
    """The tasks that hand each read of a streamed command's stdout and stderr to ``on_output``, unchanged."""

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        on_output: Callable[[Literal["out", "err"], bytes], Awaitable[None]],
    ) -> None:
        self._on_output = on_output
        self._moved = 0
        self._idle: set[str] = set()
        # When the command's process group was killed, and how many bytes its pipes may still give.
        self.killed_at: float | None = None
        self._left = _KILLED_GROUP_PIPE_BYTES
        self.tasks = {
            asyncio.create_task(self._read(process.stdout, "out")),
            asyncio.create_task(self._read(process.stderr, "err")),
        }

    async def _read(self, stream: asyncio.StreamReader | None, kind: Literal["out", "err"]) -> None:
        assert stream is not None
        loop, waited = asyncio.get_running_loop(), 0.0
        while True:
            self._idle.add(kind)
            asked = loop.time()
            chunk = await stream.read(_STREAM_CHUNK_BYTES)
            self._idle.discard(kind)
            if self.killed_at is not None:
                # Time spent handing bytes on is the reader's and does not count: only the wait for new ones does.
                waited += loop.time() - max(asked, self.killed_at)
                self._left -= len(chunk)
                if waited > _KILLED_GROUP_READ_SECONDS or self._left < 0:
                    return
            if not chunk:
                return
            await self._on_output(kind, chunk)
            self._moved += len(chunk)

    async def drain(self, until: float | None) -> None:
        """Wait, at the pace of ``on_output``, until both pipes ended, stayed silent, or are read no further."""
        pending = self.tasks
        while pending:
            seen = self._moved
            async with asyncio.timeout_at(until):
                _, pending = await asyncio.wait(pending, timeout=_POST_EXIT_READER_GRACE_SECONDS)
            # Pipes that stay silent once their command ended are held open by a process it left behind.
            if self._moved == seen and len(self._idle) == len(pending):
                break


async def stream_command(
    argv: list[str],
    env: dict[str, str],
    cwd: str | None,
    *,
    timeout: float | None,  # noqa: ASYNC109
    stdin: CommandStdin,
    on_start: Callable[[], Awaitable[None]],
    on_output: Callable[[Literal["out", "err"], bytes], Awaitable[None]],
    on_exit: Callable[[int, bool], Awaitable[None]],
    on_stopped: Callable[[], None],
) -> None:
    """Run one command on pipes of its own, handing ``on_output`` each read of its stdout and stderr unchanged.

    ``on_start`` is awaited once the command runs and ``stdin`` reaches it. A slow ``on_output`` slows the command
    down instead of letting its output pile up. ``on_exit`` gets the exit code and whether ``timeout`` stopped the
    command, once its leader and both pipes ended; a pipe only a silent process it left behind keeps open is not
    waited for. ``timeout`` runs until then, and a stop, at ``timeout`` or by cancellation, kills the whole process
    group, also once its leader ended. What the group wrote before ``timeout`` stopped it reaches ``on_output``
    before ``on_exit``, however slowly it is taken, and what a process outside the group writes after that is not
    waited for; ``on_stopped`` is called when a command that had not ended was cancelled and its group is gone,
    after the last ``on_output``. A program that is missing or cannot be executed ends as a shell ends it: the
    reason on stderr, exit code 127 or 126.
    """
    process = await _start_streamed_command(argv, env, cwd, on_start, on_output)
    if isinstance(process, int):
        await on_exit(process, False)
        return
    loop = asyncio.get_running_loop()
    deadline = None if timeout is None else loop.time() + timeout
    readers = _PipeReaders(process, on_output)
    ended = timed_out = False
    try:
        stdin._writer = process.stdin
        await on_start()
        try:
            await _await_process_exit(process, timeout_seconds=None if deadline is None else deadline - loop.time())
            await readers.drain(deadline)
        except TimeoutError:
            timed_out = True
            await _terminate_process_group(process, whole_group=True)
            # The killed group closed its pipes, so what it had written is all read, and then they end.
            readers.killed_at = loop.time()
            await readers.drain(None)
        # The command ended here: a cancel that comes while its exit waits to be taken stops nothing it left behind.
        ended = True
        # Nothing follows the exit: a reader still at a pipe that a process left behind holds reads no more.
        await _cancel_pending_tasks(*readers.tasks)
        await on_exit(-signal.SIGKILL if process.returncode is None else process.returncode, timed_out)
    finally:
        stdin._writer = None
        stopped = not ended and await _terminate_process_group(process, whole_group=True)
        await _cancel_pending_tasks(*readers.tasks)
        if stopped:
            on_stopped()
        if process.stdin is not None:
            process.stdin.close()


async def _run_command_after_reservation(  # noqa: C901, PLR0912, PLR0915
    registry: dict[str, ProcessRecord],
    *,
    namespace: str,
    argv: list[str],
    env: dict[str, str],
    cwd: str | None,
    tail: int,
    timeout: float | None,  # noqa: ASYNC109
    on_timeout: Literal["background", "kill"],
    on_progress: Callable[[str], None] | None,
    handle: str | None,
    output_destination: ShellOutputDestination | None,
    output_capture: ShellOutputCapture | None,
    stdin: int | None,
    stderr: int,
    kill_group_after_exit: bool,
    register_finished: bool,
    launch_failure_exits: bool,
    max_runtime_seconds: float | None,
    background_log: BackgroundLogPolicy | None,
) -> ShellRunResult:
    """Spawn one command after any caller-supplied handle is reserved."""
    capture = output_capture
    try:
        if capture is None and output_destination is not None:
            capture = ShellOutputCapture(output_destination)
        started_at = time.monotonic()
    except asyncio.CancelledError:
        if capture is not None:
            capture.close()
        raise
    except Exception as exc:
        if capture is not None:
            capture.close()
        return ShellRunResult(message=f"Error: {exc}", execution_error=True)
    try:
        process = await _start_command(argv, env, cwd, stdin=stdin, stderr=stderr)
    except asyncio.CancelledError:
        if capture is not None:
            capture.close()
        raise
    except Exception as exc:
        try:
            exited = _launch_failure(argv, env, cwd, exc, capture) if launch_failure_exits else None
            return exited or ShellRunResult(message=f"Error: {exc}", execution_error=True)
        finally:
            if capture is not None:
                capture.close()

    stdout_buf = _OutputBuffer()
    stderr_buf = _OutputBuffer()
    log_sink = _LogSinkRef() if background_log is not None else None
    stdout_reader = asyncio.create_task(
        _read_stream(process.stdout, stdout_buf, capture.stdout if capture else None, log_sink),
    )
    stderr_reader = asyncio.create_task(
        _read_stream(process.stderr, stderr_buf, capture.stderr if capture else None, log_sink),
    )

    try:
        try:
            await _await_process_exit(
                process,
                timeout_seconds=timeout,
                on_poll=_OutputProgress(on_progress, stdout_buf, stderr_buf).poll if on_progress is not None else None,
            )
        except TimeoutError:
            assert timeout is not None
            if on_timeout == "kill":
                return await _stop_timed_out_process(
                    process,
                    stdout_buf=stdout_buf,
                    stderr_buf=stderr_buf,
                    stdout_reader=stdout_reader,
                    stderr_reader=stderr_reader,
                    output_capture=capture,
                    tail=tail,
                    timeout=timeout,
                )
            return await _background_process(
                registry,
                namespace=namespace,
                argv=argv,
                process=process,
                stdout_buf=stdout_buf,
                stderr_buf=stderr_buf,
                tail=tail,
                timeout=timeout,
                requested_handle=handle,
                stdout_reader=stdout_reader,
                stderr_reader=stderr_reader,
                output_capture=capture,
                started_at=started_at,
                max_runtime_seconds=max_runtime_seconds,
                background_log=background_log,
                log_sink=log_sink,
            )

        if kill_group_after_exit:
            _kill_process_group(process.pid)
        await _await_reader_tasks_with_grace(
            stdout_reader,
            stderr_reader,
            grace_seconds=_POST_EXIT_READER_GRACE_SECONDS,
        )
    except asyncio.CancelledError:
        if process.returncode is None:
            await _terminate_process_group(process)
        if kill_group_after_exit:
            _kill_process_group(process.pid)
        await _cancel_pending_tasks(stdout_reader, stderr_reader)
        if capture is not None:
            capture.close()
        raise

    return_code = process.returncode
    assert return_code is not None
    finished_handle = None
    if register_finished:
        finished_handle = handle or _unused_handle(registry)
        registry[finished_handle] = ProcessRecord(
            namespace=namespace,
            handle=finished_handle,
            pid=process.pid,
            args=argv,
            process=process,
            stdout_buf=stdout_buf,
            stderr_buf=stderr_buf,
            tail=tail,
            finished=True,
            finished_at=time.monotonic(),
            return_code=return_code,
            output_capture=capture,
        )
    if capture is not None:
        capture.incomplete = stdout_reader.cancelled() or stderr_reader.cancelled()
        try:
            return ShellRunResult(
                message=capture.publish(return_code),
                handle=finished_handle,
                output_file_handled=True,
            )
        finally:
            capture.close()
    if return_code:
        # A failure keeps its whole stderr, which explains it.
        errors = stderr_buf.render() if stderr_buf else None
        message = format_shell_completion(stdout_buf.render(tail=tail), errors, return_code=return_code)
    else:
        message = _success_message(stdout_buf, stderr_buf, tail)
    return ShellRunResult(message=message, handle=finished_handle)


def _success_message(stdout_buf: _OutputBuffer, stderr_buf: _OutputBuffer, tail: int) -> str:
    """Return a success's stdout, then its stderr, both tailed, after the notice of anything either left out."""
    output_notice, output = stdout_buf.render_parts(tail=tail)
    errors_notice, errors = stderr_buf.render_parts(tail=tail, stream="stderr")
    completion = format_shell_completion(output, errors if stderr_buf else None, return_code=0)
    return "\n".join(part for part in (output_notice, errors_notice, completion) if part)


async def _stop_timed_out_process(
    process: asyncio.subprocess.Process,
    *,
    stdout_buf: _OutputBuffer,
    stderr_buf: _OutputBuffer,
    stdout_reader: asyncio.Task[None],
    stderr_reader: asyncio.Task[None],
    output_capture: ShellOutputCapture | None,
    tail: int,
    timeout: float,  # noqa: ASYNC109
) -> ShellRunResult:
    """Stop a command at its timeout and report its output so far; a stopped command saves no output file."""
    await _terminate_process_group(process)
    await _await_reader_tasks_with_grace(stdout_reader, stderr_reader, grace_seconds=_POST_EXIT_READER_GRACE_SECONDS)
    message = format_shell_timeout(
        stdout_buf.render(tail=tail),
        stderr_buf.render() if stderr_buf else None,
        timeout=timeout,
        no_output_file=output_capture is not None,
    )
    if output_capture is None:
        return ShellRunResult(message=message)
    output_capture.incomplete = True
    output_capture.interruption = message
    try:
        return ShellRunResult(message=output_capture.publish(process.returncode), output_file_handled=True)
    finally:
        output_capture.close()


def _unused_handle(registry: dict[str, ProcessRecord]) -> str:
    while (handle := f"shell:{uuid.uuid4().hex[:8]}") in registry:
        pass
    return handle


def _open_log_dir_fd(log_dir: Path) -> int:
    root, parts = (
        (log_dir.parent.parent, (".mindroom", log_dir.name))
        if log_dir.parent.name == ".mindroom"
        else (log_dir.parent, (log_dir.name,))
    )
    root.mkdir(parents=True, exist_ok=True)
    current_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    for part in parts:
        try:
            with contextlib.suppress(FileExistsError):
                os.mkdir(part, 0o700, dir_fd=current_fd)
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current_fd)
        finally:
            os.close(current_fd)
        current_fd = next_fd
    return current_fd


def _sweep_old_log_files(log_dir: Path, *, active_paths: set[str]) -> None:
    cutoff = time.time() - _LOG_MAX_AGE_SECONDS
    closed_logs: list[tuple[float, Path]] = []
    active_in_dir = 0
    with contextlib.suppress(OSError), os.scandir(log_dir) as entries:
        for entry in entries:
            if not entry.name.endswith(".log"):
                continue
            with contextlib.suppress(OSError):
                if not entry.is_file(follow_symlinks=False):
                    continue
                if entry.path in active_paths:
                    active_in_dir += 1
                    continue
                entry_path = Path(entry.path)
                mtime = entry.stat(follow_symlinks=False).st_mtime
                if mtime < cutoff:
                    entry_path.unlink()
                else:
                    closed_logs.append((mtime, entry_path))
    closed_logs.sort(key=lambda item: item[0], reverse=True)
    for _, old_path in closed_logs[max(0, _LOG_MAX_FILES - active_in_dir) :]:
        with contextlib.suppress(OSError):
            old_path.unlink()


def _open_background_log(
    policy: BackgroundLogPolicy,
    *,
    pid: int,
    handle: str,
    registry: dict[str, ProcessRecord],
) -> _BackgroundLogFile:
    log_dir = Path(policy.log_dir)
    filename = f"{pid}-{handle.removeprefix('shell:')}.log"
    log_path = log_dir / filename
    dir_fd = _open_log_dir_fd(log_dir)
    try:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW
        file_fd = os.open(filename, flags, 0o600, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)
    try:
        stream = os.fdopen(file_fd, "wb")
    except BaseException:
        os.close(file_fd)
        raise
    active_paths = {
        record.log_path
        for record in registry.values()
        if record.log_path is not None
        and (not record.finished or (record._log_file is not None and not record._log_file.closed))
    } | {str(log_path)}
    _sweep_old_log_files(log_dir, active_paths=active_paths)
    return _BackgroundLogFile(path=log_path, file=stream, max_bytes=policy.max_bytes)


def _seed_background_log(log_file: _BackgroundLogFile, stdout_buf: _OutputBuffer, stderr_buf: _OutputBuffer) -> None:
    prev_ended_newline = True
    for buf, stream in ((stdout_buf, ""), (stderr_buf, "stderr")):
        raw = "".join(buf.chunks)
        if not raw:
            continue
        if not prev_ended_newline:
            log_file.write("\n")
        notice, _ = buf.render_parts(tail=None, stream=stream)
        log_file.write(f"{notice}\n{raw}" if notice else raw)
        prev_ended_newline = raw.endswith("\n")


def _format_auto_background_message(
    *,
    pid: int,
    timeout: float,
    log_path: Path,
    display_root: str | None,
    max_runtime_seconds: float | None,
    no_output_path: str | None,
    stdout_buf: _OutputBuffer,
    stderr_buf: _OutputBuffer,
    tail: int,
) -> str:
    shown = str(log_path)
    if display_root is not None:
        with contextlib.suppress(ValueError):
            shown = str(log_path.relative_to(Path(display_root)))
    display_path = shlex.quote(shown)
    lifetime_line = (
        f"It will be stopped after {max_runtime_seconds:g}s total, or automatically when this turn ends."
        if max_runtime_seconds is not None
        else "It is stopped automatically when this turn ends."
    )
    lines = [
        f"Still running after {timeout:g}s; moved to the background (PID {pid}).",
        f"Log: {display_path} (receives ongoing stdout+stderr and ends with the exit status).",
        f"Wait: end=$((SECONDS+110)); while ((SECONDS<end)) && kill -0 {pid} 2>/dev/null; do sleep 2; done",
        f"Stop: kill -- -{pid}",
        lifetime_line,
    ]
    if no_output_path is not None:
        lines.append(f"No output file was saved to {no_output_path} because the command is still running.")
    if stdout := stdout_buf.render(tail=tail):
        lines += ["Output:", stdout]
    if stderr_buf:
        lines += ["Stderr:", stderr_buf.render()]
    return "\n".join(lines)


def _format_log_trailer(
    return_code: int | None,
    *,
    stop_reason: Literal["timeout", "scope_ended"] | None,
    max_runtime_seconds: float | None,
) -> str:
    if stop_reason == "scope_ended":
        return "\n[MindRoom: stopped when turn ended]\n"
    rc = -signal.SIGKILL if return_code is None else return_code
    if stop_reason == "timeout" and max_runtime_seconds is not None:
        return f"\n[MindRoom: stopped after {max_runtime_seconds:g}s timeout (exit code {rc})]\n"
    if rc < 0:
        return f"\n[MindRoom: killed by signal {-rc} (exit code {rc})]\n"
    return f"\n[MindRoom: exited with code {rc}]\n"


def _background_rejection(
    registry: dict[str, ProcessRecord],
    *,
    namespace: str,
    handle: str,
    active_scope: ShellBackgroundScope | None,
) -> str | None:
    if active_scope is not None and active_scope.records is registry and active_scope.closed:
        return "Error: Shell turn already ended."
    ns_active = sum(1 for r in registry.values() if not r.finished and r.namespace == namespace)
    total_active = sum(1 for r in registry.values() if not r.finished)
    if ns_active >= MAX_BACKGROUNDED or total_active >= _MAX_TOTAL_BACKGROUNDED:
        count, limit = (
            (ns_active, MAX_BACKGROUNDED) if ns_active >= MAX_BACKGROUNDED else (total_active, _MAX_TOTAL_BACKGROUNDED)
        )
        return f"Error: Too many backgrounded processes ({count}/{limit}). Kill or wait for existing ones before running more."
    if handle in registry:
        return f"Error: Shell handle '{handle}' is already registered."
    return None


def _prepare_background_log(
    policy: BackgroundLogPolicy,
    *,
    pid: int,
    handle: str,
    registry: dict[str, ProcessRecord],
    stdout_buf: _OutputBuffer,
    stderr_buf: _OutputBuffer,
    log_sink: _LogSinkRef | None,
    output_capture: ShellOutputCapture | None,
) -> tuple[_BackgroundLogFile | None, ShellOutputCapture | None, str | None, str | None]:
    log_file: _BackgroundLogFile | None = None
    try:
        log_file = _open_background_log(policy, pid=pid, handle=handle, registry=registry)
        _seed_background_log(log_file, stdout_buf, stderr_buf)
    except OSError as exc:
        if log_file is not None:
            log_file.finish()
            with contextlib.suppress(OSError):
                log_file.path.unlink()
        return None, output_capture, None, f"Error: Failed to open background shell log: {exc}"
    if log_sink is not None:
        log_sink.sink = log_file
    if output_capture is None:
        return log_file, None, None, None
    output_path_notice = output_capture.destination.path
    output_capture.close()
    return log_file, None, output_path_notice, None


async def _background_process(
    registry: dict[str, ProcessRecord],
    *,
    namespace: str,
    argv: list[str],
    process: asyncio.subprocess.Process,
    stdout_buf: _OutputBuffer,
    stderr_buf: _OutputBuffer,
    tail: int,
    timeout: float,  # noqa: ASYNC109
    requested_handle: str | None,
    stdout_reader: asyncio.Task[None],
    stderr_reader: asyncio.Task[None],
    output_capture: ShellOutputCapture | None,
    started_at: float,
    max_runtime_seconds: float | None = None,
    background_log: BackgroundLogPolicy | None = None,
    log_sink: _LogSinkRef | None = None,
) -> ShellRunResult:
    had_capture = output_capture is not None
    active_scope = current_shell_background_scope()
    handle = requested_handle or _unused_handle(registry)
    log_file: _BackgroundLogFile | None = None
    record_capture, output_path_notice = output_capture, None
    rejection = _background_rejection(registry, namespace=namespace, handle=handle, active_scope=active_scope)
    if rejection is None and background_log is not None:
        log_file, record_capture, output_path_notice, rejection = _prepare_background_log(
            background_log,
            pid=process.pid,
            handle=handle,
            registry=registry,
            stdout_buf=stdout_buf,
            stderr_buf=stderr_buf,
            log_sink=log_sink,
            output_capture=output_capture,
        )
    if rejection is not None:
        await _discard_unregistered_process(process, stdout_reader, stderr_reader)
        if output_capture is not None:
            output_capture.close()
        return ShellRunResult(message=rejection, output_file_handled=had_capture)
    record = ProcessRecord(
        namespace=namespace,
        handle=handle,
        pid=process.pid,
        args=argv,
        process=process,
        stdout_buf=stdout_buf,
        stderr_buf=stderr_buf,
        tail=tail,
        output_capture=record_capture,
        started_at=started_at,
        log_path=str(log_file.path) if log_file is not None else None,
        max_runtime_seconds=max_runtime_seconds,
        _log_file=log_file,
    )
    monitor_task = asyncio.create_task(
        _monitor_process(registry, handle, process, stdout_reader, stderr_reader, record=record),
    )
    record._monitor_task = monitor_task
    registry[handle] = record
    try:
        await asyncio.sleep(0)
    except asyncio.CancelledError:
        registry.pop(handle, None)
        monitor_task.cancel()
        if process.returncode is None:
            await _terminate_process_group(process)
        await _cancel_pending_tasks(stdout_reader, stderr_reader)
        if log_file is not None:
            log_file.finish()
        with contextlib.suppress(asyncio.CancelledError):
            await monitor_task
        raise
    if active_scope is not None and active_scope.records is registry and active_scope.closed:
        discard_background_record(registry, handle)
        return ShellRunResult(message="Error: Shell turn already ended.", output_file_handled=had_capture)
    message = (
        _format_auto_background_message(
            pid=process.pid,
            timeout=timeout,
            log_path=log_file.path,
            display_root=background_log.display_root,
            max_runtime_seconds=max_runtime_seconds,
            no_output_path=output_path_notice,
            stdout_buf=stdout_buf,
            stderr_buf=stderr_buf,
            tail=tail,
        )
        if background_log is not None and log_file is not None
        else f"Command timed out after {timeout}s. Still running (PID {process.pid}).\nHandle: {handle}"
    )
    return ShellRunResult(message=message, handle=handle, output_file_handled=had_capture)


async def _discard_unregistered_process(
    process: asyncio.subprocess.Process,
    stdout_reader: asyncio.Task[None],
    stderr_reader: asyncio.Task[None],
) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
    await _cancel_pending_tasks(stdout_reader, stderr_reader)


def check_command(registry: dict[str, ProcessRecord], *, namespace: str, handle: str) -> str:
    """Poll the status of a backgrounded shell command in *registry*."""
    record = registry.get(handle)
    if record is None or record.namespace != namespace:
        return f"Error: Unknown handle '{handle}'"

    ended_at = record.finished_at if record.finished and record.finished_at is not None else time.monotonic()
    elapsed = ended_at - record.started_at

    if record.finished:
        if record.output_receipt is not None:
            return record.output_receipt
        failed = record.return_code != 0
        output = record.stdout_buf.render(tail=record.tail)
        errors = record.stderr_buf.render(tail=None if failed else record.tail)
        result = f"Status: FINISHED (exit code {record.return_code}, ran for {elapsed:.1f}s)\n"
        if failed and record.stderr_buf:
            result += f"Stderr:\n{errors}\n"
        result += f"Output:\n{output}"
        if record.stderr_buf and not failed:
            result += f"\nStderr:\n{errors}"
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

    sig_name = "SIGKILL" if force else "SIGTERM"
    if not signal_record(record, force=force):
        return f"Process {record.pid} already exited"

    action = "Force-killed" if force else "Terminated"
    return f"{action} process {record.pid} ({sig_name} sent)."


def signal_record(record: ProcessRecord, *, force: bool = False) -> bool:
    """Send SIGTERM, or SIGKILL with *force*, to a record's process group; report whether it was delivered."""
    try:
        os.killpg(record.pid, signal.SIGKILL if force else signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return False
    if record.output_capture is not None:
        record.output_capture.incomplete = True
    return True


def kill_all_records(registry: dict[str, ProcessRecord]) -> None:
    """Kill every unfinished process group in *registry* and clear it."""
    for record in registry.values():
        _kill_record(record)
    registry.clear()


def kill_namespace_records(registry: dict[str, ProcessRecord], namespace: str) -> int:
    """Stop and remove every record in *namespace*, returning how many records were removed."""
    handles = [handle for handle, record in registry.items() if record.namespace == namespace]
    for handle in handles:
        _kill_record(registry.pop(handle), stop_reason="scope_ended")
    return len(handles)


def discard_background_record(registry: dict[str, ProcessRecord], handle: str) -> None:
    """Kill and drop one just-registered background record whose handle is undeliverable."""
    record = registry.pop(handle, None)
    if record is not None:
        _kill_record(record)


def _kill_record(
    record: ProcessRecord,
    *,
    stop_reason: Literal["timeout", "scope_ended"] | None = None,
) -> None:
    if record.output_capture is not None:
        record.output_capture.close()
    was_running = not record.finished
    if was_running:
        if stop_reason is not None and record._stop_reason is None:
            record._stop_reason = stop_reason
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(record.pid, signal.SIGKILL)
        record.finished = True
        record.finished_at = time.monotonic()
        record.return_code = record.process.returncode
    if record._log_file is not None and not record._log_file.closed:
        if stop_reason is not None and was_running:
            record._log_file.finish(
                _format_log_trailer(
                    record.process.returncode,
                    stop_reason=record._stop_reason,
                    max_runtime_seconds=record.max_runtime_seconds,
                ),
            )
        else:
            record._log_file.finish()
    if record._monitor_task is not None:
        record._monitor_task.cancel()


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


async def _read_stream(
    stream: asyncio.StreamReader | None,
    buf: _OutputBuffer,
    capture: CapturedShellStream | None = None,
    log_sink: _LogSinkRef | None = None,
) -> None:
    """Read bounded chunks from an async stream into *buf* until EOF."""
    if stream is None:
        return

    try:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            chunk = await stream.read(_STREAM_READ_CHUNK_BYTES)
            if not chunk:
                break
            text = decoder.decode(chunk)
            buf.append(text)
            if log_sink is not None and log_sink.sink is not None:
                log_sink.sink.write(text)
            if capture is not None:
                capture.append(text)

        final = decoder.decode(b"", final=True)
        buf.append(final)
        if final and log_sink is not None and log_sink.sink is not None:
            log_sink.sink.write(final)
        if capture is not None:
            capture.append(final)
            capture.reached_eof = True
    except Exception:
        if capture is None:
            raise
        capture.error = "Failed to read complete shell output."


async def _cancel_pending_tasks(*tasks: asyncio.Task[None]) -> None:
    """Cancel any pending tasks and wait for them to finish."""
    pending_tasks = [task for task in tasks if not task.done()]
    for task in pending_tasks:
        task.cancel()
    for task in pending_tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _await_process_exit(
    process: asyncio.subprocess.Process,
    *,
    timeout_seconds: float | None = None,
    on_poll: Callable[[], None] | None = None,
) -> None:
    """Wait for the process to exit without depending on inherited pipe EOF, calling ``on_poll`` while it runs."""
    if process.returncode is not None:
        return
    if timeout_seconds is not None and timeout_seconds <= 0:
        raise TimeoutError

    wait_task = asyncio.create_task(process.wait())
    deadline = None if timeout_seconds is None else asyncio.get_running_loop().time() + timeout_seconds
    try:
        while process.returncode is None:
            poll_seconds = _PROCESS_EXIT_POLL_INTERVAL_SECONDS
            if deadline is not None:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError
                poll_seconds = min(poll_seconds, remaining)
            done_tasks, _pending_tasks = await asyncio.wait(
                (wait_task,),
                timeout=poll_seconds,
            )
            if wait_task in done_tasks:
                await wait_task
                return
            if on_poll is not None:
                on_poll()
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


@functools.cache
def _idle_launcher() -> str | None:
    """Return a ``chrt`` that can start a program at ``SCHED_IDLE``, or ``None`` when there is none or it fails."""
    chrt = shutil.which("chrt")
    if chrt is None:
        return None
    chrt = str(Path(chrt).absolute())  # commands run in other directories
    # A sandbox that refuses the policy makes chrt exit without running the program; then commands start directly.
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        if subprocess.run([chrt, "-i", "0", chrt, "-m"], capture_output=True, timeout=5, check=False).returncode == 0:
            return chrt
    return None


def _find_program(program: str, env: dict[str, str], cwd: str | None, mode: int = os.F_OK | os.X_OK) -> str | None:
    """Like Popen, look a bare name up on the command's PATH and a path with a slash up, both from the command's directory."""
    program = str(Path(cwd, program)) if cwd and os.sep in program else program
    search = [str(Path(cwd, entry)) if cwd else entry for entry in os.get_exec_path(env)]
    return shutil.which(program, mode=mode, path=os.pathsep.join(search))


# Bash's exit code and words for the spawn errors that are a shell's own exit: of a program named by a path, of a bare
# name no PATH entry holds, and of a bare name found on PATH. Every other error stays a failed call.
_PATH_LAUNCH_FAILURES = {
    errno.ENOENT: (127, "No such file or directory"),
    errno.ENOTDIR: (126, "Not a directory"),
    errno.EACCES: (126, "Permission denied"),
}
_MISSING_NAME_LAUNCH_FAILURES = dict.fromkeys((errno.ENOENT, errno.ENOTDIR), (127, "command not found"))
_FOUND_NAME_LAUNCH_FAILURES = {
    # A file the program needs to start, such as its interpreter, is missing.
    errno.ENOENT: (127, "cannot execute: required file not found"),
    errno.ENOTDIR: (127, "cannot execute: required file not found"),
    errno.EACCES: (126, "Permission denied"),
}


def _launch_failure_exit(
    argv: list[str],
    env: dict[str, str],
    cwd: str | None,
    exc: Exception,
) -> tuple[int, str] | None:
    """Return Bash's exit code and words for a spawn error of a program that is missing or cannot be executed, else None.

    A working directory the command cannot enter fails before any program. The error number and the program's name
    as given decide alone: the program file is never opened.
    """
    if (
        not isinstance(exc, OSError)
        or not argv
        # uvloop, which sandbox runners serve on, names no file in a spawn error.
        or exc.filename not in (argv[0], None)
        or (cwd is not None and not (Path(cwd).is_dir() and os.access(cwd, os.X_OK)))
    ):
        return None
    program = argv[0]
    if os.sep in program:
        failures = _PATH_LAUNCH_FAILURES
    elif (found := _find_program(program, env, cwd, os.F_OK)) is None:
        failures = _MISSING_NAME_LAUNCH_FAILURES
    else:
        # Bash names a program it found on PATH by where it found it.
        program, failures = found, _FOUND_NAME_LAUNCH_FAILURES
    failure = failures.get(exc.errno or 0)
    return None if failure is None else (failure[0], f"{program}: {failure[1]}")


def _launch_failure(
    argv: list[str],
    env: dict[str, str],
    cwd: str | None,
    exc: Exception,
    capture: ShellOutputCapture | None,
) -> ShellRunResult | None:
    """Return a spawn error of a program that is missing or cannot be executed as Bash reports it, else None.

    The result is the one of a command that exited 127 or 126 with Bash's words on stderr, so everything that reads a
    failed command's result reads this one.
    """
    if (failure := _launch_failure_exit(argv, env, cwd, exc)) is None:
        return None
    return_code, said = failure
    if capture is None:
        return ShellRunResult(message=format_shell_completion("", said, return_code=return_code))
    capture.stderr.append(f"{said}\n")
    capture.stdout.reached_eof = capture.stderr.reached_eof = True
    return ShellRunResult(message=capture.publish(return_code), output_file_handled=True)


def _idle_launch_argv(argv: list[str], env: dict[str, str], cwd: str | None) -> list[str]:
    """Return *argv* behind ``chrt -i 0`` on Linux, so the command and everything it starts run at ``SCHED_IDLE``.

    Nice 19 lowers a command's CPU share, but the kernel still counts a CPU that runs it as busy when it places
    MindRoom's waking event loop. It counts a CPU that runs only idle-policy threads as free, and a waking normal thread
    preempts them at once.
    chrt execs the program in the same process with the same environment. A program that cannot be found starts
    directly instead, so the spawn fails with the same error as before; so does every command when chrt is missing.
    """
    if sys.platform != "linux" or not argv or (chrt := _idle_launcher()) is None:
        return argv
    if _find_program(argv[0], env, cwd) is None:
        return argv
    return [chrt, "-i", "0", *argv]


def _schedule_process_tree_idle(pid: int) -> None:
    """Move every thread of ``pid`` and of its descendants to ``SCHED_IDLE``; what they start later inherits it.

    This is the fallback for a command that did not start behind chrt (see ``_idle_launch_argv``).
    A busy event loop gives the command time to fork and start threads before this runs, so the walk sets each thread
    before it reads that thread's children: whatever the thread starts afterwards inherits the policy. It misses a
    thread started during the walk by a thread it has not reached yet, and a process whose parent already exited.
    """
    pending = [pid]
    while pending:
        with contextlib.suppress(OSError):
            for thread in Path(f"/proc/{pending.pop()}/task").iterdir():
                with contextlib.suppress(OSError):
                    os.sched_setscheduler(int(thread.name), os.SCHED_IDLE, os.sched_param(0))
                    pending.extend(int(child) for child in (thread / "children").read_text().split())


def _kill_process_group(pid: int) -> None:
    """Kill every process still in the group led by *pid*."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


def _process_group_exists(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


async def _terminate_process_group(
    process: asyncio.subprocess.Process,
    *,
    grace_period: float = 0.5,
    whole_group: bool = False,
) -> bool:
    """Stop a running command's process group: SIGTERM, then SIGKILL every member left after the grace.

    Members that outlive the leader are killed too, so a child that ignores SIGTERM cannot survive the stop.
    A command whose leader already exited ended on its own, and its detached children keep running, unless
    ``whole_group`` stops them too; return whether the group is then gone.
    """
    if process.returncode is not None and not whole_group:
        return False

    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGTERM)

    loop = asyncio.get_running_loop()
    deadline = loop.time() + grace_period
    try:
        while loop.time() < deadline and _process_group_exists(process.pid):  # noqa: ASYNC110 - a group has no exit event
            await asyncio.sleep(_PROCESS_EXIT_POLL_INTERVAL_SECONDS)
    finally:
        # A stop cancelled during the grace still kills what ignored SIGTERM.
        _kill_process_group(process.pid)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(process.wait(), timeout=grace_period)
    deadline = loop.time() + grace_period
    while whole_group and _process_group_exists(process.pid) and loop.time() < deadline:  # noqa: ASYNC110 - as above
        await asyncio.sleep(_PROCESS_EXIT_POLL_INTERVAL_SECONDS)
    return not _process_group_exists(process.pid)


async def _monitor_process(
    registry: dict[str, ProcessRecord],
    handle: str,
    process: asyncio.subprocess.Process,
    stdout_reader: asyncio.Task[None],
    stderr_reader: asyncio.Task[None],
    *,
    record: ProcessRecord | None = None,
) -> None:
    """Wait for a backgrounded process to exit (or hit max_runtime_seconds) and update its record."""
    tracked = record if record is not None else registry.get(handle)
    log_file = tracked._log_file if tracked is not None else None
    try:
        timeout_seconds = (
            None
            if tracked is None or tracked.max_runtime_seconds is None
            else max(0.0, tracked.started_at + tracked.max_runtime_seconds - time.monotonic())
        )
        try:
            await _await_process_exit(process, timeout_seconds=timeout_seconds)
        except TimeoutError:
            if tracked is not None and tracked._stop_reason is None:
                tracked._stop_reason = "timeout"
            await _terminate_process_group(process, whole_group=True)
    finally:
        try:
            live_record = registry.get(handle)
            capture = live_record.output_capture if live_record is not None else None
            try:
                if capture is not None and not capture.closed:
                    # Group cleanup can create EOF by killing a writer. Decide
                    # completeness before that EOF can look like normal completion.
                    _done, pending = await asyncio.wait([stdout_reader, stderr_reader], timeout=2.0)
                    capture.incomplete |= bool(pending)
            finally:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(process.pid, signal.SIGKILL)
            drain_timeout = _POST_EXIT_READER_GRACE_SECONDS if log_file is not None else 2.0
            await asyncio.wait([stdout_reader, stderr_reader], timeout=drain_timeout)
            await _cancel_pending_tasks(stdout_reader, stderr_reader)
            live_record = registry.get(handle)
            if live_record is not None:
                capture = live_record.output_capture
                if capture is not None and not capture.closed:
                    capture.incomplete |= stdout_reader.cancelled() or stderr_reader.cancelled()
                    try:
                        live_record.output_receipt = capture.publish(process.returncode)
                    finally:
                        capture.close()
            final_record = live_record or tracked
            if final_record is not None:
                final_record.finished = True
                final_record.finished_at = time.monotonic()
                final_record.return_code = process.returncode
        finally:
            if log_file is not None and not log_file.closed:
                log_file.finish(
                    _format_log_trailer(
                        process.returncode,
                        stop_reason=tracked._stop_reason if tracked is not None else None,
                        max_runtime_seconds=tracked.max_runtime_seconds if tracked is not None else None,
                    ),
                )
