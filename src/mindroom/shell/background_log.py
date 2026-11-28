"""Bounded live log files, rotation/sweeping, and status trailers for auto-backgrounded shell runs."""

from __future__ import annotations

import contextlib
import os
import re
import signal
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Literal

from mindroom.path_confinement import open_directory_within_root, open_regular_file_at

if TYPE_CHECKING:
    from collections.abc import Iterator

    from mindroom.shell.output import ShellOutputBuffer

_LOG_MAX_BYTES = 4 * 1024 * 1024
_LOG_MAX_AGE_SECONDS = 24 * 3600
_LOG_MAX_FILES = 64
_LOG_RECENT_PROTECT_SECONDS = 5 * 60
_BACKGROUND_LOG_NAME_RE = re.compile(r"^\d+-(?:[0-9a-f]{8}|[0-9a-f]{32})\.log$")

StopReason = Literal["timeout", "scope_ended", "stopped"]


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


def sweep_generated_output(dir_fd: int, *, name_pattern: re.Pattern[str], active_names: set[str]) -> None:
    """Expire or evict generated regular files, protecting active and recently written entries."""
    now = time.time()
    age_cutoff = now - _LOG_MAX_AGE_SECONDS
    recent_cutoff = now - _LOG_RECENT_PROTECT_SECONDS
    evictable: list[tuple[float, str]] = []
    protected_count = 0
    try:
        names = os.listdir(dir_fd)
    except OSError:
        return
    for name in names:
        if not name_pattern.fullmatch(name):
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
            # Protect recently written files from another concurrent scope in the same workspace.
            protected_count += 1
        else:
            evictable.append((mtime, name))
    evictable.sort(key=lambda item: item[0], reverse=True)
    for _, old_name in evictable[max(0, _LOG_MAX_FILES - protected_count) :]:
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
        sweep_generated_output(dir_fd, name_pattern=_BACKGROUND_LOG_NAME_RE, active_names=active_names)
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
        notice, _ = buf.render_parts(tail=None, stream=stream)
        log_file.write(f"{notice}\n{raw}" if notice else raw)
        prev_ended_newline = raw.endswith("\n")


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
