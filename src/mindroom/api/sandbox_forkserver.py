"""Warm-template forkserver for sandbox subprocess dispatch.

Spawning a fresh ``python -m mindroom.api.sandbox_runner`` per tool call pays
the full runtime import (~11-17 s on a 2-CPU worker pod) on every request.
This module keeps one long-lived *template* process per interpreter that pays
that import once and then blocks on a unix socket. Each request forks the
template; the fork child applies the per-request env and cwd, executes the
already-prepared envelope, streams its progress and then the response back
over the socket, and exits. Isolation semantics match spawn-per-call - every request still runs in
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
import select
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

logger = get_logger(__name__)

TEMPLATE_ARG = "--sandbox-forkserver-template"
PRELOAD_FILE_ENV = "MINDROOM_SANDBOX_FORKSERVER_PRELOAD_FILE"

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


def _template_fingerprint(
    python_executable: str,
    template_env: Mapping[str, str] | None,
    *,
    preload_fingerprint: str | None = None,
) -> str:
    """Fingerprint the interpreter, base env, and preloaded plugins that bake the template's import graph.

    The interpreter stat stamp changes when a worker venv is rebuilt in place,
    so a stale template is recycled even when the env values are unchanged.
    """
    try:
        stat_result = Path(python_executable).stat()
        interpreter_stamp = f"{stat_result.st_ino}:{stat_result.st_mtime_ns}:{stat_result.st_size}"
    except OSError:
        interpreter_stamp = "missing"
    env_items = sorted(template_env.items()) if template_env is not None else None
    payload = json.dumps([python_executable, interpreter_stamp, env_items, preload_fingerprint])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _default_template_command(python_executable: str, socket_path: str) -> list[str]:
    return [python_executable, "-P", "-s", "-m", "mindroom.api.sandbox_runner", TEMPLATE_ARG, socket_path]


def _control_socket_path(socket_path: str) -> str:
    return str(Path(socket_path).with_suffix(".ctl.sock"))


@dataclass
class _Template:
    fingerprint: str
    base_fingerprint: str
    process: subprocess.Popen[bytes]
    socket_path: str
    stderr_path: Path
    runtime_dir: Path
    preload_fingerprint: str | None = None
    spawned_at: float = field(default_factory=time.monotonic)
    # Set once the socket accepts connections; mutated only under the key lock.
    ready: bool = False


@dataclass(frozen=True)
class _ChildRequest:
    """One parent-to-child request line, authored by `_request`."""

    env: dict[str, str] | None
    cwd: str | None
    envelope: str
    timeout_seconds: float | None


@dataclass
class _RequestChild:
    """The child serving one request, signalled only while that request runs.

    The template reaps children as they exit, so after its request ended a child's PID may name another process.
    """

    _lock: threading.Lock = field(default_factory=threading.Lock)
    _pid: int | None = None
    _ended: bool = False

    def started(self, pid: int) -> None:
        with self._lock:
            if not self._ended:
                self._pid = pid

    def end(self) -> None:
        with self._lock:
            self._ended = True
            self._pid = None

    def kill(self) -> None:
        """Kill the child's process group, which also stops what its tool started."""
        with self._lock:
            # The pid is deserialized from the child's socket message; never let a
            # degenerate value reach os.killpg, where 0 targets this process's own group.
            if self._pid is not None and self._pid > 0:
                with suppress(OSError):
                    os.killpg(self._pid, signal.SIGKILL)


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
    """Read newline-framed protocol lines from a socket under one deadline, or none."""

    def __init__(self, conn: socket.socket, deadline: float | None) -> None:
        self._conn = conn
        self._deadline = deadline
        self._buffer = bytearray()

    def read_line(self) -> bytes:
        while b"\n" not in self._buffer:
            remaining = None if self._deadline is None else self._deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
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
        self._failed_preload_fingerprints: dict[str, float] = {}

    def warm(
        self,
        *,
        python_executable: str | None,
        template_env: dict[str, str] | None,
        startup_timeout_seconds: float,
        preload_spec: Mapping[str, object] | None = None,
        preload_fingerprint: str | None = None,
    ) -> None:
        """Start and wait for one warm template ahead of the first request."""
        now = time.monotonic()
        self._ensure_ready_template(
            key=python_executable or sys.executable,
            template_env=template_env,
            preload_spec=preload_spec,
            preload_fingerprint=preload_fingerprint,
            startup_deadline=now + startup_timeout_seconds,
        )

    def execute(
        self,
        *,
        python_executable: str | None,
        template_env: dict[str, str] | None,
        request_env: dict[str, str] | None,
        request_cwd: str | None,
        envelope: str,
        timeout_seconds: float | None,
        startup_timeout_seconds: float,
        bind_stop: Callable[[Callable[[], None]], None],
        on_progress: Callable[[str], None],
        preload_spec: Mapping[str, object] | None = None,
        preload_fingerprint: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Execute one prepared envelope in a fresh fork of the warm template.

        ``timeout_seconds`` of ``None`` lets the request run however long it takes; getting a warm template still
        has ``startup_timeout_seconds``. ``bind_stop`` receives a function that stops the request by hanging up on
        its forked child. ``on_progress`` receives each progress report the child sends before its response.
        """
        now = time.monotonic()
        deadline = None if timeout_seconds is None else now + timeout_seconds
        startup_deadline = deadline if deadline is not None else now + startup_timeout_seconds
        key = python_executable or sys.executable
        template = self._ensure_ready_template(
            key=key,
            template_env=template_env,
            preload_spec=preload_spec,
            preload_fingerprint=preload_fingerprint,
            startup_deadline=startup_deadline,
        )
        template_ready_at = time.monotonic()
        completed = self._request(
            key,
            template,
            request_env=request_env,
            request_cwd=request_cwd,
            envelope=envelope,
            deadline=deadline,
            bind_stop=bind_stop,
            on_progress=on_progress,
        )
        finished_at = time.monotonic()
        logger.debug(
            "sandbox_forkserver_request_timing",
            python_executable=key,
            template_wait_ms=round((template_ready_at - now) * 1000, 1),
            request_ms=round((finished_at - template_ready_at) * 1000, 1),
            total_ms=round((finished_at - now) * 1000, 1),
        )
        return completed

    def _ensure_ready_template(
        self,
        *,
        key: str,
        template_env: dict[str, str] | None,
        preload_spec: Mapping[str, object] | None,
        preload_fingerprint: str | None,
        startup_deadline: float,
    ) -> _Template:
        key_lock = self._key_lock(key)
        remaining = startup_deadline - time.monotonic()
        if remaining <= 0 or not key_lock.acquire(timeout=remaining):
            msg = "Timed out waiting for the sandbox forkserver template."
            raise ForkserverTimeoutError(msg)
        try:
            effective_spec, effective_preload_fp = self._resolve_preload(preload_spec, preload_fingerprint)
            base_fingerprint = _template_fingerprint(key, template_env)
            fingerprint = _template_fingerprint(key, template_env, preload_fingerprint=effective_preload_fp)
            return self._ready_template(
                key=key,
                fingerprint=fingerprint,
                base_fingerprint=base_fingerprint,
                template_env=template_env,
                preload_spec=effective_spec,
                preload_fingerprint=effective_preload_fp,
                deadline=startup_deadline,
            )
        finally:
            key_lock.release()

    def _resolve_preload(
        self,
        preload_spec: Mapping[str, object] | None,
        preload_fingerprint: str | None,
    ) -> tuple[Mapping[str, object] | None, str | None]:
        if preload_spec is None or preload_fingerprint is None:
            return None, None
        failed_at = self._failed_preload_fingerprints.get(preload_fingerprint)
        if failed_at is not None:
            if time.monotonic() - failed_at < self._startup_failure_cooldown_seconds:
                return None, None
            self._failed_preload_fingerprints.pop(preload_fingerprint, None)
        return preload_spec, preload_fingerprint

    def shutdown(self) -> None:
        """Terminate all template processes and clear cached state."""
        with self._lock:
            templates = list(self._templates.values())
            self._templates.clear()
            self._failed_fingerprints.clear()
            self._failed_preload_fingerprints.clear()
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
        base_fingerprint: str,
        template_env: dict[str, str] | None,
        preload_spec: Mapping[str, object] | None,
        preload_fingerprint: str | None,
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
        if (
            template is not None
            and template.ready
            and template.preload_fingerprint is None
            and preload_spec is not None
            and preload_fingerprint is not None
            and template.base_fingerprint == base_fingerprint
        ):
            self._preload_template_in_place(
                key=key,
                template=template,
                preload_spec=preload_spec,
                preload_fingerprint=preload_fingerprint,
                fingerprint=fingerprint,
                deadline=deadline,
            )
            return template
        if template is not None and template.fingerprint != fingerprint:
            logger.info("sandbox_forkserver_template_recycled", python_executable=key)
            self._discard(key, template)
            template = None
        if template is None:
            template = self._spawn_template(
                key=key,
                fingerprint=fingerprint,
                base_fingerprint=base_fingerprint,
                template_env=template_env,
                preload_spec=preload_spec,
                preload_fingerprint=preload_fingerprint,
            )
            with self._lock:
                self._templates[key] = template
        try:
            self._wait_template_ready(key=key, template=template, deadline=deadline)
        except ForkserverStartupError as exc:
            with self._lock:
                if self._templates.get(key) is template:
                    self._templates.pop(key)
            shutil.rmtree(template.runtime_dir, ignore_errors=True)
            if preload_fingerprint is not None:
                self._failed_preload_fingerprints[preload_fingerprint] = time.monotonic()
                logger.warning("sandbox_forkserver_plugin_preload_failed", error=str(exc))
            else:
                self._failed_fingerprints[fingerprint] = (str(exc), time.monotonic())
                logger.warning("sandbox_forkserver_template_startup_failed", error=str(exc))
            raise
        return template

    def _discard(self, key: str, template: _Template) -> None:
        with self._lock:
            if self._templates.get(key) is template:
                self._templates.pop(key)
        _terminate_template(template)

    def _preload_template_in_place(
        self,
        *,
        key: str,
        template: _Template,
        preload_spec: Mapping[str, object],
        preload_fingerprint: str,
        fingerprint: str,
        deadline: float,
    ) -> None:
        """Load the first snapshot's plugins into an already-warm no-plugin template without respawning it."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ForkserverTimeoutError
        control_path = _control_socket_path(template.socket_path)
        payload = json.dumps(dict(preload_spec), sort_keys=True).encode("utf-8") + b"\n"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(remaining)
                conn.connect(control_path)
                conn.sendall(payload)
                response = json.loads(_SocketLineReader(conn, deadline).read_line())
        except TimeoutError as exc:
            raise ForkserverTimeoutError from exc
        except (_ConnectionClosedError, OSError, ValueError, TypeError) as exc:
            self._discard(key, template)
            self._failed_preload_fingerprints[preload_fingerprint] = time.monotonic()
            msg = f"Sandbox forkserver in-place plugin preload failed: {exc}"
            logger.warning("sandbox_forkserver_plugin_preload_failed", error=msg)
            raise ForkserverStartupError(msg) from exc
        if not response.get("ok"):
            self._discard(key, template)
            self._failed_preload_fingerprints[preload_fingerprint] = time.monotonic()
            msg = str(response.get("error") or "Sandbox forkserver in-place plugin preload failed.")
            logger.warning("sandbox_forkserver_plugin_preload_failed", error=msg)
            raise ForkserverStartupError(msg)
        template.preload_fingerprint = preload_fingerprint
        template.fingerprint = fingerprint

    def _spawn_template(
        self,
        *,
        key: str,
        fingerprint: str,
        base_fingerprint: str,
        template_env: dict[str, str] | None,
        preload_spec: Mapping[str, object] | None,
        preload_fingerprint: str | None,
    ) -> _Template:
        runtime_dir = Path(tempfile.mkdtemp(prefix="mindroom-fs-"))
        socket_path = str(runtime_dir / "template.sock")
        stderr_path = runtime_dir / "template.err"
        spawn_env = template_env
        if preload_spec is not None:
            preload_path = runtime_dir / "preload.json"
            preload_path.write_text(json.dumps(dict(preload_spec), sort_keys=True), encoding="utf-8")
            spawn_env = dict(os.environ if template_env is None else template_env)
            spawn_env[PRELOAD_FILE_ENV] = str(preload_path)
        logger.info("sandbox_forkserver_template_spawning", python_executable=key)
        try:
            with stderr_path.open("wb") as stderr_file:
                process = subprocess.Popen(
                    self._template_command(key, socket_path),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr_file,
                    env=spawn_env,
                )
        except OSError as exc:
            shutil.rmtree(runtime_dir, ignore_errors=True)
            msg = f"Failed to start sandbox forkserver template: {exc}"
            self._failed_fingerprints[fingerprint] = (msg, time.monotonic())
            raise ForkserverStartupError(msg) from exc
        return _Template(
            fingerprint=fingerprint,
            base_fingerprint=base_fingerprint,
            process=process,
            socket_path=socket_path,
            stderr_path=stderr_path,
            runtime_dir=runtime_dir,
            preload_fingerprint=preload_fingerprint,
        )

    def _wait_template_ready(self, *, key: str, template: _Template, deadline: float) -> None:
        if template.ready:
            return
        while True:
            if template.process.poll() is not None:
                stderr_tail = _stderr_tail(template.stderr_path)
                msg = (
                    f"Sandbox forkserver template exited during startup "
                    f"(code {template.process.returncode}): {stderr_tail}"
                )
                raise ForkserverStartupError(msg)
            if time.monotonic() >= deadline:
                # Startup outlasting one request's budget is not a template
                # failure: keep it importing so a later request finds it warm,
                # and report the same timeout spawn-per-call would have hit.
                logger.info("sandbox_forkserver_template_still_importing", python_executable=key)
                raise ForkserverTimeoutError
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(1.0)
                    probe.connect(template.socket_path)
            except OSError:
                time.sleep(_READY_PROBE_INTERVAL_SECONDS)
            else:
                template.ready = True
                timing_path = Path(template.socket_path).with_suffix(".timing.json")
                with suppress(OSError, ValueError):
                    preload_timing = json.loads(timing_path.read_text(encoding="utf-8"))
                    if isinstance(preload_timing, dict):
                        logger.debug(
                            "sandbox_forkserver_template_preload_timing",
                            python_executable=key,
                            **preload_timing,
                        )
                logger.info(
                    "sandbox_forkserver_template_ready",
                    python_executable=key,
                    template_pid=template.process.pid,
                    startup_ms=round((time.monotonic() - template.spawned_at) * 1000, 1),
                )
                return

    def _send_request(
        self,
        conn: socket.socket,
        key: str,
        template: _Template,
        request_payload: bytes,
        bind_stop: Callable[[Callable[[], None]], None],
        child: _RequestChild,
    ) -> None:
        stopped = threading.Event()

        def stop() -> None:
            # Killing reaches a tool that holds the GIL, which the child's own hang-up
            # watcher cannot; the hang-up still ends a child whose PID is not known yet.
            stopped.set()
            child.kill()
            _hang_up(conn)

        try:
            conn.connect(template.socket_path)
            # Armed before the envelope leaves: a stop accepted while the template warmed
            # up hangs up at once, so sending fails and the request never starts.
            bind_stop(stop)
            conn.sendall(request_payload)
        except TimeoutError as exc:
            raise ForkserverTimeoutError from exc
        except OSError as exc:
            if stopped.is_set():
                raise ForkserverError(_STOPPED_BEFORE_DISPATCH) from exc
            self._discard(key, template)
            msg = f"Failed to reach the sandbox forkserver template: {exc}"
            raise ForkserverError(msg) from exc

    def _request(
        self,
        key: str,
        template: _Template,
        *,
        request_env: dict[str, str] | None,
        request_cwd: str | None,
        envelope: str,
        deadline: float | None,
        bind_stop: Callable[[Callable[[], None]], None],
        on_progress: Callable[[str], None],
    ) -> subprocess.CompletedProcess[str]:
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            # Never deliver a request the caller has no budget left for; a
            # delivered request forks a child the parent may not learn about.
            raise ForkserverTimeoutError
        request_payload = (
            json.dumps(
                {"env": request_env, "cwd": request_cwd, "envelope": envelope, "timeout_seconds": remaining},
            ).encode("utf-8")
            + b"\n"
        )
        try:
            conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        except OSError as exc:
            msg = f"Failed to open a sandbox forkserver connection: {exc}"
            raise ForkserverError(msg) from exc
        child = _RequestChild()
        try:
            # Handing a job's request over still has a bound; only waiting for its response does not.
            conn.settimeout(remaining if remaining is not None else _CHILD_REQUEST_READ_TIMEOUT_SECONDS)
            self._send_request(conn, key, template, request_payload, bind_stop, child)
            reader = _SocketLineReader(conn, deadline)
            try:
                child.started(int(json.loads(reader.read_line())["pid"]))
                while "progress" in (response := json.loads(reader.read_line())):
                    on_progress(str(response["progress"]))
                returncode = int(response["returncode"])
                stdout_text = str(response["stdout"])
                stderr_text = str(response["stderr"])
            except TimeoutError as exc:
                # At its deadline the child is still running, maybe holding the GIL in a
                # long C call that a hang-up cannot interrupt, so kill it as before.
                child.kill()
                raise ForkserverTimeoutError from exc
            except (_ConnectionClosedError, OSError, ValueError, KeyError, TypeError) as exc:
                if template.process.poll() is not None:
                    self._discard(key, template)
                msg = "Sandbox forkserver child exited without returning a response."
                raise ForkserverError(msg) from exc
        finally:
            child.end()
            conn.close()
        return subprocess.CompletedProcess(
            args=["sandbox-forkserver", key],
            returncode=returncode,
            stdout=stdout_text,
            stderr=stderr_text,
        )


_STOPPED_BEFORE_DISPATCH = "Sandbox forkserver request was stopped before it was sent."


def _hang_up(conn: socket.socket) -> None:
    """Stop one request; after the request ended its socket is closed and this does nothing."""
    with suppress(OSError):
        conn.shutdown(socket.SHUT_RDWR)


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


def _handle_control_preload(
    ctl_conn: socket.socket,
    preload_plugins: Callable[[Mapping[str, object]], None],
) -> bool:
    """Run one in-place plugin preload in the template parent; return False if template must exit."""
    try:
        reader = _SocketLineReader(ctl_conn, time.monotonic() + _CHILD_REQUEST_READ_TIMEOUT_SECONDS)
        spec = json.loads(reader.read_line())
        if not isinstance(spec, dict):
            _send_json(ctl_conn, {"ok": False, "error": "Invalid preload spec."})
            return True
        preload_plugins(spec)
        if (thread_count := threading.active_count()) > 1:
            msg = (
                f"Sandbox forkserver template must be single-threaded before forking; "
                f"found {thread_count} threads after plugin preload."
            )
            _send_json(ctl_conn, {"ok": False, "error": msg})
            return False
        _send_json(ctl_conn, {"ok": True})
    except BaseException as exc:
        with suppress(Exception):
            _send_json(ctl_conn, {"ok": False, "error": str(exc)})
        return False
    else:
        return True


def _accept_and_fork(
    server: socket.socket,
    control_server: socket.socket | None,
    run_payload: Callable[[str, Callable[[str], None]], tuple[int, str, str]],
) -> None:
    conn, _addr = server.accept()
    try:
        child_pid = os.fork()
    except OSError:
        # Fork pressure (e.g. EAGAIN) fails this one request via EOF on
        # the connection; the template keeps serving.
        conn.close()
        return
    if child_pid == 0:
        server.close()
        if control_server is not None:
            control_server.close()
        _child_main(conn, run_payload)
    conn.close()


def serve_template(
    socket_path: str,
    run_payload: Callable[[str, Callable[[str], None]], tuple[int, str, str]],
    *,
    preload_plugins: Callable[[Mapping[str, object]], None] | None = None,
) -> int:
    """Template process main loop: accept one connection per request and fork.

    ``run_payload`` executes one envelope and receives the function that sends its progress to the runner.

    Runs inside ``python -m mindroom.api.sandbox_runner --sandbox-forkserver-template``
    after the heavy runtime import has completed. Must stay fork-safe: no
    threads, event loops, or network clients before forking.
    """
    if (thread_count := threading.active_count()) > 1:
        msg = (
            f"Sandbox forkserver template must be single-threaded before forking; "
            f"found {thread_count} threads after the runtime import."
        )
        raise RuntimeError(msg)
    parent_pid = os.getppid()
    # Auto-reap forked request children; each child restores SIG_DFL for its
    # own tool subprocesses immediately after fork.
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    control_server: socket.socket | None = None
    try:
        if preload_plugins is not None:
            control_server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            control_server.bind(_control_socket_path(socket_path))
            control_server.listen(4)
        server.bind(socket_path)
        server.listen(_TEMPLATE_LISTEN_BACKLOG)
        watch = [server] if control_server is None else [server, control_server]
        while True:
            readable, _, _ = select.select(watch, [], [], _TEMPLATE_ACCEPT_POLL_SECONDS)
            if not readable:
                if os.getppid() != parent_pid:
                    # Orphaned: the runner died without running its cleanup
                    # (e.g. SIGKILL), so remove our runtime dir ourselves.
                    shutil.rmtree(Path(socket_path).parent, ignore_errors=True)
                    return 0
                continue
            if control_server is not None and control_server in readable:
                ctl_conn, _addr = control_server.accept()
                try:
                    keep_serving = _handle_control_preload(ctl_conn, preload_plugins)
                finally:
                    ctl_conn.close()
                if not keep_serving:
                    return 1
            if server in readable:
                _accept_and_fork(server, control_server, run_payload)
    finally:
        server.close()
        if control_server is not None:
            control_server.close()


def _child_main(
    conn: socket.socket,
    run_payload: Callable[[str, Callable[[str], None]], tuple[int, str, str]],
) -> NoReturn:
    """Fork-child request handler; never returns to the template loop."""
    # Lead a process group, outside the template's, so stopping the request stops what its tool started.
    os.setsid()
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
    except BaseException:
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
    run_payload: Callable[[str, Callable[[str], None]], tuple[int, str, str]],
) -> int:
    if request.timeout_seconds is not None:
        # Self-destruct backstop for the rare case where the runner never learned
        # this child's pid; the grace keeps the runner-side SIGKILL primary.
        signal.alarm(max(1, math.ceil(request.timeout_seconds + _CHILD_TIMEOUT_GRACE_SECONDS)))
    if request.env is not None:
        os.environ.clear()
        os.environ.update(request.env)
    if request.cwd is not None:
        os.chdir(request.cwd)
    if select.select([conn], [], [], 0)[0]:
        # The runner hung up while handing this request over; never start it.
        return 1
    answered = threading.Event()
    threading.Thread(target=_exit_when_runner_hangs_up, args=(conn, answered), daemon=True).start()
    # Tool threads report progress too; one lock keeps every line whole and the response last.
    lines = threading.Lock()

    def report(text: str) -> None:
        with lines, suppress(OSError):
            _send_json(conn, {"progress": text})

    returncode, stdout_text, stderr_text = run_payload(request.envelope, report)
    with lines:
        answered.set()
        conn.settimeout(_CHILD_RESPONSE_WRITE_TIMEOUT_SECONDS)
        _send_json(conn, {"returncode": returncode, "stdout": stdout_text, "stderr": stderr_text})
    return 0


def _exit_when_runner_hangs_up(conn: socket.socket, answered: threading.Event) -> None:
    """Exit when the runner stops this request; it sends nothing after the request line, so readable means hung up.

    A stopped request also kills what its tool started; a hang-up after the response keeps what the tool left running.
    """
    select.select([conn], [], [])
    if not answered.is_set():
        with suppress(OSError):
            os.killpg(0, signal.SIGKILL)
    os._exit(1)


def _send_json(conn: socket.socket, payload: dict[str, object]) -> None:
    conn.sendall(json.dumps(payload).encode("utf-8") + b"\n")
