"""Pure shell output primitives: rolling tail buffers, temp spools, background logs, and formatters.

This module stays free of ``mindroom.tools``, ``mindroom.tool_jobs``, ``mindroom.tool_system``, and
``mindroom.shell.background_scope`` imports so the worker-local shell supervisor starts fast and
desktop code remains import-safe across platforms.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import itertools
import os
import re
import shlex
import signal
import stat
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, Literal, NamedTuple

from mindroom.path_confinement import open_directory_within_root, open_regular_file_at

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

_MAX_OUTPUT_LINES = 10_000
_MAX_OUTPUT_BYTES = 50 * 1024
_STREAM_READ_CHUNK_BYTES = 65536
# Pause pipe reads briefly after each burst once an uncapped redirect is not active and the buffer is rotating.
_CAPPED_STREAM_BURST_READS = 16
_CAPPED_STREAM_SMALL_READ_BYTES = 4096
_CAPPED_STREAM_BULK_FREE_BYTES = 256 * 1024 * 1024
_CAPPED_STREAM_PACE_SECONDS = 0.02
# A running command reports the tail of its output at most this often, and only when it changed; replies show it live.
_PROGRESS_INTERVAL_SECONDS = 0.5
# Numbers every append to any output buffer, so a reader can tell which stream printed last.
_APPEND_SEQUENCE = itertools.count(1)

_LOG_MAX_BYTES = 4 * 1024 * 1024
_LOG_MAX_AGE_SECONDS = 24 * 3600
_LOG_MAX_FILES = 64
_LOG_RECENT_PROTECT_SECONDS = 5 * 60
_BACKGROUND_LOG_NAME_RE = re.compile(r"^\d+-(?:[0-9a-f]{8}|[0-9a-f]{32})\.log$")

StopReason = Literal["timeout", "scope_ended", "stopped"]


class _OutputChunk(NamedTuple):
    text: str
    byte_len: int
    newlines: int


@dataclass
class ShellOutputBuffer:
    """Bound shell output by both line count and encoded byte size."""

    max_lines: int = _MAX_OUTPUT_LINES
    max_bytes: int = _MAX_OUTPUT_BYTES
    chunks: deque[_OutputChunk] = field(default_factory=deque)
    byte_count: int = 0
    retained_newlines: int = 0
    # Which caps dropped output, and every line the command printed, so a notice can say what was left out.
    bytes_capped: bool = False
    lines_capped: bool = False
    newlines: int = 0
    open_line: bool = False
    # The number of this buffer's latest append, so a reader can tell new output from a full buffer that rotated to
    # the same size, and which stream printed last.
    last_append: int = 0

    def append(self, text: str) -> None:
        """Append decoded output text, trimming oldest chunks to stay within byte and line bounds."""
        if not text:
            return
        self.last_append = next(_APPEND_SEQUENCE)
        newline_count = text.count("\n")
        self.newlines += newline_count
        self.open_line = not text.endswith("\n")

        encoded = text.encode("utf-8", errors="replace")
        if len(encoded) > self.max_bytes:
            self.chunks.clear()
            self.byte_count = 0
            self.retained_newlines = 0
            text = encoded[-self.max_bytes :].decode("utf-8", errors="ignore")
            encoded = text.encode("utf-8", errors="replace")
            newline_count = text.count("\n")
            self.bytes_capped = True

        if text:
            byte_len = len(encoded)
            self.chunks.append(_OutputChunk(text, byte_len, newline_count))
            self.byte_count += byte_len
            self.retained_newlines += newline_count

        self._trim_to_max_bytes()
        self._trim_to_max_lines()

    def text(self) -> str:
        """Return the retained output text."""
        return "".join(chunk.text for chunk in self.chunks)

    def lines(self) -> list[str]:
        """Return every retained line; a final newline ends the last line rather than starting another."""
        text = self.text()
        return text.removesuffix("\n").split("\n") if text else []

    def render(self, *, tail: int | None = None) -> str:
        """Return the last ``tail`` retained lines, after one notice of what was left out."""
        return "\n".join(part for part in self._render_parts(tail=tail) if part)

    def _render_parts(self, *, tail: int | None = None, stream: str = "") -> tuple[str, str]:
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
        """Return the number of retained lines currently held in the buffer."""
        if not self.chunks:
            return 0
        return self.retained_newlines + (not self.chunks[-1].text.endswith("\n"))

    def _trim_to_max_bytes(self) -> None:
        excess = self.byte_count - self.max_bytes
        if excess <= 0:
            return

        while self.chunks and self.chunks[0].byte_len <= excess:
            removed = self.chunks.popleft()
            self.byte_count -= removed.byte_len
            self.retained_newlines -= removed.newlines
            excess -= removed.byte_len

        if excess > 0 and self.chunks:
            head = self.chunks.popleft()
            self.byte_count -= head.byte_len
            self.retained_newlines -= head.newlines
            tail = head.text.encode("utf-8", errors="replace")[excess:].decode("utf-8", errors="ignore")
            if tail:
                tail_bytes = len(tail.encode("utf-8", errors="replace"))
                tail_newlines = tail.count("\n")
                self.chunks.appendleft(_OutputChunk(tail, tail_bytes, tail_newlines))
                self.byte_count += tail_bytes
                self.retained_newlines += tail_newlines

        self.bytes_capped = True

    def _trim_to_max_lines(self) -> None:
        drop = len(self) - self.max_lines
        if drop <= 0:
            return

        while self.chunks and (
            self.chunks[0].newlines < drop or (self.chunks[0].newlines == drop and self.chunks[0].text.endswith("\n"))
        ):
            removed = self.chunks.popleft()
            self.byte_count -= removed.byte_len
            self.retained_newlines -= removed.newlines
            drop -= removed.newlines

        if drop > 0 and self.chunks:
            head = self.chunks.popleft()
            self.byte_count -= head.byte_len
            self.retained_newlines -= head.newlines
            tail = head.text.split("\n", drop)[-1]
            if tail:
                tail_bytes = len(tail.encode("utf-8", errors="replace"))
                tail_newlines = head.newlines - drop
                self.chunks.appendleft(_OutputChunk(tail, tail_bytes, tail_newlines))
                self.byte_count += tail_bytes
                self.retained_newlines += tail_newlines

        self.lines_capped = True


def _progress_lines(buffer: ShellOutputBuffer) -> str:
    """Return what a running command printed so far, each line in the state it was redrawn to last.

    It is reported whole, so whoever shows its tail can mask a credential whose label sits above the tail.
    """
    text = buffer.text().rstrip("\n")
    if "\r" not in text:
        return text
    return "\n".join(line.rstrip("\r").rsplit("\r", 1)[-1] for line in text.split("\n"))


class ShellOutputProgress:
    """Report a running command's latest output, at most twice a second and only when it changed."""

    def __init__(
        self,
        report: Callable[[str], None],
        stdout_buf: ShellOutputBuffer,
        stderr_buf: ShellOutputBuffer,
    ) -> None:
        self._report = report
        self._stdout_buf = stdout_buf
        self._stderr_buf = stderr_buf
        self._sent = ""
        self._last_appends = (0, 0)
        self._due = time.monotonic() + _PROGRESS_INTERVAL_SECONDS

    def poll(self) -> None:
        """Emit the latest combined progress snapshot when the throttle interval has elapsed and output changed."""
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


