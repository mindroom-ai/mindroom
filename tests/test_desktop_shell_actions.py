"""Tests for desktop shell actions, their caller-scoped status, and their output delivery."""

from __future__ import annotations

import asyncio
import json
import shlex
import signal
import sys
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import nio
import pytest

from mindroom.desktop.media import DesktopMediaError, download_encrypted_media, upload_encrypted_media
from mindroom.desktop.protocol import MAX_INLINE_RESPONSE_BYTES, EncryptedDesktopMedia
from mindroom.desktop.shell import DesktopShellOutput
from tests.desktop_bridge_helpers import (
    _LONGEST_SESSION_ID,
    ALICE,
    BOB,
    MEDIA,
    PRIVATE_COMMAND,
    _check_until_finished,
    _command,
    _event,
    _execute,
    _handle,
    _handle_command,
    _local_bridge,
    _local_shell,
    _longest_request_id,
    _response,
    _run_shell,
    _wait_for_pending_shell,
)
from tests.desktop_bridge_helpers import transport as transport  # noqa: PLC0414

if TYPE_CHECKING:
    from mindroom.desktop.bridge import DesktopBridge


async def _wait_for_active_shell(bridge: DesktopBridge) -> str:
    for _ in range(200):
        active_request_id = bridge.local_status()["shell"]["active_request_id"]
        if active_request_id is not None:
            return active_request_id
        await asyncio.sleep(0.005)
    pytest.fail("shell command never became active")


@pytest.mark.asyncio
async def test_run_shell_defaults_to_local_home_and_requires_local_approval(transport: AsyncMock) -> None:
    """Omitted cwd resolves locally, and a local rejection starts no process."""
    bridge = _local_bridge(shell=_local_shell())
    await bridge.on_to_device_event(_event(_command("run_shell", parameters={"command": PRIVATE_COMMAND})))
    execution = asyncio.create_task(_execute(bridge))
    pending = await _wait_for_pending_shell(bridge)
    assert pending == {
        "request_id": "request-1",
        "requester_id": ALICE,
        "agent_name": "computer",
        "command": PRIVATE_COMMAND,
        "cwd": str(Path.home()),
        "expires_at_ms": 11_000,
    }
    bridge.decide_local_shell("request-1", approved=False, auto_approve_seconds=0)
    await execution
    await bridge.deliver_pending()
    assert _response(transport).error == "Shell command denied locally."
    bridge.close()


@pytest.mark.asyncio
async def test_other_callers_see_shell_state_without_pending_command(transport: AsyncMock, tmp_path: Path) -> None:
    """Remote status and receipts expose no pending command text to another allowed caller."""
    shell = _local_shell()
    bridge = _local_bridge(shell=shell)
    command = _command("run_shell", parameters={"command": PRIVATE_COMMAND, "cwd": str(tmp_path)})
    await bridge.on_to_device_event(_event(command))
    execution = asyncio.create_task(_execute(bridge))
    await _wait_for_pending_shell(bridge)
    receipt = _command(
        "request_status",
        request_id="query",
        sequence=2,
        requester_id=BOB,
        parameters={"request_id": "request-1"},
    )
    await bridge.on_to_device_event(_event(receipt))
    await bridge.deliver_pending()
    assert _response(transport).result == {"request_id": "request-1", "state": "not_found"}
    # Shell starts wait for approval in their own lane, so status still runs while this request is pending.
    await _handle(bridge, _event(_command("status", request_id="status", sequence=3, requester_id=BOB)))
    status = _response(transport)
    assert status.result["bridge"]["shell"] == {
        "enabled": True,
        "pending": True,
        "auto_approve_remaining_seconds": 0,
        "auto_approve_until_revoked": False,
        "active_request_id": None,
        "handles": [],
    }
    assert "private-shell-text" not in json.dumps(status.to_content())
    assert bridge.local_status()["shell"]["pending"]["command"] == PRIVATE_COMMAND
    bridge.decide_local_shell("request-1", approved=False, auto_approve_seconds=0)
    await execution
    assert not (tmp_path / "marker").exists()
    bridge.close()


