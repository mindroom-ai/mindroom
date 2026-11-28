"""Worker-local shell supervisor process for shell commands and background script runs.

The sandbox runner isolates tool calls in per-request subprocesses, which can neither own
background handles whose ``check``/``kill`` follow-ups arrive in different processes nor
guarantee that a command dies with them. One long-lived ``python -m mindroom.shell.supervisor``
process owns command processes and the in-memory handle registry via :mod:`mindroom.shell.runner`
and :mod:`mindroom.shell.registry`. Client disconnect cancels the run; parent death kills all
unfinished process groups.
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
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from mindroom.logging_config import get_logger
from mindroom.shell.background_log import BackgroundLogPolicy
from mindroom.shell.output_capture import ShellOutputDestination
from mindroom.shell.registry import ProcessRegistry, ProcessStatus, SignalResult
from mindroom.shell.runner import ShellRunResult, run_foreground

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

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
_VALID_STATUS_STATES = frozenset({"running", "exited", "unknown", "error"})
_VALID_SIGNAL_OUTCOMES = frozenset({"signalled", "finished", "unknown", "error"})


class ShellSupervisorStartupError(RuntimeError):
    """The shell supervisor process failed to start or become ready."""


@dataclass(frozen=True, slots=True)
class _ShellDispatchContext:
    socket_path: str
    process_environ: Mapping[str, str] | None


_SHELL_DISPATCH_CONTEXT: ContextVar[_ShellDispatchContext | None] = ContextVar("shell_dispatch_context", default=None)


def current_shell_dispatch_context() -> _ShellDispatchContext | None:
    """Return the active request-local shell dispatch context, if any."""
    return _SHELL_DISPATCH_CONTEXT.get()


@contextmanager
def bind_shell_dispatch_context(*, socket_path: str, process_environ: Mapping[str, str] | None) -> Iterator[None]:
    """Bind a request-local supervisor socket and process environment for direct shell dispatch."""
    token = _SHELL_DISPATCH_CONTEXT.set(_ShellDispatchContext(socket_path=socket_path, process_environ=process_environ))
    try:
        yield
    finally:
        _SHELL_DISPATCH_CONTEXT.reset(token)


def background_script_supervision_supported() -> bool:
    """Return whether hard supervisor death also kills its script process group."""
    return sys.platform.startswith("linux")


# ---------------------------------------------------------------------------
# Server (runs inside `python -m mindroom.shell.supervisor <socket>`)
# ---------------------------------------------------------------------------


def _parse_max_runtime_seconds(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 < value <= sys.float_info.max:
        msg = "run request 'max_runtime_seconds' must be a positive finite number"
        raise TypeError(msg)
    return float(value)


def _parse_launch_payload(payload: dict[str, object]) -> tuple[dict[str, Any], str | None, bool | None]:
    argv_payload, env_payload = payload["argv"], payload["env"]
    if not isinstance(argv_payload, list) or not isinstance(env_payload, dict):
        msg = "run request requires an 'argv' list and an 'env' object"
        raise TypeError(msg)
    handle = payload.get("handle")
    if handle is not None and not isinstance(handle, str):
        msg = "run request 'handle' must be a string"
        raise TypeError(msg)
    kill_group = payload.get("kill_group_after_exit")
    if kill_group is not None and not isinstance(kill_group, bool):
        msg = "run request 'kill_group_after_exit' must be a boolean"
        raise TypeError(msg)
    argv = [str(item) for item in argv_payload]
    if handle is not None and background_script_supervision_supported():
        argv = [sys.executable, "-P", "-s", "-m", "mindroom.shell.parent_death_exec", str(os.getpid()), *argv]
    common: dict[str, Any] = {
        "argv": argv,
        "env": {str(k): str(v) for k, v in env_payload.items()},
        "cwd": str(payload["cwd"]) if payload.get("cwd") is not None else None,
        "tail": int(payload["tail"]),  # ty: ignore[invalid-argument-type]
        "output_destination": ShellOutputDestination.from_payload(payload.get("output_destination")),
        "launch_failure_exits": payload.get("launch_failure_exits") is True,
    }
    return common, handle, kill_group


async def _handle_launch(
    registry: ProcessRegistry,
    payload: dict[str, object],
    reader: asyncio.StreamReader,
    *,
    immediate: bool,
    write_progress: Callable[[str], None] | None = None,
) -> ShellRunResult | None:
    """Run or background-spawn one command, cancelling/rolling it back if the client disconnects."""
    common, handle, kill_group_payload = _parse_launch_payload(payload)
    background_log = BackgroundLogPolicy.from_payload(payload.get("background_log"))
    max_runtime_seconds = _parse_max_runtime_seconds(payload.get("max_runtime_seconds"))
    kill_group_after_exit = immediate if kill_group_payload is None else kill_group_payload

    bg_kwargs: dict[str, Any] = {
        **common,
        "namespace": str(payload["namespace"]),
        "handle": handle,
        "kill_group_after_exit": kill_group_after_exit,
        "max_runtime_seconds": max_runtime_seconds,
        "background_log": background_log,
    }
    if immediate:
        coro = registry.spawn_background(**bg_kwargs)
    else:
        timeout_payload = payload["timeout"]
        response_timeout = None if timeout_payload is None else float(timeout_payload)  # ty: ignore[invalid-argument-type]
        if max_runtime_seconds is not None:
            response_timeout = (
                max_runtime_seconds if response_timeout is None else min(response_timeout, max_runtime_seconds)
            )
        on_timeout = payload.get("on_timeout", "background")
        if on_timeout not in ("background", "kill"):
            msg = "run request 'on_timeout' must be 'background' or 'kill'"
            raise TypeError(msg)
        on_progress = write_progress if payload.get("progress") is True else None
        if on_timeout == "kill":
            coro = run_foreground(
                timeout=response_timeout,
                on_progress=on_progress,
                kill_group_after_exit=kill_group_after_exit,
                **common,
            )
        else:
            coro = registry.run_or_background(timeout=response_timeout, on_progress=on_progress, **bg_kwargs)

    run_task, eof_task = asyncio.create_task(coro), asyncio.create_task(reader.read())
    # EOF before the response means the client died; cancel the run (or discard an undeliverable handle).
    done, pending = await asyncio.wait({run_task, eof_task}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    if eof_task in done:
        if run_task.done() and not run_task.cancelled() and (handle := (await run_task).handle) is not None:
            registry.discard(handle)
        return None
    return await run_task


def _write_progress(writer: asyncio.StreamWriter, text: str) -> None:
    writer.write(json.dumps({"progress": text}).encode("utf-8") + b"\n")


async def _dispatch_request(
    registry: ProcessRegistry,
    payload: dict[str, object],
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> ShellRunResult | ProcessStatus | SignalResult | None:
    op = payload.get("op")
    if op == "run":
        return await _handle_launch(
            registry,
            payload,
            reader,
            immediate=False,
            write_progress=partial(_write_progress, writer),
        )
    if op == "spawn_background":
        return await _handle_launch(registry, payload, reader, immediate=True)
    if op == "check":
        return registry.status(namespace=str(payload["namespace"]), handle=str(payload["handle"]))
    if op == "kill":
        return registry.signal(
            namespace=str(payload["namespace"]),
            handle=str(payload["handle"]),
            force=bool(payload.get("force", False)),
        )
    if op == "kill_namespace":
        killed = await registry.close_namespace(str(payload["namespace"]))
        return ShellRunResult(message=f"Killed {killed}.")
    return ShellRunResult(message=f"Error: Unknown shell supervisor operation '{op}'.", execution_error=True)


async def _handle_connection(
    registry: ProcessRegistry,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    try:
        line = await reader.readline()
        if not line.strip():
            return  # Ready-probe connections close without sending a request.
        result = await _dispatch_request(registry, json.loads(line), reader, writer)
        if result is None:
            return
        writer.write(json.dumps(asdict(result)).encode("utf-8") + b"\n")
        await writer.drain()
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        with suppress(OSError):
            err = ShellRunResult(f"Error: Invalid shell supervisor request: {exc}", execution_error=True)
            writer.write(json.dumps(asdict(err)).encode() + b"\n")
            await writer.drain()
    except OSError:
        logger.warning("shell_supervisor_connection_failed", exc_info=True)
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


async def _serve(socket_path: str) -> int:
    registry = ProcessRegistry()
    parent_pid = os.getppid()
    stop_event = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop_event.set)
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
    registry.kill_all()
    if orphaned:
        shutil.rmtree(Path(socket_path).parent, ignore_errors=True)
    return 0


def _main(argv: list[str]) -> int:
    """Supervisor process entry point: serve shell requests on a unix socket."""
    return asyncio.run(_serve(argv[1]))


# ---------------------------------------------------------------------------
# Clients (run inside per-request tool subprocesses)
# ---------------------------------------------------------------------------


async def _send_async_request(
    socket_path: str,
    request: dict[str, object],
    *,
    deadline: float | None,
    on_progress: Callable[[str], None] | None = None,
    post_send_output_handled: bool = False,
) -> ShellRunResult:
    try:
        reader, writer = await asyncio.open_unix_connection(socket_path, limit=_REQUEST_LIMIT_BYTES)
    except OSError as exc:
        return ShellRunResult(message=f"Error: Shell supervisor is unavailable: {exc}", execution_error=True)
    loop = asyncio.get_running_loop()
    try:
        writer.write(json.dumps(request).encode("utf-8") + b"\n")
        await writer.drain()
        while True:
            line = await asyncio.wait_for(
                reader.readline(),
                timeout=None if deadline is None else deadline - loop.time(),
            )
            if on_progress is None or (text := _progress_text(line)) is None:
                break
            on_progress(text)
    except (TimeoutError, OSError, ValueError) as exc:
        msg = (
            "Error: Shell supervisor did not respond in time."
            if isinstance(exc, TimeoutError)
            else f"Error: Shell supervisor request failed: {exc}"
        )
        return ShellRunResult(msg, output_file_handled=post_send_output_handled, execution_error=True)
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()
    return _parse_supervisor_response(line, output_file_handled_on_error=post_send_output_handled)


def _launch_request(
    op: Literal["run", "spawn_background"],
    base: dict[str, object],
    optional: dict[str, object],
) -> dict[str, object]:
    return {"op": op, **base, **{key: value for key, value in optional.items() if value is not None}}


async def run_command_via_supervisor(
    socket_path: str,
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
    max_runtime_seconds: float | None = None,
    output_destination: ShellOutputDestination | None = None,
    background_log: BackgroundLogPolicy | None = None,
    kill_group_after_exit: bool = False,
    launch_failure_exits: bool = False,
) -> ShellRunResult:
    """Run one shell command through the supervisor, preserving execution and output-file ownership."""
    request = _launch_request(
        "run",
        {
            "namespace": namespace,
            "argv": argv,
            "env": env,
            "cwd": cwd,
            "tail": tail,
            "timeout": timeout,
            "on_timeout": on_timeout,
            "kill_group_after_exit": kill_group_after_exit,
            "launch_failure_exits": launch_failure_exits,
        },
        {
            "handle": handle,
            "max_runtime_seconds": max_runtime_seconds,
            "output_destination": asdict(output_destination) if output_destination is not None else None,
            "background_log": asdict(background_log) if background_log is not None else None,
            "progress": True if on_progress is not None else None,
        },
    )
    deadline = None if timeout is None else asyncio.get_running_loop().time() + timeout + _RUN_RESPONSE_GRACE_SECONDS
    return await _send_async_request(
        socket_path,
        request,
        deadline=deadline,
        on_progress=on_progress,
        post_send_output_handled=output_destination is not None,
    )


async def spawn_background_via_supervisor(
    socket_path: str,
    *,
    namespace: str,
    argv: list[str],
    env: dict[str, str],
    cwd: str | None,
    tail: int = 100,
    handle: str | None = None,
    max_runtime_seconds: float | None = None,
    background_log: BackgroundLogPolicy | None = None,
    kill_group_after_exit: bool = True,
) -> ShellRunResult:
    """Spawn one command directly into the supervisor's background registry without a foreground wait."""
    request = _launch_request(
        "spawn_background",
        {
            "namespace": namespace,
            "argv": argv,
            "env": env,
            "cwd": cwd,
            "tail": tail,
            "kill_group_after_exit": kill_group_after_exit,
        },
        {
            "handle": handle,
            "max_runtime_seconds": max_runtime_seconds,
            "background_log": asdict(background_log) if background_log is not None else None,
        },
    )
    deadline = asyncio.get_running_loop().time() + _RUN_RESPONSE_GRACE_SECONDS
    return await _send_async_request(socket_path, request, deadline=deadline)


