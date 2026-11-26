"""Tests for desktop shell actions, their local and caller-scoped status, and their output delivery."""

from __future__ import annotations

import asyncio
import json
import re
import shlex
import signal
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import nio
import pytest

from mindroom.desktop.media import DesktopMediaError, download_encrypted_media, upload_encrypted_media
from mindroom.desktop.protocol import DesktopCommand, DesktopProtocolError, EncryptedDesktopMedia
from mindroom.desktop.reply_fitting import fits_inline
from mindroom.desktop.shell import DesktopShell, DesktopShellError, DesktopShellOutput
from mindroom.desktop.shell_actions import caller_shell_status, execute_shell, shell_status
from tests.desktop_helpers import (
    _LARGE_OUTPUT,
    _LONGEST_SESSION_ID,
    ALICE,
    APP_ID,
    BOB,
    MEDIA,
    PRIVATE_COMMAND,
    _assert_upload_fallback,
    _command,
    _handle_command,
    _local_shell,
    _longest_request_id,
    _run_shell,
    _stall_output_uploads,
)

_CLIENT = AsyncMock(spec=nio.AsyncClient)
_UNKNOWN_HANDLE = r"^Unknown shell handle\.$"


@pytest.fixture(autouse=True)
def fake_output_uploads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer every shell output upload with fake encrypted media unless a test installs its own upload."""
    monkeypatch.setattr("mindroom.desktop.shell_actions.upload_encrypted_media", AsyncMock(return_value=MEDIA))


async def _execute(shell: DesktopShell, command: DesktopCommand) -> dict[str, object]:
    return await execute_shell(_CLIENT, shell, command)


async def _wait_for_pending(shell: DesktopShell) -> dict[str, object]:
    for _ in range(200):
        pending = shell_status(shell)["pending"]
        if isinstance(pending, dict):
            return pending
        await asyncio.sleep(0.005)
    pytest.fail("shell approval never became pending")


async def _wait_for_active(shell: DesktopShell) -> str:
    for _ in range(200):
        active_request_id = shell_status(shell)["active_request_id"]
        if isinstance(active_request_id, str):
            return active_request_id
        await asyncio.sleep(0.005)
    pytest.fail("shell command never became active")


async def _check_until_finished(
    shell: DesktopShell,
    handle: str,
    *,
    first_sequence: int,
    **parameters: object,
) -> tuple[dict[str, object], int]:
    for sequence in range(first_sequence, first_sequence + 600):
        result = await _execute(shell, _handle_command("check_shell", handle, sequence=sequence, **parameters))
        if result["state"] != "running":
            return result, sequence
        await asyncio.sleep(0.01)
    pytest.fail("shell handle never finished")


async def _wait_for_finished_handle(shell: DesktopShell, handle: str) -> None:
    for _ in range(600):
        entries = {entry["handle"]: entry["state"] for entry in shell_status(shell)["handles"]}
        if entries.get(handle) != "running":
            return
        await asyncio.sleep(0.01)
    pytest.fail("shell handle never finished")


@pytest.mark.asyncio
async def test_shell_actions_require_a_shell() -> None:
    """Without local shell access there is nothing to run, check, or kill."""
    with pytest.raises(DesktopProtocolError, match=r"^Local shell access is disabled\.$"):
        await execute_shell(_CLIENT, None, _command("run_shell", parameters={"command": PRIVATE_COMMAND}))


def test_disabled_shell_reports_empty_status_to_everyone() -> None:
    """A bridge without a shell reports it disabled, with nothing pending, active, or retained."""
    disabled = {
        "enabled": False,
        "pending": None,
        "auto_approve_remaining_seconds": 0,
        "auto_approve_until_revoked": False,
        "active_request_id": None,
        "handles": [],
    }
    assert shell_status(None) == disabled
    assert caller_shell_status(None, _command("status")) == {**disabled, "pending": False}


@pytest.mark.asyncio
async def test_run_shell_defaults_to_local_home_and_requires_local_approval() -> None:
    """Omitted cwd resolves locally, and a local rejection starts no process."""
    shell = _local_shell()
    execution = asyncio.create_task(_execute(shell, _command("run_shell", parameters={"command": PRIVATE_COMMAND})))
    pending = await _wait_for_pending(shell)
    assert pending == {
        "request_id": "request-1",
        "requester_id": ALICE,
        "agent_name": "computer",
        "command": PRIVATE_COMMAND,
        "cwd": str(Path.home()),
        "expires_at_ms": 11_000,
    }
    shell.decide("request-1", approved=False, auto_approve_seconds=0)
    with pytest.raises(DesktopShellError, match=r"^Shell command denied locally\.$"):
        await execution


@pytest.mark.asyncio
async def test_long_working_directory_reaches_the_shell(tmp_path: Path) -> None:
    """Working directories are not limited to identifier length."""
    nested = (tmp_path / Path(*["d" * 60] * 5)).resolve()
    nested.mkdir(parents=True)
    shell = _local_shell()
    shell.grant(60)
    run = _run_shell("pwd", nested)
    assert (await _execute(shell, run))["output"] == f"{nested}\n"
    await shell.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "parameters", "error"),
    [
        ("run_shell", {"command": PRIVATE_COMMAND, "app": APP_ID}, "Unexpected desktop parameters: app."),
        (
            "run_shell",
            {"command": PRIVATE_COMMAND, "timeout_seconds": "5"},
            "Desktop parameter timeout_seconds must be an integer.",
        ),
        ("run_shell", {"command": ""}, "Desktop parameter command must be a non-empty string."),
        ("check_shell", {}, "Desktop parameter handle must be a non-empty string."),
        ("check_shell", {"handle": "shell:1", "force": True}, "Unexpected desktop parameters: force."),
        ("kill_shell", {"handle": "shell:1", "force": "yes"}, "Desktop parameter force must be a boolean."),
        ("kill_shell", {"handle": "shell:1", "command": PRIVATE_COMMAND}, "Unexpected desktop parameters: command."),
    ],
)
async def test_shell_actions_reject_unrelated_or_malformed_parameters(
    tmp_path: Path,
    action: str,
    parameters: dict[str, object],
    error: str,
) -> None:
    """Strict parameters are checked before any approval request exists."""
    shell = _local_shell()
    if action == "run_shell":
        parameters = {**parameters, "cwd": str(tmp_path)}
    with pytest.raises(DesktopProtocolError, match=f"^{re.escape(error)}$"):
        await _execute(shell, _command(action, parameters=parameters))
    assert shell.status()["pending"] is None
    assert not (tmp_path / "marker").exists()


@pytest.mark.asyncio
async def test_caller_shell_status_hides_another_callers_pending_command(tmp_path: Path) -> None:
    """Another allowed caller learns only that approval is pending; the local view shows the command."""
    shell = _local_shell()
    command = _command("run_shell", parameters={"command": PRIVATE_COMMAND, "cwd": str(tmp_path)})
    execution = asyncio.create_task(_execute(shell, command))
    await _wait_for_pending(shell)
    status = caller_shell_status(shell, _command("status", requester_id=BOB))
    assert status == {
        "enabled": True,
        "pending": True,
        "auto_approve_remaining_seconds": 0,
        "auto_approve_until_revoked": False,
        "active_request_id": None,
        "handles": [],
    }
    assert "private-shell-text" not in json.dumps(status)
    assert shell_status(shell)["pending"]["command"] == PRIVATE_COMMAND
    shell.decide("request-1", approved=False, auto_approve_seconds=0)
    with pytest.raises(DesktopShellError):
        await execution
    assert not (tmp_path / "marker").exists()


@pytest.mark.asyncio
async def test_other_callers_see_active_shell_without_its_request_id(tmp_path: Path) -> None:
    """A different allowed caller sees that a command is active, but never learns its request ID."""
    shell = _local_shell()
    shell.grant(60)
    command = _run_shell("sleep 30", tmp_path, request_id="request-1", timeout_seconds=30)
    execution = asyncio.create_task(_execute(shell, command))
    await _wait_for_active(shell)
    bob = _command("status", requester_id=BOB)
    assert caller_shell_status(shell, bob)["active_request_id"] is None
    alice = _command("status")
    assert caller_shell_status(shell, alice)["active_request_id"] == "request-1"
    await asyncio.wait_for(shell.close(), timeout=3)
    with pytest.raises(DesktopShellError):
        await execution


@pytest.mark.asyncio
async def test_shell_handle_lifecycle_returns_full_output(tmp_path: Path) -> None:
    """A command past its inline wait becomes a handle, and one completed check returns its output from the offset."""
    shell = _local_shell()
    shell.grant(60)
    command = "printf 'early\\n'; while [ ! -f release ]; do sleep 0.05; done; printf late; exit 3"
    running = await _execute(shell, _run_shell(command, tmp_path))
    handle = running["handle"]
    assert isinstance(handle, str)
    assert running == {
        "state": "running",
        "handle": handle,
        "exit_code": None,
        "output": "early\n",
        "output_bytes": 6,
        "output_truncated": False,
        "output_attachment": None,
        "output_start": 0,
        "next_offset": 6,
    }
    assert (await _execute(shell, _handle_command("check_shell", handle, sequence=2)))["state"] == "running"

    (tmp_path / "release").touch()
    completed, sequence = await _check_until_finished(shell, handle, first_sequence=3, offset=6)
    assert completed == {
        "state": "completed",
        "handle": handle,
        "exit_code": 3,
        "output": "late",
        "output_bytes": 10,
        "output_truncated": False,
        "output_attachment": None,
        "output_start": 6,
        "next_offset": 10,
    }
    with pytest.raises(DesktopShellError, match=_UNKNOWN_HANDLE):
        await _execute(shell, _handle_command("check_shell", handle, sequence=sequence + 1))
    await shell.close()


@pytest.mark.asyncio
async def test_fitted_shell_results_fit_inline_with_maximum_length_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tail, offset, and upload-fallback shell results fit one reply with the widest metrics and longest IDs."""
    monkeypatch.setattr(
        "mindroom.desktop.shell_actions.upload_encrypted_media",
        AsyncMock(side_effect=DesktopMediaError("Matrix media upload failed: offline")),
    )
    (tmp_path / "output").write_bytes(("é" * 30_000).encode())
    shell = _local_shell()
    shell.grant(60)
    command = "cat output; while [ ! -f release ]; do sleep 0.05; done; cat output"
    started = replace(
        _run_shell(command, tmp_path, request_id=_longest_request_id("run")),
        session_id=_LONGEST_SESSION_ID,
    )
    running = await _execute(shell, started)
    assert fits_inline(started, running)
    handle = running["handle"]
    assert isinstance(handle, str)

    async def check(sequence: int, **parameters: object) -> dict[str, object]:
        command = _command(
            "check_shell",
            request_id=_longest_request_id(f"check-{sequence}-"),
            session_id=_LONGEST_SESSION_ID,
            sequence=sequence,
            parameters={"handle": handle, **parameters},
        )
        result = await _execute(shell, command)
        assert fits_inline(command, result)
        return result

    await check(2, offset=0)
    (tmp_path / "release").touch()
    await _wait_for_finished_handle(shell, handle)
    final = await check(3)
    assert (final["state"], final["output_truncated"]) == ("completed", True)
    assert "could not be attached" in str(final["warning"])
    await shell.close()


