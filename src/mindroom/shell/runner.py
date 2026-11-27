"""Registry-free foreground shell execution and shared live-command lifecycle helper."""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.shell.output import (
    BackgroundLogFile,
    ShellOutputBuffer,
    ShellOutputProgress,
    drain_text_stream,
    format_shell_completion,
    format_shell_success,
    format_shell_timeout,
)
from mindroom.shell.output_capture import ShellOutputCapture, ShellOutputDestination
from mindroom.shell.process import ManagedProcess, classify_launch_failure

if TYPE_CHECKING:
    from collections.abc import Callable

POST_EXIT_READER_GRACE_SECONDS = 0.5


@dataclass(frozen=True)
class ShellRunResult:
    """Outcome of one foreground or backgroundable shell command call.

    ``handle`` is set only when the command was adopted into a ``ProcessRegistry``
    (or retained as a finished record), so callers that could not deliver the
    message can roll the registration back with ``registry.discard``.
    ``output_file_handled`` means execution owns publication or its error receipt;
    the generic output-file wrapper must not write the returned message again.
    """

    message: str
    handle: str | None = None
    output_file_handled: bool = False
    execution_error: bool = False
    return_code: int | None = None
    """The exit status of a command that ended within the call; None when it did not start or still runs."""
    timed_out: bool = False
    background_rejected: bool = False

    @property
    def failed(self) -> bool:
        """Return whether the command exited non-zero, timed out, or failed to start/background."""
        return (
            self.execution_error
            or self.background_rejected
            or self.timed_out
            or (self.return_code is not None and self.return_code != 0)
        )


async def cancel_pending_tasks(*tasks: asyncio.Task[None]) -> None:
    """Cancel any pending tasks and wait for them to finish."""
    pending_tasks = [task for task in tasks if not task.done()]
    for task in pending_tasks:
        task.cancel()
    for task in pending_tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _await_reader_tasks_with_grace(
    stdout_reader: asyncio.Task[None],
    stderr_reader: asyncio.Task[None],
    *,
    grace_seconds: float,
) -> None:
    """Drain reader tasks briefly after foreground exit, then stop waiting for EOF."""
    reader_tasks = (stdout_reader, stderr_reader)
    done_tasks, pending_tasks = await asyncio.wait(reader_tasks, timeout=grace_seconds)

    try:
        for task in done_tasks:
            await task
    finally:
        for task in pending_tasks:
            task.cancel()
        for task in pending_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


def _launch_failure(
    argv: list[str],
    env: dict[str, str],
    cwd: str | None,
    exc: Exception,
    capture: ShellOutputCapture | None,
) -> ShellRunResult | None:
    """Return a spawn error of a program that is missing or cannot be executed as Bash reports it, else None.

    The result is the one of a command that exited 127 or 126 with Bash's words on stderr, so everything that reads a
    failed command's result reads this one.
    """
    if (failure := classify_launch_failure(argv, env, cwd, exc)) is None:
        return None
    return_code, said = failure
    if capture is None:
        return ShellRunResult(
            message=format_shell_completion("", said, return_code=return_code),
            return_code=return_code,
        )
    capture.stderr.append(f"{said}\n")
    capture.stdout.reached_eof = capture.stderr.reached_eof = True
    return ShellRunResult(message=capture.publish(return_code), output_file_handled=True, return_code=return_code)


