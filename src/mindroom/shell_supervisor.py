"""Worker-local shell supervisor process for background shell handles.

The sandbox runner isolates tool calls in per-request subprocesses (spawn or
forkserver children), which cannot own background shell handles: the handle
registry and the ``check``/``kill`` follow-ups arrive in different processes.
This module keeps one small long-lived *supervisor* process per runner that
owns the shell child processes and the in-memory handle registry, so shell
requests get the same subprocess isolation as every other execution tool.

Flow:

- The runner spawns ``python -m mindroom.shell_supervisor <socket>`` once and
  advertises the socket to shell tool subprocesses via
  ``MINDROOM_SANDBOX_SHELL_SUPERVISOR_SOCKET`` (the ``MINDROOM_SANDBOX_``
  prefix keeps it out of shell env passthrough).
- The shell toolkit computes argv, env, and cwd exactly as for local
  execution, then sends the run/check/kill request over the unix socket.
- The supervisor spawns and monitors the command with the shared
  :mod:`mindroom.shell_execution` core. A client that disconnects mid-run
  cancels the run, mirroring local cancellation semantics.
- The supervisor exits when its parent runner dies or terminates it, killing
  unfinished process groups, so restarting the worker invalidates handles.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from mindroom.logging_config import get_logger
from mindroom.shell_execution import (
    ProcessRecord,
    check_command,
    discard_background_record,
    kill_all_records,
    kill_command,
    run_command,
)

logger = get_logger(__name__)

SHELL_SUPERVISOR_SOCKET_ENV = "MINDROOM_SANDBOX_SHELL_SUPERVISOR_SOCKET"

_REQUEST_LIMIT_BYTES = 4 * 1024 * 1024
_PARENT_POLL_INTERVAL_SECONDS = 1.0
_READY_PROBE_INTERVAL_SECONDS = 0.05
_STARTUP_TIMEOUT_SECONDS = 30.0
_SUPERVISOR_TERMINATE_WAIT_SECONDS = 5.0
_SYNC_REQUEST_TIMEOUT_SECONDS = 10.0
_RUN_RESPONSE_GRACE_SECONDS = 30.0
_STDERR_TAIL_BYTES = 4096


class ShellSupervisorStartupError(RuntimeError):
    """The shell supervisor process failed to start or become ready."""


# ---------------------------------------------------------------------------
# Server (runs inside `python -m mindroom.shell_supervisor <socket>`)
# ---------------------------------------------------------------------------


async def _handle_run(
    registry: dict[str, ProcessRecord],
    payload: dict[str, object],
    reader: asyncio.StreamReader,
) -> str | None:
    """Run one command, cancelling it if the client disconnects mid-wait."""
    argv_payload = payload["argv"]
    env_payload = payload["env"]
    if not isinstance(argv_payload, list) or not isinstance(env_payload, dict):
        msg = "run request requires an 'argv' list and an 'env' object"
        raise TypeError(msg)
    run_task = asyncio.create_task(
        run_command(
            registry,
            namespace=str(payload["namespace"]),
            argv=[str(item) for item in argv_payload],
            env={str(key): str(value) for key, value in env_payload.items()},
            cwd=str(payload["cwd"]) if payload.get("cwd") is not None else None,
            tail=int(payload["tail"]),  # ty: ignore[invalid-argument-type]
            timeout=float(payload["timeout"]),  # ty: ignore[invalid-argument-type]
        ),
    )
    # EOF before the run response means the client (a per-request tool
    # subprocess) died; cancel the run so the command is killed exactly like a
    # cancelled local run instead of lingering with an undelivered handle.
    eof_task = asyncio.create_task(reader.read())
    done, _pending = await asyncio.wait({run_task, eof_task}, return_when=asyncio.FIRST_COMPLETED)
    if eof_task in done:
        # The client is gone even if the run finished in the same loop cycle:
        # a handle registered by that run is undeliverable, so discard it
        # instead of leaving the command running with no owner.
        if run_task.done():
            result = await run_task
            if result.handle is not None:
                discard_background_record(registry, result.handle)
        else:
            run_task.cancel()
            with suppress(asyncio.CancelledError):
                await run_task
        return None
    eof_task.cancel()
    with suppress(asyncio.CancelledError):
        await eof_task
    return (await run_task).message


async def _handle_connection(
    registry: dict[str, ProcessRecord],
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    try:
        line = await reader.readline()
        if not line.strip():
            return  # Ready-probe connections close without sending a request.
        payload = json.loads(line)
        op = payload.get("op")
        if op == "run":
            message = await _handle_run(registry, payload, reader)
            if message is None:
                return
        elif op == "check":
            message = check_command(registry, namespace=str(payload["namespace"]), handle=str(payload["handle"]))
        elif op == "kill":
            message = kill_command(
                registry,
                namespace=str(payload["namespace"]),
                handle=str(payload["handle"]),
                force=bool(payload.get("force", False)),
            )
        else:
            message = f"Error: Unknown shell supervisor operation '{op}'."
        writer.write(json.dumps({"message": message}).encode("utf-8") + b"\n")
        await writer.drain()
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        with suppress(OSError):
            writer.write(json.dumps({"message": f"Error: Invalid shell supervisor request: {exc}"}).encode() + b"\n")
            await writer.drain()
    except OSError:
        logger.warning("shell_supervisor_connection_failed", exc_info=True)
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


async def _serve(socket_path: str) -> int:
    registry: dict[str, ProcessRecord] = {}
    parent_pid = os.getppid()
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stop_event.set)
    server = await asyncio.start_unix_server(
        partial(_handle_connection, registry),
        path=socket_path,
        limit=_REQUEST_LIMIT_BYTES,
    )
    orphaned = False
    async with server:
        while not stop_event.is_set():
            if os.getppid() != parent_pid:
                orphaned = True
                break
            with suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=_PARENT_POLL_INTERVAL_SECONDS)
    # Handles cannot outlive the supervisor; kill their process groups so a
    # runner or worker restart invalidates handles without leaking processes.
    kill_all_records(registry)
    if orphaned:
        # The runner died without running its cleanup (e.g. SIGKILL), so
        # remove our runtime dir ourselves.
        shutil.rmtree(Path(socket_path).parent, ignore_errors=True)
    return 0


def _main(argv: list[str]) -> int:
    """Supervisor process entry point: serve shell requests on a unix socket."""
    return asyncio.run(_serve(argv[1]))


# ---------------------------------------------------------------------------
# Clients (run inside per-request tool subprocesses)
# ---------------------------------------------------------------------------


async def run_command_via_supervisor(
    socket_path: str,
    *,
    namespace: str,
    argv: list[str],
    env: dict[str, str],
    cwd: str | None,
    tail: int,
    timeout: float,  # noqa: ASYNC109
) -> str:
    """Run one shell command through the supervisor and return its message."""
    request = {
        "op": "run",
        "namespace": namespace,
        "argv": argv,
        "env": env,
        "cwd": cwd,
        "tail": tail,
        "timeout": timeout,
    }
    try:
        reader, writer = await asyncio.open_unix_connection(socket_path, limit=_REQUEST_LIMIT_BYTES)
    except OSError as exc:
        return f"Error: Shell supervisor is unavailable: {exc}"
    try:
        writer.write(json.dumps(request).encode("utf-8") + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=timeout + _RUN_RESPONSE_GRACE_SECONDS)
    except TimeoutError:
        return "Error: Shell supervisor did not respond in time."
    except OSError as exc:
        return f"Error: Shell supervisor request failed: {exc}"
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()
    return _parse_supervisor_response(line)


def check_command_via_supervisor(socket_path: str, *, namespace: str, handle: str) -> str:
    """Poll a background handle through the supervisor."""
    return _sync_supervisor_request(socket_path, {"op": "check", "namespace": namespace, "handle": handle})


def kill_command_via_supervisor(socket_path: str, *, namespace: str, handle: str, force: bool = False) -> str:
    """Kill a background handle through the supervisor."""
    return _sync_supervisor_request(
        socket_path,
        {"op": "kill", "namespace": namespace, "handle": handle, "force": force},
    )


def _sync_supervisor_request(socket_path: str, request: dict[str, object]) -> str:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(_SYNC_REQUEST_TIMEOUT_SECONDS)
            conn.connect(socket_path)
            conn.sendall(json.dumps(request).encode("utf-8") + b"\n")
            line = _recv_line(conn)
    except OSError as exc:
        return f"Error: Shell supervisor is unavailable: {exc}"
    return _parse_supervisor_response(line)


def _recv_line(conn: socket.socket) -> bytes:
    buffer = bytearray()
    while b"\n" not in buffer:
        if len(buffer) >= _REQUEST_LIMIT_BYTES:
            break
        chunk = conn.recv(65536)
        if not chunk:
            break
        buffer.extend(chunk)
    return bytes(buffer)


def _parse_supervisor_response(line: bytes) -> str:
    if not line.strip():
        return "Error: Shell supervisor closed the connection unexpectedly."
    try:
        payload = json.loads(line)
        return str(payload["message"])
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        return f"Error: Invalid shell supervisor response: {exc}"


# ---------------------------------------------------------------------------
# Manager (runs inside the long-lived sandbox runner process)
# ---------------------------------------------------------------------------


@dataclass
class _SupervisorProcess:
    process: subprocess.Popen[bytes]
    socket_path: str
    stderr_path: Path
    runtime_dir: Path


def _stderr_tail(stderr_path: Path) -> str:
    try:
        raw = stderr_path.read_bytes()
    except OSError:
        return ""
    return raw[-_STDERR_TAIL_BYTES:].decode("utf-8", errors="replace").strip()


def _terminate_supervisor(supervisor: _SupervisorProcess) -> None:
    process = supervisor.process
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=_SUPERVISOR_TERMINATE_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    shutil.rmtree(supervisor.runtime_dir, ignore_errors=True)


class _ShellSupervisorManager:
    """Owns the one shell supervisor process for this runner instance."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._supervisor: _SupervisorProcess | None = None

    def ensure(self) -> str:
        """Return the socket path of a live supervisor, spawning one if needed."""
        with self._lock:
            supervisor = self._supervisor
            if supervisor is not None and supervisor.process.poll() is not None:
                logger.warning("shell_supervisor_died", stderr_tail=_stderr_tail(supervisor.stderr_path))
                shutil.rmtree(supervisor.runtime_dir, ignore_errors=True)
                self._supervisor = None
                supervisor = None
            if supervisor is None:
                supervisor = self._spawn()
                try:
                    _wait_supervisor_ready(supervisor)
                except ShellSupervisorStartupError:
                    _terminate_supervisor(supervisor)
                    raise
                self._supervisor = supervisor
            return supervisor.socket_path

    def shutdown(self) -> None:
        """Terminate the supervisor process and clear cached state."""
        with self._lock:
            supervisor = self._supervisor
            self._supervisor = None
        if supervisor is not None:
            _terminate_supervisor(supervisor)

    def _spawn(self) -> _SupervisorProcess:
        runtime_dir = Path(tempfile.mkdtemp(prefix="mindroom-shell-"))
        socket_path = str(runtime_dir / "supervisor.sock")
        stderr_path = runtime_dir / "supervisor.err"
        logger.info("shell_supervisor_spawning", socket_path=socket_path)
        try:
            with stderr_path.open("wb") as stderr_file:
                process = subprocess.Popen(
                    [sys.executable, "-m", "mindroom.shell_supervisor", socket_path],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr_file,
                )
        except OSError as exc:
            shutil.rmtree(runtime_dir, ignore_errors=True)
            msg = f"Failed to start the shell supervisor: {exc}"
            raise ShellSupervisorStartupError(msg) from exc
        return _SupervisorProcess(
            process=process,
            socket_path=socket_path,
            stderr_path=stderr_path,
            runtime_dir=runtime_dir,
        )


def _wait_supervisor_ready(supervisor: _SupervisorProcess) -> None:
    deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
    while True:
        if supervisor.process.poll() is not None:
            msg = (
                f"Shell supervisor exited during startup "
                f"(code {supervisor.process.returncode}): {_stderr_tail(supervisor.stderr_path)}"
            )
            raise ShellSupervisorStartupError(msg)
        if time.monotonic() >= deadline:
            msg = "Shell supervisor did not become ready in time."
            raise ShellSupervisorStartupError(msg)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(1.0)
                probe.connect(supervisor.socket_path)
        except OSError:
            time.sleep(_READY_PROBE_INTERVAL_SECONDS)
        else:
            logger.info("shell_supervisor_ready", supervisor_pid=supervisor.process.pid)
            return


_manager: _ShellSupervisorManager | None = None
_manager_lock = threading.Lock()


def ensure_shell_supervisor() -> str:
    """Return the runner-wide shell supervisor socket, spawning it on first use."""
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = _ShellSupervisorManager()
            atexit.register(_manager.shutdown)
        manager = _manager
    return manager.ensure()


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
