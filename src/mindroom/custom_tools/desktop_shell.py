"""Unified remote desktop shell execution over the existing desktop wire protocol."""

from __future__ import annotations

import asyncio
import contextlib
import shlex
import time
import weakref
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from itertools import count
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from mindroom.desktop.client import DesktopRequestError, desktop_response_router
from mindroom.desktop.configuration import DesktopConfigurationState, DesktopConfigurationStatus
from mindroom.desktop.media import DesktopMediaError, download_encrypted_media
from mindroom.desktop.protocol import (
    MAX_COMMAND_TTL_MS,
    DesktopAction,
    DesktopCommand,
    DesktopProtocolError,
    DesktopResponse,
    EncryptedDesktopMedia,
)
from mindroom.matrix.olm_to_device import OlmToDeviceError, PinnedMatrixDevice
from mindroom.shell.execution import (
    ShellOutputBuffer,
    ShellOutputProgress,
    format_shell_success,
    parse_shell_args,
    shell_call_timeout,
)
from mindroom.shell.output_capture import (
    CapturedShellStream,
    ShellOutputCapture,
    ShellOutputDestination,
    format_shell_completion,
    format_shell_timeout,
)
from mindroom.tool_jobs.control import current_job_id
from mindroom.tool_jobs.runtime import get_background_runtime
from mindroom.tool_system.output_files import ToolOutputFileHandled, current_tool_output_file_request
from mindroom.tool_system.progress import report_tool_metadata, report_tool_progress

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable

    from mindroom.tool_system.runtime_context import ToolRuntimeContext

_SHELL_START_TIMEOUT_SECONDS = MAX_COMMAND_TTL_MS / 1000
_MAX_SHELL_COMMAND_LENGTH = 8192
_MAX_LOCAL_PATH_LENGTH = 4096
_APPROVAL_WAIT_THRESHOLD_SECONDS = 2.5
_POLL_BACKOFF_SECONDS = (0.5, 0.5, 1.0, 1.5, 2.0)
_TIMEOUT_KILL_GRACE_DELAYS = (0.0, 0.25, 0.4, 0.85)
_FORCE_KILL_DRAIN_DELAY_SECONDS = 0.2
_RECOVERY_DELAYS_SECONDS = (0.5, 0.5, 1.0, 1.5, 2.0, 2.0, 2.0, 2.0)
_CANCEL_CLEANUP_TIMEOUT_SECONDS = 5.0
_CANCEL_REQUEST_TIMEOUT_SECONDS = 2.0
_CANCEL_STATUS_RETRY_DELAY_SECONDS = 0.5
_SHELL_ARGS_ERROR = (
    '\'args\' must be a shell command string or a flat list of strings. Send args like "ls -la" or ["git", "status"].'
)
_STDOUT_KEYS = ("output", "output_start", "next_offset", "output_bytes", "output_attachment")
_STDERR_KEYS = ("stderr", "stderr_start", "stderr_next_offset", "stderr_bytes", "stderr_attachment")
_TRANSPORT_ERRORS = (DesktopRequestError, DesktopProtocolError, OlmToDeviceError)


class _DesktopShellError(RuntimeError):
    """Unrecoverable remote desktop shell error."""


@dataclass
class _TargetStartLock:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    waiters: int = 0


_START_LOCKS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[PinnedMatrixDevice, _TargetStartLock]] = (
    weakref.WeakKeyDictionary()
)


@asynccontextmanager
async def _target_start_lock(target: PinnedMatrixDevice) -> AsyncIterator[None]:
    """Serialize ``run_shell`` starts per paired desktop target and clean up idle locks."""
    per_loop = _START_LOCKS.setdefault(asyncio.get_running_loop(), {})
    entry = per_loop.setdefault(target, _TargetStartLock())
    entry.waiters += 1
    try:
        async with entry.lock:
            yield
    finally:
        entry.waiters -= 1
        if entry.waiters == 0 and per_loop.get(target) is entry:
            per_loop.pop(target, None)


def _not_started(message: str) -> str:
    report_tool_metadata(not_started=True)
    return message


def _setup_required_error(error: str | None, *, chat_pairing: bool) -> str:
    if chat_pairing:
        return (
            f"Error: Desktop configuration is invalid: {error} Run `/desktop setup` to replace it."
            if error is not None
            else "Error: Desktop setup is required for this requester and agent. Run `/desktop setup` in this Matrix chat."
        )
    return (
        f"Error: Desktop configuration is invalid: {error}"
        if error is not None
        else "Error: Desktop setup requires a live Matrix agent chat. Run `/desktop setup` in a private Matrix room."
    )