class ActiveCommand:
    """Owns one live ``ManagedProcess``, its output buffers, reader tasks, and optional capture spool."""

    def __init__(
        self,
        *,
        managed: ManagedProcess,
        stdout_buf: ShellOutputBuffer,
        stderr_buf: ShellOutputBuffer,
        stdout_reader: asyncio.Task[None],
        stderr_reader: asyncio.Task[None],
        output_capture: ShellOutputCapture | None,
        started_at: float,
        tail: int,
        kill_group_after_exit: bool,
        extra_sinks: list[Callable[[str], None]],
    ) -> None:
        self.managed = managed
        self.stdout_buf = stdout_buf
        self.stderr_buf = stderr_buf
        self.stdout_reader = stdout_reader
        self.stderr_reader = stderr_reader
        self.output_capture = output_capture
        self.started_at = started_at
        self.tail = tail
        self.kill_group_after_exit = kill_group_after_exit
        self._extra_sinks = extra_sinks

    @classmethod
    async def start(
        cls,
        *,
        argv: list[str],
        env: dict[str, str],
        cwd: str | None,
        tail: int,
        output_destination: ShellOutputDestination | None = None,
        output_capture: ShellOutputCapture | None = None,
        stdin: int | None = None,
        stderr: int = asyncio.subprocess.PIPE,
        kill_group_after_exit: bool = False,
        launch_failure_exits: bool = False,
    ) -> ActiveCommand | ShellRunResult:
        """Allocate optional capture, spawn the subprocess, and start stream reader tasks."""
        capture = output_capture
        try:
            if capture is None and output_destination is not None:
                capture = ShellOutputCapture(output_destination)
            started_at = time.monotonic()
        except asyncio.CancelledError:
            if capture is not None:
                capture.close()
            raise
        except Exception as exc:
            if capture is not None:
                capture.close()
            return ShellRunResult(message=f"Error: {exc}", execution_error=True)

        try:
            managed = await ManagedProcess.spawn(argv, env, cwd, stdin=stdin, stderr=stderr)
        except asyncio.CancelledError:
            if capture is not None:
                capture.close()
            raise
        except Exception as exc:
            try:
                exited = _launch_failure(argv, env, cwd, exc, capture) if launch_failure_exits else None
                return exited or ShellRunResult(message=f"Error: {exc}", execution_error=True)
            finally:
                if capture is not None:
                    capture.close()

        stdout_buf = ShellOutputBuffer()
        stderr_buf = ShellOutputBuffer()
        extra_sinks: list[Callable[[str], None]] = []
        stdout_reader = asyncio.create_task(
            drain_text_stream(
                managed.stdout,
                stdout_buf,
                capture.stdout if capture else None,
                extra_sinks=extra_sinks,
            ),
        )
        stderr_reader = asyncio.create_task(
            drain_text_stream(
                managed.stderr,
                stderr_buf,
                capture.stderr if capture else None,
                extra_sinks=extra_sinks,
            ),
        )
        return cls(
            managed=managed,
            stdout_buf=stdout_buf,
            stderr_buf=stderr_buf,
            stdout_reader=stdout_reader,
            stderr_reader=stderr_reader,
            output_capture=capture,
            started_at=started_at,
            tail=tail,
            kill_group_after_exit=kill_group_after_exit,
            extra_sinks=extra_sinks,
        )

    def attach_log_file(self, log_file: BackgroundLogFile) -> None:
        """Forward all subsequent decoded stdout/stderr chunks to *log_file*."""
        self._extra_sinks.append(log_file.write)

    async def wait_exit(
        self,
        *,
        timeout: float | None,  # noqa: ASYNC109
        on_progress: Callable[[str], None] | None = None,
    ) -> None:
        """Wait for the subprocess to exit within *timeout* seconds, emitting optional throttled progress."""
        await self.managed.wait_exit(
            timeout_seconds=timeout,
            on_poll=ShellOutputProgress(on_progress, self.stdout_buf, self.stderr_buf).poll
            if on_progress is not None
            else None,
        )

    async def drain_readers(self, *, grace_seconds: float = POST_EXIT_READER_GRACE_SECONDS) -> None:
        """Wait briefly for stdout/stderr readers to reach EOF after exit, cancelling stragglers."""
        await _await_reader_tasks_with_grace(
            self.stdout_reader,
            self.stderr_reader,
            grace_seconds=grace_seconds,
        )

    async def stop_timed_out(
        self,
        *,
        timeout: float,  # noqa: ASYNC109
    ) -> ShellRunResult:
        """Stop a timed-out command and report its output so far; a stopped command saves no output file."""
        await self.managed.terminate_tree()
        await self.drain_readers()
        message = format_shell_timeout(
            self.stdout_buf.render(tail=self.tail),
            self.stderr_buf.render() if self.stderr_buf else None,
            timeout=timeout,
            no_output_file=self.output_capture is not None,
        )
        if self.output_capture is None:
            return ShellRunResult(message=message, return_code=self.managed.returncode, timed_out=True)
        self.output_capture.incomplete = True
        self.output_capture.interruption = message
        try:
            return ShellRunResult(
                message=self.output_capture.publish(self.managed.returncode),
                output_file_handled=True,
                return_code=self.managed.returncode,
                timed_out=True,
            )
        finally:
            self.output_capture.close()

    async def cancel_and_cleanup(self) -> None:
        """Stop an unfinished command on cancellation and clean up reader tasks and capture spools."""
        try:
            if self.managed.returncode is None:
                await self.managed.terminate_tree()
            if self.kill_group_after_exit:
                self.managed.kill_tree()
        finally:
            try:
                await cancel_pending_tasks(self.stdout_reader, self.stderr_reader)
            finally:
                if self.output_capture is not None:
                    self.output_capture.close()

    async def discard_unregistered(self) -> None:
        """Force-kill and clean up a command that was rejected before background registration."""
        self.managed.kill_tree()
        try:
            await cancel_pending_tasks(self.stdout_reader, self.stderr_reader)
        finally:
            if self.output_capture is not None:
                self.output_capture.close()

    def finish_result(self, *, handle: str | None = None) -> ShellRunResult:
        """Format or publish the result of a command that finished in the foreground."""
        return_code = self.managed.returncode
        assert return_code is not None
        if self.output_capture is not None:
            self.output_capture.incomplete = self.stdout_reader.cancelled() or self.stderr_reader.cancelled()
            try:
                return ShellRunResult(
                    message=self.output_capture.publish(return_code),
                    handle=handle,
                    output_file_handled=True,
                    return_code=return_code,
                )
            finally:
                self.output_capture.close()
        if return_code:
            # A failure keeps its whole stderr, which explains it.
            errors = self.stderr_buf.render() if self.stderr_buf else None
            message = format_shell_completion(self.stdout_buf.render(tail=self.tail), errors, return_code=return_code)
        else:
            message = format_shell_success(self.stdout_buf, self.stderr_buf, self.tail)
        return ShellRunResult(message=message, handle=handle, return_code=return_code)