class SpoolStream:
    """Spool decoded text into a bounded anonymous tempfile without retaining it in memory."""

    def __init__(self, *, max_bytes: int, directory: str | None = None) -> None:
        self.file: BinaryIO = tempfile.TemporaryFile(dir=directory)  # noqa: SIM115
        self.max_bytes = max_bytes
        self.byte_count = 0
        self.error: str | None = None
        self.reached_eof = False

    def append(self, text: str) -> None:
        """Count decoded output, and spool it until the cap or a storage error is reached."""
        payload = text.encode("utf-8", errors="replace")
        self.byte_count += len(payload)
        if self.error is not None or self.file.closed:
            return
        if self.byte_count > self.max_bytes:
            self.error = f"Redirected shell output exceeds the {self.max_bytes} byte limit."
            return
        try:
            self.file.write(payload)
        except OSError:
            self.error = "Failed to capture complete shell output."

    @property
    def size(self) -> int:
        """Return the retained UTF-8 byte count flushed to the spool file."""
        self.file.flush()
        return os.fstat(self.file.fileno()).st_size

    def read(self) -> str:
        """Read the retained complete stream as UTF-8 text."""
        self.file.seek(0)
        return self.file.read().decode("utf-8")

    def pread(self, offset: int = 0, max_bytes: int | None = None) -> bytes:
        """Read retained bytes from *offset* without moving the append file position."""
        size = self.size
        count = max(0, size - offset) if max_bytes is None else max_bytes
        return os.pread(self.file.fileno(), count, offset)

    def tail(self, max_bytes: int) -> bytes:
        """Return at most the newest *max_bytes* bytes; the first UTF-8 character may be partial."""
        return self.pread(max(0, self.size - max_bytes))

    def close(self) -> None:
        """Close and release the unlinked spool file."""
        with contextlib.suppress(OSError):
            self.file.close()


