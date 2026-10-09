"""Optional shell engine seams used by the Desktop shell, with the agent shell tool defaults unchanged."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
import signal
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from mindroom.shell_execution import (
    ProcessRecord,
    _BackgroundHandle,
    _CheckStatus,
    _format_background_handle_message,
    _format_finished_status,
    _format_running_status,
    check_command,
    kill_all_records,
    kill_command,
    parse_background_handle_message,
    parse_check_status,
    parse_kill_message,
    parse_unknown_handle_error,
    run_command,
    signal_record,
)
from mindroom.shell_output_capture import ShellOutputCapture, ShellOutputDestination

if TYPE_CHECKING:
    from collections.abc import Iterator

_FD0_IDENTITY = "import os, sys; info = os.fstat(0); sys.stdout.write(f'{info.st_dev}:{info.st_ino}')"


class _RecordingCapture(ShellOutputCapture):
    """Keep the spool readable after the engine reports completion."""

    def __init__(self, directory: Path) -> None:
        super().__init__(ShellOutputDestination(workspace_root=str(directory), path="", max_bytes=1024), None)
        self.return_codes: list[int | None] = []

    def publish(self, return_code: int | None) -> str:
        self.return_codes.append(return_code)
        return "published"

    def close(self) -> None:
        """Defer release until the test has read the spool."""

    def release(self) -> None:
        super().close()


@pytest.fixture
def registry() -> Iterator[dict[str, ProcessRecord]]:
    """Kill anything a failing test leaves registered."""
    records: dict[str, ProcessRecord] = {}
    yield records
    kill_all_records(records)


async def _run(registry: dict[str, ProcessRecord], argv: list[str], cwd: Path, **options: object) -> str:
    result = await run_command(
        registry,
        namespace="test",
        argv=argv,
        env={"PATH": os.environ["PATH"]},
        cwd=str(cwd),
        tail=100,
        timeout=10,
        **options,
    )
    assert result.handle is None
    return result.message


async def _wait_until_gone(pid: int) -> bool:
    for _ in range(200):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        await asyncio.sleep(0.01)
    return False


def _identity(path: str) -> str:
    info = Path(path).stat()
    return f"{info.st_dev}:{info.st_ino}"


@pytest.mark.asyncio
async def test_default_stdin_is_inherited_and_devnull_seam_detaches_it(
    registry: dict[str, ProcessRecord],
    tmp_path: Path,
) -> None:
    """The agent tool keeps inheriting stdin; the Desktop helper can keep its private channel away from commands."""
    read_end, write_end = os.pipe()
    saved_stdin = os.dup(0)
    try:
        os.dup2(read_end, 0)
        pipe_identity = f"{os.fstat(0).st_dev}:{os.fstat(0).st_ino}"
        inherited = await _run(registry, [sys.executable, "-c", _FD0_IDENTITY], tmp_path)
        detached = await _run(
            registry,
            [sys.executable, "-c", _FD0_IDENTITY],
            tmp_path,
            stdin=asyncio.subprocess.DEVNULL,
        )
    finally:
        os.dup2(saved_stdin, 0)
        for descriptor in (saved_stdin, read_end, write_end):
            os.close(descriptor)
    assert inherited == pipe_identity
    assert detached == _identity(os.devnull)


@pytest.mark.asyncio
async def test_default_streams_stay_separate_and_merge_seam_interleaves(
    registry: dict[str, ProcessRecord],
    tmp_path: Path,
) -> None:
    """Merged stderr keeps terminal order in one stream; the default reply still omits stderr on success."""
    argv = ["/bin/sh", "-c", "echo first; echo second >&2; echo third"]
    assert await _run(registry, argv, tmp_path) == "first\nthird"
    assert await _run(registry, argv, tmp_path, stderr=asyncio.subprocess.STDOUT) == "first\nsecond\nthird"


@pytest.mark.asyncio
async def test_default_foreground_exit_keeps_background_children_and_seam_kills_them(
    registry: dict[str, ProcessRecord],
    tmp_path: Path,
) -> None:
    """The agent tool may leave `cmd &` running; the seam stops the rest of the process group."""
    argv = ["/bin/sh", "-c", "sleep 30 >/dev/null 2>&1 & echo $! > child.pid"]
    await _run(registry, argv, tmp_path)
    survivor = int((tmp_path / "child.pid").read_text())
    try:
        os.kill(survivor, 0)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(survivor, signal.SIGKILL)

    await _run(registry, argv, tmp_path, kill_group_after_exit=True)
    killed = int((tmp_path / "child.pid").read_text())
    try:
        assert await _wait_until_gone(killed)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(killed, signal.SIGKILL)


@pytest.mark.asyncio
@pytest.mark.parametrize("kill_group_after_exit", [False, True])
async def test_seam_kills_term_resistant_group_member_when_foreground_is_cancelled(
    registry: dict[str, ProcessRecord],
    tmp_path: Path,
    kill_group_after_exit: bool,
) -> None:
    """Cancelling the foreground removes descendants that ignore the polite stop only with the seam."""
    script = (
        "import os, pathlib, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "pathlib.Path('child.pid').write_text(str(os.getpid())); time.sleep(30)"
    )
    argv = ["/bin/sh", "-c", f"{shlex.quote(sys.executable)} -c {shlex.quote(script)} >/dev/null 2>&1 & wait"]
    task = asyncio.create_task(_run(registry, argv, tmp_path, kill_group_after_exit=kill_group_after_exit))
    pid_file = tmp_path / "child.pid"
    try:
        for _ in range(400):
            if pid_file.exists() and pid_file.read_text():
                break
            await asyncio.sleep(0.01)
        child = int(pid_file.read_text())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        if kill_group_after_exit:
            assert await _wait_until_gone(child)
        else:
            os.kill(child, 0)
    finally:
        if pid_file.exists() and pid_file.read_text():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(pid_file.read_text()), signal.SIGKILL)


@pytest.mark.asyncio
async def test_caller_capture_receives_exit_code_and_full_spool(
    registry: dict[str, ProcessRecord],
    tmp_path: Path,
) -> None:
    """A caller-owned capture sees completion without any workspace output file being written."""
    capture = _RecordingCapture(tmp_path)
    try:
        message = await _run(
            registry,
            ["/bin/sh", "-c", "printf 'kept output'; exit 3"],
            tmp_path,
            output_capture=capture,
        )
        assert message == "published"
        assert capture.return_codes == [3]
        assert capture.stdout.read() == "kept output"
        assert list(tmp_path.iterdir()) == []
    finally:
        capture.release()


@pytest.mark.asyncio
async def test_signal_record_reports_delivery_and_kill_command_messages_stay_the_same(
    registry: dict[str, ProcessRecord],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Callers learn whether a signal reached the group, while kill_command keeps its exact wording."""
    capture = _RecordingCapture(tmp_path)
    try:
        started = await run_command(
            registry,
            namespace="test",
            argv=["/bin/sh", "-c", "sleep 30"],
            env={"PATH": os.environ["PATH"]},
            cwd=str(tmp_path),
            tail=100,
            timeout=0.2,
            output_capture=capture,
        )
        assert started.handle is not None
        record = registry[started.handle]

        def vanished(_pid: int, _signal: int) -> None:
            raise ProcessLookupError

        with monkeypatch.context() as patch:
            patch.setattr(os, "killpg", vanished)
            assert signal_record(record) is False
            assert kill_command(registry, namespace="test", handle=started.handle) == (
                f"Process {record.pid} already exited"
            )
        assert capture.incomplete is False
        assert kill_command(registry, namespace="test", handle=started.handle) == (
            f"Terminated process {record.pid} (SIGTERM sent). Use check_shell_command('{started.handle}') to confirm exit."
        )
        assert capture.incomplete is True
        assert await _wait_until_gone(record.pid)
    finally:
        capture.release()


