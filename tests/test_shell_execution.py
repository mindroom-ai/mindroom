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

from mindroom.shell_execution import ProcessRecord, kill_all_records, run_command
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
        env={"PATH": os.defpath},
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
