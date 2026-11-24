"""Turn-scoped shell auto-backgrounding, live log files, and remote cleanup tracking."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import signal
import stat
import threading
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Literal

from mindroom.background_tasks import run_coroutine_until_complete
from mindroom.path_confinement import open_directory_within_root, open_regular_file_at

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator

    from mindroom.shell.execution import ProcessRecord

_logger = logging.getLogger(__name__)

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


@dataclass
class _RemoteScopeEndpoint:
    cleanup: Callable[[], None]
    in_flight: int = 0
    needs_kill: bool = False
    release_lease: Callable[[], None] | None = None


def _run_remote_cleanup(cleanup: Callable[[], None]) -> None:
    try:
        cleanup()
    except Exception:
        _logger.warning("Remote shell background scope cleanup failed", exc_info=True)


@dataclass
class ShellBackgroundScope:
    """Turn-scoped owner for auto-backgrounded local and remote worker shell processes."""

    scope_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    records: dict[str, ProcessRecord] = field(default_factory=dict)
    had_backgrounded: bool = False
    closed: bool = False
    _remote_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _remote_endpoints: dict[str, _RemoteScopeEndpoint] = field(default_factory=dict, repr=False)

    def track_remote_start(self, endpoint_key: str, cleanup: Callable[[], None]) -> None:
        """Record an in-flight remote shell request for *endpoint_key*."""
        with self._remote_lock:
            entry = self._remote_endpoints.get(endpoint_key)
            if entry is None:
                self._remote_endpoints[endpoint_key] = _RemoteScopeEndpoint(cleanup=cleanup, in_flight=1)
            else:
                entry.cleanup = cleanup
                entry.in_flight += 1

    def track_remote_end(
        self,
        endpoint_key: str,
        *,
        backgrounded: bool,
        release_lease: Callable[[], None] | None = None,
    ) -> bool:
        """Record completion of a remote shell request; adopt *release_lease* or fire late cleanup if closed."""
        late_cleanup: Callable[[], None] | None = None
        adopted = False
        with self._remote_lock:
            entry = self._remote_endpoints.get(endpoint_key)
            if entry is None:
                return False
            entry.in_flight = max(0, entry.in_flight - 1)
            if backgrounded:
                self.had_backgrounded = True
                if self.closed:
                    late_cleanup = entry.cleanup
                else:
                    entry.needs_kill = True
                    if release_lease is not None and entry.release_lease is None:
                        entry.release_lease = release_lease
                        adopted = True
            if self.closed and entry.in_flight == 0:
                self._remote_endpoints.pop(endpoint_key, None)
        if late_cleanup is not None:
            _run_remote_cleanup(late_cleanup)
        return adopted

    async def close(self) -> None:
        """Stop unfinished local and remote processes in this scope, flush log trailers, and release leases."""
        with self._remote_lock:
            self.closed = True
            remote_cleanups = [
                (entry.cleanup, entry.release_lease)
                for entry in self._remote_endpoints.values()
                if entry.needs_kill or entry.in_flight > 0 or entry.release_lease is not None
            ]
            for key in [k for k, e in self._remote_endpoints.items() if e.in_flight == 0]:
                self._remote_endpoints.pop(key, None)
            for entry in self._remote_endpoints.values():
                entry.needs_kill = False
                entry.release_lease = None
        try:
            if self.records:
                from mindroom.shell.execution import close_background_records  # noqa: PLC0415

                await close_background_records(self.records)
            if remote_cleanups:
                await asyncio.gather(
                    *(asyncio.to_thread(_run_remote_cleanup, cleanup) for cleanup, _ in remote_cleanups),
                    return_exceptions=True,
                )
        finally:
            for _, release_lease in remote_cleanups:
                if release_lease is not None:
                    release_lease()


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
async def shell_background_scope(*, reuse_bound: bool = False) -> AsyncIterator[ShellBackgroundScope]:
    """Create, bind, and close a fresh turn-scoped shell background owner, or reuse the active one."""
    if reuse_bound and (existing := current_shell_background_scope()) is not None:
        yield existing
        return
    scope = ShellBackgroundScope()
    try:
        with bind_shell_background_scope(scope):
            yield scope
    finally:
        await run_coroutine_until_complete(scope.close())


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
