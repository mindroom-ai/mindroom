"""Registry-free foreground shell execution and shared live-command lifecycle helper."""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.shell.output import (
    ShellOutputBuffer,
    ShellOutputProgress,
    SpoolCapture,
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

    ``handle`` is set only when the command was adopted into a ``ProcessRegistry``,
    so callers that could not deliver the message can roll it back with ``registry.discard``.
    ``output_file_handled`` means execution owns publication or its error receipt.
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
    pending = [task for task in tasks if not task.done()]
    for task in pending:
        task.cancel()
    for task in pending:
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _launch_failure(
    argv: list[str],
    env: dict[str, str],
    cwd: str | None,
    exc: Exception,
    capture: ShellOutputCapture | None,
) -> ShellRunResult | None:
    """Return a spawn error of a program that is missing or cannot be executed as Bash reports it, else None."""
    if (failure := classify_launch_failure(argv, env, cwd, exc)) is None:
        return None
    return_code, said = failure
    if capture is None:
        return ShellRunResult(format_shell_completion("", said, return_code=return_code), return_code=return_code)
    capture.stderr.append(f"{said}\n")
    capture.stdout.reached_eof = capture.stderr.reached_eof = True
    return ShellRunResult(capture.publish(return_code), output_file_handled=True, return_code=return_code)


@dataclass(slots=True)
class ActiveCommand:
    """Owns one live ``ManagedProcess``, its output buffers, reader tasks, and optional output capture/spool."""

    managed: ManagedProcess
    stdout_buf: ShellOutputBuffer
    stderr_buf: ShellOutputBuffer
    stdout_reader: asyncio.Task[None]
    stderr_reader: asyncio.Task[None]
    output_capture: ShellOutputCapture | None
    spool: SpoolCapture | None
    started_at: float
    tail: int
    kill_group_after_exit: bool
    _extra_sinks: list[Callable[[str], None]]

    @classmethod
    async def start(
        cls,
        *,
        argv: list[str],
        env: dict[str, str],
        cwd: str | None,
        tail: int,
        output_destination: ShellOutputDestination | None = None,
        spool: SpoolCapture | None = None,
        stdin: int | None = None,
        stderr: int = asyncio.subprocess.PIPE,
        kill_group_after_exit: bool = False,
        launch_failure_exits: bool = False,
    ) -> ActiveCommand | ShellRunResult:
        """Allocate optional output capture, spawn the subprocess, and start stream reader tasks."""
        if output_destination is not None and spool is not None:
            msg = "Pass either output_destination or spool, not both."
            raise ValueError(msg)
        capture: ShellOutputCapture | None = None
        try:
            if output_destination is not None:
                capture = ShellOutputCapture(output_destination)
            started_at = time.monotonic()
        except Exception as exc:
            if capture is not None:
                capture.close()
            return ShellRunResult(f"Error: {exc}", execution_error=True)

        try:
            managed = await ManagedProcess.spawn(argv, env, cwd, stdin=stdin, stderr=stderr)
        except BaseException as exc:
            try:
                if isinstance(exc, Exception):
                    exited = _launch_failure(argv, env, cwd, exc, capture) if launch_failure_exits else None
                    return exited or ShellRunResult(f"Error: {exc}", execution_error=True)
                raise
            finally:
                if capture is not None:
                    capture.close()

        active_spool = capture.spool if capture is not None else spool
        stdout_buf, stderr_buf = ShellOutputBuffer(), ShellOutputBuffer()
        extra_sinks: list[Callable[[str], None]] = []
        stdout_reader = asyncio.create_task(
            drain_text_stream(
                managed.stdout,
                stdout_buf,
                active_spool.stdout if active_spool else None,
                extra_sinks=extra_sinks,
            ),
        )
        stderr_reader = asyncio.create_task(
            drain_text_stream(
                managed.stderr,
                stderr_buf,
                active_spool.stderr if active_spool else None,
                extra_sinks=extra_sinks,
            ),
        )
        return cls(
            managed,
            stdout_buf,
            stderr_buf,
            stdout_reader,
            stderr_reader,
            capture,
            spool,
            started_at,
            tail,
            kill_group_after_exit,
            extra_sinks,
        )

    def add_output_sink(self, sink: Callable[[str], None]) -> None:
        """Forward all subsequent decoded stdout/stderr chunks to *sink*."""
        self._extra_sinks.append(sink)

    async def _drain_readers(self, *, grace_seconds: float = POST_EXIT_READER_GRACE_SECONDS) -> None:
        done, pending = await asyncio.wait((self.stdout_reader, self.stderr_reader), timeout=grace_seconds)
        try:
            for task in done:
                await task
        finally:
            await cancel_pending_tasks(*pending)

    async def wait_or_timeout(
        self,
        *,
        timeout: float | None,  # noqa: ASYNC109
        on_progress: Callable[[str], None] | None = None,
    ) -> bool:
        """Wait up to *timeout*; return True on timeout, else drain readers and return False."""
        on_poll = ShellOutputProgress(on_progress, self.stdout_buf, self.stderr_buf).poll if on_progress else None
        try:
            try:
                await self.managed.wait_exit(timeout_seconds=timeout, on_poll=on_poll)
            except TimeoutError:
                return True
            if self.kill_group_after_exit or self.managed.returncode != 0:
                self.managed.kill_tree()
            await self._drain_readers()
        except asyncio.CancelledError:
            await self.cancel_and_cleanup()
            raise
        return False

    async def _stop_timed_out(self, *, timeout: float) -> ShellRunResult:  # noqa: ASYNC109
        """Stop a timed-out command and report its output so far; a stopped command saves no output file."""
        await self.managed.terminate_tree()
        await self._drain_readers()
        if self.spool is not None:
            self.spool.incomplete = True
        message = format_shell_timeout(
            self.stdout_buf.render(tail=self.tail),
            self.stderr_buf.render() if self.stderr_buf else None,
            timeout=timeout,
            no_output_file=self.output_capture is not None,
        )
        if self.output_capture is None:
            return ShellRunResult(message, return_code=self.managed.returncode, timed_out=True)
        self.output_capture.incomplete = True
        self.output_capture.interruption = message
        try:
            return ShellRunResult(
                self.output_capture.publish(self.managed.returncode),
                output_file_handled=True,
                return_code=self.managed.returncode,
                timed_out=True,
            )
        finally:
            self.output_capture.close()

    async def cancel_and_cleanup(self) -> None:
        """Stop an unfinished command on cancellation and clean up reader tasks and engine-owned captures."""
        try:
            if self.managed.returncode is None:
                await self.managed.terminate_tree()
            if self.kill_group_after_exit:
                self.managed.kill_tree()
        finally:
            if self.spool is not None:
                self.spool.incomplete = True
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

    def finish_result(self) -> ShellRunResult:
        """Format or publish the result of a command that finished in the foreground."""
        return_code = self.managed.returncode
        assert return_code is not None
        readers_cancelled = self.stdout_reader.cancelled() or self.stderr_reader.cancelled()
        if self.spool is not None:
            self.spool.incomplete |= readers_cancelled
        if self.output_capture is not None:
            self.output_capture.incomplete = readers_cancelled
            try:
                return ShellRunResult(
                    self.output_capture.publish(return_code),
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
        return ShellRunResult(message, return_code=return_code)


async def run_foreground(
    *,
    argv: list[str],
    env: dict[str, str],
    cwd: str | None,
    tail: int,
    timeout: float | None,  # noqa: ASYNC109
    on_progress: Callable[[str], None] | None = None,
    output_destination: ShellOutputDestination | None = None,
    spool: SpoolCapture | None = None,
    stdin: int | None = None,
    stderr: int = asyncio.subprocess.PIPE,
    kill_group_after_exit: bool = False,
    launch_failure_exits: bool = False,
) -> ShellRunResult:
    """Run one shell command in the foreground with zero background registry state.

    When the command exceeds ``timeout`` seconds, the process tree is terminated and partial output is returned.
    With ``kill_group_after_exit=False``, intentionally detached ``nohup`` children survive clean exit (code 0).
    """
    started = await ActiveCommand.start(
        argv=argv,
        env=env,
        cwd=cwd,
        tail=tail,
        output_destination=output_destination,
        spool=spool,
        stdin=stdin,
        stderr=stderr,
        kill_group_after_exit=kill_group_after_exit,
        launch_failure_exits=launch_failure_exits,
    )
    if isinstance(started, ShellRunResult):
        return started
    if await started.wait_or_timeout(timeout=timeout, on_progress=on_progress):
        assert timeout is not None
        try:
            return await started._stop_timed_out(timeout=timeout)
        except asyncio.CancelledError:
            await started.cancel_and_cleanup()
            raise
    return started.finish_result()