def _validate_call_arguments(
    args: object,
    tail: object,
    timeout: object,
    cwd: object,
) -> tuple[str, int, float | None, str | None]:
    parsed = parse_shell_args(args)
    if isinstance(parsed, str):
        command = parsed
    else:
        if not parsed or not parsed[0]:
            raise ValueError(_SHELL_ARGS_ERROR)
        command = shlex.join(parsed)
    if len(command) > _MAX_SHELL_COMMAND_LENGTH:
        msg = f"Shell command must not exceed {_MAX_SHELL_COMMAND_LENGTH} characters."
        raise ValueError(msg)
    if isinstance(tail, bool) or not isinstance(tail, int) or tail < 0:
        msg = "tail must be a non-negative integer."
        raise ValueError(msg)
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0):
        msg = "timeout must be a positive number of seconds; omit it for the default limit."
        raise ValueError(msg)
    if cwd is not None and (not isinstance(cwd, str) or not cwd or len(cwd) > _MAX_LOCAL_PATH_LENGTH):
        msg = f"cwd must be a non-empty string of at most {_MAX_LOCAL_PATH_LENGTH} characters."
        raise ValueError(msg)
    return command, tail, float(timeout) if timeout is not None else None, cwd


async def _await_with_approval_status[T](coro: Awaitable[T], *, context: ToolRuntimeContext) -> T:
    """Pause the enclosing managed job's work timeout while waiting for local approval on the desktop."""
    task = asyncio.ensure_future(coro)
    try:
        done, _ = await asyncio.wait({task}, timeout=_APPROVAL_WAIT_THRESHOLD_SECONDS)
        if task in done:
            return await task
        job_id = current_job_id()
        runtime = get_background_runtime(context.runtime_paths) if job_id is not None else None
        if job_id is None or runtime is None:
            return await task
        async with runtime.human_decision_wait(job_id):
            return await task
    except BaseException:
        if not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        raise


def _unwrap_request_status(status_resp: DesktopResponse, request_id: str) -> DesktopResponse | None:
    """Return the recorded response, None if still queued/running, or raise ``_DesktopShellError``."""
    if not status_resp.ok:
        raise _DesktopShellError(status_resp.error or f"Desktop request_status failed for {request_id!r}.")
    state = status_resp.result.get("state")
    if state in {"queued", "running"}:
        return None
    if state == "completed" and "response" in status_resp.result:
        return DesktopResponse.from_content(status_resp.result["response"])
    if state == "unknown":
        raw_recorded = status_resp.result.get("response")
        if raw_recorded is not None:
            warning = DesktopResponse.from_content(raw_recorded).result.get("warning")
            if isinstance(warning, str) and warning:
                raise _DesktopShellError(warning)
        msg = f"Desktop shell request {request_id!r} ended with an unknown outcome; do not repeat it automatically."
        raise _DesktopShellError(msg)
    msg = (
        f"Desktop shell request {request_id!r} was not found on the desktop device."
        if state == "not_found"
        else f"Desktop shell request {request_id!r} reported unexpected state {state!r}."
    )
    raise _DesktopShellError(msg)


@dataclass(frozen=True, slots=True)
class _IngestOutcome:
    state: str
    exit_code: int | None
    has_more: bool
    bridge_truncated: bool
    advanced: bool
    warning: str | None