@pytest.mark.asyncio
async def test_running_tail_reports_where_its_output_starts_so_skipped_output_stays_readable(tmp_path: Path) -> None:
    """A trimmed newest-output reply says where it begins, and polling from 0 still returns the beginning."""
    content = b"".join(f"line {number:05}\n".encode() for number in range(10_000))
    (tmp_path / "log").write_bytes(content)
    shell = _local_shell()
    shell.grant(60)
    started = await _execute(shell, _run_shell("cat log; while [ ! -f release ]; do sleep 0.05; done", tmp_path))
    handle = started["handle"]
    assert isinstance(handle, str)
    for sequence in range(2, 600):
        tail = await _execute(shell, _handle_command("check_shell", handle, sequence=sequence))
        if tail["output_bytes"] == len(content):
            break
        await asyncio.sleep(0.01)
    shown = str(tail["output"]).encode()
    assert 0 < tail["output_start"] == len(content) - len(shown)
    assert (tail["next_offset"], content[tail["output_start"] :]) == (len(content), shown)
    head = await _execute(shell, _handle_command("check_shell", handle, sequence=700, offset=0))
    assert (head["output_start"], content.startswith(str(head["output"]).encode())) == (0, True)
    assert head["next_offset"] == len(str(head["output"]).encode())
    await shell.close()