class SpoolCapture:
    """Caller-owned bounded tempfile spool for a command's stdout and stderr."""

    def __init__(self, *, max_bytes: int, directory: str | None = None) -> None:
        self.stdout = SpoolStream(max_bytes=max_bytes, directory=directory)
        try:
            self.stderr = SpoolStream(max_bytes=max_bytes, directory=directory)
        except BaseException:
            self.stdout.close()
            raise
        self.incomplete = False
        # Why incomplete output went unsaved, when its owner knows better than "interrupted".
        self.interruption: str | None = None
        self.closed = False

    def close(self) -> None:
        """Release unlinked spool files on completion, cancellation, or eviction."""
        self.closed = True
        self.stdout.close()
        self.stderr.close()


@dataclass(frozen=True)
class BackgroundLogPolicy:
    """Where and how much output an auto-backgrounded shell command writes to its live log."""

    log_dir: str
    display_root: str | None = None
    max_bytes: int = _LOG_MAX_BYTES

    @classmethod
    def from_payload(cls, payload: object) -> BackgroundLogPolicy | None:
        """Validate the optional serialized log policy sent to the supervisor."""
        if payload is None:
            return None
        if not isinstance(payload, dict):
            msg = "Invalid background shell log policy."
            raise TypeError(msg)
        log_dir = payload.get("log_dir")
        display_root = payload.get("display_root")
        max_bytes = payload.get("max_bytes", _LOG_MAX_BYTES)
        if (
            not isinstance(log_dir, str)
            or not log_dir
            or (display_root is not None and not isinstance(display_root, str))
            or not isinstance(max_bytes, int)
            or isinstance(max_bytes, bool)
            or max_bytes <= 0
        ):
            msg = "Invalid background shell log policy."
            raise ValueError(msg)
        return cls(log_dir=log_dir, display_root=display_root, max_bytes=max_bytes)


class BackgroundLogFile:
    """Bounded live log file for one auto-backgrounded shell process."""

    def __init__(self, *, path: Path, file: BinaryIO, max_bytes: int) -> None:
        self.path = path
        self._file = file
        self._max_bytes = max_bytes
        self._written_bytes = 0
        self._truncated = False
        self.closed = False

    def write(self, text: str) -> None:
        """Append *text* up to the byte cap, writing a single truncation notice if exceeded."""
        if self.closed or self._truncated or not text:
            return
        data = text.encode("utf-8", errors="replace")
        remaining = self._max_bytes - self._written_bytes
        try:
            if len(data) <= remaining:
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
        """Write an optional status trailer and close the log file once."""
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


@contextlib.contextmanager
def _open_log_dir(log_dir: Path) -> Iterator[int]:
    root, relative_dir = (
        (log_dir.parent.parent, Path(".mindroom", log_dir.name))
        if log_dir.parent.name == ".mindroom"
        else (log_dir.parent, Path(log_dir.name))
    )
    root.mkdir(parents=True, exist_ok=True)
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with open_directory_within_root(root_fd, relative_dir, create=True, mode=0o700) as dir_fd:
            if os.fstat(dir_fd).st_uid != os.geteuid():
                msg = f"Shell log directory is not owned by the current user: {log_dir}"
                raise PermissionError(msg)
            yield dir_fd
    finally:
        os.close(root_fd)