def check_command_via_supervisor(socket_path: str, *, namespace: str, handle: str) -> ProcessStatus:
    """Poll a background handle through the supervisor and return its structured status."""
    try:
        return _parse_status_response(
            _sync_supervisor_exchange(socket_path, {"op": "check", "namespace": namespace, "handle": handle}),
        )
    except OSError as exc:
        return ProcessStatus("error", message=f"Error: Shell supervisor is unavailable: {exc}")


def kill_command_via_supervisor(socket_path: str, *, namespace: str, handle: str, force: bool = False) -> SignalResult:
    """Signal a background handle through the supervisor and return the structured outcome."""
    try:
        return _parse_signal_response(
            _sync_supervisor_exchange(
                socket_path,
                {"op": "kill", "namespace": namespace, "handle": handle, "force": force},
            ),
        )
    except OSError as exc:
        return SignalResult("error", f"Error: Shell supervisor is unavailable: {exc}")


def _kill_namespace_via_supervisor(socket_path: str, *, namespace: str) -> None:
    with suppress(OSError):
        _sync_supervisor_exchange(socket_path, {"op": "kill_namespace", "namespace": namespace})


def _progress_text(line: bytes) -> str | None:
    try:
        payload = json.loads(line)
    except ValueError:
        return None
    return str(payload["progress"]) if isinstance(payload, dict) and "progress" in payload else None


