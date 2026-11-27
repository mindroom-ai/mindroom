"""Locally approved desktop shell commands run through MindRoom's shell engine."""

from __future__ import annotations

import asyncio
import json
import math
import re
import shutil
import tempfile
import time
import unicodedata
import uuid
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from mindroom.desktop.protocol import MAX_SHELL_OUTPUT_BYTES
from mindroom.shell.output import SpoolCapture, SpoolStream
from mindroom.shell.registry import MAX_BACKGROUNDED, BackgroundProcess, ProcessRegistry

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

_INLINE_WAIT_SAFETY_SECONDS = 10.0
_MAX_COMMAND = 8_192
# Letters, marks, and symbols that render as nothing or as empty space: every default-ignorable code point outside
# the control and format categories, which approval surfaces already escape, plus two blank symbols.
_INVISIBLE_CHARACTERS = (
    "\u034f\u115f\u1160\u17b4\u17b5\u180b-\u180d\u180f\u2800\u3164\ufe00-\ufe0f\uffa0\U0001d159\U000e0100-\U000e01ef"
)
INVISIBLE_CHARACTER = re.compile(f"[{_INVISIBLE_CHARACTERS}]")
# Longer runs of whitespace or invisible characters could push part of a request out of an approver's view.
_BLANK_RUN = re.compile(rf"[\s{_INVISIBLE_CHARACTERS}]+")
_MAX_BLANK_RUN = 64
_MAX_BLANK_LINES = 2
# More combining marks on one character can be drawn over neighbouring text.
_MAX_COMBINING_MARKS = 8
# Synthetic events posted just before a request appears must reach their target before anyone can answer it.
_AGENT_INPUT_SETTLE_SECONDS = 1.0
_COMMAND_PREVIEW_CHARS = 200
# Equal to the engine's background limit, so a start is refused before approval rather than killed later.
_MAX_HANDLES = MAX_BACKGROUNDED
_UNKNOWN_HANDLE = "Unknown shell handle."
_NOT_STARTED = "The local shell request was cancelled before approval; the command did not run."
_STOPPED = "The local shell command was stopped before it finished; it may have partially run."
_INPUT_WHILE_PENDING = (
    "A shell command is waiting for local approval, so desktop input is paused until the person at the "
    "computer answers it."
)


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
    controller_key: str = ""
    max_runtime_seconds: float | None = None


def _check_stream_offset(stream: SpoolStream, offset: int, *, label: str) -> None:
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        message = f"Shell {label} offset must be a nonnegative integer."
        raise DesktopShellError(message)
    size = stream.size
    if offset > size:
        message = f"Shell {label} offset is past the captured {label} ({size} bytes)."
        raise DesktopShellError(message)
    # UTF-8 continuation bytes are 0b10xxxxxx; every other byte starts a character.
    if offset < size and stream.pread(offset, 1)[0] & 0xC0 == 0x80:
        message = f"Shell {label} offset must be at the start of a UTF-8 character."
        raise DesktopShellError(message)


@dataclass(frozen=True)
class DesktopShellResult:
    """One command's state; a finished result's output stays behind its handle until ``DesktopShell.hand_over``.

    ``killed`` means ``kill_shell`` or ``max_runtime_seconds`` stopped the command; ``completed`` means it exited on
    its own.
    """

    state: Literal["completed", "killed", "running"]
    handle: str | None
    exit_code: int | None
    output: SpoolCapture
    timed_out: bool = False


@dataclass(frozen=True)
class _ShellHandle:
    requester_id: str
    agent_name: str
    command: str
    started_at: float
    output: SpoolCapture
    namespace: str
    killed: bool = False
    controller_key: str = ""
    request_id: str = ""
    finished_at: float | None = None
    return_code: int | None = None
    timed_out: bool = False

    @property
    def state(self) -> Literal["completed", "killed", "running"]:
        if self.finished_at is None:
            return "running"
        return "killed" if (self.killed or self.timed_out) else "completed"


def _validate_max_runtime_seconds(value: object) -> None:
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value <= 0
    ):
        message = "Shell max_runtime_seconds must be a positive finite number."
        raise DesktopShellError(message)