@pytest.mark.asyncio
async def test_offset_polls_leave_later_output_of_a_running_command_intact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bounded offset read never moves where the still-running command's next output is written."""
    upload = AsyncMock(return_value=replace(MEDIA, mime_type="text/plain"))
    monkeypatch.setattr("mindroom.desktop.shell_actions.upload_encrypted_media", upload)
    first, second = b"a" * 50_000 + b"\n", b"b" * 50_000 + b"\n"
    (tmp_path / "first").write_bytes(first)
    (tmp_path / "second").write_bytes(second)
    shell = _local_shell()
    shell.grant(60)
    command = "cat first; while [ ! -f release ]; do sleep 0.05; done; cat second"
    handle = (await _execute(shell, _run_shell(command, tmp_path)))["handle"]
    assert isinstance(handle, str)
    for sequence in range(2, 600):
        polled = await _execute(shell, _handle_command("check_shell", handle, sequence=sequence, offset=0))
        if polled["output_bytes"] == len(first):
            break
        await asyncio.sleep(0.01)
    assert (polled["output_bytes"], polled["next_offset"] < len(first)) == (len(first), True)
    (tmp_path / "release").touch()
    await _wait_for_finished_handle(shell, handle)
    completed = await _execute(shell, _handle_command("check_shell", handle, sequence=700, offset=0))
    assert (completed["state"], completed["output_bytes"]) == ("completed", len(first) + len(second))
    assert upload.await_args.args[1] == first + second
    await shell.close()


