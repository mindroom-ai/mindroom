"""Background shell process ownership: ProcessRegistry, BackgroundProcess, and structured status/signal results."""

from __future__ import annotations

import asyncio
import contextlib
import re
import signal
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, TypedDict, Unpack

from mindroom.shell.background_log import (
    BackgroundLogFile,
    BackgroundLogPolicy,
    StopReason,
    format_log_trailer,
    open_background_log,
    seed_background_log,
)
from mindroom.shell.output import ShellOutputBuffer, SpoolCapture, format_auto_background_message
from mindroom.shell.runner import POST_EXIT_READER_GRACE_SECONDS, ActiveCommand, ShellRunResult, cancel_pending_tasks

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.shell.output_capture import ShellOutputCapture, ShellOutputDestination, ShellSpillPolicy
    from mindroom.shell.process import ManagedProcess

_STALE_RECORD_SECONDS = 600  # 10 minutes
MAX_BACKGROUNDED = 16
_MAX_TOTAL_BACKGROUNDED = 128
_CALLER_HANDLE_RE = re.compile(r"shell:[0-9a-f]{32}")


@dataclass(frozen=True, slots=True)
class ProcessStatus:
    """Structured outcome of polling one backgrounded shell process."""

    state: Literal["running", "exited", "unknown", "error"]
    exit_code: int | None = None
    message: str = ""


@dataclass(frozen=True, slots=True)
class SignalResult:
    """Structured outcome of signalling one backgrounded shell process."""

    outcome: Literal["signalled", "finished", "unknown", "error"]
    message: str = ""


@dataclass
class BackgroundProcess:
    """Tracks one backgrounded shell process owned by a ``ProcessRegistry``."""

    namespace: str
    handle: str
    pid: int
    managed: ManagedProcess
    kill_group_after_exit: bool = False
    stdout_buf: ShellOutputBuffer = field(default_factory=ShellOutputBuffer)
    stderr_buf: ShellOutputBuffer = field(default_factory=ShellOutputBuffer)
    started_at: float = field(default_factory=time.monotonic)
    tail: int = 100
    finished: bool = False
    finished_at: float | None = None
    return_code: int | None = None
    output_capture: ShellOutputCapture | None = None
    spool: SpoolCapture | None = None
    max_runtime_seconds: float | None = None
    log_file: BackgroundLogFile | None = field(default=None, repr=False)
    stop_reason: StopReason | None = field(default=None, repr=False)
    monitor_task: asyncio.Task[None] | None = field(default=None, repr=False)

    @property
    def process(self) -> asyncio.subprocess.Process:
        """Return the underlying subprocess handle."""
        return self.managed.process

    @property
    def _log_path(self) -> str | None:
        return str(self.log_file.path) if self.log_file is not None else None

    @property
    def timed_out(self) -> bool:
        """Whether this process was stopped because it reached ``max_runtime_seconds``."""
        return self.stop_reason == "timeout"

    @property
    def _active_spool(self) -> SpoolCapture | None:
        return self.output_capture.spool if self.output_capture is not None else self.spool

    def signal(self, *, force: bool = False) -> bool:
        """Send SIGTERM, or SIGKILL with *force*, to this command's processes; report whether delivered."""
        if not self.managed.signal(signal.SIGKILL if force else signal.SIGTERM):
            return False
        if (active_spool := self._active_spool) is not None:
            active_spool.incomplete = True
        return True

    def _finish_log(self, *, with_trailer: bool = True) -> None:
        if self.log_file is not None and not self.log_file.closed:
            trailer = (
                format_log_trailer(
                    self.managed.returncode,
                    stop_reason=self.stop_reason,
                    max_runtime_seconds=self.max_runtime_seconds,
                )
                if with_trailer
                else None
            )
            self.log_file.finish(trailer)


