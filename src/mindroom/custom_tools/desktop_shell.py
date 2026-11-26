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
from mindroom.desktop.protocol import DesktopAction, DesktopResponse
from mindroom.matrix.olm_to_device import OlmToDeviceError, PinnedMatrixDevice
from mindroom.shell import execution, output_capture
from mindroom.shell.execution import shell_call_timeout
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
_CANCEL_STATUS_RETRY_DELAY_SECONDS = 0.5
_TRANSPORT_ERRORS = (DesktopRequestError, protocol.DesktopProtocolError, OlmToDeviceError)
_STREAM_SPECS = (
    ("output", "output_start", "next_offset", "output_bytes", "output_attachment"),
    ("stderr", "stderr_start", "stderr_next_offset", "stderr_bytes", "stderr_attachment"),
)
_MAX_MIB = protocol.MAX_SHELL_OUTPUT_BYTES // (1024 * 1024)
_TRUNCATION_NOTICE = f"[Desktop shell output exceeded the {_MAX_MIB} MiB capture limit; later output was dropped.]"
_OPTIONAL_START_PARAMETERS = frozenset({"separate_stderr", "max_runtime_seconds"})

type _Params = dict[str, object]
type _LoopLocks = weakref.WeakValueDictionary[PinnedMatrixDevice, asyncio.Lock]
_START_LOCKS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _LoopLocks] = weakref.WeakKeyDictionary()


class _DesktopShellError(RuntimeError):
    """Unrecoverable remote desktop shell error."""


def _not_started(message: str, *, has_output_path: bool = False) -> str | ToolOutputFileHandled:
    report_tool_metadata(not_started=True)
    return ToolOutputFileHandled(message) if has_output_path else message


def _with_truncation_notice(message: str, *, truncated: bool) -> str:
    if not truncated:
        return message
    return f"{_TRUNCATION_NOTICE}\n{message}" if message else _TRUNCATION_NOTICE


def _is_expired(deadline: float, margin: float = 0.0) -> bool:
    return deadline - time.monotonic() <= margin


def _is_state(response: DesktopResponse | None, state: str) -> bool:
    return bool(response is not None and response.ok and response.result.get("state") == state)


def _setup_error(configuration: DesktopConfigurationState, *, chat: bool) -> str:
    if configuration.error is not None:
        suffix = " Run `/desktop setup` to replace it." if chat else ""
        return f"Error: Desktop configuration is invalid: {configuration.error}{suffix}"
    if chat:
        return (
            "Error: Desktop setup is required for this requester and agent. Run `/desktop setup` in this Matrix chat."
        )
    return "Error: Desktop setup requires a live Matrix agent chat. Run `/desktop setup` in a private Matrix room."


def _validate_args(
    args: object,
    tail: object,
    timeout_limit: object,
    cwd: object,
) -> tuple[str, int, float | None, str | None]:
    command = execution.shell_args_to_command(args)
    if len(command) > 8192:
        msg = "Shell command must not exceed 8192 characters."
        raise ValueError(msg)
    if isinstance(tail, bool) or not isinstance(tail, int) or tail < 0:
        msg = "tail must be a non-negative integer."
        raise ValueError(msg)
    if timeout_limit is not None and (
        isinstance(timeout_limit, bool)
        or not isinstance(timeout_limit, int | float)
        or not math.isfinite(timeout_limit)
        or timeout_limit <= 0
    ):
        msg = "timeout must be a positive number of seconds; omit it for the default limit."
        raise ValueError(msg)
    if cwd is not None and (not isinstance(cwd, str) or not cwd or len(cwd) > 4096):
        msg = "cwd must be a non-empty string of at most 4096 characters."
        raise ValueError(msg)
    return command, tail, float(timeout_limit) if timeout_limit is not None else None, cwd


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
        warning = DesktopResponse.from_content(raw).result.get("warning") if raw is not None else None
        fallback = (
            f"Desktop shell request {request_id!r} ended with an unknown outcome; do not repeat it automatically."
        )
        raise _DesktopShellError(warning if isinstance(warning, str) and warning else fallback)
    detail = "was not found on the desktop device." if state == "not_found" else f"reported unexpected state {state!r}."
    msg = f"Desktop shell request {request_id!r} {detail}"
    raise _DesktopShellError(msg)


def _is_legacy_optional_parameter_error(response: DesktopResponse) -> bool:
    if response.ok:
        return False
    error = response.error or ""
    return "Unexpected desktop parameters:" in error and any(name in error for name in _OPTIONAL_START_PARAMETERS)


