"""Persistent shells in pseudo-terminals, each keeping its scrollback while viewers come and go.

The sandbox runner keeps these beside the shell commands it runs (``mindroom.api.worker_terminal``). A session
belongs to one owner, an opaque key the primary derives from the agent and workspace it serves, and every lookup
names that owner, so one owner never reaches another's session.

A shell that has had no input and no viewer for ``_IDLE_SECONDS`` is hung up, an ended session is forgotten after the
same time, and the runner hangs up every shell when it stops.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import re
import secrets
import signal
import struct
import subprocess
import termios
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = get_logger(__name__)

_SCROLLBACK_BYTES = 256 * 1024
_MAX_INPUT_BYTES = 64 * 1024
_IDLE_SECONDS = 1800.0
_MAX_SESSIONS = 64
_MAX_SESSIONS_PER_OWNER = 8
_MAX_COLS = 1000
_MAX_ROWS = 500
_READ_CHUNK = 65536
# Typing the shell has not read yet; past this, more is refused until it reads.
_MAX_PENDING_INPUT_BYTES = 1024 * 1024
# A viewer that falls this far behind is dropped; it reattaches and gets the scrollback.
_MAX_VIEWER_BACKLOG_BYTES = 4 * 1024 * 1024
_HANGUP_GRACE_SECONDS = 2.0
_EXIT_POLL_SECONDS = 0.5
_OWNER = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


class TerminalError(Exception):
    """A refusal, with the HTTP status the runner answers it with."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class TerminalLaunch:
    """The command, environment and working directory a session's shell starts with."""

    argv: tuple[str, ...]
    env: dict[str, str] = field(repr=False)
    cwd: str | None


@runtime_checkable
class SupportsTerminalLaunch(Protocol):
    """A shell toolkit that can start a terminal in the environment and workspace of its commands."""

    def terminal_launch(self, cwd: str | None, *, process_environ: Mapping[str, str] | None) -> TerminalLaunch:
        """Return the shell to start in ``cwd``; ``process_environ`` is the environment its commands run from."""
        ...


@dataclass(frozen=True)
class TerminalInfo:
    """A session as the runner describes it; ``created_at`` is a Unix time."""

    id: str
    cols: int
    rows: int
    alive: bool
    exit_code: int | None
    created_at: float


@dataclass(frozen=True)
class TerminalEvent:
    """What a viewer is told: output, the shell's exit with its code, or that it fell too far behind."""

    type: Literal["output", "exit", "dropped"]
    data: bytes = b""
    code: int | None = None


def _clamp_size(cols: int, rows: int) -> tuple[int, int]:
    """Return a terminal size inside what a pty accepts and a viewer can show."""
    return max(2, min(_MAX_COLS, cols)), max(1, min(_MAX_ROWS, rows))


