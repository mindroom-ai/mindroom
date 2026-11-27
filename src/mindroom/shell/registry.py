"""Background shell process ownership: ProcessRegistry, BackgroundProcess, and structured status/signal results."""

from __future__ import annotations

import asyncio
import contextlib
import re
import signal
import time
import uuid
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

from mindroom.shell.output import (
    BackgroundLogFile,
    BackgroundLogPolicy,
    ShellOutputBuffer,
    StopReason,
    format_auto_background_message,
    format_log_trailer,
    open_background_log,
    seed_background_log,
)
from mindroom.shell.runner import (
    POST_EXIT_READER_GRACE_SECONDS,
    ActiveCommand,
    ShellRunResult,
    cancel_pending_tasks,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from mindroom.shell.output_capture import ShellOutputCapture, ShellOutputDestination
    from mindroom.shell.process import ManagedProcess

_STALE_RECORD_SECONDS = 600  # 10 minutes
MAX_BACKGROUNDED = 16
_MAX_TOTAL_BACKGROUNDED = 128
_CALLER_HANDLE_RE = re.compile(r"shell:[0-9a-f]{32}")


@dataclass(frozen=True, slots=True)
class ProcessStatus:
    """Structured outcome of polling one backgrounded shell process."""

    state: Literal["running", "exited", "unknown"]
    exit_code: int | None = None
    message: str = ""


@dataclass(frozen=True, slots=True)
class SignalResult:
    """Structured outcome of signalling one backgrounded shell process."""

    outcome: Literal["signalled", "finished", "unknown"]
    message: str = ""


@dataclass
class BackgroundProcess:
    """Tracks one backgrounded or retained finished shell process owned by a ``ProcessRegistry``."""

    namespace: str
    handle: str
    pid: int
    args: list[str]
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
    max_runtime_seconds: float | None = None
    log_file: BackgroundLogFile | None = field(default=None, repr=False)
    stop_reason: StopReason | None = field(default=None, repr=False)
    monitor_task: asyncio.Task[None] | None = field(default=None, repr=False)

    @property
    def process(self) -> asyncio.subprocess.Process:
        """Return the underlying subprocess handle."""
        return self.managed.process

    @property
    def log_path(self) -> str | None:
        """Path of the live background log file, if one is attached."""
        return str(self.log_file.path) if self.log_file is not None else None

    @property
    def timed_out(self) -> bool:
        """Whether this process was stopped because it reached ``max_runtime_seconds``."""
        return self.stop_reason == "timeout"

    def signal(self, *, force: bool = False) -> bool:
        """Send SIGTERM, or SIGKILL with *force*, to this command's processes; report whether delivered."""
        sig = signal.SIGKILL if force else signal.SIGTERM
        if not self.managed.signal(sig):
            return False
        if self.output_capture is not None:
            self.output_capture.incomplete = True
        return True


def _kill_record(
    record: BackgroundProcess,
    *,
    stop_reason: StopReason | None = None,
) -> None:
    if record.output_capture is not None:
        record.output_capture.close()
    was_running = not record.finished
    if was_running:
        if stop_reason is not None and record.stop_reason is None:
            record.stop_reason = stop_reason
        record.managed.kill_tree()
        record.finished = True
        record.finished_at = time.monotonic()
        record.return_code = record.managed.returncode
    if record.log_file is not None and not record.log_file.closed:
        if stop_reason is not None and was_running:
            record.log_file.finish(
                format_log_trailer(
                    record.managed.returncode,
                    stop_reason=record.stop_reason,
                    max_runtime_seconds=record.max_runtime_seconds,
                ),
            )
        else:
            record.log_file.finish()
    if record.monitor_task is not None:
        record.monitor_task.cancel()


class ProcessRegistry:
    """Owns backgrounded shell processes, handle reservations, limits, and lifecycle shutdown."""

    def __init__(self) -> None:
        self._records: dict[str, BackgroundProcess] = {}
        self._reservations: set[str] = set()
        self.closed: bool = False

    @property
    def record_count(self) -> int:
        """Return the number of currently tracked background or retained finished records."""
        return len(self._records)

    @property
    def records_view(self) -> Mapping[str, BackgroundProcess]:
        """Return a read-only view of tracked records keyed by handle."""
        return MappingProxyType(self._records)

    def get_process(self, namespace: str, handle: str) -> BackgroundProcess | None:
        """Return the tracked ``BackgroundProcess`` for *handle* if it belongs to *namespace*."""
        record = self._records.get(handle)
        if record is None or record.namespace != namespace:
            return None
        return record

    async def spawn_background(
        self,
        *,
        namespace: str,
        argv: list[str],
        env: dict[str, str],
        cwd: str | None,
        tail: int = 100,
        handle: str | None = None,
        output_destination: ShellOutputDestination | None = None,
        output_capture: ShellOutputCapture | None = None,
        stdin: int | None = None,
        stderr: int = asyncio.subprocess.PIPE,
        kill_group_after_exit: bool = True,
        launch_failure_exits: bool = False,
        max_runtime_seconds: float | None = None,
        background_log: BackgroundLogPolicy | None = None,
    ) -> ShellRunResult:
        """Spawn a command directly into this registry without waiting in the foreground."""
        self._sweep_stale_records()
        if (error := self._reserve_handle(handle)) is not None:
            if output_capture is not None:
                output_capture.close()
            return error

        try:
            candidate_handle = handle or self._unused_handle()
            if (rejection := self._background_rejection(namespace=namespace, handle=candidate_handle)) is not None:
                had_capture = output_capture is not None or output_destination is not None
                if output_capture is not None:
                    output_capture.close()
                return ShellRunResult(
                    message=rejection,
                    output_file_handled=had_capture,
                    background_rejected=True,
                )

            started = await ActiveCommand.start(
                argv=argv,
                env=env,
                cwd=cwd,
                tail=tail,
                output_destination=output_destination,
                output_capture=output_capture,
                stdin=stdin,
                stderr=stderr,
                kill_group_after_exit=kill_group_after_exit,
                launch_failure_exits=launch_failure_exits,
            )
            if isinstance(started, ShellRunResult):
                return started

            return await self._adopt_background(
                started,
                namespace=namespace,
                argv=argv,
                requested_handle=handle,
                max_runtime_seconds=max_runtime_seconds,
                background_log=background_log,
                kill_group_after_exit=kill_group_after_exit,
                timeout=None,
                immediate=True,
            )
        finally:
            if handle is not None:
                self._reservations.discard(handle)

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
        handle: str | None = None,
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
        """Run one shell command in the foreground up to *timeout*, adopting it into this registry on timeout."""
        self._sweep_stale_records()
        if (error := self._reserve_handle(handle)) is not None:
            if output_capture is not None:
                output_capture.close()
            return error

        try:
            started = await ActiveCommand.start(
                argv=argv,
                env=env,
                cwd=cwd,
                tail=tail,
                output_destination=output_destination,
                output_capture=output_capture,
                stdin=stdin,
                stderr=stderr,
                kill_group_after_exit=kill_group_after_exit,
                launch_failure_exits=launch_failure_exits,
            )
            if isinstance(started, ShellRunResult):
                return started

            timed_out = False
            try:
                try:
                    await started.wait_exit(timeout=timeout, on_progress=on_progress)
                except TimeoutError:
                    timed_out = True
                else:
                    if kill_group_after_exit:
                        started.managed.kill_tree()
                    await started.drain_readers()
            except asyncio.CancelledError:
                await started.cancel_and_cleanup()
                raise

            if timed_out:
                assert timeout is not None
                return await self._adopt_background(
                    started,
                    namespace=namespace,
                    argv=argv,
                    requested_handle=handle,
                    max_runtime_seconds=max_runtime_seconds,
                    background_log=background_log,
                    kill_group_after_exit=kill_group_after_exit,
                    timeout=timeout,
                    immediate=False,
                )

            finished_handle = (
                self._register_finished(
                    started,
                    namespace=namespace,
                    argv=argv,
                    requested_handle=handle,
                    kill_group_after_exit=kill_group_after_exit,
                )
                if register_finished
                else None
            )
            return started.finish_result(handle=finished_handle)
        finally:
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

        partial = record.stdout_buf.render(tail=50)
        message = (
            f"Status: RUNNING (PID {record.pid}, elapsed {elapsed:.1f}s)\n"
            f"Partial output ({len(record.stdout_buf)} lines buffered):\n{partial}"
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

        sig_name = "SIGKILL" if force else "SIGTERM"
        if not record.signal(force=force):
            return SignalResult(outcome="finished", message=f"Process {record.pid} already exited")

        action = "Force-killed" if force else "Terminated"
        return SignalResult(outcome="signalled", message=f"{action} process {record.pid} ({sig_name} sent).")

    def discard(self, handle: str) -> None:
        """Kill and drop one background record whose handle is undeliverable or no longer needed."""
        record = self._records.pop(handle, None)
        if record is not None:
            _kill_record(record, stop_reason="stopped")

    async def close_namespace(self, namespace: str) -> int:
        """Gracefully stop and remove every record in *namespace*, flushing log trailers."""
        matching = [
            self._records.pop(handle) for handle, record in list(self._records.items()) if record.namespace == namespace
        ]
        await self._close_entries(matching)
        return len(matching)

    async def close(self) -> None:
        """Mark the registry closed, gracefully stop all unfinished records, and flush log trailers."""
        self.closed = True
        if not self.record_count:
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
        """Remove records that finished more than 10 minutes ago."""
        now = time.monotonic()
        stale = [
            handle
            for handle, record in self._records.items()
            if record.finished and record.finished_at is not None and (now - record.finished_at) > _STALE_RECORD_SECONDS
        ]
        for handle in stale:
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
        if handle in self._records:
            return f"Error: Shell handle '{handle}' is already registered."
        return None

    def _prepare_background_log(
        self,
        policy: BackgroundLogPolicy,
        *,
        active: ActiveCommand,
        handle: str,
    ) -> BackgroundLogFile:
        active_paths = {
            record.log_path
            for record in self._records.values()
            if record.log_path is not None
            and (not record.finished or (record.log_file is not None and not record.log_file.closed))
        }
        log_file = open_background_log(policy, pid=active.managed.pid, handle=handle, active_paths=active_paths)
        try:
            seed_background_log(log_file, active.stdout_buf, active.stderr_buf)
        except OSError:
            log_file.finish()
            with contextlib.suppress(OSError):
                log_file.path.unlink()
            raise
        active.attach_log_file(log_file)
        return log_file

    def _register_finished(
        self,
        active: ActiveCommand,
        *,
        namespace: str,
        argv: list[str],
        requested_handle: str | None,
        kill_group_after_exit: bool,
    ) -> str:
        return_code = active.managed.returncode
        assert return_code is not None
        finished_handle = requested_handle or self._unused_handle()
        self._records[finished_handle] = BackgroundProcess(
            namespace=namespace,
            handle=finished_handle,
            pid=active.managed.pid,
            args=argv,
            managed=active.managed,
            kill_group_after_exit=kill_group_after_exit,
            stdout_buf=active.stdout_buf,
            stderr_buf=active.stderr_buf,
            started_at=active.started_at,
            tail=active.tail,
            finished=True,
            finished_at=time.monotonic(),
            return_code=return_code,
            output_capture=active.output_capture,
        )
        return finished_handle

    async def _rollback_cancelled_adoption(
        self,
        *,
        handle: str,
        active: ActiveCommand,
        monitor_task: asyncio.Task[None],
        log_file: BackgroundLogFile | None,
        record_capture: ShellOutputCapture | None,
    ) -> None:
        self._records.pop(handle, None)
        monitor_task.cancel()
        try:
            await active.managed.terminate_tree()
        finally:
            try:
                await cancel_pending_tasks(active.stdout_reader, active.stderr_reader)
            finally:
                if log_file is not None:
                    log_file.finish()
                if record_capture is not None:
                    record_capture.close()
        with contextlib.suppress(asyncio.CancelledError):
            await monitor_task

    def _format_adopted_message(
        self,
        *,
        active: ActiveCommand,
        handle: str,
        timeout: float | None,
        immediate: bool,
        max_runtime_seconds: float | None,
        background_log: BackgroundLogPolicy | None,
        log_file: BackgroundLogFile | None,
        no_output_path: str | None,
    ) -> str:
        if background_log is not None and log_file is not None:
            return format_auto_background_message(
                pid=active.managed.pid,
                timeout=0.0 if timeout is None else timeout,
                log_path=log_file.path,
                display_root=background_log.display_root,
                max_runtime_seconds=max_runtime_seconds,
                no_output_path=no_output_path,
                stdout_buf=active.stdout_buf,
                stderr_buf=active.stderr_buf,
                tail=active.tail,
            )
        if immediate:
            return f"Started background process (PID {active.managed.pid}).\nHandle: {handle}"
        return f"Command timed out after {timeout}s. Still running (PID {active.managed.pid}).\nHandle: {handle}"

    def _prepare_adoption_outputs(
        self,
        active: ActiveCommand,
        *,
        handle: str,
        background_log: BackgroundLogPolicy | None,
    ) -> tuple[BackgroundLogFile | None, ShellOutputCapture | None, str | None, str | None]:
        if background_log is None:
            return None, active.output_capture, None, None
        try:
            log_file = self._prepare_background_log(background_log, active=active, handle=handle)
        except OSError as exc:
            return None, active.output_capture, None, f"Error: Failed to open background shell log: {exc}"
        output_path_notice = None
        if active.output_capture is not None:
            output_path_notice = active.output_capture.destination.path
            active.output_capture.close()
        return log_file, None, output_path_notice, None

    async def _adopt_background(
        self,
        active: ActiveCommand,
        *,
        namespace: str,
        argv: list[str],
        requested_handle: str | None,
        max_runtime_seconds: float | None,
        background_log: BackgroundLogPolicy | None,
        kill_group_after_exit: bool,
        timeout: float | None,  # noqa: ASYNC109
        immediate: bool,
    ) -> ShellRunResult:
        had_capture = active.output_capture is not None
        handle = requested_handle or self._unused_handle()
        rejection = self._background_rejection(namespace=namespace, handle=handle)
        log_file: BackgroundLogFile | None = None
        record_capture, output_path_notice = active.output_capture, None
        if rejection is None:
            log_file, record_capture, output_path_notice, rejection = self._prepare_adoption_outputs(
                active,
                handle=handle,
                background_log=background_log,
            )
        if rejection is not None:
            await active.discard_unregistered()
            return ShellRunResult(message=rejection, output_file_handled=had_capture, background_rejected=True)

        record = BackgroundProcess(
            namespace=namespace,
            handle=handle,
            pid=active.managed.pid,
            args=argv,
            managed=active.managed,
            kill_group_after_exit=kill_group_after_exit,
            stdout_buf=active.stdout_buf,
            stderr_buf=active.stderr_buf,
            started_at=active.started_at,
            tail=active.tail,
            output_capture=record_capture,
            max_runtime_seconds=max_runtime_seconds,
            log_file=log_file,
        )
        monitor_task = asyncio.create_task(
            self._monitor_process(record, active.stdout_reader, active.stderr_reader),
        )
        record.monitor_task = monitor_task
        self._records[handle] = record
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            await self._rollback_cancelled_adoption(
                handle=handle,
                active=active,
                monitor_task=monitor_task,
                log_file=log_file,
                record_capture=record_capture,
            )
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

        message = self._format_adopted_message(
            active=active,
            handle=handle,
            timeout=timeout,
            immediate=immediate,
            max_runtime_seconds=max_runtime_seconds,
            background_log=background_log,
            log_file=log_file,
            no_output_path=output_path_notice,
        )
        return ShellRunResult(message=message, handle=handle, output_file_handled=had_capture)

    async def _close_entries(self, entries: list[BackgroundProcess]) -> None:
        if not entries:
            return
        try:
            to_stop = [record for record in entries if not record.finished and record.managed.returncode is None]
            for record in to_stop:
                record.stop_reason = record.stop_reason or "scope_ended"
            if to_stop:
                await asyncio.gather(
                    *(record.managed.terminate_tree(whole_group=True) for record in to_stop),
                    return_exceptions=True,
                )
            monitors = [
                record.monitor_task
                for record in entries
                if record.monitor_task is not None and not record.monitor_task.done()
            ]
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
        handle = record.handle
        managed = record.managed
        log_file = record.log_file
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
                if record.stop_reason is None:
                    record.stop_reason = "timeout"
                await managed.terminate_tree(whole_group=True)
        finally:
            try:
                live_record = self._records.get(handle)
                capture = live_record.output_capture if live_record is not None else None
                try:
                    if capture is not None and not capture.closed:
                        # Group cleanup can create EOF by killing a writer. Decide
                        # completeness before that EOF can look like normal completion.
                        _done, pending = await asyncio.wait([stdout_reader, stderr_reader], timeout=2.0)
                        capture.incomplete |= bool(pending)
                finally:
                    if (not natural_exit or record.kill_group_after_exit) and record.stop_reason != "scope_ended":
                        managed.kill_tree()
                drain_timeout = (
                    2.0
                    if record.kill_group_after_exit and record.stop_reason != "scope_ended"
                    else POST_EXIT_READER_GRACE_SECONDS
                )
                await asyncio.wait([stdout_reader, stderr_reader], timeout=drain_timeout)
                await cancel_pending_tasks(stdout_reader, stderr_reader)
                live_record = self._records.get(handle)
                if live_record is not None:
                    capture = live_record.output_capture
                    if capture is not None and not capture.closed:
                        capture.incomplete |= stdout_reader.cancelled() or stderr_reader.cancelled()
                        try:
                            capture.publish(managed.returncode)
                        finally:
                            capture.close()
                record.finished = True
                record.finished_at = time.monotonic()
                record.return_code = managed.returncode
            finally:
                if log_file is not None and not log_file.closed:
                    log_file.finish(
                        format_log_trailer(
                            managed.returncode,
                            stop_reason=record.stop_reason,
                            max_runtime_seconds=record.max_runtime_seconds,
                        ),
                    )
