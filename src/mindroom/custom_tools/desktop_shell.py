"""Unified remote desktop shell execution over the existing desktop wire protocol."""

from __future__ import annotations

import asyncio
import math
import time
import weakref
from contextlib import suppress
from itertools import count
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from mindroom.desktop import protocol
from mindroom.desktop.client import DesktopRequestError, desktop_response_router
from mindroom.desktop.configuration import DesktopConfigurationState, DesktopConfigurationStatus
from mindroom.desktop.media import DesktopMediaError, download_encrypted_media
from mindroom.desktop.protocol import DesktopAction, DesktopCommand, DesktopResponse
from mindroom.matrix.olm_to_device import OlmToDeviceError, PinnedMatrixDevice
from mindroom.shell import execution, output_capture
from mindroom.shell.execution import shell_call_timeout
from mindroom.shell.output_capture import format_shell_completion, format_shell_timeout
from mindroom.tool_jobs.control import current_job_id
from mindroom.tool_jobs.runtime import get_background_runtime
from mindroom.tool_system.output_files import ToolOutputFileHandled, current_tool_output_file_request
from mindroom.tool_system.progress import report_tool_metadata, report_tool_progress

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from mindroom.tool_system.runtime_context import ToolRuntimeContext

_APPROVAL_WAIT_THRESHOLD_SECONDS = 2.5
_POLL_BACKOFF_SECONDS = (0.5, 0.5, 1.0, 2.0, 4.0, 8.0, 10.0)
_TIMEOUT_KILL_GRACE_DELAYS = (0.0, 0.25, 0.4, 0.85)
_BOUNDED_KILL_GRACE_DELAY_SECONDS = 0.25
_FORCE_KILL_DRAIN_DELAY_SECONDS = 0.2
_RECOVERY_DELAYS_SECONDS = (0.5, 0.5, 1.0, 1.5, 2.0, 2.0, 2.0, 2.0)
_CANCEL_CLEANUP_TIMEOUT_SECONDS = 5.0
_CANCEL_RETRY_DELAYS = (0.0, 0.5, 0.5, 1.0)
_TRANSPORT_ERRORS = (DesktopRequestError, protocol.DesktopProtocolError, OlmToDeviceError)
_STREAM_SPECS = (
    ("output", "output_start", "next_offset", "output_bytes", "output_attachment"),
    ("stderr", "stderr_start", "stderr_next_offset", "stderr_bytes", "stderr_attachment"),
)
_TRUNCATION_NOTICE = (
    f"[Desktop shell output exceeded the {protocol.MAX_SHELL_OUTPUT_BYTES // (1024 * 1024)} MiB "
    "capture limit; later output was dropped.]"
)
_OUTDATED_HELPER_ERROR = (
    "The connected MindRoom desktop helper is outdated and does not support this shell request; "
    "update the desktop helper and try again."
)

type _Params = dict[str, object]
type _Result = str | ToolOutputFileHandled
type _LoopLocks = weakref.WeakValueDictionary[PinnedMatrixDevice, asyncio.Lock]
_START_LOCKS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _LoopLocks] = weakref.WeakKeyDictionary()


class _DesktopShellError(RuntimeError):
    """Unrecoverable remote desktop shell error."""


def desktop_setup_error(error: str | None, *, chat: bool) -> str:
    """Return the setup or invalid-configuration message shared by desktop tools."""
    if error is not None:
        suffix = " Run `/desktop setup` to replace it." if chat else ""
        return f"Desktop configuration is invalid: {error}{suffix}"
    if chat:
        return "Desktop setup is required for this requester and agent. Run `/desktop setup` in this Matrix chat."
    return "Desktop setup requires a live Matrix agent chat."


def _not_started(message: str, *, has_output_path: bool = False) -> _Result:
    report_tool_metadata(not_started=True)
    return ToolOutputFileHandled(message) if has_output_path else message


def _with_truncation_notice(message: str, *, truncated: bool) -> str:
    if not truncated:
        return message
    return f"{_TRUNCATION_NOTICE}\n{message}" if message else _TRUNCATION_NOTICE


