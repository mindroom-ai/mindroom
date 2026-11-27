"""Run desktop shell actions, report local and caller-scoped shell status, and deliver output inline or attached."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from mindroom.desktop.command_parameters import (
    optional_bool_parameter,
    optional_int_parameter,
    optional_str_parameter,
    reject_unexpected_parameters,
    required_int_parameter,
    required_str_parameter,
)
from mindroom.desktop.media import MEDIA_UPLOAD_TIMEOUT_SECONDS, DesktopMediaError, upload_encrypted_media
from mindroom.desktop.protocol import (
    DESKTOP_SHELL_PROTOCOL_VERSION,
    MAX_INLINE_RESPONSE_BYTES,
    SHELL_OUTPUT_MIME_TYPE,
    DesktopProtocolError,
)
from mindroom.desktop.reply_fitting import fits_inline, leftmost_fitting
from mindroom.desktop.shell import DesktopShellRequest
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    import nio

    from mindroom.desktop.protocol import DesktopCommand
    from mindroom.desktop.shell import DesktopShell, DesktopShellResult

logger = get_logger(__name__)

_MAX_WARNING_DETAIL = 500
_STDOUT_SLICE_KEYS = ("output", "output_truncated", "output_start", "next_offset")
_STDERR_SLICE_KEYS = ("stderr", "stderr_truncated", "stderr_start", "stderr_next_offset")


def _optional_positive_number_parameter(parameters: dict[str, object], key: str) -> float | None:
    """Return the positive finite numeric parameter ``key``, or ``None`` when absent."""
    if key not in parameters:
        return None
    value = parameters[key]
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value <= 0:
        msg = f"Desktop parameter {key} must be a positive finite number."
        raise DesktopProtocolError(msg)
    return float(value)


def shell_status(
    shell: DesktopShell | None,
    caller: tuple[str, str] | None = None,
    *,
    controller_key: str | None = None,
) -> dict[str, object]:
    """Describe local shell access; ``caller`` hides another caller's active request ID."""
    if shell is None:
        return {
            "enabled": False,
            "pending": None,
            "auto_approve_remaining_seconds": 0,
            "auto_approve_until_revoked": False,
            "active_request_id": None,
            "handles": [],
        }
    return {"enabled": True, **shell.status(caller=caller, controller_key=controller_key)}


def caller_shell_status(
    shell: DesktopShell | None,
    command: DesktopCommand,
    *,
    controller_key: str | None = None,
) -> dict[str, object]:
    """Show another allowed caller only whether approval is pending, its own active ID, and its own handles."""
    status = shell_status(shell, (command.requester_id, command.agent_name), controller_key=controller_key)
    return {
        **status,
        "pending": status["pending"] is not None,
        "handles": (
            shell.handles(command.requester_id, command.agent_name, controller_key=controller_key)
            if shell is not None
            else []
        ),
    }


async def execute_shell(
    client: nio.AsyncClient,
    shell: DesktopShell | None,
    command: DesktopCommand,
    *,
    controller_key: str = "",
) -> dict[str, object]:
    """Start a command only after local approval, or read or stop one of the caller's own handles."""
    if shell is None:
        msg = "Local shell access is disabled."
        raise DesktopProtocolError(msg)
    parameters = command.parameters
    if command.action == "check_shell":
        reject_unexpected_parameters(parameters, allowed=frozenset({"handle", "offset", "stderr_offset"}))
        handle = required_str_parameter(parameters, "handle")
        offset = optional_int_parameter(parameters, "offset")
        stderr_offset = optional_int_parameter(parameters, "stderr_offset")
        result = shell.check(
            command.requester_id,
            command.agent_name,
            handle,
            offset=offset,
            stderr_offset=stderr_offset,
            controller_key=controller_key,
        )
        return await _shell_result(client, command, shell, result, offset=offset, stderr_offset=stderr_offset)
    if command.action == "kill_shell":
        reject_unexpected_parameters(parameters, allowed=frozenset({"handle", "force", "request_id"}))
        has_handle = "handle" in parameters
        has_request_id = "request_id" in parameters
        if has_handle == has_request_id:
            msg = "Desktop kill_shell requires exactly one of handle or request_id."
            raise DesktopProtocolError(msg)
        force = optional_bool_parameter(parameters, "force")
        if has_request_id:
            request_id = required_str_parameter(parameters, "request_id")
            return {
                "state": shell.cancel_request(
                    command.requester_id,
                    command.agent_name,
                    request_id,
                    controller_key=controller_key,
                ),
                "request_id": request_id,
            }
        handle = required_str_parameter(parameters, "handle")
        return {
            "state": shell.kill(
                command.requester_id,
                command.agent_name,
                handle,
                force=force,
                controller_key=controller_key,
            ),
            "handle": handle,
        }
    reject_unexpected_parameters(
        parameters,
        allowed=frozenset({"command", "cwd", "timeout_seconds", "max_runtime_seconds", "shell_protocol"}),
    )
    if (
        "shell_protocol" not in parameters
        or required_int_parameter(parameters, "shell_protocol") != DESKTOP_SHELL_PROTOCOL_VERSION
    ):
        msg = (
            f"Unsupported desktop shell protocol; expected shell_protocol={DESKTOP_SHELL_PROTOCOL_VERSION}. "
            "Update the MindRoom server and try again."
        )
        raise DesktopProtocolError(msg)
    timeout_seconds = optional_int_parameter(parameters, "timeout_seconds")
    max_runtime_seconds = _optional_positive_number_parameter(parameters, "max_runtime_seconds")
    request = DesktopShellRequest(
        request_id=command.request_id,
        requester_id=command.requester_id,
        agent_name=command.agent_name,
        command=required_str_parameter(parameters, "command"),
        cwd=optional_str_parameter(parameters, "cwd", default=str(Path.home())),
        expires_at_ms=command.expires_at_ms,
        timeout_seconds=30 if timeout_seconds is None else timeout_seconds,
        controller_key=controller_key,
        max_runtime_seconds=max_runtime_seconds,
    )
    return await _shell_result(client, command, shell, await shell.execute(request))