@pytest.mark.asyncio
async def test_check_shell_offset_polls_every_byte_once_without_splitting_characters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Polling from each next_offset returns contiguous whole characters, and the completed slice may be attached."""
    media = replace(MEDIA, mime_type="text/plain")
    upload = AsyncMock(return_value=media)
    monkeypatch.setattr("mindroom.desktop.shell_actions.upload_encrypted_media", upload)
    first = ("é" * 30_000).encode()  # More than one inline reply, in two-byte characters.
    rest = ("ü" * 30_000 + "end").encode()
    printer = f"{shlex.quote(sys.executable)} -c 'import sys; sys.stdout.buffer.write(bytes.fromhex(sys.stdin.read()))'"
    (tmp_path / "first").write_text(first.hex())
    (tmp_path / "rest").write_text(rest.hex())
    command = f"{printer} < first; while [ ! -f release ]; do sleep 0.05; done; {printer} < rest"
    shell = _local_shell()
    shell.grant(60)
    started = await _execute(shell, _run_shell(command, tmp_path))
    handle = started["handle"]
    assert isinstance(handle, str)
    assert started["next_offset"] == started["output_bytes"]

    received, offset, polls = b"", 0, 0
    for sequence in range(2, 600):
        check = _handle_command("check_shell", handle, sequence=sequence, offset=offset)
        reply = await _execute(shell, check)
        assert fits_inline(check, reply)
        shown = str(reply["output"]).encode()
        assert (reply["output_start"], reply["next_offset"]) == (offset, offset + len(shown))
        assert reply["output_truncated"] is (reply["next_offset"] < reply["output_bytes"])
        received, offset = received + shown, offset + len(shown)
        polls += bool(shown)
        if offset == len(first):
            break
        await asyncio.sleep(0.01)
    assert (received, polls > 1) == (first, True)

    for sequence, bad_offset, error in (
        (700, len(first) + 1, "past the captured output"),
        (701, 1, "start of a UTF-8 character"),
        (702, -1, "nonnegative"),
    ):
        with pytest.raises(DesktopShellError, match=error):
            await _execute(shell, _handle_command("check_shell", handle, sequence=sequence, offset=bad_offset))

    (tmp_path / "release").touch()
    await _wait_for_finished_handle(shell, handle)
    completed = await _execute(shell, _handle_command("check_shell", handle, sequence=703, offset=offset))
    assert (completed["state"], completed["exit_code"], completed["output"]) == ("completed", 0, "")
    assert (completed["output_bytes"], completed["next_offset"]) == (len(first) + len(rest), len(first) + len(rest))
    assert completed["output_attachment"] == media.to_content()
    assert upload.await_args.args[1] == rest
    await shell.close()


@pytest.mark.asyncio
async def test_handles_answer_only_their_owner(tmp_path: Path) -> None:
    """Another allowed requester gets exactly the error of a handle that never existed."""
    shell = _local_shell()
    shell.grant(60)
    handle = (await _execute(shell, _run_shell("sleep 30", tmp_path)))["handle"]
    assert isinstance(handle, str)
    attempts = (
        ("check_shell", handle, BOB),
        ("kill_shell", handle, BOB),
        ("check_shell", "shell:00000000", ALICE),
        ("kill_shell", "shell:00000000", ALICE),
    )
    for sequence, (action, target, requester_id) in enumerate(attempts, start=2):
        with pytest.raises(DesktopShellError, match=_UNKNOWN_HANDLE):
            await _execute(shell, _handle_command(action, target, sequence=sequence, requester_id=requester_id))

    assert await _execute(shell, _handle_command("kill_shell", handle, sequence=6)) == {
        "state": "killed",
        "handle": handle,
    }
    killed, _ = await _check_until_finished(shell, handle, first_sequence=7)
    assert (killed["state"], killed["exit_code"]) == ("killed", -signal.SIGTERM)
    await shell.close()


@pytest.mark.asyncio
async def test_remote_status_lists_only_the_callers_own_handles(tmp_path: Path) -> None:
    """Handles are visible to their owner remotely and to everyone at the Mac locally."""
    shell = _local_shell()
    shell.grant(60)
    handle = (await _execute(shell, _run_shell("sleep 30", tmp_path)))["handle"]
    [entry] = caller_shell_status(shell, _command("status"))["handles"]
    assert {key: entry[key] for key in ("handle", "requester_id", "agent_name", "command_preview", "state")} == {
        "handle": handle,
        "requester_id": ALICE,
        "agent_name": "computer",
        "command_preview": "sleep 30",
        "state": "running",
    }
    assert caller_shell_status(shell, _command("status", requester_id=BOB))["handles"] == []
    assert [local["handle"] for local in shell_status(shell)["handles"]] == [handle]
    await shell.close()


@pytest.mark.asyncio
async def test_output_over_the_inline_limit_round_trips_as_an_encrypted_attachment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Running handles show the newest output that fits; completion sends every byte as encrypted text media."""
    uploaded: list[bytes] = []

    async def upload(_client: object, content: bytes, *, content_type: str, filename: str) -> nio.UploadResponse:
        assert content_type == "application/octet-stream"
        assert filename.startswith("shell-check_shell-")
        assert filename.endswith(".txt.enc")
        uploaded.append(content)
        return nio.UploadResponse("mxc://example.org/shell-output")

    monkeypatch.setattr("mindroom.desktop.shell_actions.upload_encrypted_media", upload_encrypted_media)
    monkeypatch.setattr("mindroom.desktop.media.upload_media_bytes", upload)
    text = '"é" * 60_000 + "\\x01" * 1_000 + "end"'
    expected = ("é" * 60_000 + "\x01" * 1_000 + "end").encode()
    script = f"import sys; sys.stdout.buffer.write(({text}).encode()); sys.stdout.flush()"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}; while [ ! -f release ]; do sleep 0.05; done"
    shell = _local_shell()
    shell.grant(60)
    run = _run_shell(command, tmp_path)
    running = await _execute(shell, run)
    handle = running["handle"]
    assert isinstance(handle, str)
    shown = str(running["output"])
    assert fits_inline(run, running)
    assert expected.decode().endswith(shown)
    assert 0 < len(shown.encode()) < len(expected)
    assert (running["output_bytes"], running["output_truncated"]) == (len(expected), True)
    assert running["output_attachment"] is None

    (tmp_path / "release").touch()
    completed, _ = await _check_until_finished(shell, handle, first_sequence=2)
    assert {key: value for key, value in completed.items() if key != "output_attachment"} == {
        "state": "completed",
        "handle": handle,
        "exit_code": 0,
        "output": "",
        "output_bytes": len(expected),
        "output_truncated": False,
        "output_start": 0,
        "next_offset": len(expected),
    }
    media = EncryptedDesktopMedia.from_content(completed["output_attachment"], kind="output_attachment")
    assert (media.mime_type, media.size) == ("text/plain", len(expected))
    assert expected not in uploaded[0]
    client = AsyncMock(spec=nio.AsyncClient)
    client.download.return_value = nio.DownloadResponse(uploaded[0], "application/octet-stream", None)
    assert await download_encrypted_media(client, media, timeout_seconds=1) == expected
    await shell.close()