@pytest.mark.asyncio
async def test_register_finished_keeps_a_command_that_finished_in_time_as_a_finished_record(
    registry: dict[str, ProcessRecord],
    tmp_path: Path,
) -> None:
    """An opted-in caller gets a finished record to page later, while the default still registers nothing."""
    capture = _RecordingCapture(tmp_path)
    try:
        result = await run_command(
            registry,
            namespace="test",
            argv=["/bin/sh", "-c", "printf kept; exit 3"],
            env={"PATH": os.environ["PATH"]},
            cwd=str(tmp_path),
            tail=100,
            timeout=10,
            output_capture=capture,
            register_finished=True,
        )
        assert result.handle is not None
        record = registry[result.handle]
        assert (record.finished, record.return_code, record.namespace) == (True, 3, "test")
        assert record.finished_at is not None
        assert (capture.return_codes, capture.stdout.read()) == ([3], "kept")
        assert kill_command(registry, namespace="test", handle=result.handle) == (
            "Process already finished (exit code 3)"
        )
        await _run(registry, ["/bin/sh", "-c", "true"], tmp_path)
        assert list(registry) == [result.handle]
    finally:
        capture.release()


def test_background_handle_message_round_trips() -> None:
    """The background-handle message parses back into its fields and nothing else matches."""
    text = _format_background_handle_message(10, 4242, "shell:0123abcd")

    assert parse_background_handle_message(text) == _BackgroundHandle(timeout=10, pid=4242, handle="shell:0123abcd")
    assert parse_background_handle_message(text + "\nextra") is None
    assert parse_background_handle_message("Command timed out") is None


def test_parse_check_status_matches_both_templates() -> None:
    """Both check_command templates parse into one status shape."""
    failed = _format_finished_status(return_code=2, elapsed=1.5, stderr="boom", output="out\nmore")
    succeeded = _format_finished_status(return_code=0, elapsed=0.5, stderr="ignored", output="ok")
    running = _format_running_status(pid=77, elapsed=3.0, buffered_lines=4, partial="a\nb")

    assert parse_check_status(failed) == _CheckStatus(
        running=False,
        exit_code=2,
        elapsed=1.5,
        pid=None,
        stderr="boom",
        output="out\nmore",
    )
    assert parse_check_status(succeeded) == _CheckStatus(
        running=False,
        exit_code=0,
        elapsed=0.5,
        pid=None,
        stderr=None,
        output="ok",
    )
    assert parse_check_status(running) == _CheckStatus(
        running=True,
        exit_code=None,
        elapsed=3.0,
        pid=77,
        stderr=None,
        output="a\nb",
    )


def test_parse_check_status_rejects_plain_output() -> None:
    """Output that is not a check status is never parsed."""
    assert parse_check_status("hello") is None
    assert parse_check_status("Status: FINISHED (exit code x, ran for 1s)\nOutput:\n") is None


def test_unknown_handle_error_round_trips() -> None:
    """The unknown-handle error parses back to its handle."""
    assert parse_unknown_handle_error(check_command({}, namespace="ns", handle="shell:0123abcd")) == "shell:0123abcd"
    assert parse_unknown_handle_error("Error: something else") is None


def test_kill_message_round_trips() -> None:
    """The kill confirmation parses back into its fields."""
    message = "Force-killed process 77 (SIGKILL sent). Use check_shell_command('shell:0123abcd') to confirm exit."

    assert parse_kill_message(message) == ("Force-killed", 77, "SIGKILL", "shell:0123abcd")
    assert parse_kill_message("Process 77 already exited") is None
