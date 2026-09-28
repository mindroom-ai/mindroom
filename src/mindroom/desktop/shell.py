"""Locally approved desktop shell commands run through MindRoom's shell engine."""

from __future__ import annotations

import asyncio
import json
import math
import os
import shutil
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from mindroom.desktop.protocol import MAX_SHELL_OUTPUT_BYTES
from mindroom.shell_execution import (
    MAX_BACKGROUNDED,
    ProcessRecord,
    discard_background_record,
    kill_all_records,
    kill_command,
    run_command,
)
from mindroom.shell_output_capture import ShellOutputCapture, ShellOutputDestination

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

_INLINE_WAIT_SAFETY_SECONDS = 10.0
_MIN_INLINE_WAIT_SECONDS = 1.0
_MAX_COMMAND = 8_192
_COMMAND_PREVIEW_CHARS = 200
# Equal to the engine's background limit, so a start is refused before approval rather than killed later.
_MAX_HANDLES = MAX_BACKGROUNDED
_UNKNOWN_HANDLE = "Unknown shell handle."
_NOT_STARTED = "The local shell request was cancelled before approval; the command did not run."
_STOPPED = "The local shell command was stopped before it finished; it may have partially run."


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


class DesktopShellOutput(ShellOutputCapture):
    """Spool one command's combined output privately until the bridge transfers it."""

    def __init__(self, directory: str) -> None:
        super().__init__(
            ShellOutputDestination(workspace_root=directory, path="", max_bytes=MAX_SHELL_OUTPUT_BYTES),
            None,
        )
        self.exit_code: int | None = None
        self.completed = False

    @property
    def size(self) -> int:
        """Return the retained UTF-8 bytes, at most the capture cap."""
        self.stdout.file.flush()
        return os.fstat(self.stdout.file.fileno()).st_size

    @property
    def truncated(self) -> bool:
        """Report output dropped past the cap or lost to a capture error."""
        return self.stdout.error is not None

    def read(self, offset: int = 0, max_bytes: int | None = None) -> bytes:
        """Return retained output from byte *offset*, at most *max_bytes* of it.

        ``pread`` leaves the file position where the engine's next write appends.
        """
        size = self.size
        return os.pread(self.stdout.file.fileno(), size - offset if max_bytes is None else max_bytes, offset)

    def tail(self, max_bytes: int) -> bytes:
        """Return at most the newest *max_bytes* bytes; the first character may be partial."""
        return self.read(max(0, self.size - max_bytes))

    def check_offset(self, offset: int) -> None:
        """Reject an offset that is negative, past the retained output, or inside a character."""
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            message = "Shell output offset must be a nonnegative integer."
            raise DesktopShellError(message)
        size = self.size
        if offset > size:
            message = f"Shell output offset is past the captured output ({size} bytes)."
            raise DesktopShellError(message)
        # UTF-8 continuation bytes are 0b10xxxxxx; every other byte starts a character.
        if offset < size and self.read(offset, 1)[0] & 0xC0 == 0x80:
            message = "Shell output offset must be at the start of a UTF-8 character."
            raise DesktopShellError(message)

    def publish(self, return_code: int | None) -> str:
        """Record completion; the bridge transfers the spool instead of writing a workspace file."""
        self.exit_code = return_code
        self.completed = True
        return ""

    def close(self) -> None:
        """Release on cancellation or eviction; after completion the spool waits for transfer."""
        if not self.completed:
            self.release()

    def release(self) -> None:
        """Remove the spool after transfer or handle cleanup."""
        super().close()


@dataclass(frozen=True)
class DesktopShellResult:
    """One command's state; a finished result hands its output to the caller to transfer and release.

    ``killed`` means ``kill_shell`` stopped the command; ``completed`` means it exited on its own.
    """

    state: Literal["completed", "killed", "running"]
    handle: str | None
    exit_code: int | None
    output: DesktopShellOutput


