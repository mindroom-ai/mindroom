"""Tests for approving shell commands in the terminal that owns a Desktop bridge."""

from __future__ import annotations

import asyncio
import io
import os
import time
from contextlib import suppress
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from mindroom.desktop.shell import DesktopShell, DesktopShellError, DesktopShellRequest, DesktopShellResult
from mindroom.desktop.shell_prompt import (
    _escape_terminal_text,
    _parse_shell_approval,
    _ShellApprovalChoice,
    serve_terminal_shell_approvals,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path

CHOICES = "[a]pprove once, [r]eject, [5]/[15]/[60] minutes, [u]ntil stopped"


class _ShellControl:
    """Expose one real local shell through the bridge's local approval methods."""

    def __init__(self, shell: DesktopShell) -> None:
        self.shell = shell
        self.decisions: list[tuple[str, bool, int, bool]] = []

    def local_status(self) -> dict[str, object]:
        return {"shell": {"enabled": True, **self.shell.status()}}

    def decide_local_shell(
        self,
        command_id: str,
        *,
        approved: bool,
        auto_approve_seconds: int,
        auto_approve_until_revoked: bool = False,
    ) -> dict[str, object]:
        self.decisions.append((command_id, approved, auto_approve_seconds, auto_approve_until_revoked))
        self.shell.decide(
            command_id,
            approved=approved,
            auto_approve_seconds=auto_approve_seconds,
            auto_approve_until_revoked=auto_approve_until_revoked,
        )
        return self.local_status()


def _request(
    tmp_path: Path,
    request_id: str = "request-1",
    *,
    command: str = "printf ran > marker",
    requester_id: str = "@alice:example.org",
) -> DesktopShellRequest:
    return DesktopShellRequest(
        request_id=request_id,
        requester_id=requester_id,
        agent_name="computer",
        command=command,
        cwd=str(tmp_path),
        expires_at_ms=round(time.time() * 1000) + 60_000,
    )


async def _wait_for_text(output: io.StringIO, text: str, *, occurrences: int = 1) -> None:
    for _ in range(500):
        if output.getvalue().count(text) >= occurrences:
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"terminal output never showed {text!r}: {output.getvalue()!r}")


@pytest.fixture
def terminal() -> Iterator[tuple[int, int]]:
    """Give the approver a real pseudo-terminal whose keyboard side the test controls."""
    master, slave = os.openpty()
    try:
        yield master, slave
    finally:
        for descriptor in (master, slave):
            with suppress(OSError):
                os.close(descriptor)


@pytest_asyncio.fixture
async def shell() -> AsyncIterator[DesktopShell]:
    """Run real, harmless commands only after the approver's decision."""
    local_shell = DesktopShell(environment={"PATH": os.defpath})
    try:
        yield local_shell
    finally:
        await local_shell.close()


async def _serve(control: _ShellControl, *, input_fd: int | None, output: io.StringIO) -> asyncio.Task[None]:
    return asyncio.create_task(serve_terminal_shell_approvals(control, input_fd=input_fd, output=output))


async def _stop(task: asyncio.Task[None]) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def _release(result: DesktopShellResult) -> None:
    result.output.release()


@pytest.mark.parametrize(
    ("answer", "choice"),
    [
        ("a", _ShellApprovalChoice(approved=True)),
        (" A \n", _ShellApprovalChoice(approved=True)),
        ("r", _ShellApprovalChoice(approved=False)),
        ("R", _ShellApprovalChoice(approved=False)),
        ("5", _ShellApprovalChoice(approved=True, auto_approve_seconds=300)),
        ("15", _ShellApprovalChoice(approved=True, auto_approve_seconds=900)),
        ("60", _ShellApprovalChoice(approved=True, auto_approve_seconds=3600)),
        ("u", _ShellApprovalChoice(approved=True, until_revoked=True)),
        ("", None),
        ("y", None),
        ("yes", None),
        ("10", None),
        ("approve", None),
        ("a r", None),
    ],
)
def test_approval_answers_map_to_exact_local_decisions(answer: str, choice: _ShellApprovalChoice | None) -> None:
    """Only the listed answers decide; anything else asks again instead of guessing."""
    assert _parse_shell_approval(answer) == choice