async def run_foreground(
    *,
    argv: list[str],
    env: dict[str, str],
    cwd: str | None,
    tail: int,
    timeout: float | None,  # noqa: ASYNC109
    on_progress: Callable[[str], None] | None = None,
    output_destination: ShellOutputDestination | None = None,
    output_capture: ShellOutputCapture | None = None,
    stdin: int | None = None,
    stderr: int = asyncio.subprocess.PIPE,
    kill_group_after_exit: bool = False,
    launch_failure_exits: bool = False,
) -> ShellRunResult:
    """Run one shell command in the foreground with zero background registry state.

    When the command completes within ``timeout`` seconds, its tailed stdout and
    stderr (or published output-file receipt) are returned. When ``timeout`` is
    exceeded, the whole process tree is terminated and partial output is returned.
    Cancellation terminates an unfinished process tree and cleans up readers/spools.
    With ``kill_group_after_exit=False``, intentionally detached ``nohup`` children
    survive natural foreground exit.
    """
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

    try:
        try:
            await started.wait_exit(timeout=timeout, on_progress=on_progress)
        except TimeoutError:
            assert timeout is not None
            return await started.stop_timed_out(timeout=timeout)

        if kill_group_after_exit:
            started.managed.kill_tree()
        await started.drain_readers()
    except asyncio.CancelledError:
        await started.cancel_and_cleanup()
        raise

    return started.finish_result()