@dataclass(frozen=True, slots=True)
class _StreamSlice:
    text: str
    offset: int | None
    size: int
    base_truncated: bool
    keys: tuple[str, str, str, str]

    def apply(self, cut: int) -> dict[str, object]:
        text_key, truncated_key, start_key, next_key = self.keys
        shown = self.text[cut:] if self.offset is None else self.text[: len(self.text) - cut]
        shown_bytes = len(shown.encode())
        requested = self.size if self.offset is None else self.size - self.offset
        return {
            text_key: shown,
            truncated_key: self.base_truncated or shown_bytes < requested,
            start_key: self.size - shown_bytes if self.offset is None else self.offset,
            next_key: self.size if self.offset is None else self.offset + shown_bytes,
        }


async def _shell_result(
    client: nio.AsyncClient,
    command: DesktopCommand,
    shell: DesktopShell,
    result: DesktopShellResult,
    *,
    offset: int | None = None,
    stderr_offset: int | None = None,
) -> dict[str, object]:
    """Reply inline when the encrypted response fits one to-device message, otherwise attach the output.

    Output starts at byte ``offset`` and stderr at ``stderr_offset``; without one, a running command shows its
    newest bytes.
    """
    output = result.output
    payload: dict[str, object] = {
        "state": result.state,
        "handle": result.handle,
        "exit_code": result.exit_code,
        "output": "",
        "output_bytes": output.stdout.size,
        "output_truncated": output.stdout.truncated,
        "output_attachment": None,
        "output_start": offset or 0,
        "next_offset": output.stdout.size,
        "stderr": "",
        "stderr_bytes": output.stderr.size,
        "stderr_truncated": output.stderr.truncated,
        "stderr_attachment": None,
        "stderr_start": stderr_offset or 0,
        "stderr_next_offset": output.stderr.size,
    }
    if result.timed_out:
        payload["timed_out"] = True
    if result.state == "running":
        stdout_raw = (
            output.stdout.tail(MAX_INLINE_RESPONSE_BYTES)
            if offset is None
            else output.stdout.pread(offset, MAX_INLINE_RESPONSE_BYTES)
        )
        stderr_raw = (
            output.stderr.tail(MAX_INLINE_RESPONSE_BYTES)
            if stderr_offset is None
            else output.stderr.pread(stderr_offset, MAX_INLINE_RESPONSE_BYTES)
        )
        return _fit_output(command, payload, stdout_raw, stderr_raw, offset=offset, stderr_offset=stderr_offset)
    try:
        return await _finished_shell_result(client, command, shell, result, payload, offset or 0, stderr_offset or 0)
    except BaseException:
        if command.action == "run_shell":
            # The caller never learned this handle, so nothing could page from it.
            shell.hand_over(result)
        raise


async def _upload_attachments(
    client: nio.AsyncClient,
    request_id: str,
    stdout_content: bytes,
    stderr_content: bytes,
) -> tuple[dict[str, object] | None, dict[str, object] | None, str | None]:
    """Upload stdout and stderr concurrently within one shared upload timeout budget."""

    async def _upload(content: bytes, filename: str) -> dict[str, object] | None:
        if not content:
            return None
        media = await upload_encrypted_media(
            client,
            content,
            mime_type=SHELL_OUTPUT_MIME_TYPE,
            filename=filename,
            timeout_seconds=MEDIA_UPLOAD_TIMEOUT_SECONDS,
        )
        return media.to_content()

    outcomes = await asyncio.gather(
        _upload(stdout_content, f"shell-{request_id}.txt"),
        _upload(stderr_content, f"shell-{request_id}-stderr.txt"),
        return_exceptions=True,
    )
    for outcome in outcomes:
        if isinstance(outcome, BaseException) and not isinstance(outcome, Exception):
            raise outcome
    error: str | None = None
    for outcome in outcomes:
        if isinstance(outcome, DesktopMediaError):
            error = error or str(outcome)
        elif isinstance(outcome, Exception):
            logger.exception("shell_output_upload_failed", request_id=request_id, exc_info=outcome)
            error = error or "Shell output upload failed."
    if error is not None:
        return None, None, error
    return cast("dict[str, object] | None", outcomes[0]), cast("dict[str, object] | None", outcomes[1]), None