def test_request_text_escapes_control_and_directional_characters() -> None:
    """Terminal controls, bidirectional overrides, and separators cannot restyle or hide request text."""
    assert _escape_terminal_text("ls\x1b[2J\u202egnp.exe\nrm -rf ~") == "ls\\x1b[2J\\u202egnp.exe\\nrm -rf ~"
    assert _escape_terminal_text("\x9b\u2028\u2066\u200f\t\r\x7f") == "\\x9b\\u2028\\u2066\\u200f\\t\\r\\x7f"
    # A literal backslash sequence stays distinguishable from an escaped control character.
    assert _escape_terminal_text("printf 'a\\nb'") == "printf 'a\\\\nb'"
    assert _escape_terminal_text("café 日本 ~/src") == "café 日本 ~/src"


@pytest.mark.asyncio
@pytest.mark.parametrize("interactive_input", [False, True])
async def test_noninteractive_input_rejects_pending_commands_with_guidance(
    shell: DesktopShell,
    tmp_path: Path,
    *,
    interactive_input: bool,
) -> None:
    """Without a terminal nobody can approve, so requests are rejected and waiting input is never read."""
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"a\n")
    control = _ShellControl(shell)
    output = io.StringIO()
    approvals = await _serve(control, input_fd=read_fd if interactive_input else None, output=output)
    try:
        with pytest.raises(DesktopShellError, match="denied locally"):
            await asyncio.wait_for(shell.execute(_request(tmp_path)), 5)
    finally:
        await _stop(approvals)
    assert control.decisions == [("request-1", False, 0, False)]
    assert not (tmp_path / "marker").exists()
    assert "not an interactive terminal" in output.getvalue()
    assert "mindroom desktop access --no-shell" in output.getvalue()
    assert os.read(read_fd, 16) == b"a\n"
    os.close(read_fd)
    os.close(write_fd)


@pytest.mark.asyncio
async def test_terminal_shows_escaped_request_and_runs_only_after_approval(
    shell: DesktopShell,
    terminal: tuple[int, int],
    tmp_path: Path,
) -> None:
    """The person at the terminal sees the exact escaped request, and approval runs that request once."""
    master, slave = terminal
    control = _ShellControl(shell)
    output = io.StringIO()
    approvals = await _serve(control, input_fd=slave, output=output)
    command = "printf ran > marker\n# \x1b[2J\u202ehidden"
    execution = asyncio.create_task(
        shell.execute(_request(tmp_path, command=command, requester_id="@alice:example.org\u2066")),
    )
    try:
        await _wait_for_text(output, CHOICES)
        shown = output.getvalue()
        assert "Requester: @alice:example.org\\u2066" in shown
        assert "Agent: computer" in shown
        assert f"Working directory: {tmp_path}" in shown
        assert "Command: printf ran > marker\\n# \\x1b[2J\\u202ehidden" in shown
        assert "\x1b" not in shown
        assert "\u202e" not in shown
        assert "full access" in shown
        assert not (tmp_path / "marker").exists()
        os.write(master, b"a\n")
        result = await asyncio.wait_for(execution, 5)
    finally:
        await _stop(approvals)
    _release(result)
    assert result.exit_code == 0
    assert (tmp_path / "marker").read_text() == "ran"
    assert control.decisions == [("request-1", True, 0, False)]
    assert "Approved once" in output.getvalue()