def _is_expired(deadline: float | None, margin: float = 0.0) -> bool:
    return deadline is not None and deadline - time.monotonic() <= margin


def _is_state(response: DesktopResponse | None, state: str) -> bool:
    return bool(response is not None and response.ok and response.result.get("state") == state)


def _nonempty_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _validate_args(args: object, tail: object, limit: object, cwd: object) -> tuple[str, int, float | None, str | None]:
    command = execution.shell_args_to_command(args)
    if len(command) > 8192:
        msg = "Shell command must not exceed 8192 characters."
        raise ValueError(msg)
    if isinstance(tail, bool) or not isinstance(tail, int) or tail < 0:
        msg = "tail must be a non-negative integer."
        raise ValueError(msg)
    if limit is not None and (
        isinstance(limit, bool) or not isinstance(limit, int | float) or not math.isfinite(limit) or limit <= 0
    ):
        msg = "timeout must be a positive number of seconds; omit it for the default limit."
        raise ValueError(msg)
    if cwd is not None and (not isinstance(cwd, str) or not cwd or len(cwd) > 4096):
        msg = "cwd must be a non-empty string of at most 4096 characters."
        raise ValueError(msg)
    return command, tail, float(limit) if limit is not None else None, cwd


async def _await_with_approval_status[T](coro: Awaitable[T], *, context: ToolRuntimeContext) -> T:
    """Pause the enclosing managed job's work timeout while waiting for local approval on the desktop."""
    task = asyncio.ensure_future(coro)
    try:
        done, _ = await asyncio.wait({task}, timeout=_APPROVAL_WAIT_THRESHOLD_SECONDS)
        job_id = None if task in done else current_job_id()
        runtime = get_background_runtime(context.runtime_paths) if job_id is not None else None
        if job_id is not None and runtime is not None:
            async with runtime.human_decision_wait(job_id):
                return await task
        return await task
    except BaseException:
        if not task.done():
            task.cancel()
            with suppress(BaseException):
                await task
        raise


def _unwrap_request_status(response: DesktopResponse, request_id: str) -> DesktopResponse | None:
    """Return the recorded response, None if still queued/running, or raise ``_DesktopShellError``."""
    if not response.ok:
        raise _DesktopShellError(response.error or f"Desktop request_status failed for {request_id!r}.")
    state = response.result.get("state")
    if state in {"queued", "running"}:
        return None
    if state == "completed" and "response" in response.result:
        return DesktopResponse.from_content(response.result["response"])
    if state == "unknown":
        raw = response.result.get("response")
        warning = _nonempty_str(DesktopResponse.from_content(raw).result.get("warning")) if raw is not None else None
        fallback = (
            f"Desktop shell request {request_id!r} ended with an unknown outcome; do not repeat it automatically."
        )
        raise _DesktopShellError(warning or fallback)
    detail = "was not found on the desktop device." if state == "not_found" else f"reported unexpected state {state!r}."
    msg = f"Desktop shell request {request_id!r} {detail}"
    raise _DesktopShellError(msg)


