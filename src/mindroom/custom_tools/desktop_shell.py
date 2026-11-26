"""Unified remote desktop shell execution over the existing desktop wire protocol."""

from __future__ import annotations

import asyncio
import time
import weakref
from contextlib import suppress
from itertools import count
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from mindroom.desktop import protocol as proto
from mindroom.desktop.client import DesktopRequestError, desktop_response_router
from mindroom.desktop.configuration import DesktopConfigurationState, DesktopConfigurationStatus
from mindroom.desktop.media import DesktopMediaError, download_encrypted_media
from mindroom.desktop.protocol import DesktopAction, DesktopResponse
from mindroom.matrix.olm_to_device import OlmToDeviceError, PinnedMatrixDevice
from mindroom.shell import execution
from mindroom.shell import output_capture as cap
from mindroom.shell.execution import shell_call_timeout
from mindroom.tool_jobs.control import current_job_id
from mindroom.tool_jobs.runtime import get_background_runtime
from mindroom.tool_system.output_files import ToolOutputFileHandled, current_tool_output_file_request
from mindroom.tool_system.progress import report_tool_metadata, report_tool_progress

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from mindroom.tool_system.runtime_context import ToolRuntimeContext

_APPROVAL_WAIT_THRESHOLD_SECONDS = 2.5
_POLL_BACKOFF_SECONDS = (0.5, 0.5, 1.0, 1.5, 2.0)
_TIMEOUT_KILL_GRACE_DELAYS = (0.0, 0.25, 0.4, 0.85)
_FORCE_KILL_DRAIN_DELAY_SECONDS = 0.2
_RECOVERY_DELAYS_SECONDS = (0.5, 0.5, 1.0, 1.5, 2.0, 2.0, 2.0, 2.0)
_CANCEL_CLEANUP_TIMEOUT_SECONDS = 5.0
_CANCEL_STATUS_RETRY_DELAY_SECONDS = 0.5
_TRANSPORT_ERRORS = (DesktopRequestError, proto.DesktopProtocolError, OlmToDeviceError)
_STREAM_SPECS = (
    ("output", "output_start", "next_offset", "output_bytes", "output_attachment"),
    ("stderr", "stderr_start", "stderr_next_offset", "stderr_bytes", "stderr_attachment"),
)
_MAX_MIB = proto.MAX_SHELL_OUTPUT_BYTES // (1024 * 1024)
_TRUNCATION_NOTICE = f"[Desktop shell output exceeded the {_MAX_MIB} MiB capture limit; later output was dropped.]"
type _Params = dict[str, object]
type _LoopLocks = weakref.WeakValueDictionary[PinnedMatrixDevice, asyncio.Lock]
_START_LOCKS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _LoopLocks] = weakref.WeakKeyDictionary()
_LEGACY_COMBINED_STDERR_TARGETS: dict[PinnedMatrixDevice, None] = {}


class _DesktopShellError(RuntimeError):
    """Unrecoverable remote desktop shell error."""


def _not_started(message: str) -> str:
    report_tool_metadata(not_started=True)
    return message


def _with_truncation_notice(message: str, *, truncated: bool) -> str:
    return message if not truncated else (f"{_TRUNCATION_NOTICE}\n{message}" if message else _TRUNCATION_NOTICE)


def _expired(dl: float | None, margin: float = 0.0) -> bool:
    return dl is not None and dl - time.monotonic() <= margin


def _is_state(resp: DesktopResponse | None, state: str) -> bool:
    return bool(resp is not None and resp.ok and resp.result.get("state") == state)


def _setup_error(cfg: DesktopConfigurationState, *, chat: bool) -> str:
    if cfg.error is not None:
        suffix = " Run `/desktop setup` to replace it." if chat else ""
        return f"Error: Desktop configuration is invalid: {cfg.error}{suffix}"
    if chat:
        return (
            "Error: Desktop setup is required for this requester and agent. Run `/desktop setup` in this Matrix chat."
        )
    return "Error: Desktop setup requires a live Matrix agent chat. Run `/desktop setup` in a private Matrix room."


def _validate_args(args: object, tail: object, limit: object, cwd: object) -> tuple[str, int, float | None, str | None]:
    command = execution.shell_args_to_command(args)
    if len(command) > 8192:
        msg = "Shell command must not exceed 8192 characters."
        raise ValueError(msg)
    if isinstance(tail, bool) or not isinstance(tail, int) or tail < 0:
        msg = "tail must be a non-negative integer."
        raise ValueError(msg)
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int | float) or limit <= 0):
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