def _adopted_message(
    record: BackgroundProcess,
    timeout: float | None,
    background_log: BackgroundLogPolicy | None,
    output_path_notice: str | None,
) -> str:
    if background_log is not None and record.log_file is not None:
        return format_auto_background_message(
            pid=record.pid,
            timeout=0.0 if timeout is None else timeout,
            log_path=record.log_file.path,
            display_root=background_log.display_root,
            max_runtime_seconds=record.max_runtime_seconds,
            no_output_path=output_path_notice,
            stdout_buf=record.stdout_buf,
            stderr_buf=record.stderr_buf,
            tail=record.tail,
        )
    prefix = "Started background process" if timeout is None else f"Command timed out after {timeout}s. Still running"
    return f"{prefix} (PID {record.pid}).\nHandle: {record.handle}"


def _kill_record(record: BackgroundProcess, *, stop_reason: StopReason | None = None) -> None:
    if record.output_capture is not None:
        record.output_capture.close()
    was_running = not record.finished
    if was_running:
        record.stop_reason = record.stop_reason or stop_reason
        record.managed.kill_tree()
        record.finished = True
        record.finished_at = time.monotonic()
        record.return_code = record.managed.returncode
    record._finish_log(with_trailer=stop_reason is not None and was_running)
    if record.monitor_task is not None:
        record.monitor_task.cancel()


class _LaunchOptions(TypedDict, total=False):
    handle: str | None
    output_destination: ShellOutputDestination | None
    spill_policy: ShellSpillPolicy | None
    spool: SpoolCapture | None
    stdin: int | None
    stderr: int
    launch_failure_exits: bool
    max_runtime_seconds: float | None
    background_log: BackgroundLogPolicy | None