@dataclass
class _DesktopShellRunner:
    """Drive one ``run_desktop_shell`` call across ``run_shell``, ``check_shell``, ``kill_shell``, and recovery."""

    context: ToolRuntimeContext
    target: PinnedMatrixDevice
    requester_id: str
    agent_name: str
    transport_timeout: float
    capture: ShellOutputCapture | None
    session_id: str = field(default_factory=lambda: uuid4().hex)
    sequences: count[int] = field(default_factory=count)
    start_request_id: str | None = None
    handle: str | None = None
    split_stderr: bool = False
    stdout_buf: ShellOutputBuffer = field(default_factory=ShellOutputBuffer)
    stderr_buf: ShellOutputBuffer = field(default_factory=ShellOutputBuffer)
    stdout_offset: int = 0
    stderr_offset: int = 0
    progress: ShellOutputProgress = field(init=False)

    def __post_init__(self) -> None:
        self.progress = ShellOutputProgress(report_tool_progress, self.stdout_buf, self.stderr_buf)

    async def _send_raw(
        self,
        action: DesktopAction,
        parameters: dict[str, object],
        *,
        timeout_seconds: float,
    ) -> DesktopResponse:
        now_ms = round(time.time() * 1000)
        ttl_ms = max(1, min(MAX_COMMAND_TTL_MS, round(timeout_seconds * 1000)))
        command = DesktopCommand(
            request_id=uuid4().hex,
            session_id=self.session_id,
            sequence=next(self.sequences),
            issued_at_ms=now_ms,
            expires_at_ms=now_ms + ttl_ms,
            action=action,
            requester_id=self.requester_id,
            agent_name=self.agent_name,
            parameters=parameters,
        )
        if action == "run_shell":
            self.start_request_id = command.request_id
        return await desktop_response_router(self.context.client).request(
            self.target,
            command,
            timeout_seconds=timeout_seconds,
        )

    async def _recover_request(self, request_id: str) -> DesktopResponse:
        for attempt, delay in enumerate((0.0, *_RECOVERY_DELAYS_SECONDS)):
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                status_resp = await self._send_raw(
                    "request_status",
                    {"request_id": request_id},
                    timeout_seconds=self.transport_timeout,
                )
            except (DesktopRequestError, OlmToDeviceError) as exc:
                if attempt == len(_RECOVERY_DELAYS_SECONDS):
                    msg = f"Failed to recover desktop shell request {request_id!r}: {exc}"
                    raise _DesktopShellError(msg) from exc
                continue
            recovered = _unwrap_request_status(status_resp, request_id)
            if recovered is not None:
                return recovered
        msg = f"Desktop shell request {request_id!r} did not settle within the recovery window."
        raise _DesktopShellError(msg)

    async def _send(
        self,
        action: DesktopAction,
        parameters: dict[str, object],
        *,
        timeout_seconds: float | None = None,
    ) -> DesktopResponse:
        try:
            return await self._send_raw(
                action,
                parameters,
                timeout_seconds=self.transport_timeout if timeout_seconds is None else timeout_seconds,
            )
        except DesktopRequestError as exc:
            if exc.request_id is None:
                raise _DesktopShellError(str(exc)) from exc
            return await self._recover_request(exc.request_id)

    async def _start(self, command_str: str, *, start_wait: int, cwd: str | None) -> tuple[DesktopResponse, float]:
        params: dict[str, object] = {"command": command_str, "timeout_seconds": start_wait}
        if cwd is not None:
            params["cwd"] = cwd

        async def _locked_start() -> tuple[DesktopResponse, float]:
            async with _target_start_lock(self.target):
                sent_at = time.monotonic()
                response = await self._send("run_shell", params, timeout_seconds=_SHELL_START_TIMEOUT_SECONDS)
                return response, time.monotonic() - sent_at

        return await _await_with_approval_status(_locked_start(), context=self.context)

    def _check_parameters(self) -> dict[str, object]:
        assert self.handle is not None
        params: dict[str, object] = {"handle": self.handle, "offset": self.stdout_offset}
        if self.split_stderr:
            params["stderr_offset"] = self.stderr_offset
        return params

    async def _ingest_stream(
        self,
        result: dict[str, Any],
        keys: tuple[str, str, str, str, str],
        buf: ShellOutputBuffer,
        capture_stream: CapturedShellStream | None,
        current_offset: int,
        *,
        is_initial: bool,
        state: str,
    ) -> tuple[int, bool, bool]:
        text_key, start_key, next_key, bytes_key, attachment_key = keys
        raw_attachment, raw_next, raw_bytes = result.get(attachment_key), result.get(next_key), result.get(bytes_key)
        if raw_attachment is not None:
            media = EncryptedDesktopMedia.from_content(raw_attachment, kind="output_attachment")
            downloaded = await download_encrypted_media(
                self.context.client,
                media,
                timeout_seconds=self.transport_timeout,
            )
            text = downloaded.decode("utf-8", errors="replace")
            new_offset = raw_next if isinstance(raw_next, int) else current_offset + len(downloaded)
        else:
            raw_start = result.get(start_key, 0)
            start = raw_start if isinstance(raw_start, int) else 0
            if is_initial and state == "running" and start > 0:
                return 0, True, False
            inline = result.get(text_key)
            text = inline if start == current_offset and isinstance(inline, str) else ""
            new_offset = (
                (raw_next if isinstance(raw_next, int) else current_offset + len(text.encode("utf-8")))
                if start == current_offset
                else current_offset
            )
        if text:
            buf.append(text)
            if capture_stream is not None:
                capture_stream.append(text)
        return new_offset, isinstance(raw_bytes, int) and new_offset < raw_bytes, new_offset > current_offset

    async def _ingest(self, response: DesktopResponse, *, is_initial: bool) -> _IngestOutcome:
        if not response.ok:
            raise _DesktopShellError(response.error or "Desktop device rejected the shell request.")
        result = response.result
        if result.get("action_outcome") == "unknown":
            warning = result.get("warning")
            raise _DesktopShellError(
                warning
                if isinstance(warning, str) and warning
                else "The desktop shell command outcome is unknown; do not repeat it automatically.",
            )
        if is_initial:
            self.split_stderr = "stderr_next_offset" in result
        state = str(result.get("state") or "completed")
        raw_handle = result.get("handle")
        if isinstance(raw_handle, str) and raw_handle:
            self.handle = raw_handle
        elif raw_handle is None and state != "running":
            self.handle = None

        raw_exit = result.get("exit_code")
        exit_code = raw_exit if isinstance(raw_exit, int) and not isinstance(raw_exit, bool) else None
        raw_warning = result.get("warning")
        warning = raw_warning if isinstance(raw_warning, str) and raw_warning else None
        streams = [
            ("stdout_offset", _STDOUT_KEYS, self.stdout_buf, self.capture.stdout if self.capture else None),
            *(
                [("stderr_offset", _STDERR_KEYS, self.stderr_buf, self.capture.stderr if self.capture else None)]
                if self.split_stderr
                else []
            ),
        ]
        has_more, advanced = False, False
        try:
            for offset_attr, keys, buf, cap_stream in streams:
                new_off, more, adv = await self._ingest_stream(
                    result,
                    keys,
                    buf,
                    cap_stream,
                    getattr(self, offset_attr),
                    is_initial=is_initial,
                    state=state,
                )
                setattr(self, offset_attr, new_off)
                has_more = has_more or more
                advanced = advanced or adv
        except (DesktopMediaError, DesktopProtocolError) as exc:
            if exit_code is not None:
                report_tool_metadata(exit_code=exit_code)
            suffix = f" (exit code {exit_code})" if exit_code is not None else ""
            msg = f"The desktop shell command finished{suffix}, but its full output could not be downloaded: {exc}"
            raise _DesktopShellError(msg) from exc

        bridge_truncated = bool(result.get("output_truncated")) or (
            self.split_stderr and bool(result.get("stderr_truncated"))
        )
        return _IngestOutcome(state, exit_code, has_more, bridge_truncated, advanced, warning)

    async def _check(self) -> _IngestOutcome:
        return await self._ingest(await self._send("check_shell", self._check_parameters()), is_initial=False)

    async def _drain_finished(self, outcome: _IngestOutcome) -> _IngestOutcome:
        while outcome.state != "running" and outcome.has_more and self.handle is not None:
            outcome = await self._check()
            if not outcome.advanced and outcome.has_more:
                break
        return outcome

    async def _safe_check_drain(self) -> tuple[bool, int | None]:
        if self.handle is None:
            return True, None
        try:
            outcome = await self._check()
        except (_DesktopShellError, DesktopProtocolError, OlmToDeviceError):
            return True, None
        if outcome.state == "running":
            return False, None
        with contextlib.suppress(_DesktopShellError, DesktopProtocolError, OlmToDeviceError):
            outcome = await self._drain_finished(outcome)
        return True, outcome.exit_code

    async def _kill_and_drain_for_timeout(self) -> int | None:
        if self.handle is None:
            return None
        with contextlib.suppress(_DesktopShellError, DesktopProtocolError, OlmToDeviceError):
            await self._send("kill_shell", {"handle": self.handle})
        for delay in _TIMEOUT_KILL_GRACE_DELAYS:
            if delay > 0:
                await asyncio.sleep(delay)
            stopped, exit_code = await self._safe_check_drain()
            if stopped:
                return exit_code
        if self.handle is None:
            return None
        with contextlib.suppress(_DesktopShellError, DesktopProtocolError, OlmToDeviceError):
            await self._send("kill_shell", {"handle": self.handle, "force": True})
        if _FORCE_KILL_DRAIN_DELAY_SECONDS > 0:
            await asyncio.sleep(_FORCE_KILL_DRAIN_DELAY_SECONDS)
        _, exit_code = await self._safe_check_drain()
        return exit_code

    async def _stop_on_timeout(self, *, tail: int, stop_after: float) -> str | ToolOutputFileHandled:
        exit_code = await self._kill_and_drain_for_timeout()
        if exit_code is not None:
            report_tool_metadata(exit_code=exit_code)
        message = format_shell_timeout(
            self.stdout_buf.render(tail=tail),
            self.stderr_buf.render() if self.stderr_buf else None,
            timeout=stop_after,
            no_output_file=self.capture is not None,
        )
        if self.capture is not None:
            self.capture.incomplete = True
            self.capture.interruption = message
            return ToolOutputFileHandled(self.capture.publish(exit_code))
        return message

    async def _recover_handle_on_cancel(self, deadline: float) -> str | None:
        recovered: str | None = None
        while self.start_request_id is not None and time.monotonic() < deadline:
            timeout_left = max(0.1, min(_CANCEL_REQUEST_TIMEOUT_SECONDS, deadline - time.monotonic()))
            try:
                status_resp = await self._send_raw(
                    "request_status",
                    {"request_id": self.start_request_id},
                    timeout_seconds=timeout_left,
                )
            except _TRANSPORT_ERRORS:
                break
            if not status_resp.ok:
                break
            state = status_resp.result.get("state")
            if state == "completed" and "response" in status_resp.result:
                with contextlib.suppress(DesktopProtocolError):
                    recorded = DesktopResponse.from_content(status_resp.result["response"])
                    raw_handle = recorded.result.get("handle")
                    if recorded.ok and recorded.result.get("state") == "running" and isinstance(raw_handle, str):
                        recovered = raw_handle or None
                break
            if state not in {"queued", "running"}:
                break
            sleep_for = min(_CANCEL_STATUS_RETRY_DELAY_SECONDS, max(0.0, deadline - time.monotonic()))
            if sleep_for <= 0:
                break
            await asyncio.sleep(sleep_for)
        return recovered

    async def cleanup_on_cancel(self) -> None:
        deadline = time.monotonic() + _CANCEL_CLEANUP_TIMEOUT_SECONDS

        def remaining() -> float:
            return max(0.1, min(_CANCEL_REQUEST_TIMEOUT_SECONDS, deadline - time.monotonic()))

        if self.handle is None:
            recovered = await self._recover_handle_on_cancel(deadline)
            if recovered is not None and time.monotonic() < deadline:
                with contextlib.suppress(*_TRANSPORT_ERRORS):
                    await self._send_raw(
                        "kill_shell",
                        {"handle": recovered, "force": True},
                        timeout_seconds=remaining(),
                    )
            return

        if time.monotonic() >= deadline:
            return
        kill_ok = False
        with contextlib.suppress(*_TRANSPORT_ERRORS):
            kill_resp = await self._send_raw("kill_shell", {"handle": self.handle}, timeout_seconds=remaining())
            kill_ok = kill_resp.ok
            if kill_resp.ok and kill_resp.result.get("state") == "completed":
                return
        if kill_ok and deadline - time.monotonic() > 1.0:
            await asyncio.sleep(0.25)
            with contextlib.suppress(*_TRANSPORT_ERRORS):
                check_resp = await self._send_raw("check_shell", self._check_parameters(), timeout_seconds=remaining())
                if check_resp.ok and check_resp.result.get("state") != "running":
                    return
        if time.monotonic() < deadline:
            with contextlib.suppress(*_TRANSPORT_ERRORS):
                await self._send_raw("kill_shell", {"handle": self.handle, "force": True}, timeout_seconds=remaining())

    def _format_finished(self, outcome: _IngestOutcome, *, tail: int) -> str | ToolOutputFileHandled:
        exit_code = outcome.exit_code
        if exit_code is not None:
            report_tool_metadata(exit_code=exit_code)
        if self.capture is not None:
            self.capture.stdout.reached_eof = True
            self.capture.stderr.reached_eof = True
            if outcome.bridge_truncated or outcome.has_more:
                self.capture.incomplete = True
                self.capture.interruption = (
                    outcome.warning
                    or "Desktop shell output was truncated before full delivery; no complete output file was saved."
                )
            return ToolOutputFileHandled(self.capture.publish(exit_code))
        if exit_code is None:
            return "Error: Desktop shell command ended without an exit code."
        if exit_code != 0:
            errors = self.stderr_buf.render() if self.stderr_buf else None
            return format_shell_completion(self.stdout_buf.render(tail=tail), errors, return_code=exit_code)
        success = format_shell_success(self.stdout_buf, self.stderr_buf, tail)
        if outcome.state == "killed":
            return f"Command was killed on the desktop.\n{success}" if success else "Command was killed on the desktop."
        return success

    async def execute(
        self,
        command_str: str,
        *,
        tail: int,
        timeout: float | None,  # noqa: ASYNC109
        cwd: str | None,
    ) -> str | ToolOutputFileHandled:
        stop_after = shell_call_timeout(timeout)
        start_wait = 1 if (timeout is not None and timeout <= 1) else 2
        start_response, start_elapsed = await self._start(command_str, start_wait=start_wait, cwd=cwd)
        outcome = await self._ingest(start_response, is_initial=True)
        if outcome.state != "running":
            return self._format_finished(await self._drain_finished(outcome), tail=tail)

        self.progress.poll()
        deadline = (
            None
            if stop_after is None
            else time.monotonic() + max(0.0, stop_after - min(start_elapsed, float(start_wait)))
        )
        poll_index = 0
        while outcome.state == "running":
            if self.handle is None:
                msg = "Desktop shell command is still running but returned no handle."
                raise _DesktopShellError(msg)
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    assert stop_after is not None
                    return await self._stop_on_timeout(tail=tail, stop_after=stop_after)
                delay = 0.0 if outcome.has_more else min(_POLL_BACKOFF_SECONDS[poll_index], remaining)
            else:
                delay = 0.0 if outcome.has_more else _POLL_BACKOFF_SECONDS[poll_index]
            if delay > 0:
                await asyncio.sleep(delay)
            if not outcome.has_more and poll_index + 1 < len(_POLL_BACKOFF_SECONDS):
                poll_index += 1
            outcome = await self._check()
            if outcome.state == "running":
                if outcome.advanced:
                    poll_index = 0
                self.progress.poll()
        return self._format_finished(await self._drain_finished(outcome), tail=tail)


