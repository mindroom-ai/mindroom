"""Local terminal approval for shell commands requested through a terminal-owned Desktop bridge."""

from __future__ import annotations

import asyncio
import os
import termios
import time
import unicodedata
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, TextIO, cast

if TYPE_CHECKING:
    from collections.abc import Mapping

_CHOICES = "[a]pprove once, [r]eject, [5]/[15]/[60] minutes, [u]ntil stopped"
_POLL_SECONDS = 0.25
_MAX_ANSWER_BYTES = 256
_TIMED_MINUTES = {"5": 5, "15": 15, "60": 60}
_ESCAPES = {"\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t"}
_NOT_INTERACTIVE = (
    "Standard input is not an interactive terminal, so nobody can approve shell commands here. "
    "Run `mindroom desktop run` in an interactive terminal to approve commands, or turn shell requests off "
    "with `mindroom desktop access --no-shell`."
)
_INPUT_CLOSED = (
    "Terminal input closed, so this and later shell requests are rejected. Restart "
    "`mindroom desktop run` in an interactive terminal to approve commands."
)


class _ShellApprovalControl(Protocol):
    """The local shell approval surface of a running bridge."""

    def local_status(self) -> dict[str, object]: ...

    def decide_local_shell(
        self,
        command_id: str,
        *,
        approved: bool,
        auto_approve_seconds: int,
        auto_approve_until_revoked: bool = False,
    ) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class _ShellApprovalChoice:
    """One local answer to the pending shell request."""

    approved: bool
    auto_approve_seconds: int = 0
    until_revoked: bool = False


_REJECT = _ShellApprovalChoice(approved=False)


def _parse_shell_approval(answer: str) -> _ShellApprovalChoice | None:
    """Map exactly one listed answer to a decision; anything else is no answer."""
    answer = answer.strip().lower()
    if answer == "a":
        return _ShellApprovalChoice(approved=True)
    if answer == "r":
        return _REJECT
    if answer == "u":
        return _ShellApprovalChoice(approved=True, until_revoked=True)
    minutes = _TIMED_MINUTES.get(answer)
    return _ShellApprovalChoice(approved=True, auto_approve_seconds=minutes * 60) if minutes else None


def _escape_terminal_text(value: str) -> str:
    """Show controls, format characters, separators, and backslashes as escapes the terminal cannot interpret."""
    return "".join(_escaped_character(character) for character in value)


def _escaped_character(character: str) -> str:
    if character in _ESCAPES:
        return _ESCAPES[character]
    category = unicodedata.category(character)
    if not category.startswith("C") and category not in {"Zl", "Zp"}:
        return character
    code = ord(character)
    if code <= 0xFF:
        return f"\\x{code:02x}"
    return f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}"


def auto_approval_notice(minutes: int | None) -> str:
    """Describe a local auto-approval grant, including every caller it covers and how it ends."""
    duration = (
        f"for {minutes} minute{'s' if minutes != 1 else ''}, until it expires, is revoked, or the bridge stops"
        if minutes is not None
        else "until it is revoked or the bridge stops"
    )
    return (
        "Shell commands from every locally allowed requester and agent are approved automatically "
        f"{duration}. Press Ctrl-C to stop the bridge and revoke it."
    )


def _describe_pending_request(pending: Mapping[str, object], *, now: float) -> str:
    fields = {
        "Requester": str(pending["requester_id"]),
        "Agent": str(pending["agent_name"]),
        "Working directory": str(pending["cwd"]),
        "Command": str(pending["command"]),
    }
    shown = {label: _escape_terminal_text(value) for label, value in fields.items()}
    expires_in = max(0, round(cast("int", pending["expires_at_ms"]) / 1000 - now))
    lines = [
        "",
        f"Shell command request {_escape_terminal_text(str(pending['request_id']))} (expires in {expires_in} s):",
        *(f"  {label}: {value}" for label, value in shown.items()),
        "It runs as your user account with its full access, including files outside selected folders and the network.",
    ]
    if shown != fields:
        lines.append("Control, formatting, and backslash characters are shown escaped.")
    lines.append("Timed and until-stopped choices also approve later commands from every allowed requester and agent.")
    return "\n".join(lines) + "\n"


def _confirmation(choice: _ShellApprovalChoice) -> str:
    if not choice.approved:
        return "Rejected; the command will not run."
    if choice.until_revoked:
        return f"Approved. {auto_approval_notice(None)}"
    if choice.auto_approve_seconds:
        return f"Approved. {auto_approval_notice(choice.auto_approve_seconds // 60)}"
    return "Approved once; the command runs now."