def _sync_supervisor_exchange(socket_path: str, request: dict[str, object]) -> bytes:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(_SYNC_REQUEST_TIMEOUT_SECONDS)
        conn.connect(socket_path)
        conn.sendall(json.dumps(request).encode("utf-8") + b"\n")
        buffer = bytearray()
        while b"\n" not in buffer and len(buffer) < _REQUEST_LIMIT_BYTES:
            if not (chunk := conn.recv(65536)):
                break
            buffer.extend(chunk)
        return bytes(buffer)


def _decode_json_object(line: bytes) -> tuple[dict[str, object] | None, str]:
    if not line.strip():
        return None, "Error: Shell supervisor closed the connection unexpectedly."
    try:
        payload = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"Error: Invalid shell supervisor response: {exc}"
    if not isinstance(payload, dict):
        return None, "Error: Invalid shell supervisor response: expected JSON object."
    return cast("dict[str, object]", payload), ""


def _parse_supervisor_response(line: bytes, *, output_file_handled_on_error: bool = False) -> ShellRunResult:
    def error(msg: str) -> ShellRunResult:
        return ShellRunResult(message=msg, output_file_handled=output_file_handled_on_error, execution_error=True)

    payload, decode_error = _decode_json_object(line)
    if payload is None:
        return error(decode_error)
    try:
        message, handle, handled = payload["message"], payload["handle"], payload["output_file_handled"]
    except KeyError as exc:
        return error(f"Error: Invalid shell supervisor response: {exc}")
    exec_err = payload.get("execution_error", False)
    rc = payload.get("return_code")
    timed_out = payload.get("timed_out", False)
    bg_rejected = payload.get("background_rejected", False)
    if (
        not isinstance(message, str)
        or (handle is not None and not isinstance(handle, str))
        or not isinstance(handled, bool)
        or not isinstance(exec_err, bool)
        or not isinstance(timed_out, bool)
        or not isinstance(bg_rejected, bool)
        or (rc is not None and (isinstance(rc, bool) or not isinstance(rc, int)))
    ):
        return error("Error: Invalid shell supervisor response: invalid result fields.")
    return ShellRunResult(message, handle, handled, exec_err, rc, timed_out, bg_rejected)