def _capture_error(capture: ShellOutputCapture | None, message: str) -> str | ToolOutputFileHandled:
    if capture is None:
        return f"Error: {message}"
    capture.incomplete = True
    capture.interruption = message
    return ToolOutputFileHandled(capture.publish(None))


def _open_capture() -> ShellOutputCapture | None:
    output_request = current_tool_output_file_request()
    if output_request is None or output_request.path is None:
        return None
    return ShellOutputCapture(
        ShellOutputDestination(
            workspace_root=str(output_request.policy.workspace_root),
            path=output_request.path.requested_path,
            max_bytes=output_request.policy.max_bytes,
            tools_use_workdir=output_request.policy.tools_use_workdir,
        ),
    )


async def run_desktop_shell_command(
    *,
    args: list[str] | str,
    tail: int,
    timeout: int | None,  # noqa: ASYNC109
    cwd: str | None,
    context: ToolRuntimeContext | None,
    credential_scope: tuple[str, str] | None,
    configuration: DesktopConfigurationState,
) -> str | ToolOutputFileHandled:
    """Run one shell command on the paired desktop with native ``run_shell_command`` semantics."""
    if configuration.status is not DesktopConfigurationStatus.READY or configuration.target is None:
        return _not_started(_setup_required_error(configuration.error, chat_pairing=credential_scope is not None))
    if context is None:
        return _not_started("Error: Desktop shell requires a live Matrix runtime context.")
    assert credential_scope is not None
    requester_id, agent_name = credential_scope

    try:
        command_str, validated_tail, validated_timeout, validated_cwd = _validate_call_arguments(
            args,
            tail,
            timeout,
            cwd,
        )
    except ValueError as exc:
        return _not_started(f"Error: {exc}")

    try:
        capture = _open_capture()
    except Exception as exc:
        return f"Error: {exc}"

    runner = _DesktopShellRunner(
        context=context,
        target=configuration.target,
        requester_id=requester_id,
        agent_name=agent_name,
        transport_timeout=configuration.timeout_seconds,
        capture=capture,
    )
    try:
        return await runner.execute(command_str, tail=validated_tail, timeout=validated_timeout, cwd=validated_cwd)
    except asyncio.CancelledError:
        with contextlib.suppress(BaseException):
            await asyncio.shield(
                asyncio.wait_for(runner.cleanup_on_cancel(), timeout=_CANCEL_CLEANUP_TIMEOUT_SECONDS),
            )
        raise
    except (_DesktopShellError, DesktopProtocolError, OlmToDeviceError) as exc:
        return _capture_error(capture, str(exc))
    finally:
        if capture is not None:
            capture.close()