def _unwrap_request_status(resp: DesktopResponse, req_id: str) -> DesktopResponse | None:
    """Return the recorded response, None if still queued/running, or raise ``_DesktopShellError``."""
    if not resp.ok:
        raise _DesktopShellError(resp.error or f"Desktop request_status failed for {req_id!r}.")
    state = resp.result.get("state")
    if state in {"queued", "running"}:
        return None
    if state == "completed" and "response" in resp.result:
        return DesktopResponse.from_content(resp.result["response"])
    if state == "unknown":
        raw = resp.result.get("response")
        warn = DesktopResponse.from_content(raw).result.get("warning") if raw is not None else None
        fallback = f"Desktop shell request {req_id!r} ended with an unknown outcome; do not repeat it automatically."
        raise _DesktopShellError(warn if isinstance(warn, str) and warn else fallback)
    detail = "was not found on the desktop device." if state == "not_found" else f"reported unexpected state {state!r}."
    msg = f"Desktop shell request {req_id!r} {detail}"
    raise _DesktopShellError(msg)


class _DesktopShellRunner:
    """Drive one ``run_desktop_shell`` call across ``run_shell``, ``check_shell``, ``kill_shell``, and recovery."""

    def __init__(self, ctx: ToolRuntimeContext, cfg: DesktopConfigurationState, scope: tuple[str, str]) -> None:
        assert cfg.target is not None
        self.context, self.target, self.transport_timeout, self.scope = ctx, cfg.target, cfg.timeout_seconds, scope
        self.capture: cap.ShellOutputCapture | None = None
        self.session_id, self.sequences = uuid4().hex, count()
        self.start_request_id: str | None = None
        self.handle: str | None = None
        self.split_stderr, self.state, self.truncated = False, "completed", False
        self.exit_code: int | None = None
        self.warning: str | None = None
        self.buffers = (execution.ShellOutputBuffer(), execution.ShellOutputBuffer())
        self.offsets = [0, 0]
        self.progress = execution.ShellOutputProgress(report_tool_progress, *self.buffers)

    async def _raw(self, act: DesktopAction, params: _Params, wait: float) -> DesktopResponse:
        now = round(time.time() * 1000)
        end = now + max(1, min(proto.MAX_COMMAND_TTL_MS, round(wait * 1000)))
        req_id = uuid4().hex
        cmd = proto.DesktopCommand(req_id, self.session_id, next(self.sequences), now, end, act, *self.scope, params)
        if act == "run_shell":
            self.start_request_id = req_id
        return await desktop_response_router(self.context.client).request(self.target, cmd, timeout_seconds=wait)

    async def _send(self, act: DesktopAction, params: _Params, wait: float | None = None) -> DesktopResponse:
        try:
            return await self._raw(act, params, self.transport_timeout if wait is None else wait)
        except DesktopRequestError as exc:
            if act == "run_shell" and exc.action_outcome in {"not_sent", "not_started"}:
                report_tool_metadata(not_started=True)
            if exc.request_id is None:
                raise _DesktopShellError(str(exc)) from exc
            return await self._recover_request(exc.request_id)

    async def _recover_request(self, request_id: str) -> DesktopResponse:
        for attempt, delay in enumerate((0.0, *_RECOVERY_DELAYS_SECONDS)):
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                resp = await self._raw("request_status", {"request_id": request_id}, self.transport_timeout)
            except (DesktopRequestError, OlmToDeviceError) as exc:
                if attempt == len(_RECOVERY_DELAYS_SECONDS):
                    msg = f"Failed to recover desktop shell request {request_id!r}: {exc}"
                    raise _DesktopShellError(msg) from exc
            else:
                if (recovered := _unwrap_request_status(resp, request_id)) is not None:
                    return recovered
        msg = f"Desktop shell request {request_id!r} did not settle within the recovery window."
        raise _DesktopShellError(msg)

    async def _try(self, act: DesktopAction, params: _Params, dl: float | None) -> DesktopResponse | None:
        try:
            if dl is None:
                return await self._send(act, params)
            return await self._raw(act, params, max(0.1, min(2.0, dl - time.monotonic())))
        except (*_TRANSPORT_ERRORS, _DesktopShellError):
            return None

    async def _start(self, cmd: str, *, start_wait: int, cwd: str | None) -> tuple[DesktopResponse, float]:
        start_timeout = proto.MAX_COMMAND_TTL_MS / 1000

        async def _locked() -> tuple[DesktopResponse, float]:
            per_loop = _START_LOCKS.setdefault(asyncio.get_running_loop(), weakref.WeakValueDictionary())
            if (lock := per_loop.get(self.target)) is None:
                per_loop[self.target] = lock = asyncio.Lock()
            async with lock:
                send_split = self.target not in _LEGACY_COMBINED_STDERR_TARGETS
                params: _Params = {"command": cmd, "timeout_seconds": start_wait}
                if cwd is not None:
                    params["cwd"] = cwd
                if send_split:
                    params["separate_stderr"] = True
                sent_at = time.monotonic()
                resp = await self._send("run_shell", params, start_timeout)
                err = resp.error or ""
                if send_split and not resp.ok and "Unexpected desktop parameters:" in err and "separate_stderr" in err:
                    if len(_LEGACY_COMBINED_STDERR_TARGETS) >= 256:
                        _LEGACY_COMBINED_STDERR_TARGETS.pop(next(iter(_LEGACY_COMBINED_STDERR_TARGETS)))
                    _LEGACY_COMBINED_STDERR_TARGETS[self.target] = None
                    legacy_params = {k: v for k, v in params.items() if k != "separate_stderr"}
                    sent_at = time.monotonic()
                    resp = await self._send("run_shell", legacy_params, start_timeout)
                return resp, time.monotonic() - sent_at

        return await _await_with_approval_status(_locked(), context=self.context)

    def _check_params(self) -> _Params:
        assert self.handle is not None
        extra = {"stderr_offset": self.offsets[1]} if self.split_stderr else {}
        return {"handle": self.handle, "offset": self.offsets[0], **extra}

    async def _ingest_stream(self, result: dict[str, Any], idx: int, *, initial: bool) -> tuple[bool, bool]:
        text_key, start_key, next_key, bytes_key, att_key = _STREAM_SPECS[idx]
        cur, raw_next, raw_bytes = self.offsets[idx], result.get(next_key), result.get(bytes_key)
        if (raw_att := result.get(att_key)) is not None:
            media = proto.EncryptedDesktopMedia.from_content(raw_att, kind="output_attachment")
            data = await download_encrypted_media(self.context.client, media, timeout_seconds=self.transport_timeout)
            text = data.decode("utf-8", errors="replace")
            new_off = raw_next if isinstance(raw_next, int) else cur + len(data)
        else:
            start = raw_s if isinstance(raw_s := result.get(start_key, 0), int) else 0
            if initial and self.state == "running" and start > 0:
                self.offsets[idx] = 0
                return True, False
            text = inline if start == cur and isinstance(inline := result.get(text_key), str) else ""
            new_off = (raw_next if isinstance(raw_next, int) else cur + len(text.encode())) if start == cur else cur
        if text:
            self.buffers[idx].append(text)
            if self.capture is not None:
                (self.capture.stdout if idx == 0 else self.capture.stderr).append(text)
        self.offsets[idx] = new_off
        return isinstance(raw_bytes, int) and new_off < raw_bytes, new_off > cur

    async def _ingest(self, response: DesktopResponse, *, initial: bool) -> tuple[bool, bool]:
        if not response.ok:
            raise _DesktopShellError(response.error or "Desktop device rejected the shell request.")
        result = response.result
        if result.get("action_outcome") == "unknown":
            warn = result.get("warning")
            fallback = "The desktop shell command outcome is unknown; do not repeat it automatically."
            raise _DesktopShellError(warn if isinstance(warn, str) and warn else fallback)
        if initial:
            self.split_stderr = "stderr_next_offset" in result
        self.state = str(result.get("state") or "completed")
        if isinstance(raw_handle := result.get("handle"), str) and raw_handle:
            self.handle = raw_handle
        elif raw_handle is None and self.state != "running":
            self.handle = None
        raw_exit = result.get("exit_code")
        self.exit_code = raw_exit if isinstance(raw_exit, int) and not isinstance(raw_exit, bool) else None
        self.warning = w if isinstance(w := result.get("warning"), str) and w else None
        has_more, advanced = False, False
        try:
            for idx in (0, 1) if self.split_stderr else (0,):
                more, adv = await self._ingest_stream(result, idx, initial=initial)
                has_more, advanced = has_more or more, advanced or adv
        except (DesktopMediaError, proto.DesktopProtocolError) as exc:
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

    async def _poll_stopped(self, dl: float | None) -> bool | None:
        if dl is not None:
            r = await self._try("check_shell", self._check_params(), dl)
            return None if r is None or not r.ok else not _is_state(r, "running")
        try:
            has_more, _ = await self._check()
        except (*_TRANSPORT_ERRORS, _DesktopShellError):
            return None
        if self.state == "running":
            return False
        with suppress(*_TRANSPORT_ERRORS, _DesktopShellError):
            await self._drain_finished(has_more)
        return True

    async def _grace_poll(self, dl: float | None) -> bool:
        for delay in _TIMEOUT_KILL_GRACE_DELAYS if dl is None else (0.25,):
            if _expired(dl, 1.0):
                break
            if delay > 0:
                await asyncio.sleep(delay)
            if (stopped := await self._poll_stopped(dl)) is not False:
                return bool(stopped)
        return False

    async def _stop_handle(self, handle: str, *, dl: float | None = None) -> bool:
        """Stop ``handle`` via SIGTERM -> grace checks -> SIGKILL and return whether stop was confirmed."""
        if _expired(dl):
            return False
        resp = await self._try("kill_shell", {"handle": handle}, dl)
        completed = _is_state(resp, "completed")
        if completed and dl is not None:
            return True
        if (dl is None or (resp is not None and resp.ok)) and await self._grace_poll(dl):
            return True
        if not _expired(dl):
            fresp = await self._try("kill_shell", {"handle": handle, "force": True}, dl)
            completed = completed or _is_state(fresp, "completed")
        if dl is not None:
            return completed
        if _FORCE_KILL_DRAIN_DELAY_SECONDS > 0:
            await asyncio.sleep(_FORCE_KILL_DRAIN_DELAY_SECONDS)
        return bool(await self._poll_stopped(None)) or completed

    def _interrupt_or_return(self, message: str, exit_code: int | None = None) -> str | ToolOutputFileHandled:
        if self.capture is None:
            return message
        self.capture.incomplete, self.capture.interruption = True, message
        return ToolOutputFileHandled(self.capture.publish(exit_code))

    async def _stop_on_timeout(self, *, tail: int, stop_after: float) -> str | ToolOutputFileHandled:
        assert self.handle is not None
        confirmed = await self._stop_handle(self.handle)
        exit_code = self.exit_code if self.state != "running" else None
        if exit_code is not None:
            report_tool_metadata(exit_code=exit_code)
        stdout, stderr = self.buffers[0].render(tail=tail), self.buffers[1].render() if self.buffers[1] else None
        if confirmed:
            message = cap.format_shell_timeout(stdout, stderr, timeout=stop_after, no_output_file=bool(self.capture))
        else:
            no_file = " No output file was saved." if self.capture is not None else ""
            head = (
                f"Timed out after {stop_after:g}s, but stopping the remote command could not be confirmed; "
                f"it may still be running on the desktop.{no_file}"
            )
            extra = (["Output:", stdout] if stdout else []) + (["Stderr:", stderr] if stderr is not None else [])
            message = "\n".join([head, *extra])
        message = _with_truncation_notice(message, truncated=self.state != "running" and self.truncated)
        return self._interrupt_or_return(message, exit_code)

    async def _cancel_start(self, req_id: str, dl: float) -> None:
        resp = await self._try("kill_shell", {"request_id": req_id}, dl)
        if resp is None or (resp.ok and not _is_state(resp, "not_found")):
            return
        while not _expired(dl):
            if (sresp := await self._try("request_status", {"request_id": req_id}, dl)) is None:
                break
            try:
                rec = _unwrap_request_status(sresp, req_id)
            except (_DesktopShellError, proto.DesktopProtocolError):
                break
            if rec is not None:
                h = rec.result.get("handle")
                if _is_state(rec, "running") and isinstance(h, str) and h and not _expired(dl):
                    await self._try("kill_shell", {"handle": h, "force": True}, dl)
                break
            await asyncio.sleep(min(_CANCEL_STATUS_RETRY_DELAY_SECONDS, max(0.0, dl - time.monotonic())))
            if resp.ok and not _expired(dl):
                cresp = await self._try("kill_shell", {"request_id": req_id}, dl)
                if cresp is not None and cresp.ok and not _is_state(cresp, "not_found"):
                    break

    async def cleanup_on_cancel(self) -> None:
        """Stop any running handle or dismiss a pending approval when the caller cancels."""
        deadline = time.monotonic() + _CANCEL_CLEANUP_TIMEOUT_SECONDS
        if self.handle is not None:
            await self._stop_handle(self.handle, dl=deadline)
        elif self.start_request_id is not None:
            await self._cancel_start(self.start_request_id, deadline)

    def _format_finished(self, *, has_more: bool, tail: int) -> str | ToolOutputFileHandled:
        if (exit_code := self.exit_code) is not None:
            report_tool_metadata(exit_code=exit_code)
        if self.capture is not None:
            self.capture.stdout.reached_eof = self.capture.stderr.reached_eof = True
            if self.truncated or has_more:
                fallback = "Desktop shell output was truncated before full delivery; no complete output file was saved."
                return self._interrupt_or_return(self.warning or fallback, exit_code)
            return ToolOutputFileHandled(self.capture.publish(exit_code))
        if exit_code is None:
            return "Error: Desktop shell command ended without an exit code."
        if exit_code != 0:
            errors = self.buffers[1].render() if self.buffers[1] else None
            body = cap.format_shell_completion(self.buffers[0].render(tail=tail), errors, return_code=exit_code)
        else:
            body = execution.format_shell_success(self.buffers[0], self.buffers[1], tail)
            if self.state == "killed":
                prefix = "Command was killed on the desktop."
                body = f"{prefix}\n{body}" if body else prefix
        return _with_truncation_notice(body, truncated=self.truncated)

    async def execute(self, cmd: str, tail: int, limit: float | None, cwd: str | None) -> str | ToolOutputFileHandled:
        """Start, poll, drain, and format one desktop shell command."""
        stop_after = shell_call_timeout(limit)
        start_wait = 1 if (limit is not None and limit <= 1) else 2
        start_response, start_elapsed = await self._start(cmd, start_wait=start_wait, cwd=cwd)
        has_more, _ = await self._ingest(start_response, initial=True)
        dl = None if stop_after is None else time.monotonic() + max(0.0, stop_after - min(start_elapsed, start_wait))
        poll_index = 0
        while self.state == "running":
            self.progress.poll()
            if self.handle is None:
                msg = "Desktop shell command is still running but returned no handle."
                raise _DesktopShellError(msg)
            remaining = None if dl is None else dl - time.monotonic()
            if remaining is not None and remaining <= 0:
                assert stop_after is not None
                return await self._stop_on_timeout(tail=tail, stop_after=stop_after)
            if not has_more:
                backoff = _POLL_BACKOFF_SECONDS[poll_index]
                if (delay := backoff if remaining is None else min(backoff, remaining)) > 0:
                    await asyncio.sleep(delay)
                poll_index = min(poll_index + 1, len(_POLL_BACKOFF_SECONDS) - 1)
            has_more, advanced = await self._check()
            if self.state == "running" and advanced:
                poll_index = 0
        return self._format_finished(has_more=await self._drain_finished(has_more), tail=tail)


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
        return _not_started(_setup_error(configuration, chat=credential_scope is not None))
    if context is None:
        return _not_started("Error: Desktop shell requires a live Matrix runtime context.")
    assert credential_scope is not None
    try:
        cmd_str, tail_n, limit, cwd_str = _validate_args(args, tail, timeout, cwd)
        capture = None
        if (req := current_tool_output_file_request()) is not None and req.path is not None:
            p, root = req.policy, str(req.policy.workspace_root)
            dest = cap.ShellOutputDestination(root, req.path.requested_path, p.max_bytes, p.tools_use_workdir)
            capture = cap.ShellOutputCapture(dest)
    except Exception as exc:
        return _not_started(f"Error: {exc}")
    runner = _DesktopShellRunner(context, configuration, credential_scope)
    runner.capture = capture
    try:
        return await runner.execute(cmd_str, tail_n, limit, cwd_str)
    except asyncio.CancelledError:
        with suppress(BaseException):
            await asyncio.shield(asyncio.wait_for(runner.cleanup_on_cancel(), timeout=_CANCEL_CLEANUP_TIMEOUT_SECONDS))
        raise
    except (_DesktopShellError, proto.DesktopProtocolError, OlmToDeviceError) as exc:
        return runner._interrupt_or_return(f"Error: {exc}" if capture is None else str(exc))
    finally:
        if capture is not None:
            capture.close()