def _pending_request(control: _ShellApprovalControl) -> dict[str, object] | None:
    shell = control.local_status().get("shell")
    pending = cast("dict[str, object]", shell).get("pending") if isinstance(shell, dict) else None
    return cast("dict[str, object]", pending) if isinstance(pending, dict) else None


def _pending_request_id(control: _ShellApprovalControl) -> str | None:
    pending = _pending_request(control)
    return str(pending["request_id"]) if pending is not None else None


def _write(output: TextIO, text: str) -> None:
    output.write(text)
    output.flush()


class _RequestGoneError(Exception):
    """The pending request was settled elsewhere before an answer arrived."""


async def _read_answer(control: _ShellApprovalControl, request_id: str, input_fd: int) -> str | None:
    """Read one line typed after the prompt, or None at end of input, while the request is still pending."""
    loop = asyncio.get_running_loop()
    # Anything typed or queued before this prompt is not an answer to it.
    with suppress(OSError, termios.error):
        termios.tcflush(input_fd, termios.TCIFLUSH)
    line: asyncio.Future[str | None] = loop.create_future()
    buffer = bytearray()

    def on_readable() -> None:
        try:
            chunk = os.read(input_fd, _MAX_ANSWER_BYTES)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            chunk = b""
        if line.done():
            return
        if not chunk:
            line.set_result(None)
            return
        buffer.extend(chunk)
        if b"\n" in buffer:
            line.set_result(bytes(buffer.split(b"\n", 1)[0]).decode(errors="replace"))
        elif len(buffer) > _MAX_ANSWER_BYTES:
            line.set_result("")

    loop.add_reader(input_fd, on_readable)
    try:
        while not line.done():
            if _pending_request_id(control) != request_id:
                raise _RequestGoneError
            await asyncio.wait({line}, timeout=_POLL_SECONDS)
        return line.result()
    finally:
        loop.remove_reader(input_fd)


async def _ask(
    control: _ShellApprovalControl,
    request_id: str,
    input_fd: int,
    output: TextIO,
) -> _ShellApprovalChoice | None:
    """Ask until a listed answer arrives; None means terminal input closed."""
    while True:
        _write(output, f"{_CHOICES}: ")
        answer = await _read_answer(control, request_id, input_fd)
        if answer is None:
            return None
        choice = _parse_shell_approval(answer)
        if choice is not None:
            return choice
        _write(output, "Answer a, r, 5, 15, 60, or u.\n")


def _decide(control: _ShellApprovalControl, request_id: str, choice: _ShellApprovalChoice, output: TextIO) -> None:
    try:
        control.decide_local_shell(
            request_id,
            approved=choice.approved,
            auto_approve_seconds=choice.auto_approve_seconds,
            auto_approve_until_revoked=choice.until_revoked,
        )
    except ValueError as exc:
        _write(output, f"The shell request is no longer pending: {exc}\n")
        return
    _write(output, f"{_confirmation(choice)}\n")


async def serve_terminal_shell_approvals(
    control: _ShellApprovalControl,
    *,
    input_fd: int | None,
    output: TextIO,
) -> None:
    """Show each pending shell request and settle it only with an answer typed at this terminal.

    Terminal input is read only while a request is pending and only after its prompt; without an
    interactive terminal every request is rejected.
    """
    interactive = input_fd is not None and os.isatty(input_fd)
    # A decided request can still look pending until the bridge's waiting task runs; never ask twice.
    decided: str | None = None
    while True:
        pending = _pending_request(control)
        if pending is None or str(pending["request_id"]) == decided:
            await asyncio.sleep(_POLL_SECONDS)
            continue
        request_id = decided = str(pending["request_id"])
        _write(output, _describe_pending_request(pending, now=time.time()))
        if not interactive or input_fd is None:
            _write(output, f"{_NOT_INTERACTIVE}\n")
            _decide(control, request_id, _REJECT, output)
            continue
        try:
            choice = await _ask(control, request_id, input_fd, output)
        except _RequestGoneError:
            _write(output, "\nThe shell request is no longer pending.\n")
            continue
        if choice is None:
            interactive = False
            _write(output, f"\n{_INPUT_CLOSED}\n")
            choice = _REJECT
        _decide(control, request_id, choice, output)


__all__ = ["auto_approval_notice", "serve_terminal_shell_approvals"]