def _set_size(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


class _Scrollback:
    """The last ``_SCROLLBACK_BYTES`` of output."""

    def __init__(self) -> None:
        self._data = bytearray()

    def append(self, chunk: bytes) -> None:
        self._data += chunk
        excess = len(self._data) - _SCROLLBACK_BYTES
        if excess > 0:
            # Cut at a line start when one is near, so a replay starts with whole lines and escape sequences.
            newline = self._data.find(b"\n", excess, excess + 4096)
            del self._data[: newline + 1 if newline != -1 else excess]

    def snapshot(self) -> bytes:
        return bytes(self._data)


class TerminalViewer:
    """One attached viewer's queue of events."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[TerminalEvent] = asyncio.Queue()
        self._backlog = 0
        self.dropped = False

    def put(self, event: TerminalEvent) -> None:
        """Queue ``event``, or drop this viewer when it has fallen too far behind."""
        self._backlog += len(event.data)
        if self._backlog > _MAX_VIEWER_BACKLOG_BYTES:
            self.dropped = True
            event = TerminalEvent("dropped")
        self._queue.put_nowait(event)

    async def next(self) -> TerminalEvent:
        """Wait for the next event."""
        event = await self._queue.get()
        self._backlog -= len(event.data)
        return event


class TerminalSession:
    """One shell in a pty, its output and its viewers."""

    def __init__(self, *, owner: str, cols: int, rows: int, process: subprocess.Popen[bytes], master_fd: int) -> None:
        self.id = secrets.token_urlsafe(12)
        self.owner = owner
        self.cols = cols
        self.rows = rows
        self.created_at = time.time()
        self.exit_code: int | None = None
        self._process = process
        self._master_fd: int | None = master_fd
        self._scrollback = _Scrollback()
        self._viewers: set[TerminalViewer] = set()
        self._pending_input = bytearray()
        self._writing = False
        self._loop = asyncio.get_running_loop()
        self._last_active = self._loop.time()
        self._loop.add_reader(master_fd, self._on_readable)
        self._exit_task = asyncio.create_task(self._wait_exit(), name=f"terminal-exit-{self.id}")

    @property
    def alive(self) -> bool:
        """Whether the shell still runs."""
        return self.exit_code is None

    @property
    def pid(self) -> int:
        """The shell's process ID."""
        return self._process.pid

    def info(self) -> TerminalInfo:
        """Describe the session."""
        return TerminalInfo(self.id, self.cols, self.rows, self.alive, self.exit_code, self.created_at)

    def idle_seconds(self) -> float:
        """How long the session has had no input and no viewer."""
        return 0.0 if self._viewers else self._loop.time() - self._last_active

    # --- viewers --------------------------------------------------------------------------------------------------

    def attach(self) -> tuple[bytes, TerminalViewer]:
        """Return the scrollback and a viewer that gets everything after it, with no gap and nothing twice."""
        viewer = TerminalViewer()
        self._last_active = self._loop.time()
        if self.alive:
            self._viewers.add(viewer)
        else:
            viewer.put(TerminalEvent("exit", code=self.exit_code))
        return self._scrollback.snapshot(), viewer

    def detach(self, viewer: TerminalViewer) -> None:
        """Stop sending ``viewer`` events."""
        self._viewers.discard(viewer)
        self._last_active = self._loop.time()

    def _publish(self, event: TerminalEvent) -> None:
        for viewer in tuple(self._viewers):
            viewer.put(event)
            if viewer.dropped:
                self._viewers.discard(viewer)

    # --- output ---------------------------------------------------------------------------------------------------

    def _on_readable(self) -> None:
        if self._master_fd is None:
            return
        try:
            chunk = os.read(self._master_fd, _READ_CHUNK)
        except BlockingIOError:
            return
        except OSError:
            # EIO: every process holding the pty's other end closed it. The exit task ends the session.
            chunk = b""
        if not chunk:
            self._loop.remove_reader(self._master_fd)
            return
        self._on_output(chunk)

    def _on_output(self, chunk: bytes) -> None:
        self._scrollback.append(chunk)
        self._publish(TerminalEvent("output", data=chunk))

    async def _wait_exit(self) -> None:
        try:
            pidfd = os.pidfd_open(self._process.pid)
        except (AttributeError, OSError):
            # No pidfds on this platform: poll, since no thread may sit in wait() while the loop shuts down.
            while (code := self._process.poll()) is None:  # noqa: ASYNC110 - no event says a child exited
                await asyncio.sleep(_EXIT_POLL_SECONDS)
        else:
            try:
                exited = self._loop.create_future()
                self._loop.add_reader(pidfd, lambda: exited.done() or exited.set_result(None))
                try:
                    await exited
                finally:
                    self._loop.remove_reader(pidfd)
            finally:
                os.close(pidfd)
            code = self._process.wait()
        self._finish(code)

    def _finish(self, code: int) -> None:
        if self._master_fd is not None:
            # What the shell wrote before it exited.
            with contextlib.suppress(OSError):
                while chunk := os.read(self._master_fd, _READ_CHUNK):
                    self._on_output(chunk)
            self._loop.remove_reader(self._master_fd)
            self._loop.remove_writer(self._master_fd)
            with contextlib.suppress(OSError):
                os.close(self._master_fd)
            self._master_fd = None
        self._pending_input.clear()
        self.exit_code = code
        self._publish(TerminalEvent("exit", code=code))
        self._viewers.clear()

    # --- input ----------------------------------------------------------------------------------------------------

    def write(self, data: bytes) -> None:
        """Type ``data`` into the shell, in order, without blocking."""
        if self._master_fd is None:
            raise TerminalError(409, "The terminal has exited.")
        if len(data) > _MAX_INPUT_BYTES:
            raise TerminalError(413, "Too much input at once.")
        if len(self._pending_input) + len(data) > _MAX_PENDING_INPUT_BYTES:
            raise TerminalError(429, "The terminal is not reading its input.")
        self._last_active = self._loop.time()
        self._pending_input += data
        self._flush_input()

    def _flush_input(self) -> None:
        if self._master_fd is None:
            return
        while self._pending_input:
            try:
                written = os.write(self._master_fd, self._pending_input)
            except BlockingIOError:
                break
            except OSError:
                self._pending_input.clear()
                break
            del self._pending_input[:written]
        if self._pending_input and not self._writing:
            self._writing = True
            self._loop.add_writer(self._master_fd, self._flush_input)
        elif not self._pending_input and self._writing:
            self._writing = False
            self._loop.remove_writer(self._master_fd)

    def resize(self, cols: int, rows: int) -> None:
        """Change the terminal's size; the kernel tells the shell's foreground job (SIGWINCH)."""
        cols, rows = _clamp_size(cols, rows)
        if self._master_fd is None or (cols, rows) == (self.cols, self.rows):
            return
        _set_size(self._master_fd, cols, rows)
        self.cols, self.rows = cols, rows

    # --- ending ---------------------------------------------------------------------------------------------------

    async def close(self) -> None:
        """Hang up the shell, as closing a terminal window does, and kill it if it lingers."""
        if self.alive:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self._process.pid, signal.SIGHUP)
            done, _ = await asyncio.wait({self._exit_task}, timeout=_HANGUP_GRACE_SECONDS)
            if not done:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(self._process.pid, signal.SIGKILL)
        await asyncio.wait({self._exit_task})