def _wire_error_message(message: object, fallback: str) -> str:
    return message if isinstance(message, str) and message.startswith("Error:") else fallback


def _parse_status_response(line: bytes) -> ProcessStatus:
    payload, decode_error = _decode_json_object(line)
    if payload is None:
        return ProcessStatus(state="error", message=decode_error)
    state, exit_code, message = payload.get("state"), payload.get("exit_code"), payload.get("message")
    valid_exit = (
        isinstance(exit_code, int) and not isinstance(exit_code, bool) if state == "exited" else exit_code is None
    )
    if isinstance(state, str) and state in _VALID_STATUS_STATES and isinstance(message, str) and valid_exit:
        return ProcessStatus(cast('Literal["running", "exited", "unknown", "error"]', state), exit_code, message)
    return ProcessStatus(
        "error",
        message=_wire_error_message(message, "Error: Invalid shell supervisor response: invalid status fields."),
    )


def _parse_signal_response(line: bytes) -> SignalResult:
    payload, decode_error = _decode_json_object(line)
    if payload is None:
        return SignalResult(outcome="error", message=decode_error)
    outcome, message = payload.get("outcome"), payload.get("message")
    if isinstance(outcome, str) and outcome in _VALID_SIGNAL_OUTCOMES and isinstance(message, str):
        return SignalResult(cast('Literal["signalled", "finished", "unknown", "error"]', outcome), message)
    return SignalResult(
        "error",
        _wire_error_message(message, "Error: Invalid shell supervisor response: invalid signal fields."),
    )