@pytest.mark.asyncio
async def test_text_waiting_before_the_prompt_is_never_an_answer(
    shell: DesktopShell,
    terminal: tuple[int, int],
    tmp_path: Path,
) -> None:
    """Input queued before a request appears is discarded; only an answer typed after the prompt decides."""
    master, slave = terminal
    control = _ShellControl(shell)
    output = io.StringIO()
    approvals = await _serve(control, input_fd=slave, output=output)
    os.write(master, b"a\n")
    execution = asyncio.create_task(shell.execute(_request(tmp_path)))
    try:
        await _wait_for_text(output, CHOICES)
        os.write(master, b"maybe\n")
        await _wait_for_text(output, "Answer a, r, 5, 15, 60, or u.")
        os.write(master, b"r\n")
        with pytest.raises(DesktopShellError, match="denied locally"):
            await asyncio.wait_for(execution, 5)
    finally:
        await _stop(approvals)
    assert control.decisions == [("request-1", False, 0, False)]
    assert not (tmp_path / "marker").exists()
    assert "Rejected" in output.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "decision", "scope"),
    [
        (b"15\n", ("request-1", True, 900, False), "for 15 minutes, until it expires, is revoked, or the bridge stops"),
        (b"u\n", ("request-1", True, 0, True), "until it is revoked or the bridge stops"),
    ],
)
async def test_auto_approval_choices_cover_every_allowed_caller_and_say_so(
    shell: DesktopShell,
    terminal: tuple[int, int],
    tmp_path: Path,
    answer: bytes,
    decision: tuple[str, bool, int, bool],
    scope: str,
) -> None:
    """A timed or until-stopped choice approves later commands too, and the confirmation names that scope."""
    master, slave = terminal
    control = _ShellControl(shell)
    output = io.StringIO()
    approvals = await _serve(control, input_fd=slave, output=output)
    execution = asyncio.create_task(shell.execute(_request(tmp_path)))
    try:
        await _wait_for_text(output, CHOICES)
        assert "every allowed requester and agent" in output.getvalue()
        os.write(master, answer)
        _release(await asyncio.wait_for(execution, 5))
        later = await asyncio.wait_for(
            shell.execute(_request(tmp_path, "request-2", command="printf later > later", requester_id="@bob:org")),
            5,
        )
    finally:
        await _stop(approvals)
    _release(later)
    assert (tmp_path / "later").read_text() == "later"
    assert control.decisions == [decision]
    shown = output.getvalue()
    assert "every locally allowed requester and agent" in shown
    assert scope in shown
    assert shown.count(CHOICES) == 1


@pytest.mark.asyncio
async def test_request_that_stops_pending_releases_the_terminal(
    shell: DesktopShell,
    terminal: tuple[int, int],
    tmp_path: Path,
) -> None:
    """A revoked request stops waiting for an answer, and later typing is left unread until another prompt."""
    master, slave = terminal
    control = _ShellControl(shell)
    output = io.StringIO()
    approvals = await _serve(control, input_fd=slave, output=output)
    execution = asyncio.create_task(shell.execute(_request(tmp_path)))
    try:
        await _wait_for_text(output, CHOICES)
        shell.revoke()
        with pytest.raises(DesktopShellError, match="cancelled before approval"):
            await asyncio.wait_for(execution, 5)
        await _wait_for_text(output, "no longer pending")
        os.write(master, b"a\n")
        await asyncio.sleep(0.3)
        assert os.read(slave, 16) == b"a\n"
    finally:
        await _stop(approvals)
    assert control.decisions == []
    assert not (tmp_path / "marker").exists()


@pytest.mark.asyncio
async def test_closed_terminal_input_rejects_now_and_later(
    shell: DesktopShell,
    terminal: tuple[int, int],
    tmp_path: Path,
) -> None:
    """End of input is a rejection, and later requests fail closed instead of waiting for an answer."""
    master, slave = terminal
    control = _ShellControl(shell)
    output = io.StringIO()
    approvals = await _serve(control, input_fd=slave, output=output)
    execution = asyncio.create_task(shell.execute(_request(tmp_path)))
    try:
        await _wait_for_text(output, CHOICES)
        os.close(master)
        with pytest.raises(DesktopShellError, match="denied locally"):
            await asyncio.wait_for(execution, 5)
        with pytest.raises(DesktopShellError, match="denied locally"):
            await asyncio.wait_for(shell.execute(_request(tmp_path, "request-2")), 5)
    finally:
        await _stop(approvals)
    assert control.decisions == [("request-1", False, 0, False), ("request-2", False, 0, False)]
    assert "input closed" in output.getvalue()
    assert not (tmp_path / "marker").exists()


@pytest.mark.asyncio
async def test_background_job_rejects_without_touching_the_terminal(
    shell: DesktopShell,
    terminal: tuple[int, int],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reading or flushing the terminal from a background job would stop the bridge, so it rejects instead."""
    master, slave = terminal
    monkeypatch.setattr("mindroom.desktop.shell_prompt.os.tcgetpgrp", lambda _fd: os.getpgrp() + 1)
    os.write(master, b"a\n")
    control = _ShellControl(shell)
    output = io.StringIO()
    approvals = await _serve(control, input_fd=slave, output=output)
    try:
        with pytest.raises(DesktopShellError, match="denied locally"):
            await asyncio.wait_for(shell.execute(_request(tmp_path)), 5)
    finally:
        await _stop(approvals)
    assert control.decisions == [("request-1", False, 0, False)]
    assert "runs in the background" in output.getvalue()
    assert os.read(slave, 16) == b"a\n"
    assert not (tmp_path / "marker").exists()