def _spawn(launch: TerminalLaunch, *, owner: str, cols: int, rows: int) -> TerminalSession:
    """Start ``launch`` in a new pty as the leader of its own session."""
    cols, rows = _clamp_size(cols, rows)
    master_fd, slave_fd = os.openpty()
    try:
        _set_size(slave_fd, cols, rows)
        os.set_blocking(master_fd, False)
        # The shell leads a new session, and interactive Bash takes the pty as its controlling terminal.
        process = subprocess.Popen(
            launch.argv,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=launch.cwd,
            env=launch.env,
            close_fds=True,
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError) as error:
        os.close(master_fd)
        raise TerminalError(500, f"The terminal could not start: {error}") from None
    finally:
        os.close(slave_fd)
    return TerminalSession(owner=owner, cols=cols, rows=rows, process=process, master_fd=master_fd)


class TerminalManager:
    """The sessions of one process, each under its owner."""

    def __init__(self) -> None:
        self._sessions: dict[str, TerminalSession] = {}
        self._idle_watchers: dict[str, asyncio.Task[None]] = {}

    def open(self, owner: str, launch: TerminalLaunch, *, cols: int, rows: int) -> TerminalSession:
        """Start a session for ``owner``, making room by forgetting its oldest ended ones."""
        if not _OWNER.fullmatch(owner):
            raise TerminalError(400, "Invalid terminal owner.")
        mine = self.sessions(owner)
        ended = [session for session in mine if not session.alive]
        for session in ended[: max(0, len(mine) - _MAX_SESSIONS_PER_OWNER + 1)]:
            self._forget(session)
        if len(self.sessions(owner)) >= _MAX_SESSIONS_PER_OWNER:
            raise TerminalError(429, f"At most {_MAX_SESSIONS_PER_OWNER} terminals per workspace; close one first.")
        if len(self._sessions) >= _MAX_SESSIONS:
            raise TerminalError(429, "Too many terminals are open here.")
        session = _spawn(launch, owner=owner, cols=cols, rows=rows)
        self._sessions[session.id] = session
        self._idle_watchers[session.id] = asyncio.create_task(
            self._end_when_idle(session),
            name=f"terminal-idle-{session.id}",
        )
        logger.info("Terminal opened", session_id=session.id, pid=session.pid)
        return session

    def get(self, owner: str, session_id: str) -> TerminalSession:
        """Return ``owner``'s session, or refuse as if no other owner's existed."""
        session = self._sessions.get(session_id)
        if session is None or session.owner != owner:
            raise TerminalError(404, "No such terminal.")
        return session

    def sessions(self, owner: str) -> list[TerminalSession]:
        """Return ``owner``'s sessions, oldest first."""
        return sorted((s for s in self._sessions.values() if s.owner == owner), key=lambda s: s.created_at)

    async def close(self, owner: str, session_id: str) -> None:
        """End ``owner``'s session and forget it."""
        session = self.get(owner, session_id)
        self._forget(session)
        await session.close()
        logger.info("Terminal closed", session_id=session.id)

    async def close_all(self) -> None:
        """End every session, as the process stops."""
        sessions = tuple(self._sessions.values())
        for session in sessions:
            self._forget(session)
        await asyncio.gather(*(session.close() for session in sessions), return_exceptions=True)

    def _forget(self, session: TerminalSession) -> None:
        self._sessions.pop(session.id, None)
        watcher = self._idle_watchers.pop(session.id, None)
        if watcher is not None and watcher is not asyncio.current_task():
            watcher.cancel()

    async def _end_when_idle(self, session: TerminalSession) -> None:
        while (idle := session.idle_seconds()) < _IDLE_SECONDS:  # noqa: ASYNC110 - idle time has no event to await
            await asyncio.sleep(_IDLE_SECONDS - idle)
        self._forget(session)
        logger.info("Terminal idle", session_id=session.id, alive=session.alive)
        await session.close()
