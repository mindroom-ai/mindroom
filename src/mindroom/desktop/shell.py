"""Locally approved, bounded shell execution for a paired desktop device."""

from __future__ import annotations

import asyncio
import os
import signal
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

_MAX_COMMAND = 8_192
_MAX_OUTPUT = 16_384
_TERM_GRACE_SECONDS = 1.0


class DesktopShellError(ValueError):
    """Invalid or unauthorized local shell execution."""


@dataclass(frozen=True)
class DesktopShellRequest:
    """Exact remote command shown to the local approver."""

    request_id: str
    requester_id: str
    agent_name: str
    command: str
    cwd: str
    expires_at_ms: int
    timeout_seconds: int = 30


class DesktopShell:
    """Require a local decision or live local lease before each process starts."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not hasattr(os, "killpg"):
            message = "This platform cannot stop local process groups safely."
            raise DesktopShellError(message)
        self._clock = clock
        self._monotonic_clock = monotonic_clock
        self._lease_until = 0.0
        self._pending: DesktopShellRequest | None = None
        self._decision: asyncio.Future[str] | None = None
        self._active_request_id: str | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._cancel_event = asyncio.Event()
        self._finished = asyncio.Event()
        self._finished.set()
        self._used_ids: set[str] = set()
        self._busy = False
        self._closed = False

    def _validate_request(self, request: DesktopShellRequest) -> float:
        if not isinstance(request, DesktopShellRequest):
            message = "Invalid shell request."
            raise DesktopShellError(message)
        for field in (request.request_id, request.requester_id, request.agent_name):
            if not isinstance(field, str) or not field or "\x00" in field:
                message = "Shell request identity is invalid."
                raise DesktopShellError(message)
        if not isinstance(request.command, str) or not request.command.strip() or "\x00" in request.command:
            message = "Shell command must be nonempty and contain no NUL."
            raise DesktopShellError(message)
        if len(request.command) > _MAX_COMMAND:
            message = "Shell command is too long."
            raise DesktopShellError(message)
        if not isinstance(request.cwd, str) or not Path(request.cwd).is_absolute() or not Path(request.cwd).is_dir():
            message = "Shell working directory must be an existing absolute directory."
            raise DesktopShellError(message)
        if (
            not isinstance(request.timeout_seconds, int)
            or isinstance(request.timeout_seconds, bool)
            or not 1 <= request.timeout_seconds <= 60
        ):
            message = "Shell timeout must be an integer from 1 to 60 seconds."
            raise DesktopShellError(message)
        if not isinstance(request.expires_at_ms, int) or isinstance(request.expires_at_ms, bool):
            message = "Shell approval expiry is invalid."
            raise DesktopShellError(message)
        remaining = request.expires_at_ms / 1000 - self._clock()
        if remaining <= 0:
            message = "Shell approval expired."
            raise DesktopShellError(message)
        return self._monotonic_clock() + remaining

    def status(self) -> dict[str, object]:
        """Describe pending approval, active process, and remaining lease."""
        pending = self._pending
        return {
            "pending": (
                {
                    "request_id": pending.request_id,
                    "requester_id": pending.requester_id,
                    "agent_name": pending.agent_name,
                    "command": pending.command,
                    "cwd": pending.cwd,
                    "expires_at_ms": pending.expires_at_ms,
                }
                if pending
                else None
            ),
            "auto_approve_remaining_seconds": max(0.0, self._lease_until - self._monotonic_clock()),
            "active_request_id": self._active_request_id,
        }

    def decide(self, command_id: str, *, approved: bool, auto_approve_seconds: int = 0) -> None:
        """Settle only the exact pending request ID from the local UI."""
        pending = self._pending
        decision = self._decision
        if self._closed or pending is None or decision is None or decision.done() or pending.request_id != command_id:
            message = "No matching pending shell command."
            raise DesktopShellError(message)
        if not isinstance(approved, bool):
            message = "Shell decision must be boolean."
            raise DesktopShellError(message)
        if (
            not isinstance(auto_approve_seconds, int)
            or isinstance(auto_approve_seconds, bool)
            or (auto_approve_seconds != 0 and not 60 <= auto_approve_seconds <= 3600)
        ):
            message = "Auto-approval duration must be 0 or 60 to 3600 seconds."
            raise DesktopShellError(message)
        if self._monotonic_clock() >= self._pending_deadline:
            message = "Shell approval expired."
            raise DesktopShellError(message)
        if approved and auto_approve_seconds:
            self._lease_until = self._monotonic_clock() + auto_approve_seconds
        decision.set_result("approved" if approved else "denied")

    def grant(self, duration_seconds: int) -> None:
        """Grant a time-limited local auto-approval lease."""
        if self._closed:
            message = "Local shell is closed."
            raise DesktopShellError(message)
        if (
            not isinstance(duration_seconds, int)
            or isinstance(duration_seconds, bool)
            or not 60 <= duration_seconds <= 3600
        ):
            message = "Grant duration must be 60 to 3600 seconds."
            raise DesktopShellError(message)
        self._lease_until = self._monotonic_clock() + duration_seconds

    async def _await_approval(self, request: DesktopShellRequest, deadline: float) -> str:
        self._pending = request
        self._pending_deadline = deadline
        self._decision = asyncio.get_running_loop().create_future()
        try:
            return await asyncio.wait_for(self._decision, max(0, deadline - self._monotonic_clock()))
        except TimeoutError as exc:
            message = "Shell approval expired."
            raise DesktopShellError(message) from exc
        finally:
            self._pending = None
            self._decision = None

    def _admit(self, request: DesktopShellRequest) -> float:
        if self._closed:
            message = "Local shell is closed."
            raise DesktopShellError(message)
        if self._busy:
            message = "A local shell command is already pending or running."
            raise DesktopShellError(message)
        deadline = self._validate_request(request)
        if request.request_id in self._used_ids:
            message = "Shell request ID has already been used."
            raise DesktopShellError(message)
        self._used_ids.add(request.request_id)
        self._busy = True
        self._finished.clear()
        self._cancel_event.clear()
        return deadline

    async def execute(self, request: DesktopShellRequest) -> dict[str, object]:
        """Admit one request, await local approval if needed, then run it."""
        deadline = self._admit(request)
        try:
            if self._monotonic_clock() >= self._lease_until:
                outcome = await self._await_approval(request, deadline)
                if outcome == "denied":
                    message = "Shell command denied locally."
                    raise DesktopShellError(message)
                if outcome == "cancelled":
                    return self._cancelled_result()
                if self._monotonic_clock() >= deadline:
                    message = "Shell approval expired."
                    raise DesktopShellError(message)
            if self._monotonic_clock() >= deadline:
                message = "Shell request expired."
                raise DesktopShellError(message)
            if self._cancel_event.is_set():
                return self._cancelled_result()
            self._active_request_id = request.request_id
            self._process = await asyncio.create_subprocess_exec(
                "/bin/sh",
                "-c",
                request.command,
                cwd=request.cwd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            )
            return await self._run_process(request.timeout_seconds)
        except asyncio.CancelledError:
            await self._terminate_process()
            raise
        finally:
            self._pending = None
            self._decision = None
            self._active_request_id = None
            self._process = None
            self._busy = False
            self._finished.set()

    @staticmethod
    def _cancelled_result() -> dict[str, object]:
        return {
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "truncated": False,
            "timed_out": False,
            "cancelled": True,
        }

    @staticmethod
    async def _drain(reader: asyncio.StreamReader) -> tuple[bytes, bool]:
        output = bytearray()
        truncated = False
        while chunk := await reader.read(4096):
            remaining = _MAX_OUTPUT - len(output)
            output.extend(chunk[:remaining])
            truncated |= len(chunk) > remaining
        return bytes(output), truncated

    async def _run_process(self, timeout_seconds: int) -> dict[str, object]:
        process = self._process
        assert process is not None
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_task = asyncio.create_task(self._drain(process.stdout))
        stderr_task = asyncio.create_task(self._drain(process.stderr))
        wait_task = asyncio.create_task(process.wait())
        complete = asyncio.gather(wait_task, stdout_task, stderr_task)
        cancel_task = asyncio.create_task(self._cancel_event.wait())
        timed_out = False
        cancelled = False
        try:
            done, _ = await asyncio.wait(
                {complete, cancel_task},
                timeout=timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            timed_out = not done
            cancelled = cancel_task in done and self._cancel_event.is_set()
            if timed_out or cancelled:
                await self._terminate_process()
                try:
                    await asyncio.wait_for(asyncio.shield(complete), _TERM_GRACE_SECONDS)
                except TimeoutError:
                    self._signal_group(signal.SIGKILL)
            _, (stdout, stdout_truncated), (stderr, stderr_truncated) = await asyncio.shield(complete)
            return {
                "exit_code": process.returncode,
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace"),
                "truncated": stdout_truncated or stderr_truncated,
                "timed_out": timed_out,
                "cancelled": cancelled,
            }
        finally:
            cancel_task.cancel()
            if not complete.done():
                await self._terminate_process()
                try:
                    await asyncio.wait_for(asyncio.shield(complete), _TERM_GRACE_SECONDS)
                except TimeoutError:
                    self._signal_group(signal.SIGKILL)
            await asyncio.gather(complete, cancel_task, return_exceptions=True)

    def _signal_group(self, sig: signal.Signals) -> None:
        process = self._process
        if process is None:
            return
        with suppress(ProcessLookupError):
            os.killpg(process.pid, sig)

    async def _terminate_process(self) -> None:
        process = self._process
        if process is None:
            return
        self._signal_group(signal.SIGTERM)
        if process.returncode is not None:
            return
        try:
            await asyncio.wait_for(process.wait(), _TERM_GRACE_SECONDS)
        except TimeoutError:
            self._signal_group(signal.SIGKILL)
            await process.wait()

    async def revoke(self) -> None:
        """Clear lease and settle any pending or active command."""
        self._lease_until = 0.0
        self._cancel_event.set()
        if self._decision is not None and not self._decision.done():
            self._decision.set_result("cancelled")
        await self._finished.wait()

    async def close(self) -> None:
        """Revoke current access and refuse future commands."""
        self._closed = True
        await self.revoke()