class _DesktopShellRunner:
    """Drive one ``run_desktop_shell`` call across ``run_shell``, ``check_shell``, ``kill_shell``, and recovery."""

    def __init__(
        self,
        context: ToolRuntimeContext,
        configuration: DesktopConfigurationState,
        scope: tuple[str, str],
        capture: output_capture.ShellOutputCapture | None = None,
    ) -> None:
        assert configuration.target is not None
        self.context = context
        self.target = configuration.target
        self.transport_timeout = configuration.timeout_seconds
        self.scope = scope
        self.capture = capture
        self.session_id = uuid4().hex
        self.sequences = count()
        self.start_request_id: str | None = None
        self.handle: str | None = None
        self.state = "completed"
        self.truncated = False
        self.timed_out = False
        self.exit_code: int | None = None
        self.warning: str | None = None
        self.buffers = (execution.ShellOutputBuffer(), execution.ShellOutputBuffer())
        self.offsets = [0, 0]

    async def _raw(self, action: DesktopAction, params: _Params, wait: float) -> DesktopResponse:
        now = round(time.time() * 1000)
        expires_at_ms = now + max(1, min(protocol.MAX_COMMAND_TTL_MS, round(wait * 1000)))
        request_id = uuid4().hex
        if action == "run_shell":
            self.start_request_id = request_id
        seq = next(self.sequences)
        command = DesktopCommand(request_id, self.session_id, seq, now, expires_at_ms, action, *self.scope, params)
        return await desktop_response_router(self.context.client).request(self.target, command, timeout_seconds=wait)

    async def _send(self, action: DesktopAction, params: _Params, wait: float | None = None) -> DesktopResponse:
        try:
            return await self._raw(action, params, self.transport_timeout if wait is None else wait)
        except DesktopRequestError as exc:
            if action == "run_shell" and exc.action_outcome in {"not_sent", "not_started"}:
                report_tool_metadata(not_started=True)
            if exc.request_id is None:
                raise _DesktopShellError(str(exc)) from exc
            return await self._recover_request(exc.request_id)

    async def _recover_request(self, request_id: str) -> DesktopResponse:
        for attempt, delay in enumerate((0.0, *_RECOVERY_DELAYS_SECONDS)):
            await self._sleep(delay)
            try:
                status_response = await self._raw("request_status", {"request_id": request_id}, self.transport_timeout)
            except (DesktopRequestError, OlmToDeviceError) as exc:
                if attempt == len(_RECOVERY_DELAYS_SECONDS):
                    msg = f"Failed to recover desktop shell request {request_id!r}: {exc}"
                    raise _DesktopShellError(msg) from exc
            else:
                if (recovered := _unwrap_request_status(status_response, request_id)) is not None:
                    return recovered
        msg = f"Desktop shell request {request_id!r} did not settle within the recovery window."
        raise _DesktopShellError(msg)

    async def _request_optional(
        self,
        action: DesktopAction,
        params: _Params,
        *,
        deadline: float | None = None,
    ) -> DesktopResponse | None:
        try:
            if deadline is None:
                return await self._send(action, params)
            remaining = deadline - time.monotonic()
            return None if remaining <= 0 else await self._raw(action, params, min(2.0, remaining))
        except (*_TRANSPORT_ERRORS, _DesktopShellError):
            return None

    async def _sleep(self, seconds: float, deadline: float | None = None) -> None:
        delay = seconds if deadline is None else min(seconds, deadline - time.monotonic())
        if delay > 0:
            await asyncio.sleep(delay)

    async def _start(self, params: _Params) -> tuple[DesktopResponse, float]:
        start_timeout = protocol.MAX_COMMAND_TTL_MS / 1000

        async def _locked() -> tuple[DesktopResponse, float]:
            per_loop = _START_LOCKS.setdefault(asyncio.get_running_loop(), weakref.WeakValueDictionary())
            lock = per_loop.get(self.target) or asyncio.Lock()
            per_loop[self.target] = lock
            async with lock:
                sent_at = time.monotonic()
                response = await self._send("run_shell", params, start_timeout)
                if not response.ok and "Unexpected desktop parameters:" in (response.error or ""):
                    report_tool_metadata(not_started=True)
                    raise _DesktopShellError(_OUTDATED_HELPER_ERROR)
                return response, time.monotonic() - sent_at

        return await _await_with_approval_status(_locked(), context=self.context)

    def _check_params(self) -> _Params:
        assert self.handle is not None
        return {"handle": self.handle, "offset": self.offsets[0], "stderr_offset": self.offsets[1]}

    async def _ingest_stream(self, result: dict[str, Any], stream_index: int, *, initial: bool) -> tuple[bool, bool]:
        text_key, start_key, next_key, bytes_key, attachment_key = _STREAM_SPECS[stream_index]
        current_offset = self.offsets[stream_index]
        raw_next = result.get(next_key)
        raw_bytes = result.get(bytes_key)
        if (raw_attachment := result.get(attachment_key)) is not None:
            media = protocol.EncryptedDesktopMedia.from_content(raw_attachment, kind="output_attachment")
            data = await download_encrypted_media(self.context.client, media, timeout_seconds=self.transport_timeout)
            text = data.decode("utf-8", errors="replace")
            byte_len = len(data)
        else:
            raw_start = result.get(start_key, 0)
            start = raw_start if isinstance(raw_start, int) else 0
            if initial and self.state == "running" and start > 0:
                self.offsets[stream_index] = 0
                return True, False
            if start != current_offset:
                return isinstance(raw_bytes, int) and current_offset < raw_bytes, False
            inline = result.get(text_key)
            text = inline if isinstance(inline, str) else ""
            byte_len = len(text.encode())
        new_offset = raw_next if isinstance(raw_next, int) else current_offset + byte_len
        if text:
            self.buffers[stream_index].append(text)
            if self.capture is not None:
                (self.capture.stdout if stream_index == 0 else self.capture.stderr).append(text)
        self.offsets[stream_index] = new_offset
        return isinstance(raw_bytes, int) and new_offset < raw_bytes, new_offset > current_offset

    async def _ingest(self, response: DesktopResponse, *, initial: bool) -> tuple[bool, bool]:
        if not response.ok:
            raise _DesktopShellError(response.error or "Desktop device rejected the shell request.")
        result = response.result
        if result.get("action_outcome") == "unknown":
            fallback = "The desktop shell command outcome is unknown; do not repeat it automatically."
            raise _DesktopShellError(_nonempty_str(result.get("warning")) or fallback)
        self.state = str(result.get("state") or "completed")
        self.timed_out = self.timed_out or bool(result.get("timed_out"))
        if handle := _nonempty_str(result.get("handle")):
            self.handle = handle
        elif result.get("handle") is None and self.state != "running":
            self.handle = None
        raw_exit = result.get("exit_code")
        self.exit_code = raw_exit if isinstance(raw_exit, int) and not isinstance(raw_exit, bool) else None
        self.warning = _nonempty_str(result.get("warning"))
        try:
            stdout_more, stdout_advanced = await self._ingest_stream(result, 0, initial=initial)
            stderr_more, stderr_advanced = await self._ingest_stream(result, 1, initial=initial)
        except (DesktopMediaError, protocol.DesktopProtocolError) as exc:
            if self.exit_code is not None:
                report_tool_metadata(exit_code=self.exit_code)
            suffix = f" (exit code {self.exit_code})" if self.exit_code is not None else ""
            msg = (
                f"The desktop shell command finished{suffix}, but its full output could not be downloaded: {exc}. "
                f'Recover it without re-running the command via desktop(action="request_status", request_id={response.request_id!r}).'
            )
            raise _DesktopShellError(msg) from exc
        self.truncated = bool(result.get("output_truncated") or result.get("stderr_truncated"))
        return stdout_more or stderr_more, stdout_advanced or stderr_advanced

    async def _check(self) -> tuple[bool, bool]:
        return await self._ingest(await self._send("check_shell", self._check_params()), initial=False)

    async def _drain_finished(self, has_more: bool) -> bool:
        while self.state != "running" and has_more and self.handle is not None:
            has_more, advanced = await self._check()
            if not advanced and has_more:
                break
        return has_more

    async def _check_stopped(self, *, deadline: float | None = None, drain: bool = False) -> bool | None:
        response = await self._request_optional("check_shell", self._check_params(), deadline=deadline)
        if response is None or not response.ok:
            return None
        if not drain:
            return not _is_state(response, "running")
        try:
            has_more, _ = await self._ingest(response, initial=False)
        except (*_TRANSPORT_ERRORS, _DesktopShellError):
            return None
        if self.state == "running":
            return False
        with suppress(*_TRANSPORT_ERRORS, _DesktopShellError):
            await self._drain_finished(has_more)
        return True

    async def _stop_handle(self, handle: str, *, deadline: float | None = None, drain: bool = False) -> bool:
        """Stop ``handle`` via SIGTERM -> grace checks -> SIGKILL -> final check."""
        completed = False
        grace_delays = _TIMEOUT_KILL_GRACE_DELAYS if deadline is None else (_BOUNDED_KILL_GRACE_DELAY_SECONDS,)
        for force, delays, margin in ((False, grace_delays, 1.0), (True, (_FORCE_KILL_DRAIN_DELAY_SECONDS,), 0.0)):
            params: _Params = {"handle": handle, "force": True} if force else {"handle": handle}
            response = await self._request_optional("kill_shell", params, deadline=deadline)
            completed = completed or _is_state(response, "completed")
            if completed and not drain:
                return True
            if not drain and (response is None or not response.ok):
                continue
            for delay in delays:
                if _is_expired(deadline, margin=margin):
                    break
                await self._sleep(delay, deadline)
                stopped = await self._check_stopped(deadline=deadline, drain=drain)
                if stopped is True or (stopped is None and completed):
                    return True
                if stopped is None:
                    break
        return completed

    async def cleanup_on_cancel(self) -> None:
        """Stop any running handle or dismiss a pending approval when the caller cancels."""
        deadline = time.monotonic() + _CANCEL_CLEANUP_TIMEOUT_SECONDS
        if self.handle is not None:
            await self._stop_handle(self.handle, deadline=deadline)
            return
        if self.start_request_id is None:
            return
        params: _Params = {"request_id": self.start_request_id}
        for delay in _CANCEL_RETRY_DELAYS:
            if _is_expired(deadline):
                return
            await self._sleep(delay, deadline)
            if not _is_state(await self._request_optional("kill_shell", params, deadline=deadline), "not_found"):
                return

    def _interrupt_or_return(self, message: str, exit_code: int | None = None) -> _Result:
        if self.capture is None:
            return message
        self.capture.incomplete = True
        self.capture.interruption = message
        return ToolOutputFileHandled(self.capture.publish(exit_code))

    def _format_timeout_result(self, *, tail: int, timeout_seconds: float, confirmed: bool) -> _Result:
        finished = self.state != "running"
        exit_code = self.exit_code if finished else None
        if exit_code is not None:
            report_tool_metadata(exit_code=exit_code)
        stdout = self.buffers[0].render(tail=tail)
        stderr = self.buffers[1].render() if self.buffers[1] else None
        message = format_shell_timeout(stdout, stderr, timeout=timeout_seconds, no_output_file=self.capture is not None)
        if not confirmed:
            unconfirmed = (
                f"Timed out after {timeout_seconds:g}s, but stopping the remote command could not be confirmed; "
                "it may still be running on the desktop."
            )
            message = unconfirmed + message.removeprefix(f"Stopped after {timeout_seconds:g}s.")
        notice = _with_truncation_notice(message, truncated=finished and self.truncated)
        return self._interrupt_or_return(notice, exit_code)

    def _format_finished(self, *, has_more: bool, tail: int) -> _Result:
        exit_code = self.exit_code
        if exit_code is not None:
            report_tool_metadata(exit_code=exit_code)
        if self.capture is not None:
            self.capture.stdout.reached_eof = True
            self.capture.stderr.reached_eof = True
            if self.truncated or has_more:
                fallback = "Desktop shell output was truncated before full delivery; no complete output file was saved."
                return self._interrupt_or_return(self.warning or fallback, exit_code)
            return ToolOutputFileHandled(self.capture.publish(exit_code))
        if exit_code is None:
            return "Error: Desktop shell command ended without an exit code."
        if exit_code != 0:
            errors = self.buffers[1].render() if self.buffers[1] else None
            body = format_shell_completion(self.buffers[0].render(tail=tail), errors, return_code=exit_code)
        else:
            body = execution.format_shell_success(self.buffers[0], self.buffers[1], tail)
            if self.state == "killed":
                body = f"Command was killed on the desktop.\n{body}" if body else "Command was killed on the desktop."
        return _with_truncation_notice(body, truncated=self.truncated)

    async def execute(self, command: str, tail: int, timeout_limit: float | None, cwd: str | None) -> _Result:
        """Start, poll, drain, and format one desktop shell command."""
        stop_after = shell_call_timeout(timeout_limit)
        start_wait = 1 if (timeout_limit is not None and timeout_limit <= 1) else 2
        params: _Params = {"command": command, "timeout_seconds": start_wait}
        if cwd is not None:
            params["cwd"] = cwd
        if stop_after is not None:
            params["max_runtime_seconds"] = stop_after
        started_at = time.monotonic()
        start_response, start_elapsed = await self._start(params)
        has_more, _ = await self._ingest(start_response, initial=True)
        deadline = (
            None if stop_after is None else time.monotonic() + max(0.0, stop_after - min(start_elapsed, start_wait))
        )
        progress = execution.ShellOutputProgress(report_tool_progress, *self.buffers)
        poll_index = 0
        while self.state == "running":
            progress.poll()
            if self.handle is None:
                msg = "Desktop shell command is still running but returned no handle."
                raise _DesktopShellError(msg)
            if _is_expired(deadline):
                assert stop_after is not None
                confirmed = await self._stop_handle(self.handle, drain=True)
                return self._format_timeout_result(tail=tail, timeout_seconds=stop_after, confirmed=confirmed)
            if not has_more:
                await self._sleep(_POLL_BACKOFF_SECONDS[poll_index], deadline)
                poll_index = min(poll_index + 1, len(_POLL_BACKOFF_SECONDS) - 1)
            has_more, advanced = await self._check()
            if self.state == "running" and advanced:
                poll_index = 0
        has_more = await self._drain_finished(has_more)
        if self.timed_out:
            timeout_seconds = (
                stop_after if stop_after is not None else round(max(0.0, time.monotonic() - started_at), 1)
            )
            return self._format_timeout_result(tail=tail, timeout_seconds=timeout_seconds, confirmed=True)
        return self._format_finished(has_more=has_more, tail=tail)


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
    output_request = current_tool_output_file_request()
    has_output_path = output_request is not None and output_request.path is not None
    if configuration.status is not DesktopConfigurationStatus.READY or configuration.target is None:
        setup_error = f"Error: {desktop_setup_error(configuration.error, chat=credential_scope is not None)}"
        return _not_started(setup_error, has_output_path=has_output_path)
    if context is None:
        context_error = "Error: Desktop shell requires a live Matrix runtime context."
        return _not_started(context_error, has_output_path=has_output_path)
    assert credential_scope is not None
    try:
        command_str, tail_lines, timeout_limit, cwd_str = _validate_args(args, tail, timeout, cwd)
        capture: output_capture.ShellOutputCapture | None = None
        if output_request is not None and output_request.path is not None:
            policy = output_request.policy
            destination = output_capture.ShellOutputDestination(
                str(policy.workspace_root),
                output_request.path.requested_path,
                policy.max_bytes,
                policy.tools_use_workdir,
            )
            capture = output_capture.ShellOutputCapture(destination)
    except Exception as exc:
        return _not_started(f"Error: {exc}", has_output_path=has_output_path)
    runner = _DesktopShellRunner(context, configuration, credential_scope, capture=capture)
    try:
        return await runner.execute(command_str, tail_lines, timeout_limit, cwd_str)
    except asyncio.CancelledError:
        with suppress(BaseException):
            await asyncio.shield(asyncio.wait_for(runner.cleanup_on_cancel(), timeout=_CANCEL_CLEANUP_TIMEOUT_SECONDS))
        raise
    except (_DesktopShellError, protocol.DesktopProtocolError, OlmToDeviceError) as exc:
        detail = str(exc)
        if runner.handle is not None and runner.state == "running":
            deadline = time.monotonic() + _CANCEL_CLEANUP_TIMEOUT_SECONDS
            stopped = await runner._stop_handle(runner.handle, deadline=deadline)
            note = (
                "The remote command was stopped."
                if stopped
                else "Stopping the remote command could not be confirmed; it may still be running on the desktop."
            )
            suffix = "" if not detail or detail.endswith((".", "!", "?")) else "."
            detail = f"{detail}{suffix} {note}" if detail else note
        return runner._interrupt_or_return(f"Error: {detail}" if capture is None else detail)
    finally:
        if capture is not None:
            capture.close()
