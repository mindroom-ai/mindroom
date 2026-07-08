"""Warm-template forkserver for sandbox subprocess dispatch.

Spawning a fresh ``python -m mindroom.api.sandbox_runner`` per tool call pays
the full runtime import (~11-17 s on a 2-CPU worker pod) on every request.
This module keeps one long-lived *template* process per interpreter that pays
that import once and then blocks on a unix socket. Each request forks the
template; the fork child applies the per-request env and cwd, executes the
already-prepared envelope, streams the response back over the socket, and
exits. Isolation semantics match spawn-per-call - every request still runs in
its own fresh process - at ~ms fork cost instead of the import cost.

The template must stay fork-safe: it only imports modules and then blocks on
``accept()``; it never starts threads, event loops, or network clients.

The template's stderr file collects raw-fd output from fork children (native
libraries or tool subprocesses writing to fd 2); Python-level tool output is
captured per request. The file lives for the template's lifetime and is
removed on recycle, shutdown, or orphan exit.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import math
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

logger = get_logger(__name__)

TEMPLATE_ARG = "--sandbox-forkserver-template"

_TEMPLATE_LISTEN_BACKLOG = 64
_TEMPLATE_ACCEPT_POLL_SECONDS = 1.0
_TEMPLATE_TERMINATE_WAIT_SECONDS = 5.0
_CHILD_REQUEST_READ_TIMEOUT_SECONDS = 30.0
_CHILD_RESPONSE_WRITE_TIMEOUT_SECONDS = 60.0
_CHILD_TIMEOUT_GRACE_SECONDS = 5.0
_STARTUP_FAILURE_COOLDOWN_SECONDS = 300.0
_READY_PROBE_INTERVAL_SECONDS = 0.05
_STDERR_TAIL_BYTES = 4096
_RECV_CHUNK_BYTES = 65536


class ForkserverError(RuntimeError):
    """One forkserver-dispatched request failed; message maps to a worker failure."""


class ForkserverStartupError(ForkserverError):
    """The template process failed to start; callers should fall back to spawn-per-call."""


class ForkserverTimeoutError(ForkserverError):
    """One forkserver-dispatched request exceeded the sandbox subprocess timeout."""


def forkserver_supported() -> bool:
    """Return whether warm-template fork dispatch is available on this platform."""
    return hasattr(os, "fork") and hasattr(socket, "AF_UNIX")


def _template_fingerprint(python_executable: str, template_env: Mapping[str, str] | None) -> str:
    """Fingerprint the interpreter and base env that bake the template's import graph.

    The interpreter stat stamp changes when a worker venv is rebuilt in place,
    so a stale template is recycled even when the env values are unchanged.
    """
    try:
        stat_result = Path(python_executable).stat()
        interpreter_stamp = f"{stat_result.st_ino}:{stat_result.st_mtime_ns}:{stat_result.st_size}"
    except OSError:
        interpreter_stamp = "missing"
    env_items = sorted(template_env.items()) if template_env is not None else None
    payload = json.dumps([python_executable, interpreter_stamp, env_items])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _default_template_command(python_executable: str, socket_path: str) -> list[str]:
    return [python_executable, "-m", "mindroom.api.sandbox_runner", TEMPLATE_ARG, socket_path]


@dataclass(frozen=True)
class _Template:
    fingerprint: str
    process: subprocess.Popen[bytes]
    socket_path: str
    stderr_path: Path
    runtime_dir: Path


@dataclass(frozen=True)
class _ChildRequest:
    """One parent-to-child request line, authored by `_request`."""

    env: dict[str, str] | None
    cwd: str | None
    envelope: str
    timeout_seconds: float


def _stderr_tail(stderr_path: Path) -> str:
    try:
        raw = stderr_path.read_bytes()
    except OSError:
        return ""
    return raw[-_STDERR_TAIL_BYTES:].decode("utf-8", errors="replace").strip()


def _terminate_template(template: _Template) -> None:
    process = template.process
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=_TEMPLATE_TERMINATE_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    shutil.rmtree(template.runtime_dir, ignore_errors=True)


class _ConnectionClosedError(RuntimeError):
    """The socket closed before a full protocol line arrived."""


class _SocketLineReader:
    """Read newline-framed protocol lines from a socket under one deadline."""

    def __init__(self, conn: socket.socket, deadline: float) -> None:
        self._conn = conn
        self._deadline = deadline
        self._buffer = bytearray()

    def read_line(self) -> bytes:
        while b"\n" not in self._buffer:
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            self._conn.settimeout(remaining)
            chunk = self._conn.recv(_RECV_CHUNK_BYTES)
            if not chunk:
                raise _ConnectionClosedError
            self._buffer.extend(chunk)
        line, _, rest = bytes(self._buffer).partition(b"\n")
        self._buffer = bytearray(rest)
        return line


class _SandboxForkserver:
    """Runner-side manager for warm template processes keyed by interpreter."""

    def __init__(
        self,
        template_command: Callable[[str, str], list[str]] | None = None,
        *,
        startup_failure_cooldown_seconds: float = _STARTUP_FAILURE_COOLDOWN_SECONDS,
    ) -> None:
        self._template_command = template_command or _default_template_command
        self._startup_failure_cooldown_seconds = startup_failure_cooldown_seconds
        self._lock = threading.Lock()
        self._key_locks: dict[str, threading.Lock] = {}
        self._templates: dict[str, _Template] = {}
        self._failed_fingerprints: dict[str, tuple[str, float]] = {}

    def execute(
        self,
        *,
        python_executable: str | None,
        template_env: dict[str, str] | None,
        request_env: dict[str, str] | None,
        request_cwd: str | None,
        envelope: str,
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]:
        """Execute one prepared envelope in a fresh fork of the warm template."""
        deadline = time.monotonic() + timeout_seconds
        key = python_executable or sys.executable
        fingerprint = _template_fingerprint(key, template_env)
        key_lock = self._key_lock(key)
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not key_lock.acquire(timeout=remaining):
            msg = "Timed out waiting for the sandbox forkserver template."
            raise ForkserverTimeoutError(msg)
        try:
            template = self._ready_template(
                key=key,
                fingerprint=fingerprint,
                template_env=template_env,
                deadline=deadline,
            )
        finally:
            key_lock.release()
        return self._request(
            key,
            template,
            request_env=request_env,
            request_cwd=request_cwd,
            envelope=envelope,
            deadline=deadline,
        )

    def shutdown(self) -> None:
        """Terminate all template processes and clear cached state."""
        with self._lock:
            templates = list(self._templates.values())
            self._templates.clear()
            self._failed_fingerprints.clear()
        for template in templates:
            _terminate_template(template)

    def _key_lock(self, key: str) -> threading.Lock:
        with self._lock:
            return self._key_locks.setdefault(key, threading.Lock())

    def _ready_template(
        self,
        *,
        key: str,
        fingerprint: str,
        template_env: dict[str, str] | None,
        deadline: float,
    ) -> _Template:
        template = self._templates.get(key)
        if template is not None and template.process.poll() is not None:
            logger.warning("sandbox_forkserver_template_died", python_executable=key)
            self._discard(key, template)
            template = None
        # A pinned fingerprint must bail out before the recycle check so it
        # cannot tear down a healthy template of another fingerprint.
        failure = self._failed_fingerprints.get(fingerprint)
        if failure is not None:
            message, failed_at = failure
            if time.monotonic() - failed_at < self._startup_failure_cooldown_seconds:
                raise ForkserverStartupError(message)
            self._failed_fingerprints.pop(fingerprint, None)
        if template is not None and template.fingerprint != fingerprint:
            logger.info("sandbox_forkserver_template_recycled", python_executable=key)
            self._discard(key, template)
            template = None
        if template is None:
            template = self._spawn_template(
                key=key,
                fingerprint=fingerprint,
                template_env=template_env,
                deadline=deadline,
            )
            with self._lock:
                self._templates[key] = template
        return template

    def _discard(self, key: str, template: _Template) -> None:
        with self._lock:
            if self._templates.get(key) is template:
                self._templates.pop(key)
        _terminate_template(template)

    def _spawn_template(
        self,
        *,
        key: str,
        fingerprint: str,
        template_env: dict[str, str] | None,
        deadline: float,
    ) -> _Template:
        runtime_dir = Path(tempfile.mkdtemp(prefix="mindroom-fs-"))
        socket_path = str(runtime_dir / "template.sock")
        stderr_path = runtime_dir / "template.err"
        logger.info("sandbox_forkserver_template_spawning", python_executable=key)
        try:
            with stderr_path.open("wb") as stderr_file:
                process = subprocess.Popen(
                    self._template_command(key, socket_path),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr_file,
                    env=template_env,
                )
        except OSError as exc:
            shutil.rmtree(runtime_dir, ignore_errors=True)
            msg = f"Failed to start sandbox forkserver template: {exc}"
            self._failed_fingerprints[fingerprint] = (msg, time.monotonic())
            raise ForkserverStartupError(msg) from exc
        template = _Template(
            fingerprint=fingerprint,
            process=process,
            socket_path=socket_path,
            stderr_path=stderr_path,
            runtime_dir=runtime_dir,
        )
        self._wait_template_ready(template=template, fingerprint=fingerprint, deadline=deadline)
        logger.info("sandbox_forkserver_template_ready", python_executable=key, template_pid=process.pid)
        return template

    def _wait_template_ready(self, *, template: _Template, fingerprint: str, deadline: float) -> None:
        while True:
            if template.process.poll() is not None:
                stderr_tail = _stderr_tail(template.stderr_path)
                shutil.rmtree(template.runtime_dir, ignore_errors=True)
                msg = (
                    f"Sandbox forkserver template exited during startup "
                    f"(code {template.process.returncode}): {stderr_tail}"
                )
                self._failed_fingerprints[fingerprint] = (msg, time.monotonic())
                logger.warning("sandbox_forkserver_template_startup_failed", error=msg)
                raise ForkserverStartupError(msg)
            if time.monotonic() >= deadline:
                _terminate_template(template)
                msg = "Timed out waiting for the sandbox forkserver template to import the runtime."
                self._failed_fingerprints[fingerprint] = (msg, time.monotonic())
                logger.warning("sandbox_forkserver_template_startup_failed", error=msg)
                raise ForkserverStartupError(msg)
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(1.0)
                    probe.connect(template.socket_path)
            except OSError:
                time.sleep(_READY_PROBE_INTERVAL_SECONDS)
            else:
                return

    def _request(
        self,
        key: str,
        template: _Template,
        *,
        request_env: dict[str, str] | None,
        request_cwd: str | None,
        envelope: str,
        deadline: float,
    ) -> subprocess.CompletedProcess[str]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # Never deliver a request the caller has no budget left for; a
            # delivered request forks a child the parent may not learn about.
            raise ForkserverTimeoutError
        request_payload = (
            json.dumps(
                {"env": request_env, "cwd": request_cwd, "envelope": envelope, "timeout_seconds": remaining},
            ).encode("utf-8")
            + b"\n"
        )
        child_pid: int | None = None
        try:
            conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        except OSError as exc:
            msg = f"Failed to open a sandbox forkserver connection: {exc}"
            raise ForkserverError(msg) from exc
        try:
            conn.settimeout(remaining)
            try:
                conn.connect(template.socket_path)
                conn.sendall(request_payload)
            except TimeoutError as exc:
                raise ForkserverTimeoutError from exc
            except OSError as exc:
                self._discard(key, template)
                msg = f"Failed to reach the sandbox forkserver template: {exc}"
                raise ForkserverError(msg) from exc
            reader = _SocketLineReader(conn, deadline)
            try:
                child_pid = int(json.loads(reader.read_line())["pid"])
                response = json.loads(reader.read_line())
                returncode = int(response["returncode"])
                stdout_text = str(response["stdout"])
                stderr_text = str(response["stderr"])
            except TimeoutError as exc:
                self._kill_child(child_pid)
                raise ForkserverTimeoutError from exc
            except (_ConnectionClosedError, OSError, ValueError, KeyError, TypeError) as exc:
                if template.process.poll() is not None:
                    self._discard(key, template)
                msg = "Sandbox forkserver child exited without returning a response."
                raise ForkserverError(msg) from exc
        finally:
            conn.close()
        return subprocess.CompletedProcess(
            args=["sandbox-forkserver", key],
            returncode=returncode,
            stdout=stdout_text,
            stderr=stderr_text,
        )

    @staticmethod
    def _kill_child(child_pid: int | None) -> None:
        # The pid is deserialized from the child's socket message; never let a
        # degenerate value reach os.kill, where 0 targets the process group.
        if child_pid is None or child_pid <= 0:
            return
        with suppress(OSError):
            os.kill(child_pid, signal.SIGKILL)


_forkserver: _SandboxForkserver | None = None
_forkserver_lock = threading.Lock()


def get_sandbox_forkserver() -> _SandboxForkserver:
    """Return the process-wide forkserver manager, creating it on first use."""
    global _forkserver
    with _forkserver_lock:
        if _forkserver is None:
            _forkserver = _SandboxForkserver()
            atexit.register(_forkserver.shutdown)
        return _forkserver


def serve_template(socket_path: str, run_payload: Callable[[str], tuple[int, str, str]]) -> int:
    """Template process main loop: accept one connection per request and fork.

    Runs inside ``python -m mindroom.api.sandbox_runner --sandbox-forkserver-template``
    after the heavy runtime import has completed. Must stay fork-safe: no
    threads, event loops, or network clients before forking.
    """
    parent_pid = os.getppid()
    # Auto-reap forked request children; each child restores SIG_DFL for its
    # own tool subprocesses immediately after fork.
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(socket_path)
        server.listen(_TEMPLATE_LISTEN_BACKLOG)
        server.settimeout(_TEMPLATE_ACCEPT_POLL_SECONDS)
        while True:
            try:
                conn, _addr = server.accept()
            except TimeoutError:
                if os.getppid() != parent_pid:
                    # Orphaned: the runner died without running its cleanup
                    # (e.g. SIGKILL), so remove our runtime dir ourselves.
                    shutil.rmtree(Path(socket_path).parent, ignore_errors=True)
                    return 0
                continue
            try:
                child_pid = os.fork()
            except OSError:
                # Fork pressure (e.g. EAGAIN) fails this one request via EOF on
                # the connection; the template keeps serving.
                conn.close()
                continue
            if child_pid == 0:
                server.close()
                _child_main(conn, run_payload)
            conn.close()
    finally:
        server.close()


def _child_main(conn: socket.socket, run_payload: Callable[[str], tuple[int, str, str]]) -> NoReturn:
    """Fork-child request handler; never returns to the template loop."""
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    exit_code = 1
    try:
        _send_json(conn, {"pid": os.getpid()})
        reader = _SocketLineReader(conn, time.monotonic() + _CHILD_REQUEST_READ_TIMEOUT_SECONDS)
        try:
            request_line = reader.read_line()
        except _ConnectionClosedError:
            # Ready-wait probe connections close without sending a request line.
            request_line = b""
        if request_line.strip():
            exit_code = _run_child_request(conn, _parse_child_request(request_line), run_payload)
    except Exception:
        with suppress(Exception):
            _send_json(
                conn,
                {
                    "returncode": 1,
                    "stdout": "",
                    "stderr": f"Sandbox forkserver child failed: {traceback.format_exc()}",
                },
            )
    with suppress(Exception):
        conn.close()
    os._exit(exit_code)


def _parse_child_request(request_line: bytes) -> _ChildRequest:
    payload = json.loads(request_line)
    return _ChildRequest(
        env=payload["env"],
        cwd=payload["cwd"],
        envelope=payload["envelope"],
        timeout_seconds=payload["timeout_seconds"],
    )


def _run_child_request(
    conn: socket.socket,
    request: _ChildRequest,
    run_payload: Callable[[str], tuple[int, str, str]],
) -> int:
    # Self-destruct backstop for the rare case where the runner never learned
    # this child's pid; the grace keeps the runner-side SIGKILL primary.
    signal.alarm(max(1, math.ceil(request.timeout_seconds + _CHILD_TIMEOUT_GRACE_SECONDS)))
    if request.env is not None:
        os.environ.clear()
        os.environ.update(request.env)
    if request.cwd is not None:
        os.chdir(request.cwd)
    # `python -m` prepends the effective cwd to sys.path; mirror that for the
    # request cwd (the runner template drops its own baked entry at startup).
    sys.path.insert(0, str(Path.cwd()))
    returncode, stdout_text, stderr_text = run_payload(request.envelope)
    conn.settimeout(_CHILD_RESPONSE_WRITE_TIMEOUT_SECONDS)
    _send_json(conn, {"returncode": returncode, "stdout": stdout_text, "stderr": stderr_text})
    return 0


def _send_json(conn: socket.socket, payload: dict[str, object]) -> None:
    conn.sendall(json.dumps(payload).encode("utf-8") + b"\n")