@pytest.mark.asyncio
async def test_failed_upload_of_a_finished_run_shell_keeps_exit_code_and_a_first_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An attachment failure still shows the exit code and the output's start, with a handle to page on from."""
    monkeypatch.setattr(
        "mindroom.desktop.shell_actions.upload_encrypted_media",
        AsyncMock(side_effect=DesktopMediaError("Matrix media upload failed: offline")),
    )
    shell = _local_shell()
    shell.grant(60)
    script = "import sys; sys.stdout.write('x' * 100_000 + 'tail')"
    run = _run_shell(f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}; exit 4", tmp_path)
    result = await _execute(shell, run)
    assert (result["state"], result["exit_code"], result["output_attachment"]) == ("completed", 4, None)
    assert isinstance(result["handle"], str)
    assert (result["output_start"], set(str(result["output"]))) == (0, {"x"})
    assert result["next_offset"] == len(str(result["output"]))
    assert (result["output_bytes"], result["output_truncated"]) == (100_004, True)
    assert "Matrix media upload failed: offline" in str(result["warning"])
    assert "Continue with check_shell from next_offset" in str(result["warning"])
    assert fits_inline(run, result)
    await shell.close()


@pytest.mark.asyncio
async def test_output_past_the_capture_cap_is_reported_as_truncated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Commands that print more than the capture cap say so instead of implying complete output."""
    monkeypatch.setattr("mindroom.desktop.shell.MAX_SHELL_OUTPUT_BYTES", 1_024)
    shell = _local_shell()
    shell.grant(60)
    script = "import sys; sys.stdout.write('z' * 20_000)"
    result = await _execute(shell, _run_shell(f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}", tmp_path))
    assert result["output_truncated"] is True
    assert int(result["output_bytes"]) <= 1_024
    assert set(str(result["output"])) <= {"z"}
    await shell.close()


@pytest.mark.parametrize("later_uploads", ["fail", "succeed"])
@pytest.mark.asyncio
async def test_failed_upload_keeps_a_finished_handle_until_its_output_is_delivered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    later_uploads: str,
) -> None:
    """After a failed attachment the agent pages on from next_offset, and only a complete page consumes the handle."""
    media = replace(MEDIA, mime_type="text/plain")
    failure = DesktopMediaError("Matrix media upload failed: offline")
    upload = AsyncMock(side_effect=[failure, *([failure] * 10 if later_uploads == "fail" else [media])])
    monkeypatch.setattr("mindroom.desktop.shell_actions.upload_encrypted_media", upload)
    content = b"".join(f"line {number:05}\n".encode() for number in range(9_000))
    (tmp_path / "log").write_bytes(content)
    shell = _local_shell()
    shell.grant(60)
    handle = (await _execute(shell, _run_shell("while [ ! -f release ]; do sleep 0.05; done; cat log", tmp_path)))[
        "handle"
    ]
    assert isinstance(handle, str)
    (tmp_path / "release").touch()
    await _wait_for_finished_handle(shell, handle)
    received, offset = b"", 0
    for sequence in range(2, 20):
        page = await _execute(shell, _handle_command("check_shell", handle, sequence=sequence, offset=offset))
        assert (page["state"], page["exit_code"], page["output_start"]) == ("completed", 0, offset)
        attached = page["output_attachment"] is not None
        shown = upload.await_args.args[1] if attached else str(page["output"]).encode()
        assert page["next_offset"] == offset + len(shown)
        received, offset = received + shown, offset + len(shown)
        if offset == len(content):
            assert "warning" not in page
            break
        assert "could not be attached" in str(page["warning"])
        assert [entry["handle"] for entry in shell_status(shell)["handles"]] == [handle]
    assert received == content
    assert shell_status(shell)["handles"] == []
    with pytest.raises(DesktopShellError, match=_UNKNOWN_HANDLE):
        await _execute(shell, _handle_command("check_shell", handle, sequence=30, offset=offset))
    await shell.close()


@pytest.mark.parametrize("ending", ["paged", "revoked"])
@pytest.mark.asyncio
async def test_undelivered_output_of_a_finished_run_shell_stays_pageable_until_paged_or_revoked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ending: str,
) -> None:
    """Undeliverable output of a finished run_shell stays behind a handle until paged in full or revoked."""
    monkeypatch.setattr(
        "mindroom.desktop.shell_actions.upload_encrypted_media",
        AsyncMock(side_effect=DesktopMediaError("Matrix media upload failed: offline")),
    )
    released: list[DesktopShellOutput] = []
    release = DesktopShellOutput.release

    def record_release(output: DesktopShellOutput) -> None:
        released.append(output)
        release(output)

    monkeypatch.setattr(DesktopShellOutput, "release", record_release)
    content = b"".join(f"line {number:05}\n".encode() for number in range(9_000))
    (tmp_path / "log").write_bytes(content)
    shell = _local_shell()
    shell.grant(60)
    first = await _execute(shell, _run_shell("cat log", tmp_path))
    handle = first["handle"]
    assert isinstance(handle, str)
    assert (first["state"], first["exit_code"], first["output_start"]) == ("completed", 0, 0)
    assert "Continue with check_shell from next_offset" in str(first["warning"])
    received, offset = str(first["output"]).encode(), first["next_offset"]
    assert received == content[:offset]
    spool = Path(str(shell._directory))
    assert released == []
    if ending == "revoked":
        shell.revoke()
    else:
        for sequence in range(3, 20):
            page = await _execute(shell, _handle_command("check_shell", handle, sequence=sequence, offset=offset))
            shown = str(page["output"]).encode()
            assert (page["output_start"], page["next_offset"]) == (offset, offset + len(shown))
            received, offset = received + shown, offset + len(shown)
            if offset == len(content):
                break
            assert released == []
        assert received == content
    assert [output.stdout.file.closed for output in released] == [True]
    assert shell_status(shell)["handles"] == []
    with pytest.raises(DesktopShellError, match=_UNKNOWN_HANDLE):
        await _execute(shell, _handle_command("check_shell", handle, sequence=30, offset=offset))
    await shell.close()
    assert not spool.exists()


@pytest.mark.parametrize("action", ["run_shell", "check_shell"])
@pytest.mark.asyncio
async def test_stalled_output_upload_falls_back_to_a_first_page_within_the_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    """A stalled attachment upload cannot hold a shell result past the upload bound."""
    _started, released = _stall_output_uploads(monkeypatch)
    shell = _local_shell()
    shell.grant(60)
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(_LARGE_OUTPUT)}"
    if action == "run_shell":
        result = await asyncio.wait_for(_execute(shell, _run_shell(command, tmp_path)), timeout=3)
    else:
        started = await _execute(shell, _run_shell(f"{command}; while [ ! -f release ]; do sleep 0.05; done", tmp_path))
        handle = started["handle"]
        assert isinstance(handle, str)
        (tmp_path / "release").touch()
        result, _ = await asyncio.wait_for(_check_until_finished(shell, handle, first_sequence=2), 5)
    _assert_upload_fallback(result)
    # The handle keeps its output for the next page.
    assert released == []
    await shell.close()


@pytest.mark.asyncio
async def test_split_stderr_streams_inline_and_pages_independently(tmp_path: Path) -> None:
    """Running and finished shell replies carry separate stdout/stderr fields and support stderr_offset."""
    shell = _local_shell()
    shell.grant(60)
    command = (
        "printf 'out1'; printf 'err1' >&2; "
        "while [ ! -f release ]; do sleep 0.05; done; "
        "printf 'out2'; printf 'err2' >&2"
    )
    running = await _execute(shell, _run_shell(command, tmp_path, separate_stderr=True))
    handle = running["handle"]
    assert isinstance(handle, str)
    assert (running["output"], running["next_offset"]) == ("out1", 4)
    assert (running["stderr"], running["stderr_next_offset"]) == ("err1", 4)

    (tmp_path / "release").touch()
    finished = None
    for seq in range(2, 200):
        reply = await _execute(
            shell,
            _handle_command("check_shell", handle, sequence=seq, offset=4, stderr_offset=4),
        )
        if reply["state"] != "running":
            finished = reply
            break
        await asyncio.sleep(0.02)
    assert finished is not None
    assert (finished["output"], finished["output_start"], finished["next_offset"]) == ("out2", 4, 8)
    assert (finished["stderr"], finished["stderr_start"], finished["stderr_next_offset"]) == ("err2", 4, 8)
    await shell.close()


@pytest.mark.asyncio
async def test_large_split_stdout_and_stderr_attach_separately_and_fallback_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Large stdout and stderr upload separately; if stderr upload fails, handle stays open with fitted pages."""
    uploaded: list[tuple[str, bytes]] = []

    async def fake_upload(
        _client: object,
        content: bytes,
        *,
        mime_type: str,
        filename: str,
        timeout_seconds: float,
    ) -> EncryptedDesktopMedia:
        del mime_type, timeout_seconds
        uploaded.append((filename, content))
        if filename.endswith("-stderr.txt") and len(uploaded) == 2:
            msg = "stderr upload failed"
            raise DesktopMediaError(msg)
        return replace(MEDIA, url=f"mxc://example.org/{filename}", mime_type="text/plain", size=len(content))

    monkeypatch.setattr("mindroom.desktop.shell_actions.upload_encrypted_media", fake_upload)
    shell = _local_shell()
    shell.grant(60)
    script = "import sys; sys.stdout.write('O' * 60_000); sys.stderr.write('E' * 60_000)"
    first = await _execute(
        shell,
        _run_shell(
            f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}",
            tmp_path,
            timeout_seconds=5,
            separate_stderr=True,
        ),
    )
    # First attempt failed on stderr upload -> handle kept open and both streams make inline progress.
    handle = first["handle"]
    assert isinstance(handle, str)
    assert first["output_attachment"] is None
    assert first["stderr_attachment"] is None
    assert "stderr upload failed" in str(first["warning"])
    assert 0 < first["next_offset"] < 60_000
    assert 0 < first["stderr_next_offset"] < 60_000
    assert fits_inline(_run_shell("true", tmp_path), first)

    # Next check uploads both remaining slices and hands over the handle.
    second = await _execute(
        shell,
        _handle_command(
            "check_shell",
            handle,
            sequence=2,
            offset=first["next_offset"],
            stderr_offset=first["stderr_next_offset"],
        ),
    )
    assert second["output_attachment"] is not None
    assert second["stderr_attachment"] is not None
    assert (second["next_offset"], second["stderr_next_offset"]) == (60_000, 60_000)
    with pytest.raises(DesktopShellError, match=_UNKNOWN_HANDLE):
        await _execute(shell, _handle_command("check_shell", handle, sequence=3))
    await shell.close()