def _sweep_old_log_files(dir_fd: int, *, active_names: set[str]) -> None:
    now = time.time()
    age_cutoff = now - _LOG_MAX_AGE_SECONDS
    recent_cutoff = now - _LOG_RECENT_PROTECT_SECONDS
    evictable_logs: list[tuple[float, str]] = []
    protected_count = 0
    try:
        names = os.listdir(dir_fd)
    except OSError:
        return
    for name in names:
        if not _BACKGROUND_LOG_NAME_RE.match(name):
            continue
        try:
            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        if name in active_names:
            protected_count += 1
            continue
        mtime = st.st_mtime
        if mtime < age_cutoff:
            with contextlib.suppress(OSError):
                os.unlink(name, dir_fd=dir_fd)
        elif mtime >= recent_cutoff:
            # Protect recently written logs from another concurrent scope in the same workspace.
            protected_count += 1
        else:
            evictable_logs.append((mtime, name))
    evictable_logs.sort(key=lambda item: item[0], reverse=True)
    for _, old_name in evictable_logs[max(0, _LOG_MAX_FILES - protected_count) :]:
        with contextlib.suppress(OSError):
            os.unlink(old_name, dir_fd=dir_fd)


def open_background_log(
    policy: BackgroundLogPolicy,
    *,
    pid: int,
    handle: str,
    active_paths: set[str],
) -> BackgroundLogFile:
    """Create a private log file for one auto-backgrounded process and sweep expired generated logs."""
    log_dir = Path(policy.log_dir)
    filename = f"{pid}-{handle.removeprefix('shell:')}.log"
    log_path = log_dir / filename
    with _open_log_dir(log_dir) as dir_fd:
        file_fd = open_regular_file_at(dir_fd, filename, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            stream = os.fdopen(file_fd, "wb")
        except BaseException:
            os.close(file_fd)
            raise
        active_names = {Path(path).name for path in active_paths if Path(path).parent == log_dir} | {filename}
        _sweep_old_log_files(dir_fd, active_names=active_names)
    return BackgroundLogFile(path=log_path, file=stream, max_bytes=policy.max_bytes)


def seed_background_log(
    log_file: BackgroundLogFile,
    stdout_buf: ShellOutputBuffer,
    stderr_buf: ShellOutputBuffer,
) -> None:
    """Write buffered foreground stdout and stderr into a newly opened background log file."""
    prev_ended_newline = True
    for buf, stream in ((stdout_buf, ""), (stderr_buf, "stderr")):
        raw = buf.text()
        if not raw:
            continue
        if not prev_ended_newline:
            log_file.write("\n")
        notice, _ = buf._render_parts(tail=None, stream=stream)
        log_file.write(f"{notice}\n{raw}" if notice else raw)
        prev_ended_newline = raw.endswith("\n")


def format_shell_completion(stdout: str, stderr: str | None, *, return_code: int) -> str:
    """Format completed output: stdout, then any stderr under ``Stderr:``; a failure leads with its exit code.

    The exit code comes first so a bounded job summary still shows that the command failed.
    ``stderr`` is None when the command printed nothing to it, so stderr of only blank lines keeps its section.
    """
    lines = [f"Exit code {return_code}"] if return_code else []
    if stdout:
        lines += ["Output:", stdout] if return_code else [stdout]
    if stderr is not None:
        lines += ["Stderr:", stderr]
    return "\n".join(lines)


def format_shell_timeout(stdout: str, stderr: str | None, *, timeout: float, no_output_file: bool = False) -> str:
    """Format the output so far of a command stopped at its timeout, leading with the stop.

    ``stderr`` is None when the command printed nothing to it, as for `format_shell_completion`.
    ``no_output_file`` says the call asked for an output file, which a stopped command does not save.
    """
    lines = [f"Stopped after {timeout:g}s." + (" No output file was saved." if no_output_file else "")]
    if stdout:
        lines += ["Output:", stdout]
    if stderr is not None:
        lines += ["Stderr:", stderr]
    return "\n".join(lines)


def format_shell_success(stdout_buf: ShellOutputBuffer, stderr_buf: ShellOutputBuffer, tail: int) -> str:
    """Return a success's stdout, then its stderr, both tailed, after the notice of anything either left out."""
    output_notice, output = stdout_buf._render_parts(tail=tail)
    errors_notice, errors = stderr_buf._render_parts(tail=tail, stream="stderr")
    completion = format_shell_completion(output, errors if stderr_buf else None, return_code=0)
    return "\n".join(part for part in (output_notice, errors_notice, completion) if part)


def format_auto_background_message(
    *,
    pid: int,
    timeout: float,
    log_path: Path,
    display_root: str | None,
    max_runtime_seconds: float | None,
    no_output_path: str | None,
    stdout_buf: ShellOutputBuffer,
    stderr_buf: ShellOutputBuffer,
    tail: int,
) -> str:
    """Format the tool response returned when a shell command moves to the background."""
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


def format_log_trailer(
    return_code: int | None,
    *,
    stop_reason: StopReason | None,
    max_runtime_seconds: float | None,
) -> str:
    """Return the final status line appended when a backgrounded process stops or exits."""
    if stop_reason == "scope_ended":
        return "\n[MindRoom: stopped when turn ended]\n"
    if stop_reason == "stopped":
        return "\n[MindRoom: stopped]\n"
    rc = -signal.SIGKILL if return_code is None else return_code
    if stop_reason == "timeout" and max_runtime_seconds is not None:
        return f"\n[MindRoom: stopped after {max_runtime_seconds:g}s timeout (exit code {rc})]\n"
    if rc < 0:
        return f"\n[MindRoom: killed by signal {-rc} (exit code {rc})]\n"
    return f"\n[MindRoom: exited with code {rc}]\n"


async def _pace_capped_stream(stream: asyncio.StreamReader) -> None:
    """Pause pipe reads while sleeping so a fast writer does not wake epoll between capped reads."""
    transport = getattr(stream, "_transport", None)
    paused_transport: Any | None = None
    if transport is not None and not transport.is_closing() and not getattr(stream, "_paused", False):
        with contextlib.suppress(NotImplementedError):
            transport.pause_reading()
            paused_transport = transport
    try:
        await asyncio.sleep(_CAPPED_STREAM_PACE_SECONDS)
    finally:
        if paused_transport is not None and not paused_transport.is_closing() and not getattr(stream, "_paused", False):
            paused_transport.resume_reading()


def _count_capped_read(
    buf: ShellOutputBuffer,
    capture: SpoolStream | None,
    stream: asyncio.StreamReader,
    chunk_len: int,
    capped_bytes: int,
    capped_reads: int,
) -> tuple[int, int]:
    """Update rotating-stream counters once output is capped without an active capture."""
    if (
        (buf.bytes_capped or buf.lines_capped)
        and (capture is None or capture.error is not None)
        and not stream.at_eof()
    ):
        capped_bytes += chunk_len
        if chunk_len < _CAPPED_STREAM_SMALL_READ_BYTES or capped_bytes >= _CAPPED_STREAM_BULK_FREE_BYTES:
            capped_reads += 1
    return capped_bytes, capped_reads


def _deliver_text(
    text: str,
    buf: ShellOutputBuffer,
    capture: SpoolStream | None,
    extra_sinks: list[Callable[[str], None]] | None,
) -> None:
    if not text:
        return
    if extra_sinks:
        for sink in extra_sinks:
            sink(text)
    buf.append(text)
    if capture is not None:
        capture.append(text)


async def drain_text_stream(
    stream: asyncio.StreamReader | None,
    buf: ShellOutputBuffer,
    capture: SpoolStream | None = None,
    *,
    extra_sinks: list[Callable[[str], None]] | None = None,
) -> None:
    """Read bounded UTF-8 chunks from *stream* into *buf*, optional *capture*, and any dynamic *extra_sinks*."""
    if stream is None:
        return

    try:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        capped_reads = 0
        capped_bytes = 0
        while True:
            chunk = await stream.read(_STREAM_READ_CHUNK_BYTES)
            if not chunk:
                break
            _deliver_text(decoder.decode(chunk), buf, capture, extra_sinks)
            capped_bytes, capped_reads = _count_capped_read(
                buf,
                capture,
                stream,
                len(chunk),
                capped_bytes,
                capped_reads,
            )
            if capped_reads >= _CAPPED_STREAM_BURST_READS:
                capped_reads = 0
                await _pace_capped_stream(stream)

        _deliver_text(decoder.decode(b"", final=True), buf, capture, extra_sinks)
        if capture is not None:
            capture.reached_eof = True
    except Exception:
        if capture is None:
            raise
        capture.error = "Failed to read complete shell output."
