"""Run desktop shell actions, report caller-scoped shell status, and deliver output inline or attached."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast

from mindroom.desktop.command_parameters import (
    optional_bool_parameter,
    optional_int_parameter,
    optional_str_parameter,
    reject_unexpected_parameters,
    required_str_parameter,
)
from mindroom.desktop.media import MEDIA_UPLOAD_TIMEOUT_SECONDS, DesktopMediaError, upload_encrypted_media
from mindroom.desktop.protocol import MAX_INLINE_RESPONSE_BYTES, SHELL_OUTPUT_MIME_TYPE, DesktopProtocolError
from mindroom.desktop.reply_fitting import fits_inline, leftmost_fitting
from mindroom.desktop.shell import DesktopShellRequest
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    import nio

    from mindroom.desktop.protocol import DesktopCommand
    from mindroom.desktop.shell import DesktopShell, DesktopShellResult

logger = get_logger(__name__)

_MAX_WARNING_DETAIL = 500


def shell_status(shell: DesktopShell | None, caller: tuple[str, str] | None = None) -> dict[str, object]:
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
    return {"enabled": True, **shell.status(caller=caller)}


def caller_shell_status(shell: DesktopShell | None, command: DesktopCommand) -> dict[str, object]:
    """Show another allowed caller only whether approval is pending, its own active ID, and its own handles."""
    status = shell_status(shell, (command.requester_id, command.agent_name))
    return {
        **status,
        "pending": status["pending"] is not None,
        "handles": shell.handles(command.requester_id, command.agent_name) if shell is not None else [],
    }


async def execute_shell(
    client: nio.AsyncClient,
    shell: DesktopShell | None,
    command: DesktopCommand,
) -> dict[str, object]:
    """Start a command only after local approval, or read or stop one of the caller's own handles."""
    if shell is None:
        msg = "Local shell access is disabled."
        raise DesktopProtocolError(msg)
    parameters = command.parameters
    if command.action == "check_shell":
        reject_unexpected_parameters(parameters, allowed=frozenset({"handle", "offset"}))
        handle = required_str_parameter(parameters, "handle")
        offset = optional_int_parameter(parameters, "offset")
        result = shell.check(command.requester_id, command.agent_name, handle, offset=offset)
        return await _shell_result(client, command, shell, result, offset=offset)
    if command.action == "kill_shell":
        reject_unexpected_parameters(parameters, allowed=frozenset({"handle", "force"}))
        handle = required_str_parameter(parameters, "handle")
        force = optional_bool_parameter(parameters, "force")
        return {"state": shell.kill(command.requester_id, command.agent_name, handle, force=force), "handle": handle}
    reject_unexpected_parameters(parameters, allowed=frozenset({"command", "cwd", "timeout_seconds"}))
    timeout_seconds = optional_int_parameter(parameters, "timeout_seconds")
    request = DesktopShellRequest(
        request_id=command.request_id,
        requester_id=command.requester_id,
        agent_name=command.agent_name,
        command=required_str_parameter(parameters, "command"),
        cwd=optional_str_parameter(parameters, "cwd", default=str(Path.home())),
        expires_at_ms=command.expires_at_ms,
        timeout_seconds=30 if timeout_seconds is None else timeout_seconds,
    )
    return await _shell_result(client, command, shell, await shell.execute(request))