@pytest.mark.asyncio
async def test_legacy_controller_without_separate_stderr_merges_stderr_and_emits_only_legacy_keys(
    tmp_path: Path,
) -> None:
    """An older controller omitting separate_stderr gets merged stderr in output and never sees stderr* keys."""
    shell = _local_shell()
    shell.grant(60)
    inline = await _execute(shell, _run_shell("printf 'out;'; printf 'err' >&2", tmp_path, request_id="r-inline"))
    assert inline["output"] == "out;err"
    assert not any(key.startswith("stderr") for key in inline)

    script = "import sys; sys.stdout.write('O' * 40_000); sys.stderr.write('E' * 40_000)"
    attached = await _execute(
        shell,
        _run_shell(
            f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}",
            tmp_path,
            request_id="r-att",
            timeout_seconds=5,
        ),
    )
    assert attached["output_attachment"] is not None
    assert attached["output_bytes"] == 80_000
    assert not any(key.startswith("stderr") for key in attached)

    running = await _execute(shell, _run_shell("sleep 30", tmp_path, request_id="r-run"))
    assert not any(key.startswith("stderr") for key in running)
    handle = running["handle"]
    assert isinstance(handle, str)
    with pytest.raises(DesktopShellError, match="separate_stderr"):
        await _execute(shell, _handle_command("check_shell", handle, sequence=2, stderr_offset=0))
    await shell.close()