class _DesktopShellRunner:
    """Drive one ``run_desktop_shell`` call across ``run_shell``, ``check_shell``, ``kill_shell``, and recovery."""

    def __init__(
        self,
        context: ToolRuntimeContext,
        configuration: DesktopConfigurationState,
        scope: tuple[str, str],
    ) -> None:
        assert configuration.target is not None
        self.context = context
        self.target = configuration.target
        self.transport_timeout = configuration.timeout_seconds
        self.scope = scope
        self.capture: output_capture.ShellOutputCapture | None = None
        self.session_id = uuid4().hex
        self.sequences = count()
        self.start_request_id: str | None = None
        self.handle: str | None = None
        self.split_stderr = False
        self.state = "completed"
        self.truncated = False
        self.timed_out = False
        self.exit_code: int | None = None
        self.warning: str | None = None
        self.buffers = (execution.ShellOutputBuffer(), execution.ShellOutputBuffer())
        self.offsets = [0, 0]
        self.progress = execution.ShellOutputProgress(report_tool_progress, *self.buffers)

    async def _raw(self, action: DesktopAction, params: _Params, wait: float) -> DesktopResponse:
        now = round(time.time() * 1000)
        expires_at_ms = now + max(1, min(protocol.MAX_COMMAND_TTL_MS, round(wait * 1000)))
        request_id = uuid4().hex
        command = protocol.DesktopCommand(
            request_id,
            self.session_id,
            next(self.sequences),
            now,
            expires_at_ms,
            action,
            *self.scope,
            params,
        )
        if action == "run_shell":
            self.start_request_id = request_id
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
            if delay > 0:
                await asyncio.sleep(delay)
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

    async def _try_send(self, action: DesktopAction, params: _Params) -> DesktopResponse | None:
        try:
            return await self._send(action, params)
        except (*_TRANSPORT_ERRORS, _DesktopShellError):
            return None

    async def _try_bounded(self, action: DesktopAction, params: _Params, deadline: float) -> DesktopResponse | None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            return await self._raw(action, params, min(2.0, remaining))
        except (*_TRANSPORT_ERRORS, _DesktopShellError):
            return None

    async def _sleep_bounded(self, seconds: float, deadline: float) -> None:
        delay = min(seconds, deadline - time.monotonic())
        if delay > 0:
            await asyncio.sleep(delay)

    async def _start(
        self,
        command: str,
        *,
        start_wait: int,
        cwd: str | None,
        max_runtime_seconds: float | None,
    ) -> tuple[DesktopResponse, float]:
        start_timeout = protocol.MAX_COMMAND_TTL_MS / 1000

        async def _locked() -> tuple[DesktopResponse, float]:
            per_loop = _START_LOCKS.setdefault(asyncio.get_running_loop(), weakref.WeakValueDictionary())
            lock = per_loop.get(self.target)
            if lock is None:
                lock = asyncio.Lock()
                per_loop[self.target] = lock
            async with lock:
                params: _Params = {"command": command, "timeout_seconds": start_wait, "separate_stderr": True}
                if cwd is not None:
                    params["cwd"] = cwd
                if max_runtime_seconds is not None:
                    params["max_runtime_seconds"] = max_runtime_seconds
                sent_at = time.monotonic()
                response = await self._send("run_shell", params, start_timeout)
                if _is_legacy_optional_parameter_error(response):
                    legacy_params = {
                        key: value for key, value in params.items() if key not in _OPTIONAL_START_PARAMETERS
                    }
                    sent_at = time.monotonic()
                    response = await self._send("run_shell", legacy_params, start_timeout)
                return response, time.monotonic() - sent_at

        return await _await_with_approval_status(_locked(), context=self.context)

    def _check_params(self) -> _Params:
        assert self.handle is not None
        params: _Params = {"handle": self.handle, "offset": self.offsets[0]}
        if self.split_stderr:
            params["stderr_offset"] = self.offsets[1]
        return params

    async def _ingest_stream(self, result: dict[str, Any], stream_index: int, *, initial: bool) -> tuple[bool, bool]:
        text_key, start_key, next_key, bytes_key, attachment_key = _STREAM_SPECS[stream_index]
        current_offset = self.offsets[stream_index]
        raw_next = result.get(next_key)
        raw_bytes = result.get(bytes_key)
        raw_attachment = result.get(attachment_key)
        if raw_attachment is not None:
            media = protocol.EncryptedDesktopMedia.from_content(raw_attachment, kind="output_attachment")
            data = await download_encrypted_media(self.context.client, media, timeout_seconds=self.transport_timeout)
            text = data.decode("utf-8", errors="replace")
            new_offset = raw_next if isinstance(raw_next, int) else current_offset + len(data)
        else:
            raw_start = result.get(start_key, 0)
            start = raw_start if isinstance(raw_start, int) else 0
            if initial and self.state == "running" and start > 0:
                self.offsets[stream_index] = 0
                return True, False
            inline = result.get(text_key)
            text = inline if start == current_offset and isinstance(inline, str) else ""
            if start == current_offset:
                new_offset = raw_next if isinstance(raw_next, int) else current_offset + len(text.encode())
            else:
                new_offset = current_offset
        if text:
            self.buffers[stream_index].append(text)
            if self.capture is not None:
                stream_capture = self.capture.stdout if stream_index == 0 else self.capture.stderr
                stream_capture.append(text)
        self.offsets[stream_index] = new_offset
        has_more = isinstance(raw_bytes, int) and new_offset < raw_bytes
        advanced = new_offset > current_offset
        return has_more, advanced

    async def _ingest(self, response: DesktopResponse, *, initial: bool) -> tuple[bool, bool]:
        if not response.ok:
            raise _DesktopShellError(response.error or "Desktop device rejected the shell request.")
        result = response.result
        if result.get("action_outcome") == "unknown":
            raw_warning = result.get("warning")
            fallback = "The desktop shell command outcome is unknown; do not repeat it automatically."
            raise _DesktopShellError(raw_warning if isinstance(raw_warning, str) and raw_warning else fallback)
        if initial:
            self.split_stderr = "stderr_next_offset" in result
        self.state = str(result.get("state") or "completed")
        self.timed_out = self.timed_out or bool(result.get("timed_out"))
        raw_handle = result.get("handle")
        if isinstance(raw_handle, str) and raw_handle:
            self.handle = raw_handle
        elif raw_handle is None and self.state != "running":
            self.handle = None
        raw_exit = result.get("exit_code")
        self.exit_code = raw_exit if isinstance(raw_exit, int) and not isinstance(raw_exit, bool) else None
        raw_warning = result.get("warning")
        self.warning = raw_warning if isinstance(raw_warning, str) and raw_warning else None
        has_more = False
        advanced = False
        try:
            for stream_index in (0, 1) if self.split_stderr else (0,):
                stream_has_more, stream_advanced = await self._ingest_stream(result, stream_index, initial=initial)
                has_more = has_more or stream_has_more
                advanced = advanced or stream_advanced
        except (DesktopMediaError, protocol.DesktopProtocolError) as exc:
            if self.exit_code is not None:
                report_tool_metadata(exit_code=self.exit_code)
            suffix = f" (exit code {self.exit_code})" if self.exit_code is not None else ""
            msg = (
                f"The desktop shell command finished{suffix}, but its full output could not be downloaded: {exc}. "
                f'Recover it without re-running the command via desktop(action="request_status", request_id={response.request_id!r}).'
            )
            raise _DesktopShellError(msg) from exc
        self.truncated = bool(result.get("output_truncated") or (self.split_stderr and result.get("stderr_truncated")))
        return has_more, advanced

    async def _check(self) -> tuple[bool, bool]:
        return await self._ingest(await self._send("check_shell", self._check_params()), initial=False)

    async def _drain_finished(self, has_more: bool) -> bool:
        while self.state != "running" and has_more and self.handle is not None:
            has_more, advanced = await self._check()
            if not advanced and has_more:
                break
        return has_more

    async def _drain_and_check_stopped(self) -> bool | None:
        """Poll once during timeout stop and drain remaining output once the command has ended."""
        try:
            has_more, _ = await self._check()
        except (*_TRANSPORT_ERRORS, _DesktopShellError):
            return None
        if self.state == "running":
            return False
        with suppress(*_TRANSPORT_ERRORS, _DesktopShellError):
            await self._drain_finished(has_more)
        return True

    async def _stop_handle_for_timeout(self, handle: str) -> bool:
        """Stop ``handle`` via SIGTERM -> grace drain checks -> SIGKILL -> final drain, returning confirmation."""
        term_response = await self._try_send("kill_shell", {"handle": handle})
        completed = _is_state(term_response, "completed")
        for delay in _TIMEOUT_KILL_GRACE_DELAYS:
            if delay > 0:
                await asyncio.sleep(delay)
            stopped = await self._drain_and_check_stopped()
            if stopped is True:
                return True
            if stopped is None:
                if completed:
                    return True
                break
        force_response = await self._try_send("kill_shell", {"handle": handle, "force": True})
        completed = completed or _is_state(force_response, "completed")
        if _FORCE_KILL_DRAIN_DELAY_SECONDS > 0:
            await asyncio.sleep(_FORCE_KILL_DRAIN_DELAY_SECONDS)
        return bool(await self._drain_and_check_stopped()) or completed

    async def _check_stopped_bounded(self, deadline: float) -> bool | None:
        response = await self._try_bounded("check_shell", self._check_params(), deadline)
        if response is None or not response.ok:
            return None
        return not _is_state(response, "running")

    async def _stop_handle_bounded(self, handle: str, *, deadline: float) -> bool:
        """Stop ``handle`` within ``deadline`` via SIGTERM -> brief check -> SIGKILL -> check."""
        term_response = await self._try_bounded("kill_shell", {"handle": handle}, deadline)
        if _is_state(term_response, "completed"):
            return True
        if term_response is not None and term_response.ok and not _is_expired(deadline, margin=1.0):
            await self._sleep_bounded(_BOUNDED_KILL_GRACE_DELAY_SECONDS, deadline)
            if await self._check_stopped_bounded(deadline) is True:
                return True
        force_response = await self._try_bounded("kill_shell", {"handle": handle, "force": True}, deadline)
        if _is_state(force_response, "completed"):
            return True
        if force_response is not None and force_response.ok and not _is_expired(deadline):
            await self._sleep_bounded(_FORCE_KILL_DRAIN_DELAY_SECONDS, deadline)
            return await self._check_stopped_bounded(deadline) is True
        return False

    def _interrupt_or_return(self, message: str, exit_code: int | None = None) -> str | ToolOutputFileHandled:
        if self.capture is None:
            return message
        self.capture.incomplete = True
        self.capture.interruption = message
        return ToolOutputFileHandled(self.capture.publish(exit_code))

    def _format_timeout_result(
        self,
        *,
        tail: int,
        timeout_seconds: float,
        confirmed: bool,
    ) -> str | ToolOutputFileHandled:
        finished = self.state != "running"
        exit_code = self.exit_code if finished else None
        if exit_code is not None:
            report_tool_metadata(exit_code=exit_code)
        stdout = self.buffers[0].render(tail=tail)
        stderr = self.buffers[1].render() if self.buffers[1] else None
        if confirmed:
            message = output_capture.format_shell_timeout(
                stdout,
                stderr,
                timeout=timeout_seconds,
                no_output_file=self.capture is not None,
            )
        else:
            no_file = " No output file was saved." if self.capture is not None else ""
            header = (
                f"Timed out after {timeout_seconds:g}s, but stopping the remote command could not be confirmed; "
                f"it may still be running on the desktop.{no_file}"
            )
            parts = [header]
            if stdout:
                parts.extend(["Output:", stdout])
            if stderr is not None:
                parts.extend(["Stderr:", stderr])
            message = "\n".join(parts)
        message = _with_truncation_notice(message, truncated=finished and self.truncated)
        return self._interrupt_or_return(message, exit_code)

    async def _stop_on_timeout(self, *, tail: int, stop_after: float) -> str | ToolOutputFileHandled:
        assert self.handle is not None
        confirmed = await self._stop_handle_for_timeout(self.handle)
        return self._format_timeout_result(tail=tail, timeout_seconds=stop_after, confirmed=confirmed)

    async def _cancel_start(self, request_id: str, deadline: float) -> None:
        cancel_response = await self._try_bounded("kill_shell", {"request_id": request_id}, deadline)
        if cancel_response is None or (cancel_response.ok and not _is_state(cancel_response, "not_found")):
            return
        while not _is_expired(deadline):
            status_response = await self._try_bounded("request_status", {"request_id": request_id}, deadline)
            if status_response is None:
                break
            try:
                recorded_response = _unwrap_request_status(status_response, request_id)
            except (_DesktopShellError, protocol.DesktopProtocolError):
                break
            if recorded_response is not None:
                handle = recorded_response.result.get("handle")
                if _is_state(recorded_response, "running") and isinstance(handle, str) and handle:
                    await self._try_bounded("kill_shell", {"handle": handle, "force": True}, deadline)
                break
            await self._sleep_bounded(_CANCEL_STATUS_RETRY_DELAY_SECONDS, deadline)
            if cancel_response.ok and not _is_expired(deadline):
                retry_response = await self._try_bounded("kill_shell", {"request_id": request_id}, deadline)
                if retry_response is not None and retry_response.ok and not _is_state(retry_response, "not_found"):
                    break

    async def cleanup_on_cancel(self) -> None:
        """Stop any running handle or dismiss a pending approval when the caller cancels."""
        deadline = time.monotonic() + _CANCEL_CLEANUP_TIMEOUT_SECONDS
        if self.handle is not None:
            await self._stop_handle_bounded(self.handle, deadline=deadline)
        elif self.start_request_id is not None:
            await self._cancel_start(self.start_request_id, deadline)

    async def stop_running_after_error(self) -> bool:
        """Attempt a bounded stop of a still-running handle after an unrecoverable polling error."""
        if self.handle is None or self.state != "running":
            return False
        deadline = time.monotonic() + _CANCEL_CLEANUP_TIMEOUT_SECONDS
        return await self._stop_handle_bounded(self.handle, deadline=deadline)

    def _format_finished(self, *, has_more: bool, tail: int) -> str | ToolOutputFileHandled:
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
            body = output_capture.format_shell_completion(
                self.buffers[0].render(tail=tail),
                errors,
                return_code=exit_code,
            )
        else:
            body = execution.format_shell_success(self.buffers[0], self.buffers[1], tail)
            if self.state == "killed":
                prefix = "Command was killed on the desktop."
                body = f"{prefix}\n{body}" if body else prefix
        return _with_truncation_notice(body, truncated=self.truncated)

    async def execute(
        self,
        command: str,
        tail: int,
        timeout_limit: float | None,
        cwd: str | None,
    ) -> str | ToolOutputFileHandled:
        """Start, poll, drain, and format one desktop shell command."""
        stop_after = shell_call_timeout(timeout_limit)
        start_wait = 1 if (timeout_limit is not None and timeout_limit <= 1) else 2
        started_at = time.monotonic()
        start_response, start_elapsed = await self._start(
            command,
            start_wait=start_wait,
            cwd=cwd,
            max_runtime_seconds=stop_after,
        )
        has_more, _ = await self._ingest(start_response, initial=True)
        deadline = (
            None if stop_after is None else time.monotonic() + max(0.0, stop_after - min(start_elapsed, start_wait))
        )
        poll_index = 0
        while self.state == "running":
            self.progress.poll()
            if self.handle is None:
                msg = "Desktop shell command is still running but returned no handle."
                raise _DesktopShellError(msg)
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                assert stop_after is not None
                return await self._stop_on_timeout(tail=tail, stop_after=stop_after)
            if not has_more:
                backoff = _POLL_BACKOFF_SECONDS[poll_index]
                delay = backoff if remaining is None else min(backoff, remaining)
                if delay > 0:
                    await asyncio.sleep(delay)
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
        return _not_started(
            _setup_error(configuration, chat=credential_scope is not None),
            has_output_path=has_output_path,
        )
    if context is None:
        return _not_started(
            "Error: Desktop shell requires a live Matrix runtime context.",
            has_output_path=has_output_path,
        )
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
    runner = _DesktopShellRunner(context, configuration, credential_scope)
    runner.capture = capture
    try:
        return await runner.execute(command_str, tail_lines, timeout_limit, cwd_str)
    except asyncio.CancelledError:
        with suppress(BaseException):
            await asyncio.shield(asyncio.wait_for(runner.cleanup_on_cancel(), timeout=_CANCEL_CLEANUP_TIMEOUT_SECONDS))
        raise
    except (_DesktopShellError, protocol.DesktopProtocolError, OlmToDeviceError) as exc:
        detail = str(exc)
        if runner.handle is not None and runner.state == "running":
            stopped = await runner.stop_running_after_error()
            status_note = (
                "The remote command was stopped."
                if stopped
                else "Stopping the remote command could not be confirmed; it may still be running on the desktop."
            )
            if detail:
                suffix = "" if detail.endswith((".", "!", "?")) else "."
                detail = f"{detail}{suffix} {status_note}"
            else:
                detail = status_note
        return runner._interrupt_or_return(f"Error: {detail}" if capture is None else detail)
    finally:
        if capture is not None:
            capture.close()