async def _shell_result(
    client: nio.AsyncClient,
    command: DesktopCommand,
    shell: DesktopShell,
    result: DesktopShellResult,
    *,
    offset: int | None = None,
) -> dict[str, object]:
    """Reply inline when the encrypted response fits one to-device message, otherwise attach the output.

    Output starts at byte ``offset``; without one, a running command shows its newest output. The returned
    output covers ``[output_start, next_offset)``, and the next check continues from ``next_offset``.
    """
    output = result.output
    size = output.size
    payload: dict[str, object] = {
        "state": result.state,
        "handle": result.handle,
        "exit_code": result.exit_code,
        "output": "",
        "output_bytes": size,
        "output_truncated": output.truncated,
        "output_attachment": None,
        "output_start": offset or 0,
        "next_offset": size,
    }
    if result.state == "running":
        if offset is None:
            return _fit_output_tail(command, payload, output.tail(MAX_INLINE_RESPONSE_BYTES), requested=size)
        head = output.read(offset, MAX_INLINE_RESPONSE_BYTES)
        return _fit_output_head(command, payload, head, offset=offset, requested=size - offset)
    try:
        return await _finished_shell_result(client, command, shell, result, payload, offset or 0)
    except BaseException:
        if command.action == "run_shell":
            # The caller never learned this handle, so nothing could page from it.
            shell.hand_over(result)
        raise


async def _finished_shell_result(
    client: nio.AsyncClient,
    command: DesktopCommand,
    shell: DesktopShell,
    result: DesktopShellResult,
    payload: dict[str, object],
    start: int,
) -> dict[str, object]:
    """Deliver a finished command's output from ``start``; its handle stays until the rest arrives in full."""
    content = result.output.read(start)
    # A run_shell reply names its handle only while that handle stays to page from.
    delivered = payload if command.action == "check_shell" else {**payload, "handle": None}
    # JSON escaping only grows text, so larger output cannot fit and is never decoded here.
    if len(content) <= MAX_INLINE_RESPONSE_BYTES:
        inline = {**delivered, "output": content.decode()}
        if fits_inline(command, inline):
            shell.hand_over(result)
            return inline
    try:
        media = await upload_encrypted_media(
            client,
            content,
            mime_type=SHELL_OUTPUT_MIME_TYPE,
            filename=f"shell-{command.request_id}.txt",
            timeout_seconds=MEDIA_UPLOAD_TIMEOUT_SECONDS,
        )
    except DesktopMediaError as exc:
        error = str(exc)
    except Exception:
        logger.exception("shell_output_upload_failed", request_id=command.request_id)
        error = "Shell output upload failed."
    else:
        shell.hand_over(result)
        return {**delivered, "output_attachment": media.to_content()}
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
    head = content[:MAX_INLINE_RESPONSE_BYTES]
    return _fit_output_head(
        command,
        {**payload, "warning": warning},
        head,
        offset=start,
        requested=len(content),
    )


def _fit_output_tail(
    command: DesktopCommand,
    payload: dict[str, object],
    tail: bytes,
    *,
    requested: int,
) -> dict[str, object]:
    """Show the newest output whose escaped reply still fits inline, marking anything older as omitted."""
    text = tail.decode(errors="ignore")  # Only the first character can be cut; the spool is UTF-8.

    def reply(start: int) -> dict[str, object]:
        shown = text[start:]
        shown_bytes = len(shown.encode())
        truncated = bool(payload["output_truncated"]) or shown_bytes < requested
        output_start = cast("int", payload["next_offset"]) - shown_bytes
        return {**payload, "output": shown, "output_truncated": truncated, "output_start": output_start}

    # Dropping older characters never grows the reply, so search for the fewest to drop.
    start = leftmost_fitting(command, 0, len(text), reply)
    return reply(start)


def _fit_output_head(
    command: DesktopCommand,
    payload: dict[str, object],
    head: bytes,
    *,
    offset: int,
    requested: int,
) -> dict[str, object]:
    """Show the oldest output from ``offset`` whose escaped reply still fits inline, and where to continue."""
    # The offset starts a character, so only the last one can be cut; ``next_offset`` then points at it.
    text = head.decode(errors="ignore")

    def reply(dropped: int) -> dict[str, object]:
        shown = text[: len(text) - dropped]
        shown_bytes = len(shown.encode())
        truncated = bool(payload["output_truncated"]) or shown_bytes < requested
        return {**payload, "output": shown, "output_truncated": truncated, "next_offset": offset + shown_bytes}

    # Dropping newer characters never grows the reply, so search for the fewest to drop.
    dropped = leftmost_fitting(command, 0, len(text), reply)
    return reply(dropped)