@pytest.mark.asyncio
async def test_dual_stream_attachments_upload_concurrently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Large split stdout and stderr attachments upload concurrently via asyncio.gather."""
    stdout_started = asyncio.Event()
    stderr_started = asyncio.Event()

    async def barrier_upload(
        _client: object,
        content: bytes,
        *,
        mime_type: str,
        filename: str,
        timeout_seconds: float,
    ) -> EncryptedDesktopMedia:
        del mime_type, timeout_seconds
        if filename.endswith("-stderr.txt"):
            stderr_started.set()
            await asyncio.wait_for(stdout_started.wait(), timeout=2)
        else:
            stdout_started.set()
            await asyncio.wait_for(stderr_started.wait(), timeout=2)
        return replace(MEDIA, url=f"mxc://example.org/{filename}", mime_type="text/plain", size=len(content))

    monkeypatch.setattr("mindroom.desktop.shell_actions.upload_encrypted_media", barrier_upload)
    shell = _local_shell()
    shell.grant(60)
    script = "import sys; sys.stdout.write('O' * 60_000); sys.stderr.write('E' * 60_000)"
    result = await _execute(
        shell,
        _run_shell(
            f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}",
            tmp_path,
            timeout_seconds=5,
            separate_stderr=True,
        ),
    )
    assert result["output_attachment"] is not None
    assert result["stderr_attachment"] is not None
    await shell.close()


@pytest.mark.asyncio
async def test_kill_shell_accepts_handle_or_request_id_mutually_exclusively(tmp_path: Path) -> None:
    """kill_shell requires exactly one of handle or request_id, and request_id cancels a pending approval."""
    shell = _local_shell()
    for bad_params in ({}, {"handle": "shell:1", "request_id": "req-1"}):
        with pytest.raises(DesktopProtocolError, match="exactly one of handle or request_id"):
            await _execute(shell, _command("kill_shell", parameters=bad_params))

    marker = tmp_path / "never"
    run_task = asyncio.create_task(
        _execute(shell, _run_shell(f"touch {marker}", tmp_path, request_id="req-cancel")),
    )
    await _wait_for_pending(shell)
    cancelled = await _execute(
        shell,
        _command("kill_shell", request_id="k1", sequence=2, parameters={"request_id": "req-cancel"}),
    )
    assert cancelled == {"state": "cancelled", "request_id": "req-cancel"}
    assert shell.status()["pending"] is None
    with pytest.raises(DesktopShellError, match="cancelled before approval"):
        await asyncio.wait_for(run_task, timeout=2)
    assert not marker.exists()
    await shell.close()