@pytest.mark.asyncio
async def test_other_callers_see_active_shell_without_its_request_id(transport: AsyncMock, tmp_path: Path) -> None:
    """A different allowed caller sees that a command is active, but never learns its request ID."""
    shell = _local_shell()
    shell.grant(60)
    bridge = _local_bridge(shell=shell)
    command = _command("run_shell", parameters={"command": "sleep 30", "cwd": str(tmp_path)})
    await bridge.on_to_device_event(_event(command))
    execution = asyncio.create_task(_execute(bridge))
    await _wait_for_active_shell(bridge)
    await _handle(bridge, _event(_command("status", request_id="status-bob", sequence=2, requester_id=BOB)))
    assert _response(transport).result["bridge"]["shell"]["active_request_id"] is None
    await _handle(bridge, _event(_command("status", request_id="status-alice", sequence=3)))
    assert _response(transport).result["bridge"]["shell"]["active_request_id"] == "request-1"
    await asyncio.wait_for(bridge.stop(), timeout=3)
    await execution
    await bridge.deliver_pending()
    bridge.close()


def _without_metrics(result: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in result.items() if key != "metrics"}


@pytest.mark.asyncio
async def test_shell_handle_lifecycle_returns_full_output_through_the_bridge(
    transport: AsyncMock,
    tmp_path: Path,
) -> None:
    """A command past its inline wait becomes a handle, and one completed check returns its output from the offset."""
    shell = _local_shell()
    shell.grant(60)
    bridge = _local_bridge(shell=shell)
    command = "printf 'early\\n'; while [ ! -f release ]; do sleep 0.05; done; printf late; exit 3"
    await _handle(bridge, _event(_run_shell(command, tmp_path)))
    running = _response(transport).result
    handle = running["handle"]
    assert isinstance(handle, str)
    assert _without_metrics(running) == {
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
    await _handle(bridge, _event(_handle_command("check_shell", handle, sequence=2)))
    assert _response(transport).result["state"] == "running"

    (tmp_path / "release").touch()
    completed, sequence = await _check_until_finished(bridge, transport, handle, first_sequence=3, offset=6)
    assert _without_metrics(completed) == {
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
    await _handle(bridge, _event(_handle_command("check_shell", handle, sequence=sequence + 1)))
    assert _response(transport).error == "Unknown shell handle."
    await bridge.stop()
    bridge.close()


@pytest.mark.asyncio
async def test_fitted_shell_replies_fit_the_budget_as_recorded_and_sent(
    transport: AsyncMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tail, offset, and upload-fallback shell replies fit with their metrics and maximum-length IDs."""
    monkeypatch.setattr(
        "mindroom.desktop.bridge.upload_encrypted_media",
        AsyncMock(side_effect=DesktopMediaError("Matrix media upload failed: offline")),
    )
    (tmp_path / "output").write_bytes(("\u00e9" * 30_000).encode())
    shell = _local_shell()
    shell.grant(60)
    bridge = _local_bridge(shell=shell)
    command = "cat output; while [ ! -f release ]; do sleep 0.05; done; cat output"
    run_id = _longest_request_id("run")
    started = replace(_run_shell(command, tmp_path, request_id=run_id), session_id=_LONGEST_SESSION_ID)
    await _handle(bridge, _event(started))
    handle = _response(transport).result["handle"]
    assert isinstance(handle, str)
    request_ids = [run_id]

    async def check(sequence: int, **parameters: object) -> None:
        request_id = _longest_request_id(f"check-{sequence}-")
        command = _command(
            "check_shell",
            request_id=request_id,
            session_id=_LONGEST_SESSION_ID,
            sequence=sequence,
            parameters={"handle": handle, **parameters},
        )
        await _handle(bridge, _event(command))
        request_ids.append(request_id)

    await check(2, offset=0)
    (tmp_path / "release").touch()
    await _wait_for_finished_handle(bridge, handle)
    await check(3)
    for request_id in request_ids:
        recorded = bridge._journal.get(request_id).response
        assert recorded is not None
        assert "metrics" in recorded.result
        assert recorded.content_bytes() <= MAX_INLINE_RESPONSE_BYTES
    final = _response(transport)
    assert (final.result["state"], final.result["output_truncated"]) == ("completed", True)
    assert "could not be attached" in str(final.result["warning"])
    await bridge.stop()
    bridge.close()


async def _wait_for_finished_handle(bridge: DesktopBridge, handle: str) -> None:
    for _ in range(600):
        entries = {entry["handle"]: entry["state"] for entry in bridge.local_status()["shell"]["handles"]}
        if entries.get(handle) != "running":
            return
        await asyncio.sleep(0.01)
    pytest.fail("shell handle never finished")


@pytest.mark.asyncio
async def test_running_tail_reports_where_its_output_starts_so_skipped_output_stays_readable(
    transport: AsyncMock,
    tmp_path: Path,
) -> None:
    """A trimmed newest-output reply says where it begins, and polling from 0 still returns the beginning."""
    content = b"".join(f"line {number:05}\n".encode() for number in range(10_000))
    (tmp_path / "log").write_bytes(content)
    shell = _local_shell()
    shell.grant(60)
    bridge = _local_bridge(shell=shell)
    await _handle(bridge, _event(_run_shell("cat log; while [ ! -f release ]; do sleep 0.05; done", tmp_path)))
    handle = _response(transport).result["handle"]
    assert isinstance(handle, str)
    for sequence in range(2, 600):
        await _handle(bridge, _event(_handle_command("check_shell", handle, sequence=sequence)))
        tail = _response(transport).result
        if tail["output_bytes"] == len(content):
            break
        await asyncio.sleep(0.01)
    shown = str(tail["output"]).encode()
    assert 0 < tail["output_start"] == len(content) - len(shown)
    assert (tail["next_offset"], content[tail["output_start"] :]) == (len(content), shown)
    await _handle(bridge, _event(_handle_command("check_shell", handle, sequence=700, offset=0)))
    head = _response(transport).result
    assert (head["output_start"], content.startswith(str(head["output"]).encode())) == (0, True)
    assert head["next_offset"] == len(str(head["output"]).encode())
    await bridge.stop()
    bridge.close()


@pytest.mark.asyncio
async def test_offset_polls_leave_later_output_of_a_running_command_intact(
    transport: AsyncMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bounded offset read never moves where the still-running command's next output is written."""
    upload = AsyncMock(return_value=replace(MEDIA, mime_type="text/plain"))
    monkeypatch.setattr("mindroom.desktop.bridge.upload_encrypted_media", upload)
    first, second = b"a" * 50_000 + b"\n", b"b" * 50_000 + b"\n"
    (tmp_path / "first").write_bytes(first)
    (tmp_path / "second").write_bytes(second)
    shell = _local_shell()
    shell.grant(60)
    bridge = _local_bridge(shell=shell)
    command = "cat first; while [ ! -f release ]; do sleep 0.05; done; cat second"
    await _handle(bridge, _event(_run_shell(command, tmp_path)))
    handle = _response(transport).result["handle"]
    assert isinstance(handle, str)
    for sequence in range(2, 600):
        await _handle(bridge, _event(_handle_command("check_shell", handle, sequence=sequence, offset=0)))
        polled = _response(transport).result
        if polled["output_bytes"] == len(first):
            break
        await asyncio.sleep(0.01)
    assert (polled["output_bytes"], polled["next_offset"] < len(first)) == (len(first), True)
    (tmp_path / "release").touch()
    await _wait_for_finished_handle(bridge, handle)
    await _handle(bridge, _event(_handle_command("check_shell", handle, sequence=700, offset=0)))
    completed = _response(transport).result
    assert (completed["state"], completed["output_bytes"]) == ("completed", len(first) + len(second))
    assert upload.await_args.args[1] == first + second
    await bridge.stop()
    bridge.close()


@pytest.mark.asyncio
async def test_check_shell_offset_polls_every_byte_once_without_splitting_characters(
    transport: AsyncMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Polling from each next_offset returns contiguous whole characters, and the completed slice may be attached."""
    media = replace(MEDIA, mime_type="text/plain")
    upload = AsyncMock(return_value=media)
    monkeypatch.setattr("mindroom.desktop.bridge.upload_encrypted_media", upload)
    first = ("\u00e9" * 30_000).encode()  # More than one inline reply, in two-byte characters.
    rest = ("\u00fc" * 30_000 + "end").encode()
    printer = f"{shlex.quote(sys.executable)} -c 'import sys; sys.stdout.buffer.write(bytes.fromhex(sys.stdin.read()))'"
    (tmp_path / "first").write_text(first.hex())
    (tmp_path / "rest").write_text(rest.hex())
    command = f"{printer} < first; while [ ! -f release ]; do sleep 0.05; done; {printer} < rest"
    shell = _local_shell()
    shell.grant(60)
    bridge = _local_bridge(shell=shell)
    await _handle(bridge, _event(_run_shell(command, tmp_path)))
    started = _response(transport).result
    handle = started["handle"]
    assert isinstance(handle, str)
    assert started["next_offset"] == started["output_bytes"]

    received, offset, polls = b"", 0, 0
    for sequence in range(2, 600):
        await _handle(bridge, _event(_handle_command("check_shell", handle, sequence=sequence, offset=offset)))
        reply = _response(transport)
        assert reply.content_bytes() <= MAX_INLINE_RESPONSE_BYTES
        shown = str(reply.result["output"]).encode()
        assert (reply.result["output_start"], reply.result["next_offset"]) == (offset, offset + len(shown))
        assert reply.result["output_truncated"] is (reply.result["next_offset"] < reply.result["output_bytes"])
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
        await _handle(bridge, _event(_handle_command("check_shell", handle, sequence=sequence, offset=bad_offset)))
        assert error in str(_response(transport).error)

    (tmp_path / "release").touch()
    await _wait_for_finished_handle(bridge, handle)
    await _handle(bridge, _event(_handle_command("check_shell", handle, sequence=703, offset=offset)))
    completed = _response(transport).result
    assert (completed["state"], completed["exit_code"], completed["output"]) == ("completed", 0, "")
    assert (completed["output_bytes"], completed["next_offset"]) == (len(first) + len(rest), len(first) + len(rest))
    assert completed["output_attachment"] == media.to_content()
    assert upload.await_args.args[1] == rest
    await bridge.stop()
    bridge.close()


@pytest.mark.asyncio
async def test_other_callers_cannot_check_or_kill_a_handle(transport: AsyncMock, tmp_path: Path) -> None:
    """Another allowed requester gets exactly the error of a handle that never existed."""
    shell = _local_shell()
    shell.grant(60)
    bridge = _local_bridge(shell=shell)
    await _handle(bridge, _event(_run_shell("sleep 30", tmp_path)))
    handle = _response(transport).result["handle"]
    assert isinstance(handle, str)
    attempts = (
        ("check_shell", handle, BOB),
        ("kill_shell", handle, BOB),
        ("check_shell", "shell:00000000", ALICE),
        ("kill_shell", "shell:00000000", ALICE),
    )
    for sequence, (action, target, requester_id) in enumerate(attempts, start=2):
        await _handle(bridge, _event(_handle_command(action, target, sequence=sequence, requester_id=requester_id)))
        response = _response(transport)
        assert (response.ok, response.error, _without_metrics(response.result)) == (False, "Unknown shell handle.", {})

    await _handle(bridge, _event(_handle_command("kill_shell", handle, sequence=6)))
    assert _without_metrics(_response(transport).result) == {"state": "killed", "handle": handle}
    killed, sequence = await _check_until_finished(bridge, transport, handle, first_sequence=7)
    assert (killed["state"], killed["exit_code"]) == ("killed", -signal.SIGTERM)
    query = _command(
        "request_status",
        request_id="query",
        sequence=sequence + 1,
        parameters={"request_id": f"check_shell-{sequence}"},
    )
    await _handle(bridge, _event(query))
    assert _response(transport).result["response"]["result"]["state"] == "killed"
    await bridge.stop()
    bridge.close()


@pytest.mark.asyncio
async def test_remote_status_lists_only_the_callers_own_handles(transport: AsyncMock, tmp_path: Path) -> None:
    """Handles are visible to their owner remotely and to everyone at the Mac locally."""
    shell = _local_shell()
    shell.grant(60)
    bridge = _local_bridge(shell=shell)
    await _handle(bridge, _event(_run_shell("sleep 30", tmp_path)))
    handle = _response(transport).result["handle"]
    await _handle(bridge, _event(_command("status", request_id="own", sequence=2)))
    [entry] = _response(transport).result["bridge"]["shell"]["handles"]
    assert {key: entry[key] for key in ("handle", "requester_id", "agent_name", "command_preview", "state")} == {
        "handle": handle,
        "requester_id": ALICE,
        "agent_name": "computer",
        "command_preview": "sleep 30",
        "state": "running",
    }
    await _handle(bridge, _event(_command("status", request_id="other", sequence=3, requester_id=BOB)))
    assert _response(transport).result["bridge"]["shell"]["handles"] == []
    assert [local["handle"] for local in bridge.local_status()["shell"]["handles"]] == [handle]
    await bridge.stop()
    bridge.close()


@pytest.mark.asyncio
async def test_output_over_the_inline_limit_round_trips_as_an_encrypted_attachment(
    transport: AsyncMock,
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

    monkeypatch.setattr("mindroom.desktop.bridge.upload_encrypted_media", upload_encrypted_media)
    monkeypatch.setattr("mindroom.desktop.media.upload_media_bytes", upload)
    text = '"é" * 60_000 + "\\x01" * 1_000 + "end"'
    expected = ("é" * 60_000 + "\x01" * 1_000 + "end").encode()
    script = f"import sys; sys.stdout.buffer.write(({text}).encode()); sys.stdout.flush()"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}; while [ ! -f release ]; do sleep 0.05; done"
    shell = _local_shell()
    shell.grant(60)
    bridge = _local_bridge(shell=shell)
    await _handle(bridge, _event(_run_shell(command, tmp_path)))
    running = _response(transport)
    handle = running.result["handle"]
    assert isinstance(handle, str)
    shown = str(running.result["output"])
    assert running.content_bytes() <= MAX_INLINE_RESPONSE_BYTES
    assert expected.decode().endswith(shown)
    assert 0 < len(shown.encode()) < len(expected)
    assert (running.result["output_bytes"], running.result["output_truncated"]) == (len(expected), True)
    assert running.result["output_attachment"] is None

    (tmp_path / "release").touch()
    completed, _ = await _check_until_finished(bridge, transport, handle, first_sequence=2)
    assert {key: value for key, value in _without_metrics(completed).items() if key != "output_attachment"} == {
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
    await bridge.stop()
    bridge.close()


@pytest.mark.asyncio
async def test_failed_upload_of_a_finished_run_shell_keeps_exit_code_and_a_first_page(
    transport: AsyncMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An attachment failure still shows the exit code and the output's start, with a handle to page on from."""
    monkeypatch.setattr(
        "mindroom.desktop.bridge.upload_encrypted_media",
        AsyncMock(side_effect=DesktopMediaError("Matrix media upload failed: offline")),
    )
    shell = _local_shell()
    shell.grant(60)
    bridge = _local_bridge(shell=shell)
    script = "import sys; sys.stdout.write('x' * 100_000 + 'tail')"
    await _handle(
        bridge,
        _event(_run_shell(f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}; exit 4", tmp_path)),
    )
    response = _response(transport)
    assert response.ok
    result = response.result
    assert (result["state"], result["exit_code"], result["output_attachment"]) == ("completed", 4, None)
    assert isinstance(result["handle"], str)
    assert (result["output_start"], set(str(result["output"]))) == (0, {"x"})
    assert result["next_offset"] == len(str(result["output"]))
    assert (result["output_bytes"], result["output_truncated"]) == (100_004, True)
    assert "Matrix media upload failed: offline" in str(result["warning"])
    assert "Continue with check_shell from next_offset" in str(result["warning"])
    assert response.content_bytes() <= MAX_INLINE_RESPONSE_BYTES
    await bridge.stop()
    bridge.close()


@pytest.mark.asyncio
async def test_output_past_the_capture_cap_is_reported_as_truncated(
    transport: AsyncMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Commands that print more than the capture cap say so instead of implying complete output."""
    monkeypatch.setattr("mindroom.desktop.shell.MAX_SHELL_OUTPUT_BYTES", 1_024)
    shell = _local_shell()
    shell.grant(60)
    bridge = _local_bridge(shell=shell)
    script = "import sys; sys.stdout.write('z' * 20_000)"
    await _handle(bridge, _event(_run_shell(f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}", tmp_path)))
    result = _response(transport).result
    assert result["output_truncated"] is True
    assert int(result["output_bytes"]) <= 1_024
    assert set(str(result["output"])) <= {"z"}
    await bridge.stop()
    bridge.close()


def _stall_output_uploads(monkeypatch: pytest.MonkeyPatch) -> tuple[asyncio.Event, list[DesktopShellOutput]]:
    """Use the real encrypted upload against a homeserver that never answers, with a short bound."""
    started = asyncio.Event()
    released: list[DesktopShellOutput] = []
    release = DesktopShellOutput.release

    async def stalled(*_args: object, **_kwargs: object) -> nio.UploadResponse:
        started.set()
        await asyncio.Event().wait()
        pytest.fail("stalled upload returned")

    def record_release(output: DesktopShellOutput) -> None:
        released.append(output)
        release(output)

    monkeypatch.setattr("mindroom.desktop.bridge._MEDIA_UPLOAD_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr("mindroom.desktop.bridge.upload_encrypted_media", upload_encrypted_media)
    monkeypatch.setattr("mindroom.desktop.media.upload_media_bytes", stalled)
    monkeypatch.setattr(DesktopShellOutput, "release", record_release)
    return started, released


_LARGE_OUTPUT = "import sys; sys.stdout.write('x' * 100_000 + 'tail')"


def _assert_upload_fallback(result: dict[str, object]) -> None:
    assert (result["state"], result["exit_code"], result["output_attachment"]) == ("completed", 0, None)
    assert isinstance(result["handle"], str)
    assert (result["output_start"], set(str(result["output"]))) == (0, {"x"})
    assert result["next_offset"] == len(str(result["output"]))
    assert (result["output_bytes"], result["output_truncated"]) == (100_004, True)
    assert "upload did not finish within 0.2 seconds" in str(result["warning"])


@pytest.mark.parametrize("later_uploads", ["fail", "succeed"])
@pytest.mark.asyncio
async def test_failed_upload_keeps_a_finished_handle_until_its_output_is_delivered(
    transport: AsyncMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    later_uploads: str,
) -> None:
    """After a failed attachment the agent pages on from next_offset, and only a complete page consumes the handle."""
    media = replace(MEDIA, mime_type="text/plain")
    failure = DesktopMediaError("Matrix media upload failed: offline")
    upload = AsyncMock(side_effect=[failure, *([failure] * 10 if later_uploads == "fail" else [media])])
    monkeypatch.setattr("mindroom.desktop.bridge.upload_encrypted_media", upload)
    content = b"".join(f"line {number:05}\n".encode() for number in range(9_000))
    (tmp_path / "log").write_bytes(content)
    shell = _local_shell()
    shell.grant(60)
    bridge = _local_bridge(shell=shell)
    await _handle(bridge, _event(_run_shell("while [ ! -f release ]; do sleep 0.05; done; cat log", tmp_path)))
    handle = _response(transport).result["handle"]
    assert isinstance(handle, str)
    (tmp_path / "release").touch()
    await _wait_for_finished_handle(bridge, handle)
    received, offset = b"", 0
    for sequence in range(2, 20):
        await _handle(bridge, _event(_handle_command("check_shell", handle, sequence=sequence, offset=offset)))
        page = _response(transport).result
        assert (page["state"], page["exit_code"], page["output_start"]) == ("completed", 0, offset)
        attached = page["output_attachment"] is not None
        shown = upload.await_args.args[1] if attached else str(page["output"]).encode()
        assert page["next_offset"] == offset + len(shown)
        received, offset = received + shown, offset + len(shown)
        if offset == len(content):
            assert "warning" not in page
            break
        assert "could not be attached" in str(page["warning"])
        assert [entry["handle"] for entry in bridge.local_status()["shell"]["handles"]] == [handle]
    assert received == content
    assert bridge.local_status()["shell"]["handles"] == []
    await _handle(bridge, _event(_handle_command("check_shell", handle, sequence=30, offset=offset)))
    assert _response(transport).error == "Unknown shell handle."
    await bridge.stop()
    bridge.close()


@pytest.mark.parametrize("ending", ["paged", "revoked"])
@pytest.mark.asyncio
async def test_failed_upload_turns_an_inline_finished_run_shell_into_a_pageable_handle(
    transport: AsyncMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ending: str,
) -> None:
    """Undeliverable output of a finished run_shell stays behind a handle until paged in full or revoked."""
    monkeypatch.setattr(
        "mindroom.desktop.bridge.upload_encrypted_media",
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
    bridge = _local_bridge(shell=shell)
    await _handle(bridge, _event(_run_shell("cat log", tmp_path)))
    first = _response(transport)
    handle = first.result["handle"]
    assert isinstance(handle, str)
    assert (first.result["state"], first.result["exit_code"], first.result["output_start"]) == ("completed", 0, 0)
    assert "Continue with check_shell from next_offset" in str(first.result["warning"])
    received, offset = str(first.result["output"]).encode(), first.result["next_offset"]
    assert received == content[:offset]
    spool = Path(str(shell._directory))
    await _handle(
        bridge,
        _event(_command("request_status", request_id="query", sequence=2, parameters={"request_id": "run"})),
    )
    assert _response(transport).result["response"] == first.to_content()
    assert released == []
    if ending == "revoked":
        bridge.revoke_local_shell()
    else:
        for sequence in range(3, 20):
            await _handle(bridge, _event(_handle_command("check_shell", handle, sequence=sequence, offset=offset)))
            page = _response(transport).result
            shown = str(page["output"]).encode()
            assert (page["output_start"], page["next_offset"]) == (offset, offset + len(shown))
            received, offset = received + shown, offset + len(shown)
            if offset == len(content):
                break
            assert released == []
        assert received == content
    assert [output.stdout.file.closed for output in released] == [True]
    assert bridge.local_status()["shell"]["handles"] == []
    await _handle(bridge, _event(_handle_command("check_shell", handle, sequence=30, offset=offset)))
    assert _response(transport).error == "Unknown shell handle."
    await bridge.stop()
    assert not spool.exists()
    bridge.close()


@pytest.mark.parametrize("action", ["run_shell", "check_shell"])
@pytest.mark.asyncio
async def test_stalled_output_upload_falls_back_to_a_first_page_within_the_bound(
    transport: AsyncMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    """A stalled attachment upload cannot hold a shell reply or its executor lane past the upload bound."""
    _started, released = _stall_output_uploads(monkeypatch)
    shell = _local_shell()
    shell.grant(60)
    bridge = _local_bridge(shell=shell)
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(_LARGE_OUTPUT)}"
    if action == "run_shell":
        await asyncio.wait_for(_handle(bridge, _event(_run_shell(command, tmp_path))), timeout=3)
        result = _response(transport).result
    else:
        await _handle(bridge, _event(_run_shell(f"{command}; while [ ! -f release ]; do sleep 0.05; done", tmp_path)))
        handle = _response(transport).result["handle"]
        assert isinstance(handle, str)
        (tmp_path / "release").touch()
        result, _ = await asyncio.wait_for(_check_until_finished(bridge, transport, handle, first_sequence=2), 5)
    _assert_upload_fallback(result)
    # The handle keeps its output for the next page.
    assert released == []
    await bridge.stop()
    bridge.close()


@pytest.mark.asyncio
@pytest.mark.usefixtures("transport")
async def test_bridge_stop_during_a_stalled_output_upload_returns_within_the_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop drains the in-flight reply, and a stalled upload bounds that drain instead of hanging it."""
    started, released = _stall_output_uploads(monkeypatch)
    shell = _local_shell()
    shell.grant(60)
    bridge = _local_bridge(shell=shell)
    worker = asyncio.create_task(bridge.run())
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(_LARGE_OUTPUT)}"
    await bridge.on_to_device_event(_event(_run_shell(command, tmp_path)))
    await asyncio.wait_for(started.wait(), timeout=3)
    await asyncio.wait_for(bridge.stop(), timeout=2)
    await asyncio.wait_for(worker, timeout=2)
    _assert_upload_fallback(bridge._journal.get("run").response.result)
    assert [output.closed for output in released] == [True]
    bridge.close()