# ---------------------------------------------------------------------------
# Manager (runs inside the long-lived sandbox runner process)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _SupervisorProcess:
    process: subprocess.Popen[bytes]
    socket_path: str
    stderr_path: Path
    runtime_dir: Path

    def stderr_tail(self) -> str:
        try:
            return self.stderr_path.read_bytes()[-_STDERR_TAIL_BYTES:].decode("utf-8", errors="replace").strip()
        except OSError:
            return ""

    def terminate(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=_SUPERVISOR_TERMINATE_WAIT_SECONDS)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        shutil.rmtree(self.runtime_dir, ignore_errors=True)


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
                logger.warning("shell_supervisor_died", stderr_tail=supervisor.stderr_tail())
                shutil.rmtree(supervisor.runtime_dir, ignore_errors=True)
                self._supervisor = supervisor = None
            if supervisor is None:
                supervisor = self._spawn()
                try:
                    _wait_supervisor_ready(supervisor)
                except ShellSupervisorStartupError:
                    supervisor.terminate()
                    raise
                self._supervisor = supervisor
            return supervisor.socket_path

    def kill_namespace(self, namespace: str) -> None:
        """Kill every process in *namespace* if a supervisor is already running; never spawn one."""
        with self._lock:
            supervisor = self._supervisor
            socket_path = (
                supervisor.socket_path if supervisor is not None and supervisor.process.poll() is None else None
            )
        if socket_path is not None:
            _kill_namespace_via_supervisor(socket_path, namespace=namespace)

    def shutdown(self) -> None:
        """Terminate the supervisor process and clear cached state."""
        with self._lock:
            supervisor = self._supervisor
            self._supervisor = None
        if supervisor is not None:
            supervisor.terminate()

    def _spawn(self) -> _SupervisorProcess:
        runtime_dir = Path(tempfile.mkdtemp(prefix="mindroom-shell-"))
        socket_path = str(runtime_dir / "supervisor.sock")
        stderr_path = runtime_dir / "supervisor.err"
        logger.info("shell_supervisor_spawning", socket_path=socket_path)
        try:
            with stderr_path.open("wb") as stderr_file:
                process = subprocess.Popen(
                    [sys.executable, "-m", "mindroom.shell.supervisor", socket_path],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr_file,
                )
        except OSError as exc:
            shutil.rmtree(runtime_dir, ignore_errors=True)
            msg = f"Failed to start the shell supervisor: {exc}"
            raise ShellSupervisorStartupError(msg) from exc
        return _SupervisorProcess(process, socket_path, stderr_path, runtime_dir)


def _wait_supervisor_ready(supervisor: _SupervisorProcess) -> None:
    deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
    while True:
        if supervisor.process.poll() is not None:
            msg = f"Shell supervisor exited during startup (code {supervisor.process.returncode}): {supervisor.stderr_tail()}"
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


def kill_shell_supervisor_namespace(namespace: str) -> None:
    """Kill every process in *namespace* in the running supervisor without spawning one."""
    with _manager_lock:
        manager = _manager
    if manager is not None:
        manager.kill_namespace(namespace)


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