def _validate_command(command: object) -> None:
    """Accept only a bounded command that every approval surface can show in full."""
    if not isinstance(command, str) or not command.strip() or "\x00" in command:
        message = "Shell command must be nonempty and contain no NUL."
        raise DesktopShellError(message)
    if len(command) > _MAX_COMMAND:
        message = "Shell command is too long."
        raise DesktopShellError(message)
    _reject_padding(command, "Shell command")


def _reject_padding(text: str, field: str) -> None:
    if any(len(run) > _MAX_BLANK_RUN or run.count("\n") > _MAX_BLANK_LINES + 1 for run in _BLANK_RUN.findall(text)):
        message = (
            f"{field} must not contain more than {_MAX_BLANK_RUN} whitespace or invisible characters or "
            f"{_MAX_BLANK_LINES} blank lines in a row, which could hide part of the request from the approver."
        )
        raise DesktopShellError(message)
    marks = 0
    for character in text:
        marks = marks + 1 if unicodedata.category(character).startswith("M") else 0
        if marks > _MAX_COMBINING_MARKS:
            message = (
                f"{field} must not stack more than {_MAX_COMBINING_MARKS} combining marks on one character, "
                "which could hide part of the request from the approver."
            )
            raise DesktopShellError(message)


class DesktopShell:
    """Require a local decision or live local lease before each command starts, then track its handle."""

    def __init__(
        self,
        *,
        environment: Mapping[str, str],
        clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._environment = dict(environment)
        self._clock = clock
        self._monotonic_clock = monotonic_clock
        self._leases: dict[str, float] = {}
        self._pending: DesktopShellRequest | None = None
        self._admitted_request: DesktopShellRequest | None = None
        # True from the moment a request needs a decision, including while it is held back behind agent input.
        self._awaiting_decision = False
        self._decision: asyncio.Future[str] | None = None
        self._active_controller_key: str | None = None
        self._active_request_id: str | None = None
        self._active_owner: tuple[str, str, str] | None = None
        self._cancel_event = asyncio.Event()
        self._finished = asyncio.Event()
        self._finished.set()
        self._agent_inputs = 0
        self._agent_input_idle = asyncio.Event()
        self._agent_input_idle.set()
        self._agent_input_ended_at = -math.inf
        self._used_ids: set[tuple[str, str]] = set()
        self._registry = ProcessRegistry()
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
        if not isinstance(request.controller_key, str) or "\x00" in request.controller_key:
            message = "Shell request identity is invalid."
            raise DesktopShellError(message)
        _validate_command(request.command)
        # Only syntax is checked before approval, so the remote caller learns nothing about local directories.
        if not isinstance(request.cwd, str) or "\x00" in request.cwd or not Path(request.cwd).is_absolute():
            message = "Shell working directory must be an absolute path."
            raise DesktopShellError(message)
        _reject_padding(request.cwd, "Shell working directory")
        if (
            not isinstance(request.timeout_seconds, int)
            or isinstance(request.timeout_seconds, bool)
            or not 1 <= request.timeout_seconds <= 60
        ):
            message = "Shell timeout must be an integer from 1 to 60 seconds."
            raise DesktopShellError(message)
        _validate_max_runtime_seconds(request.max_runtime_seconds)
        if not isinstance(request.expires_at_ms, int) or isinstance(request.expires_at_ms, bool):
            message = "Shell approval expiry is invalid."
            raise DesktopShellError(message)
        remaining = request.expires_at_ms / 1000 - self._clock()
        if remaining <= 0:
            message = "Shell approval expired."
            raise DesktopShellError(message)
        return self._monotonic_clock() + remaining

    def _effective_lease(self, controller_key: str) -> float:
        return self._leases.get(controller_key, 0.0)

    def _lease_summary(self, lease_until: float) -> tuple[int, bool]:
        until_revoked = lease_until == math.inf
        # Rounded up, so a live lease never reads as zero seconds.
        remaining = 0 if until_revoked else max(0, math.ceil(lease_until - self._monotonic_clock()))
        return remaining, until_revoked

    def controller_status(self, controller_key: str) -> dict[str, object]:
        """Report auto-approval state for one exact controller."""
        remaining, until_revoked = self._lease_summary(self._effective_lease(controller_key))
        return {
            "auto_approve_remaining_seconds": remaining,
            "auto_approve_until_revoked": until_revoked,
        }

    def status(
        self,
        *,
        caller: tuple[str, str] | None = None,
        controller_key: str | None = None,
    ) -> dict[str, object]:
        """Describe pending approval, active command, auto-approval, and every retained handle.

        ``caller``, given as (requester_id, agent_name), hides ``active_request_id`` unless the
        active command belongs to that exact caller; omit it for the local, unrestricted view.
        """
        pending = self._pending
        effective_lease = (
            self._effective_lease(controller_key)
            if controller_key is not None
            else max(self._leases.values(), default=0.0)
        )
        remaining, until_revoked = self._lease_summary(effective_lease)
        if caller is None:
            owns_active = True
        elif controller_key is not None:
            owns_active = self._active_owner == (controller_key, caller[0], caller[1])
        else:
            owns_active = self._active_owner is not None and self._active_owner[1:] == caller
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
            "auto_approve_remaining_seconds": remaining,
            "auto_approve_until_revoked": until_revoked,
            "active_request_id": self._active_request_id if owns_active else None,
            "handles": self.handles(controller_key=controller_key),
        }

    def handles(
        self,
        requester_id: str | None = None,
        agent_name: str | None = None,
        *,
        controller_key: str | None = None,
    ) -> list[dict[str, object]]:
        """List retained handles, optionally scoped to one requester, agent, and/or controller."""
        self._sync_all_handles()
        entries: list[dict[str, object]] = []
        for handle, entry in self._handles.items():
            if requester_id is not None and (entry.requester_id, entry.agent_name) != (requester_id, agent_name):
                continue
            if controller_key is not None and entry.controller_key != controller_key:
                continue
            ended_at = entry.finished_at if entry.finished_at is not None else time.monotonic()
            entries.append(
                {
                    "handle": handle,
                    "requester_id": entry.requester_id,
                    "agent_name": entry.agent_name,
                    "command_preview": entry.command[:_COMMAND_PREVIEW_CHARS],
                    "elapsed_seconds": round(max(0.0, ended_at - entry.started_at), 1),
                    "state": entry.state,
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
        if approved and (auto_approve_until_revoked or auto_approve_seconds):
            lease_until = math.inf if auto_approve_until_revoked else self._monotonic_clock() + auto_approve_seconds
            self._leases[pending.controller_key] = lease_until
        decision.set_result("approved" if approved else "denied")
        # The reply to this decision already shows the request as settled, so no approval surface keeps it live.
        self._pending = None

    def grant(
        self,
        duration_seconds: int | None = None,
        *,
        until_revoked: bool = False,
        controller_key: str | None = None,
    ) -> None:
        """Auto-approve one controller (or the unscoped default key) for a bounded duration or until revoked."""
        if self._closed:
            message = "Local shell is closed."
            raise DesktopShellError(message)
        if until_revoked is True and duration_seconds is None:
            lease_until = math.inf
        elif (
            until_revoked is False
            and isinstance(duration_seconds, int)
            and not isinstance(duration_seconds, bool)
            and 60 <= duration_seconds <= 3600
        ):
            lease_until = self._monotonic_clock() + duration_seconds
        else:
            message = "Grant 60 to 3600 seconds or until revoked."
            raise DesktopShellError(message)
        if controller_key is None:
            self._leases[""] = lease_until
            return
        if not isinstance(controller_key, str) or not controller_key.strip():
            message = "Shell controller key must be non-empty text."
            raise DesktopShellError(message)
        self._leases[controller_key] = lease_until

    @contextmanager
    def agent_input(self) -> Iterator[None]:
        """Run agent-driven desktop input only while no request awaits approval, holding new approvals back.

        Synthetic pointer and keyboard input could otherwise press an approval card's buttons or answer
        the terminal prompt.
        """
        if self._awaiting_decision:
            raise DesktopShellError(_INPUT_WHILE_PENDING)
        self._agent_inputs += 1
        self._agent_input_idle.clear()
        try:
            yield
        finally:
            self._agent_inputs -= 1
            if not self._agent_inputs:
                self._agent_input_ended_at = self._monotonic_clock()
                self._agent_input_idle.set()

    async def _await_approval(self, request: DesktopShellRequest, deadline: float) -> str:
        # New agent input is refused from here on; input already in flight finishes and its events get time to
        # reach their target before anyone can see, and answer, this request.
        self._awaiting_decision = True
        try:
            await self._agent_input_idle.wait()
            settle = self._agent_input_ended_at + _AGENT_INPUT_SETTLE_SECONDS - self._monotonic_clock()
            await asyncio.sleep(max(0.0, settle))
            if self._cancel_event.is_set():
                return "cancelled"
            if self._monotonic_clock() >= deadline:
                message = "Shell approval expired."
                raise DesktopShellError(message)
            self._pending = request
            self._pending_deadline = deadline
            self._decision = asyncio.get_running_loop().create_future()
            return await asyncio.wait_for(self._decision, max(0, deadline - self._monotonic_clock()))
        except TimeoutError as exc:
            message = "Shell approval expired."
            raise DesktopShellError(message) from exc
        finally:
            self._awaiting_decision = False
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
        used_key = (request.controller_key, request.request_id)
        if used_key in self._used_ids:
            message = "Shell request ID has already been used."
            raise DesktopShellError(message)
        self._check_handle_capacity()
        self._used_ids.add(used_key)
        self._admitted_request = request
        self._active_controller_key = request.controller_key
        self._busy = True
        self._finished.clear()
        self._cancel_event.clear()
        return deadline

    def _oldest_finished_handle(self) -> str | None:
        self._sync_all_handles()
        if len(self._handles) < _MAX_HANDLES:
            return None
        finished = [
            (entry.finished_at, handle) for handle, entry in self._handles.items() if entry.finished_at is not None
        ]
        if not finished:
            message = "Too many local shell commands are running; kill one before starting another."
            raise DesktopShellError(message)
        return min(finished)[1]

    def _check_handle_capacity(self) -> None:
        self._oldest_finished_handle()

    def _reserve_handle_capacity(self) -> None:
        oldest = self._oldest_finished_handle()
        if oldest is not None:
            self._discard(oldest)

    async def execute(self, request: DesktopShellRequest) -> DesktopShellResult:
        """Admit one request, await local approval if needed, then run it until it completes or becomes a handle."""
        deadline = self._admit(request)
        try:
            if self._monotonic_clock() >= self._effective_lease(request.controller_key):
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
            if not Path(request.cwd).is_dir():
                message = "Shell working directory must be an existing directory; the command did not run."
                raise DesktopShellError(message)
            self._reserve_handle_capacity()
            self._active_request_id = request.request_id
            self._active_owner = (request.controller_key, request.requester_id, request.agent_name)
            # Reply before the remote caller stops waiting; a longer command continues as a handle.
            remaining = deadline - self._monotonic_clock() - _INLINE_WAIT_SAFETY_SECONDS
            return await self._run(request, max(0.0, min(request.timeout_seconds, remaining)))
        finally:
            self._pending = None
            self._admitted_request = None
            self._decision = None
            self._active_controller_key = None
            self._active_request_id = None
            self._active_owner = None
            self._busy = False
            self._finished.set()

    async def _run(self, request: DesktopShellRequest, inline_wait: float) -> DesktopShellResult:
        if request.max_runtime_seconds is not None:
            inline_wait = min(inline_wait, request.max_runtime_seconds)
        self._sync_all_handles()
        spool = SpoolCapture(max_bytes=MAX_SHELL_OUTPUT_BYTES, directory=self._spool_directory())
        transferred = False
        started_at = time.monotonic()
        namespace = json.dumps([request.controller_key, request.requester_id, request.agent_name])
        try:
            launch = (
                self._registry.spawn_background
                if inline_wait <= 0
                else partial(self._registry.run_or_background, timeout=inline_wait)
            )
            run = asyncio.create_task(
                launch(
                    namespace=namespace,
                    argv=["/bin/sh", "-c", request.command],
                    env=self._environment,
                    cwd=request.cwd,
                    # The spool carries output; the engine's line tail is unused.
                    tail=0,
                    spool=spool,
                    stdin=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                    kill_group_after_exit=True,
                    max_runtime_seconds=request.max_runtime_seconds,
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
            if result.execution_error or result.background_rejected:
                raise DesktopShellError(result.message.removeprefix("Error: "))
            entry = _ShellHandle(
                request.requester_id,
                request.agent_name,
                request.command,
                started_at,
                spool,
                namespace,
                controller_key=request.controller_key,
                request_id=request.request_id,
            )
            if result.handle is None:
                # Completed inline without registering a background record in ProcessRegistry.
                transferred = True
                if self._cancel_event.is_set():
                    return DesktopShellResult("completed", None, result.return_code, spool)
                handle = self._new_handle()
                self._handles[handle] = replace(entry, finished_at=time.monotonic(), return_code=result.return_code)
                return DesktopShellResult("completed", handle, result.return_code, spool)
            record = self._registry.get_process(namespace, result.handle)
            if self._cancel_event.is_set():
                # Revocation raced the new handle's registration and has already killed and forgotten it.
                self._registry.discard(result.handle)
                if record is None or not record.finished:
                    raise DesktopShellError(_STOPPED)
                transferred = True
                state: Literal["completed", "killed"] = "killed" if record.timed_out else "completed"
                return DesktopShellResult(state, None, record.return_code, spool, timed_out=record.timed_out)
            self._handles[result.handle] = entry
            entry, _ = self._sync_handle(result.handle)
            transferred = True
            return DesktopShellResult(
                entry.state,
                result.handle,
                entry.return_code,
                spool,
                timed_out=entry.finished_at is not None and entry.timed_out,
            )
        finally:
            if not transferred:
                spool.close()

    def check(
        self,
        requester_id: str,
        agent_name: str,
        handle: str,
        *,
        offset: int | None = None,
        stderr_offset: int | None = None,
        controller_key: str | None = None,
    ) -> DesktopShellResult:
        """Report the caller's own handle; a finished one stays until ``hand_over`` confirms its output arrived.

        An invalid output ``offset`` or ``stderr_offset`` is rejected without changing the handle, so a corrected check
        still gets the output.
        """
        entry = self._caller_handle(requester_id, agent_name, handle, controller_key=controller_key)
        if offset is not None:
            _check_stream_offset(entry.output.stdout, offset, label="output")
        if stderr_offset is not None:
            _check_stream_offset(entry.output.stderr, stderr_offset, label="stderr")
        if entry.finished_at is None:
            return DesktopShellResult("running", handle, None, entry.output)
        return DesktopShellResult(entry.state, handle, entry.return_code, entry.output, timed_out=entry.timed_out)

    def hand_over(self, result: DesktopShellResult) -> None:
        """Forget a finished result's handle and release its output once the rest of it reached the caller."""
        if result.handle is not None and result.handle in self._handles:
            self._registry.discard(result.handle)
            self._handles.pop(result.handle)
        result.output.close()

    def kill(
        self,
        requester_id: str,
        agent_name: str,
        handle: str,
        *,
        force: bool = False,
        controller_key: str | None = None,
    ) -> Literal["killed", "completed"]:
        """Signal the caller's own running handle, keeping its output for a later check that reports it killed."""
        self._caller_handle(requester_id, agent_name, handle, controller_key=controller_key)
        return self._signal(handle, force=force)

    def cancel_request(
        self,
        requester_id: str,
        agent_name: str,
        request_id: str,
        *,
        controller_key: str | None = None,
    ) -> Literal["cancelled", "killed", "completed", "not_found"]:
        """Cancel a caller's pending approval or stop its active/retained command by ``request_id``."""

        def _matches(req_id: str, req_user: str, req_agent: str, req_controller: str) -> bool:
            return (
                req_id == request_id
                and (req_user, req_agent) == (requester_id, agent_name)
                and (controller_key is None or req_controller == controller_key)
            )

        pending = self._pending
        decision = self._decision
        if (
            pending is not None
            and decision is not None
            and not decision.done()
            and _matches(pending.request_id, pending.requester_id, pending.agent_name, pending.controller_key)
        ):
            decision.set_result("cancelled")
            self._pending = None
            return "cancelled"
        admitted = self._admitted_request
        if (
            self._awaiting_decision
            and admitted is not None
            and _matches(admitted.request_id, admitted.requester_id, admitted.agent_name, admitted.controller_key)
        ):
            self._cancel_event.set()
            return "cancelled"
        if (
            self._active_request_id is not None
            and self._active_owner is not None
            and _matches(
                self._active_request_id,
                self._active_owner[1],
                self._active_owner[2],
                self._active_owner[0],
            )
        ):
            self._cancel_event.set()
            return "killed"
        self._sync_all_handles()
        for handle, entry in self._handles.items():
            if _matches(entry.request_id, entry.requester_id, entry.agent_name, entry.controller_key):
                return self._signal(handle, force=True)
        return "not_found"

    def kill_handle(self, handle: str) -> None:
        """Force-kill any caller's handle from the local management channel; its owner's check reports it killed."""
        if handle not in self._handles:
            raise DesktopShellError(_UNKNOWN_HANDLE)
        self._signal(handle, force=True)

    def _new_handle(self) -> str:
        while (handle := f"shell:{uuid.uuid4().hex[:8]}") in self._handles:
            pass
        return handle

    def _sync_handle(self, handle: str) -> tuple[_ShellHandle, BackgroundProcess | None]:
        entry = self._handles[handle]
        if entry.finished_at is not None:
            return entry, None
        record = self._registry.get_process(entry.namespace, handle)
        if record is None or not record.finished:
            return entry, record
        entry = replace(
            entry,
            finished_at=record.finished_at or time.monotonic(),
            return_code=record.return_code,
            timed_out=record.timed_out,
        )
        self._handles[handle] = entry
        self._registry.discard(handle)
        return entry, None

    def _sync_all_handles(self) -> None:
        for handle in list(self._handles):
            self._sync_handle(handle)

    def _signal(self, handle: str, *, force: bool) -> Literal["killed", "completed"]:
        entry, record = self._sync_handle(handle)
        # Only a delivered signal makes the handle killed; a process that exited first ended on its own.
        if (
            record is not None
            and record.process.returncode is None
            and self._registry.signal(entry.namespace, handle, force=force).outcome == "signalled"
        ):
            entry = replace(entry, killed=True)
            self._handles[handle] = entry
        # An earlier kill stays the reason it ended, matching what the next check reports.
        return "killed" if (entry.killed or entry.timed_out) else "completed"

    def _caller_handle(
        self,
        requester_id: str,
        agent_name: str,
        handle: str,
        *,
        controller_key: str | None = None,
    ) -> _ShellHandle:
        entry = self._handles.get(handle)
        if (
            entry is None
            or (entry.requester_id, entry.agent_name) != (requester_id, agent_name)
            or (controller_key is not None and entry.controller_key != controller_key)
        ):
            raise DesktopShellError(_UNKNOWN_HANDLE)
        synced, _ = self._sync_handle(handle)
        return synced

    def _discard(self, handle: str) -> None:
        self._registry.discard(handle)
        self._handles.pop(handle).output.close()

    def _spool_directory(self) -> str:
        if self._directory is None:
            self._directory = tempfile.mkdtemp(prefix="mindroom-desktop-shell-")
        return self._directory

    def revoke(self, controller_key: str | None = None) -> None:
        """Clear auto-approval, reject pending approval, stop the active command, and kill handles.

        When ``controller_key`` is given, only that controller's auto-approval, pending/active command,
        and handles are affected; other controllers' leases and running handles remain untouched.
        """
        if controller_key is None:
            self._leases.clear()
            self._cancel_event.set()
            if self._decision is not None and not self._decision.done():
                self._decision.set_result("cancelled")
            self._registry.kill_all()
            for entry in self._handles.values():
                entry.output.close()
            self._handles.clear()
            return
        if not isinstance(controller_key, str) or not controller_key.strip():
            message = "Shell controller key must be non-empty text."
            raise DesktopShellError(message)
        self._leases.pop(controller_key, None)
        if self._busy and self._active_controller_key == controller_key:
            self._cancel_event.set()
            if self._decision is not None and not self._decision.done():
                self._decision.set_result("cancelled")
        for handle in [h for h, entry in self._handles.items() if entry.controller_key == controller_key]:
            self._discard(handle)

    async def close(self) -> None:
        """Revoke current access, wait for the active command to stop, and refuse future commands."""
        self._closed = True
        self.revoke()
        await self._finished.wait()
        if self._directory is not None:
            shutil.rmtree(self._directory, ignore_errors=True)
            self._directory = None