class ProcessRegistry:
    """Owns backgrounded shell processes, handle reservations, limits, and lifecycle shutdown."""

    def __init__(self) -> None:
        self._records: dict[str, BackgroundProcess] = {}
        self._reservations: set[str] = set()
        self.closed: bool = False

    def get_process(self, namespace: str, handle: str) -> BackgroundProcess | None:
        """Return the tracked ``BackgroundProcess`` for *handle* if it belongs to *namespace*."""
        record = self._records.get(handle)
        return record if record is not None and record.namespace == namespace else None

    async def spawn_background(
        self,
        *,
        namespace: str,
        argv: list[str],
        env: dict[str, str],
        cwd: str | None,
        tail: int = 100,
        kill_group_after_exit: bool = True,
        **options: Unpack[_LaunchOptions],
    ) -> ShellRunResult:
        """Spawn a command directly into this registry without waiting in the foreground."""
        return await self._run_tracked(namespace, argv, env, cwd, tail, kill_group_after_exit, options, immediate=True)

    async def run_or_background(
        self,
        *,
        namespace: str,
        argv: list[str],
        env: dict[str, str],
        cwd: str | None,
        tail: int,
        timeout: float | None,  # noqa: ASYNC109
        on_progress: Callable[[str], None] | None = None,
        kill_group_after_exit: bool = False,
        **options: Unpack[_LaunchOptions],
    ) -> ShellRunResult:
        """Run one shell command in the foreground up to *timeout*, adopting it into this registry on timeout."""
        return await self._run_tracked(
            namespace,
            argv,
            env,
            cwd,
            tail,
            kill_group_after_exit,
            options,
            immediate=False,
            timeout=timeout,
            on_progress=on_progress,
        )

    async def _run_tracked(
        self,
        namespace: str,
        argv: list[str],
        env: dict[str, str],
        cwd: str | None,
        tail: int,
        kill_group_after_exit: bool,
        options: _LaunchOptions,
        *,
        immediate: bool,
        timeout: float | None = None,  # noqa: ASYNC109
        on_progress: Callable[[str], None] | None = None,
    ) -> ShellRunResult:
        self._sweep_stale_records()
        handle = options.get("handle")
        if (error := self._reserve_handle(handle)) is not None:
            return error
        output_destination = options.get("output_destination")
        started: ActiveCommand | ShellRunResult | None = None
        try:
            if immediate:
                candidate = handle or self._unused_handle()
                if (rejection := self._background_rejection(namespace=namespace, handle=candidate)) is not None:
                    return ShellRunResult(
                        rejection,
                        output_file_handled=output_destination is not None,
                        background_rejected=True,
                    )
            started = await ActiveCommand.start(
                argv=argv,
                env=env,
                cwd=cwd,
                tail=tail,
                output_destination=output_destination,
                spill_policy=options.get("spill_policy") if not immediate else None,
                spool=options.get("spool"),
                stdin=options.get("stdin"),
                stderr=options.get("stderr", asyncio.subprocess.PIPE),
                kill_group_after_exit=kill_group_after_exit,
                launch_failure_exits=options.get("launch_failure_exits", False),
            )
            if isinstance(started, ShellRunResult):
                return started
            if not immediate and not await started.wait_or_timeout(timeout=timeout, on_progress=on_progress):
                return await started.finish_result()
            return await self._adopt_background(
                started,
                namespace=namespace,
                requested_handle=handle,
                max_runtime_seconds=options.get("max_runtime_seconds"),
                background_log=options.get("background_log"),
                timeout=timeout,
            )
        finally:
            if isinstance(started, ActiveCommand):
                started.close_spill()
            if handle is not None:
                self._reservations.discard(handle)

    def status(self, namespace: str, handle: str) -> ProcessStatus:
        """Poll the status of a backgrounded shell command in *namespace*."""
        record = self.get_process(namespace, handle)
        if record is None:
            return ProcessStatus(state="unknown", message=f"Error: Unknown handle '{handle}'")
        ended_at = record.finished_at if record.finished and record.finished_at is not None else time.monotonic()
        elapsed = ended_at - record.started_at
        if record.finished:
            failed = record.return_code != 0
            output = record.stdout_buf.render(tail=record.tail)
            errors = record.stderr_buf.render(tail=None if failed else record.tail)
            result = f"Status: FINISHED (exit code {record.return_code}, ran for {elapsed:.1f}s)\n"
            if failed and record.stderr_buf:
                result += f"Stderr:\n{errors}\n"
            result += f"Output:\n{output}"
            if record.stderr_buf and not failed:
                result += f"\nStderr:\n{errors}"
            return ProcessStatus(state="exited", exit_code=record.return_code, message=result)
        message = (
            f"Status: RUNNING (PID {record.pid}, elapsed {elapsed:.1f}s)\n"
            f"Partial output ({len(record.stdout_buf)} lines buffered):\n{record.stdout_buf.render(tail=50)}"
        )
        return ProcessStatus(state="running", message=message)

    def signal(self, namespace: str, handle: str, *, force: bool = False) -> SignalResult:
        """Signal a backgrounded shell command in *namespace* with SIGTERM or SIGKILL."""
        record = self.get_process(namespace, handle)
        if record is None:
            return SignalResult(outcome="unknown", message=f"Error: Unknown handle '{handle}'")
        if record.finished:
            return SignalResult(
                outcome="finished",
                message=f"Process already finished (exit code {record.return_code})",
            )
        if not record.signal(force=force):
            return SignalResult(outcome="finished", message=f"Process {record.pid} already exited")
        action, sig_name = ("Force-killed", "SIGKILL") if force else ("Terminated", "SIGTERM")
        return SignalResult(outcome="signalled", message=f"{action} process {record.pid} ({sig_name} sent).")

    def discard(self, handle: str) -> None:
        """Kill and drop one background record whose handle is undeliverable or no longer needed."""
        if (record := self._records.pop(handle, None)) is not None:
            _kill_record(record, stop_reason="stopped")

    async def close_namespace(self, namespace: str) -> int:
        """Stop and remove all processes currently registered in *namespace*."""
        matching = [
            self._records.pop(handle) for handle, record in list(self._records.items()) if record.namespace == namespace
        ]
        await self._close_entries(matching)
        return len(matching)

    async def close(self) -> None:
        """Mark the registry closed, gracefully stop all unfinished records, and flush log trailers."""
        self.closed = True
        if not self._records:
            return
        entries = list(self._records.values())
        try:
            await self._close_entries(entries)
        finally:
            self._records.clear()

    def kill_all(self) -> None:
        """Synchronously force-kill every unfinished process tree in this registry and clear it."""
        for record in self._records.values():
            _kill_record(record, stop_reason="stopped")
        self._records.clear()

    def _reserve_handle(self, handle: str | None) -> ShellRunResult | None:
        if handle is None:
            return None
        if _CALLER_HANDLE_RE.fullmatch(handle) is None:
            return ShellRunResult(message="Error: Invalid caller-supplied shell handle.", execution_error=True)
        if handle in self._records or handle in self._reservations:
            return ShellRunResult(
                message=f"Error: Shell handle '{handle}' is already registered.",
                execution_error=True,
            )
        self._reservations.add(handle)
        return None

    def _unused_handle(self) -> str:
        while (handle := f"shell:{uuid.uuid4().hex[:8]}") in self._records or handle in self._reservations:
            pass
        return handle

    def _sweep_stale_records(self) -> None:
        now = time.monotonic()
        for handle in [
            h
            for h, r in self._records.items()
            if (
                r.finished
                and r.finished_at is not None
                and r.spool is None
                and (now - r.finished_at) > _STALE_RECORD_SECONDS
            )
        ]:
            self._records.pop(handle, None)

    def _background_rejection(self, *, namespace: str, handle: str) -> str | None:
        if self.closed:
            return "Error: Shell turn already ended."
        active = [record for record in self._records.values() if not record.finished]
        for count, limit in (
            (sum(1 for record in active if record.namespace == namespace), MAX_BACKGROUNDED),
            (len(active), _MAX_TOTAL_BACKGROUNDED),
        ):
            if count >= limit:
                return (
                    f"Error: Too many backgrounded processes ({count}/{limit}). "
                    "Kill or wait for existing ones before running more."
                )
        return f"Error: Shell handle '{handle}' is already registered." if handle in self._records else None

    def _open_adoption_log(self, active: ActiveCommand, handle: str, policy: BackgroundLogPolicy) -> BackgroundLogFile:
        active_paths = {
            r._log_path
            for r in self._records.values()
            if r._log_path is not None and (not r.finished or (r.log_file is not None and not r.log_file.closed))
        }
        log_file = open_background_log(policy, pid=active.managed.pid, handle=handle, active_paths=active_paths)
        try:
            seed_background_log(log_file, active.stdout_buf, active.stderr_buf)
        except OSError:
            log_file.finish()
            with contextlib.suppress(OSError):
                log_file.path.unlink()
            raise
        active.add_output_sink(log_file.write)
        return log_file

    async def _adopt_background(
        self,
        active: ActiveCommand,
        *,
        namespace: str,
        requested_handle: str | None,
        max_runtime_seconds: float | None,
        background_log: BackgroundLogPolicy | None,
        timeout: float | None,  # noqa: ASYNC109
    ) -> ShellRunResult:
        active.close_spill()
        had_capture = active.output_capture is not None
        handle = requested_handle or self._unused_handle()
        rejection = self._background_rejection(namespace=namespace, handle=handle)
        log_file: BackgroundLogFile | None = None
        record_capture, output_path_notice = active.output_capture, None
        if rejection is None and background_log is not None:
            try:
                log_file = self._open_adoption_log(active, handle, background_log)
            except OSError as exc:
                rejection = f"Error: Failed to open background shell log: {exc}"
            else:
                if active.output_capture is not None:
                    output_path_notice = active.output_capture.destination.path
                    active.output_capture.close()
                record_capture = None
        if rejection is not None:
            await active.discard_unregistered()
            return ShellRunResult(message=rejection, output_file_handled=had_capture, background_rejected=True)

        record = BackgroundProcess(
            namespace=namespace,
            handle=handle,
            pid=active.managed.pid,
            managed=active.managed,
            kill_group_after_exit=active.kill_group_after_exit,
            stdout_buf=active.stdout_buf,
            stderr_buf=active.stderr_buf,
            started_at=active.started_at,
            tail=active.tail,
            output_capture=record_capture,
            spool=active.spool,
            max_runtime_seconds=max_runtime_seconds,
            log_file=log_file,
        )
        monitor_task = asyncio.create_task(self._monitor_process(record, active.stdout_reader, active.stderr_reader))
        record.monitor_task = monitor_task
        self._records[handle] = record
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            self._records.pop(handle, None)
            monitor_task.cancel()
            try:
                await active.cancel_and_cleanup()
            finally:
                if log_file is not None:
                    log_file.finish()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor_task
            raise

        if self.closed or self._records.get(handle) is not record:
            self.discard(handle)
            if record.stop_reason is None:
                _kill_record(record, stop_reason="stopped")
            return ShellRunResult(
                message="Error: Shell turn already ended.",
                output_file_handled=had_capture,
                background_rejected=True,
            )

        message = _adopted_message(record, timeout, background_log, output_path_notice)
        return ShellRunResult(message=message, handle=handle, output_file_handled=had_capture)

    async def _close_entries(self, entries: list[BackgroundProcess]) -> None:
        if not entries:
            return
        try:
            to_stop = [r for r in entries if not r.finished and r.managed.returncode is None]
            for record in to_stop:
                record.stop_reason = record.stop_reason or "scope_ended"
            if to_stop:
                await asyncio.gather(
                    *(r.managed.terminate_tree(whole_group=True) for r in to_stop),
                    return_exceptions=True,
                )
            monitors = [r.monitor_task for r in entries if r.monitor_task is not None and not r.monitor_task.done()]
            if monitors:
                await asyncio.gather(*monitors, return_exceptions=True)
        finally:
            for record in entries:
                _kill_record(record, stop_reason="scope_ended")

    async def _monitor_process(
        self,
        record: BackgroundProcess,
        stdout_reader: asyncio.Task[None],
        stderr_reader: asyncio.Task[None],
    ) -> None:
        """Wait for a backgrounded process to exit (or hit max_runtime_seconds) and finalize its record."""
        handle, managed = record.handle, record.managed
        natural_exit = False
        try:
            timeout_seconds = (
                None
                if record.max_runtime_seconds is None
                else max(0.0, record.started_at + record.max_runtime_seconds - time.monotonic())
            )
            try:
                await managed.wait_exit(timeout_seconds=timeout_seconds)
                natural_exit = record.stop_reason is None and managed.returncode is not None and managed.returncode >= 0
            except TimeoutError:
                record.stop_reason = record.stop_reason or "timeout"
                await managed.terminate_tree(whole_group=True)
        finally:
            try:
                live = self._records.get(handle)
                spool = live._active_spool if live is not None else None
                try:
                    if spool is not None and not spool.closed:
                        # Group cleanup can create EOF by killing a writer; decide completeness first.
                        _, pending = await asyncio.wait([stdout_reader, stderr_reader], timeout=2.0)
                        spool.incomplete |= bool(pending)
                finally:
                    should_kill = (
                        not natural_exit or record.kill_group_after_exit
                    ) and record.stop_reason != "scope_ended"
                    if should_kill:
                        managed.kill_tree()
                drain_timeout = 2.0 if should_kill else POST_EXIT_READER_GRACE_SECONDS
                await asyncio.wait([stdout_reader, stderr_reader], timeout=drain_timeout)
                await cancel_pending_tasks(stdout_reader, stderr_reader)
                if (live := self._records.get(handle)) is not None:
                    if (spool := live._active_spool) is not None and not spool.closed:
                        spool.incomplete |= stdout_reader.cancelled() or stderr_reader.cancelled()
                    if (capture := live.output_capture) is not None and not capture.closed:
                        try:
                            capture.publish(managed.returncode)
                        finally:
                            capture.close()
                record.finished = True
                record.finished_at = time.monotonic()
                record.return_code = managed.returncode
            finally:
                record._finish_log()