async def _finished_shell_result(
    client: nio.AsyncClient,
    command: DesktopCommand,
    shell: DesktopShell,
    result: DesktopShellResult,
    payload: dict[str, object],
    start: int,
    stderr_start: int,
) -> dict[str, object]:
    """Deliver a finished command's stdout/stderr from their offsets; its handle stays until the rest arrives."""
    stdout_content = result.output.stdout.pread(start)
    stderr_content = result.output.stderr.pread(stderr_start)
    # A run_shell reply names its handle only while that handle stays to page from.
    delivered = payload if command.action == "check_shell" else {**payload, "handle": None}
    # JSON escaping only grows text, so larger output cannot fit and is never decoded here.
    if len(stdout_content) + len(stderr_content) <= MAX_INLINE_RESPONSE_BYTES:
        inline: dict[str, object] = {**delivered, "output": stdout_content.decode(), "stderr": stderr_content.decode()}
        if fits_inline(command, inline):
            shell.hand_over(result)
            return inline
    stdout_attachment, stderr_attachment, error = await _upload_attachments(
        client,
        command.request_id,
        stdout_content,
        stderr_content,
    )
    if error is None:
        shell.hand_over(result)
        return {**delivered, "output_attachment": stdout_attachment, "stderr_attachment": stderr_attachment}
    detail = error[:_MAX_WARNING_DETAIL]
    if result.handle is None:
        # Revocation raced this command's registration, so there is no handle to page from.
        shell.hand_over(result)
        warning = (
            f"The output could not be attached ({detail}); only its beginning is shown and the rest is not "
            "kept. Do not run the command again automatically."
        )
    else:
        warning = (
            f"The rest of the output could not be attached ({detail}); this page shows it from output_start. "
            "Continue with check_shell from next_offset."
        )
    return _fit_output(
        command,
        {**payload, "warning": warning},
        stdout_content[:MAX_INLINE_RESPONSE_BYTES],
        stderr_content[:MAX_INLINE_RESPONSE_BYTES],
        offset=start,
        stderr_offset=stderr_start,
    )


def _fit_output(
    command: DesktopCommand,
    payload: dict[str, object],
    stdout_raw: bytes,
    stderr_raw: bytes,
    *,
    offset: int | None,
    stderr_offset: int | None,
) -> dict[str, object]:
    """Fit stdout and stderr slices into one inline reply without starving either stream."""
    stdout_slice = _StreamSlice(
        stdout_raw.decode(errors="ignore"),
        offset,
        cast("int", payload["output_bytes"]),
        bool(payload["output_truncated"]),
        _STDOUT_SLICE_KEYS,
    )
    stderr_slice = _StreamSlice(
        stderr_raw.decode(errors="ignore"),
        stderr_offset,
        cast("int", payload["stderr_bytes"]),
        bool(payload["stderr_truncated"]),
        _STDERR_SLICE_KEYS,
    )

    def build(stdout_cut: int, stderr_cut: int) -> dict[str, object]:
        return {**payload, **stdout_slice.apply(stdout_cut), **stderr_slice.apply(stderr_cut)}

    full = build(0, 0)
    if fits_inline(command, full):
        return full
    stdout_len = len(stdout_slice.text)
    stderr_len = len(stderr_slice.text)
    if stderr_len == 0:
        return build(leftmost_fitting(command, 0, stdout_len, lambda cut: build(cut, 0)), 0)
    if stdout_len == 0:
        return build(0, leftmost_fitting(command, 0, stderr_len, lambda cut: build(0, cut)))

    stderr_alone_cut = leftmost_fitting(command, 0, stderr_len, lambda cut: build(stdout_len, cut))
    stderr_alone_kept = stderr_len - stderr_alone_cut
    stderr_reserved = min(stderr_len, max(1, stderr_alone_kept // 2)) if stderr_alone_kept > 0 else 0
    stderr_cut = stderr_len - stderr_reserved
    stdout_cut = leftmost_fitting(command, 0, stdout_len, lambda cut: build(cut, stderr_cut))
    if stdout_cut == 0 and stderr_cut > 0:
        stderr_cut = leftmost_fitting(command, 0, stderr_cut, lambda cut: build(0, cut))
    return build(stdout_cut, stderr_cut)