@dataclass(frozen=True)
class _ShellHandle:
    requester_id: str
    agent_name: str
    command: str
    started_at: float
    output: DesktopShellOutput
    killed: bool = False

    def state(self, record: ProcessRecord) -> Literal["completed", "killed", "running"]:
        if not record.finished:
            return "running"
        return "killed" if self.killed else "completed"


class DesktopShell:
    """Require a local decision or live local lease before each command starts, then track its handle."""

    def __init__(
        self,
        *,
        environment: Mapping[str, str],
        clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not hasattr(os, "killpg"):
            message = "This platform cannot stop local process groups safely."
            raise DesktopShellError(message)
        self._environment = dict(environment)
        self._clock = clock
        self._monotonic_clock = monotonic_clock
        self._lease_until = 0.0
        self._pending: DesktopShellRequest | None = None
        self._decision: asyncio.Future[str] | None = None
        self._active_request_id: str | None = None
        self._active_owner: tuple[str, str] | None = None
        self._cancel_event = asyncio.Event()
        self._finished = asyncio.Event()
        self._finished.set()
        self._used_ids: set[str] = set()
        self._records: dict[str, ProcessRecord] = {}
        self._handles: dict[str, _ShellHandle] = {}
        self._directory: str | None = None
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

    def status(self, *, caller: tuple[str, str] | None = None) -> dict[str, object]:
        """Describe pending approval, active command, auto-approval, and every retained handle.

        ``caller``, given as (requester_id, agent_name), hides ``active_request_id`` unless the
        active command belongs to that exact caller; omit it for the local, unrestricted view.
        """
        pending = self._pending
        until_revoked = self._lease_until == math.inf
        owns_active = caller is None or caller == self._active_owner
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
            # Rounded up, so a live lease never reads as zero seconds.
            "auto_approve_remaining_seconds": (
                0 if until_revoked else max(0, math.ceil(self._lease_until - self._monotonic_clock()))
            ),
            "auto_approve_until_revoked": until_revoked,
            "active_request_id": self._active_request_id if owns_active else None,
            "handles": self.handles(),
        }

    def handles(self, requester_id: str | None = None, agent_name: str | None = None) -> list[dict[str, object]]:
        """List retained handles, optionally only those owned by one exact requester and agent."""
        self._prune()
        entries: list[dict[str, object]] = []
        for handle, entry in self._handles.items():
            if requester_id is not None and (entry.requester_id, entry.agent_name) != (requester_id, agent_name):
                continue
            record = self._records[handle]
            ended_at = record.finished_at if record.finished_at is not None else time.monotonic()
            entries.append(
                {
                    "handle": handle,
                    "requester_id": entry.requester_id,
                    "agent_name": entry.agent_name,
                    "command_preview": entry.command[:_COMMAND_PREVIEW_CHARS],
                    "elapsed_seconds": round(max(0.0, ended_at - entry.started_at), 1),
                    "state": entry.state(record),
                },
            )
        return entries

    def decide(
        self,
        command_id: str,
        *,
        approved: bool,
        auto_approve_seconds: int = 0,
        auto_approve_until_revoked: bool = False,
    ) -> None:
        """Settle only the exact pending request ID from the local UI."""
        pending = self._pending
        decision = self._decision
        if self._closed or pending is None or decision is None or decision.done() or pending.request_id != command_id:
            message = "No matching pending shell command."
            raise DesktopShellError(message)
        if not isinstance(approved, bool) or not isinstance(auto_approve_until_revoked, bool):
            message = "Shell decision must be boolean."
            raise DesktopShellError(message)
        if (
            not isinstance(auto_approve_seconds, int)
            or isinstance(auto_approve_seconds, bool)
            or (auto_approve_seconds != 0 and not 60 <= auto_approve_seconds <= 3600)
            or (auto_approve_seconds and auto_approve_until_revoked)
        ):
            message = "Auto-approval must be 0 or 60 to 3600 seconds, or until revoked."
            raise DesktopShellError(message)
        if self._monotonic_clock() >= self._pending_deadline:
            message = "Shell approval expired."
            raise DesktopShellError(message)
        if approved and auto_approve_until_revoked:
            self._lease_until = math.inf
        elif approved and auto_approve_seconds:
            self._lease_until = self._monotonic_clock() + auto_approve_seconds
        decision.set_result("approved" if approved else "denied")

    def grant(self, duration_seconds: int | None = None, *, until_revoked: bool = False) -> None:
        """Auto-approve every locally allowed caller for a bounded duration or until revoked or stopped."""
        if self._closed:
            message = "Local shell is closed."
            raise DesktopShellError(message)
        if until_revoked is True and duration_seconds is None:
            self._lease_until = math.inf
            return
        if (
            until_revoked is not False
            or not isinstance(duration_seconds, int)
            or isinstance(duration_seconds, bool)
            or not 60 <= duration_seconds <= 3600
        ):
            message = "Grant 60 to 3600 seconds or until revoked."
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
            message = "Another local shell command is still awaiting approval or its first result."
            raise DesktopShellError(message)
        deadline = self._validate_request(request)
        if request.request_id in self._used_ids:
            message = "Shell request ID has already been used."
            raise DesktopShellError(message)
        self._reserve_handle_capacity()
        self._used_ids.add(request.request_id)
        self._busy = True
        self._finished.clear()
        self._cancel_event.clear()
        return deadline

    def _reserve_handle_capacity(self) -> None:
        self._prune()
        if len(self._handles) < _MAX_HANDLES:
            return
        finished = [(record.finished_at or 0.0, handle) for handle, record in self._records.items() if record.finished]
        if not finished:
            message = "Too many local shell commands are running; kill one before starting another."
            raise DesktopShellError(message)
        self._discard(min(finished)[1])

    async def execute(self, request: DesktopShellRequest) -> DesktopShellResult:
        """Admit one request, await local approval if needed, then run it until it completes or becomes a handle."""
        deadline = self._admit(request)
        try:
            if self._monotonic_clock() >= self._lease_until:
                outcome = await self._await_approval(request, deadline)
                if outcome == "denied":
                    message = "Shell command denied locally."
                    raise DesktopShellError(message)
                if outcome == "cancelled":
                    raise DesktopShellError(_NOT_STARTED)
                if self._monotonic_clock() >= deadline:
                    message = "Shell approval expired."
                    raise DesktopShellError(message)
            if self._monotonic_clock() >= deadline:
                message = "Shell request expired."
                raise DesktopShellError(message)
            if self._cancel_event.is_set():
                raise DesktopShellError(_NOT_STARTED)
            self._active_request_id = request.request_id
            self._active_owner = (request.requester_id, request.agent_name)
            # Reply before the remote caller stops waiting; a longer command continues as a handle.
            remaining = deadline - self._monotonic_clock() - _INLINE_WAIT_SAFETY_SECONDS
            return await self._run(request, max(_MIN_INLINE_WAIT_SECONDS, min(request.timeout_seconds, remaining)))
        finally:
            self._pending = None
            self._decision = None
            self._active_request_id = None
            self._active_owner = None
            self._busy = False
            self._finished.set()

    async def _run(self, request: DesktopShellRequest, inline_wait: float) -> DesktopShellResult:
        output = DesktopShellOutput(self._spool_directory())
        started_at = time.monotonic()
        run = asyncio.create_task(
            run_command(
                self._records,
                namespace=json.dumps([request.requester_id, request.agent_name]),
                argv=["/bin/sh", "-c", request.command],
                env=self._environment,
                cwd=request.cwd,
                # The spool carries output; the engine's line tail is unused.
                tail=0,
                timeout=inline_wait,
                output_capture=output,
                stdin=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.STDOUT,
                kill_group_after_exit=True,
            ),
        )
        cancelled = asyncio.create_task(self._cancel_event.wait())
        try:
            await asyncio.wait({run, cancelled}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            cancelled.cancel()
            if not run.done():
                run.cancel()
                # The engine stops the process group within its bounded grace before this settles.
                with suppress(asyncio.CancelledError):
                    await run
        if run.cancelled():
            raise DesktopShellError(_STOPPED)
        result = run.result()
        self._prune()
        if result.handle is not None:
            if self._cancel_event.is_set():
                # Revocation raced the new handle's registration and has already killed it.
                output.release()
                raise DesktopShellError(_STOPPED)
            self._handles[result.handle] = _ShellHandle(
                request.requester_id,
                request.agent_name,
                request.command,
                started_at,
                output,
            )
            return DesktopShellResult("running", result.handle, None, output)
        if not output.completed:
            output.release()
            raise DesktopShellError(result.message.removeprefix("Error: "))
        return DesktopShellResult("completed", None, output.exit_code, output)

    def check(
        self,
        requester_id: str,
        agent_name: str,
        handle: str,
        *,
        offset: int | None = None,
    ) -> DesktopShellResult:
        """Report the caller's own handle; a finished (completed or killed) handle is handed over once and forgotten.

        An invalid output ``offset`` is rejected before the hand-over, so a corrected check still gets the output.
        """
        record = self._caller_record(requester_id, agent_name, handle)
        if offset is not None:
            self._handles[handle].output.check_offset(offset)
        if not record.finished:
            return DesktopShellResult("running", handle, None, self._handles[handle].output)
        self._records.pop(handle)
        entry = self._handles.pop(handle)
        return DesktopShellResult(entry.state(record), handle, record.return_code, entry.output)

    def kill(
        self,
        requester_id: str,
        agent_name: str,
        handle: str,
        *,
        force: bool = False,
    ) -> Literal["killed", "completed"]:
        """Signal the caller's own running handle, keeping its output for a later check that reports it killed."""
        return self._signal(self._caller_record(requester_id, agent_name, handle), handle, force=force)

    def kill_handle(self, handle: str) -> None:
        """Force-kill any caller's handle from the local management channel; its owner's check reports it killed."""
        self._prune()
        if handle not in self._handles:
            raise DesktopShellError(_UNKNOWN_HANDLE)
        self._signal(self._records[handle], handle, force=True)

    def _signal(self, record: ProcessRecord, handle: str, *, force: bool) -> Literal["killed", "completed"]:
        if record.process.returncode is not None:
            # An earlier kill stays the reason it ended, matching what the next check reports.
            return "killed" if self._handles[handle].killed else "completed"
        kill_command(self._records, namespace=record.namespace, handle=handle, force=force)
        self._handles[handle] = replace(self._handles[handle], killed=True)
        return "killed"

    def _caller_record(self, requester_id: str, agent_name: str, handle: str) -> ProcessRecord:
        self._prune()
        entry = self._handles.get(handle)
        if entry is None or (entry.requester_id, entry.agent_name) != (requester_id, agent_name):
            raise DesktopShellError(_UNKNOWN_HANDLE)
        return self._records[handle]

    def _discard(self, handle: str) -> None:
        discard_background_record(self._records, handle)
        self._handles.pop(handle).output.release()

    def _prune(self) -> None:
        # The engine drops finished records after ten minutes; release the output they left behind.
        for handle in self._handles.keys() - self._records.keys():
            self._handles.pop(handle).output.release()

    def _spool_directory(self) -> str:
        if self._directory is None:
            self._directory = tempfile.mkdtemp(prefix="mindroom-desktop-shell-")
        return self._directory

    def revoke(self) -> None:
        """Clear auto-approval, reject pending approval, stop the active command, and kill every handle."""
        self._lease_until = 0.0
        self._cancel_event.set()
        if self._decision is not None and not self._decision.done():
            self._decision.set_result("cancelled")
        kill_all_records(self._records)
        for entry in self._handles.values():
            entry.output.release()
        self._handles.clear()

    async def close(self) -> None:
        """Revoke current access, wait for the active command to stop, and refuse future commands."""
        self._closed = True
        self.revoke()
        await self._finished.wait()
        if self._directory is not None:
            shutil.rmtree(self._directory, ignore_errors=True)
            self._directory = None
