"""Real subprocess checks for local desktop shell approval, handles, and output capture."""

import asyncio
import contextlib
import os
import shlex
import signal
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from mindroom.desktop.protocol import MAX_SHELL_OUTPUT_BYTES
from mindroom.desktop.shell import DesktopShell, DesktopShellError, DesktopShellRequest, DesktopShellResult

ENVIRONMENT = {"PATH": os.defpath, "MINDROOM_TEST_LOGIN_VALUE": "from-login"}
REQUESTER = "@user:local"
AGENT = "agent"


def request(
    command: str,
    cwd: Path,
    *,
    request_id: str = "r1",
    timeout: int = 30,
    requester_id: str = REQUESTER,
    agent_name: str = AGENT,
    lifetime_ms: int = 60_000,
) -> DesktopShellRequest:
    """Build one short-lived request for real subprocess tests."""
    return DesktopShellRequest(
        request_id,
        requester_id,
        agent_name,
        command,
        str(cwd),
        int(time.time() * 1000) + lifetime_ms,
        timeout,
    )


def local_shell(**clocks: object) -> DesktopShell:
    """Build a shell with a fixed environment so tests never read the real login profile."""
    return DesktopShell(environment=ENVIRONMENT, **clocks)


@pytest.fixture
def pids(tmp_path: Path) -> Iterator[Path]:
    """Collect PIDs written by commands that deliberately escape, and kill them after the test."""
    directory = tmp_path / "pids"
    directory.mkdir()
    yield directory
    for pid_file in directory.iterdir():
        if pid_file.read_text().strip():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(pid_file.read_text()), signal.SIGKILL)


async def wait_pending(shell: DesktopShell) -> None:
    """Bound waiting for asynchronous admission without relying on timing."""
    for _ in range(100):
        if shell.status()["pending"] is not None:
            return
        await asyncio.sleep(0.01)
    pytest.fail("approval request never appeared")


async def wait_for_file(path: Path) -> str:
    """Wait until a command has written a nonempty marker."""
    for _ in range(400):
        if path.exists() and path.read_text().strip():
            return path.read_text().strip()
        await asyncio.sleep(0.01)
    pytest.fail(f"{path.name} was never written")


async def wait_until_gone(pid: int) -> None:
    """Wait until a process no longer exists."""
    for _ in range(400):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"process {pid} survived")


def completed_output(result: DesktopShellResult) -> str:
    """Read and release a completed command's spool."""
    assert result.state == "completed"
    try:
        return result.output.read().decode()
    finally:
        result.output.release()


async def check_until_completed(shell: DesktopShell, handle: str) -> DesktopShellResult:
    """Poll one handle until its process has been reaped."""
    for _ in range(600):
        result = shell.check(REQUESTER, AGENT, handle)
        if result.state == "completed":
            return result
        await asyncio.sleep(0.01)
    pytest.fail("handle never completed")


@pytest.mark.asyncio
async def test_command_starts_only_after_matching_approval(tmp_path: Path) -> None:
    """Wrong IDs cannot approve and no process starts before local consent."""
    shell = local_shell()
    marker = tmp_path / "marker"
    task = asyncio.create_task(shell.execute(request("printf approved > marker; printf done", tmp_path)))
    await wait_pending(shell)
    assert not marker.exists()
    with pytest.raises(DesktopShellError):
        shell.decide("wrong", approved=True)
    assert not marker.exists()
    shell.decide("r1", approved=True)
    result = await task
    assert marker.read_text() == "approved"
    assert (result.exit_code, result.handle) == (0, None)
    assert completed_output(result) == "done"
    assert shell.status()["pending"] is None
    await shell.close()


@pytest.mark.asyncio
async def test_commands_use_the_captured_environment_and_merge_output(tmp_path: Path) -> None:
    """The login environment reaches commands, stdin is detached, and stderr keeps terminal order."""
    shell = local_shell()
    shell.grant(60)
    command = 'printf "$MINDROOM_TEST_LOGIN_VALUE\\n"; printf "warning\\n" >&2; cat; printf "last"'
    result = await shell.execute(request(command, tmp_path))
    assert completed_output(result) == "from-login\nwarning\nlast"
    await shell.close()


@pytest.mark.asyncio
async def test_rejection_and_expiry_never_start_process(tmp_path: Path) -> None:
    """Denied, replayed, and expired requests leave no side effects."""
    shell = local_shell()
    marker = tmp_path / "marker"
    task = asyncio.create_task(shell.execute(request("touch marker", tmp_path)))
    await wait_pending(shell)
    shell.decide("r1", approved=False)
    with pytest.raises(DesktopShellError, match="denied"):
        await task
    assert not marker.exists()
    with pytest.raises(DesktopShellError):
        await shell.execute(request("touch marker", tmp_path, request_id="r1"))
    expired = request("touch marker", tmp_path, request_id="r2")
    expired = DesktopShellRequest(
        expired.request_id,
        expired.requester_id,
        expired.agent_name,
        expired.command,
        expired.cwd,
        0,
    )
    with pytest.raises(DesktopShellError):
        await shell.execute(expired)
    assert not marker.exists()
    await shell.close()


@pytest.mark.asyncio
async def test_lease_expires_using_monotonic_clock(tmp_path: Path) -> None:
    """A lease expires against monotonic time despite stable wall time."""
    now = [100.0]
    shell = local_shell(clock=lambda: 100.0, monotonic_clock=lambda: now[0])
    shell.grant(60)
    first = DesktopShellRequest("r1", REQUESTER, AGENT, "printf yes", str(tmp_path), 150_000)
    assert completed_output(await shell.execute(first)) == "yes"
    now[0] += 61
    second = DesktopShellRequest("r2", REQUESTER, AGENT, "printf no", str(tmp_path), 150_000)
    task = asyncio.create_task(shell.execute(second))
    await wait_pending(shell)
    shell.decide("r2", approved=True)
    assert completed_output(await task) == "no"
    await shell.close()


@pytest.mark.asyncio
async def test_until_revoked_grant_outlives_any_timed_lease_until_revoke(tmp_path: Path) -> None:
    """An until-revoked grant is still active after an hour of fake monotonic time, and revoke ends it."""
    now = [100.0]
    shell = local_shell(clock=lambda: 100.0, monotonic_clock=lambda: now[0])
    shell.grant(until_revoked=True)
    now[0] += 3_601
    status = shell.status()
    assert (status["auto_approve_until_revoked"], status["auto_approve_remaining_seconds"]) == (True, 0.0)
    first = DesktopShellRequest("r1", REQUESTER, AGENT, "printf still-approved", str(tmp_path), 150_000)
    assert completed_output(await shell.execute(first)) == "still-approved"
    shell.revoke()
    assert shell.status()["auto_approve_until_revoked"] is False
    second = DesktopShellRequest("r2", REQUESTER, AGENT, "touch marker", str(tmp_path), 150_000)
    task = asyncio.create_task(shell.execute(second))
    await wait_pending(shell)
    shell.decide("r2", approved=False)
    with pytest.raises(DesktopShellError, match="denied"):
        await task
    assert not (tmp_path / "marker").exists()
    await shell.close()


@pytest.mark.asyncio
async def test_approval_can_start_an_until_revoked_grant(tmp_path: Path) -> None:
    """A decision may approve once and keep approving, but never combine two grant kinds."""
    shell = local_shell()
    first = asyncio.create_task(shell.execute(request("printf first", tmp_path)))
    await wait_pending(shell)
    for invalid in ({"auto_approve_seconds": 60, "auto_approve_until_revoked": True}, {"auto_approve_seconds": 59}):
        with pytest.raises(DesktopShellError):
            shell.decide("r1", approved=True, **invalid)
    shell.decide("r1", approved=True, auto_approve_until_revoked=True)
    assert completed_output(await first) == "first"
    assert completed_output(await shell.execute(request("printf second", tmp_path, request_id="r2"))) == "second"
    await shell.close()


@pytest.mark.parametrize(
    "grant",
    [
        {},
        {"duration_seconds": 59},
        {"duration_seconds": 3601},
        {"duration_seconds": True},
        {"duration_seconds": 60, "until_revoked": True},
        {"until_revoked": False},
    ],
)
@pytest.mark.asyncio
async def test_grant_requires_exactly_one_bounded_or_until_revoked_choice(grant: dict[str, object]) -> None:
    """Grants are either 60 to 3600 seconds or until revoked."""
    shell = local_shell()
    with pytest.raises(DesktopShellError):
        shell.grant(**grant)
    assert shell.status()["auto_approve_remaining_seconds"] == 0.0
    assert shell.status()["auto_approve_until_revoked"] is False
    await shell.close()


@pytest.mark.asyncio
async def test_revoke_stops_running_process_group(tmp_path: Path, pids: Path) -> None:
    """Revocation kills an active command before its delayed write."""
    shell = local_shell()
    shell.grant(60)
    command = f"echo $$ > {pids / 'leader.pid'}; sleep 2; touch marker"
    task = asyncio.create_task(shell.execute(request(command, tmp_path)))
    leader = int(await wait_for_file(pids / "leader.pid"))
    assert shell.status()["active_request_id"] == "r1"
    shell.revoke()
    with pytest.raises(DesktopShellError, match="stopped"):
        await asyncio.wait_for(task, timeout=3)
    await wait_until_gone(leader)
    assert not (tmp_path / "marker").exists()
    await shell.close()


@pytest.mark.asyncio
async def test_revoke_kills_term_resistant_child_after_parent_exits(tmp_path: Path, pids: Path) -> None:
    """Revocation escalates even when shell exits and redirected pipes close."""
    shell = local_shell()
    shell.grant(60)
    script = (
        "import os, pathlib, signal, sys, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
        "time.sleep(30)"
    )
    pid_file = pids / "child.pid"
    command = (
        f"{shlex.quote(sys.executable)} -c {shlex.quote(script)} {shlex.quote(str(pid_file))} >/dev/null 2>&1 & wait"
    )
    task = asyncio.create_task(shell.execute(request(command, tmp_path)))
    child = int(await wait_for_file(pid_file))
    shell.revoke()
    with pytest.raises(DesktopShellError, match="stopped"):
        await asyncio.wait_for(task, timeout=3)
    await wait_until_gone(child)
    await shell.close()


@pytest.mark.asyncio
async def test_successful_parent_exit_stops_background_group_before_result(tmp_path: Path, pids: Path) -> None:
    """A successful command keeps its result but cannot leave an ordinary background child running."""
    shell = local_shell()
    shell.grant(60)
    command = f"printf done; (sleep 1; printf survived > marker) >/dev/null 2>&1 & echo $! > {pids / 'child.pid'}"
    result = await shell.execute(request(command, tmp_path))
    assert (result.exit_code, completed_output(result)) == (0, "done")
    await wait_until_gone(int((pids / "child.pid").read_text()))
    assert not (tmp_path / "marker").exists()
    await shell.close()


@pytest.mark.asyncio
async def test_detached_pipe_holder_blocks_neither_execute_nor_close(tmp_path: Path, pids: Path) -> None:
    """A descendant in its own session may keep the pipes, but every call still returns promptly."""
    shell = local_shell()
    shell.grant(60)
    script = "import os, time; os.setsid(); time.sleep(8)"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)} & echo $! > {pids / 'detached.pid'}; printf ok"
    started = time.monotonic()
    result = await asyncio.wait_for(shell.execute(request(command, tmp_path, timeout=1)), timeout=4)
    assert (result.exit_code, completed_output(result)) == (0, "ok")
    await asyncio.wait_for(shell.close(), timeout=2)
    assert time.monotonic() - started < 4


@pytest.mark.asyncio
async def test_command_past_inline_wait_becomes_a_handle_with_full_output(tmp_path: Path) -> None:
    """A command still running after its inline wait keeps running; checking collects its complete output."""
    shell = local_shell()
    shell.grant(60)
    command = "printf 'early\\n'; while [ ! -f release ]; do sleep 0.05; done; printf late; exit 7"
    running = await asyncio.wait_for(shell.execute(request(command, tmp_path, timeout=1)), timeout=4)
    assert (running.state, running.exit_code) == ("running", None)
    assert running.handle is not None
    assert running.output.tail(1024) == b"early\n"
    [entry] = shell.status()["handles"]
    assert entry["handle"] == running.handle
    assert (entry["requester_id"], entry["agent_name"], entry["state"]) == (REQUESTER, AGENT, "running")
    assert entry["command_preview"] == command
    assert entry["elapsed_seconds"] >= 1
    assert shell.check(REQUESTER, AGENT, running.handle).state == "running"
    (tmp_path / "release").touch()
    completed = await check_until_completed(shell, running.handle)
    assert (completed.exit_code, completed_output(completed)) == (7, "early\nlate")
    assert shell.status()["handles"] == []
    with pytest.raises(DesktopShellError, match="Unknown shell handle"):
        shell.check(REQUESTER, AGENT, running.handle)
    await shell.close()


@pytest.mark.asyncio
async def test_inline_wait_leaves_margin_before_command_expiry(tmp_path: Path) -> None:
    """A long requested wait is capped so the reply can still arrive before the command lifetime ends."""
    shell = local_shell()
    shell.grant(60)
    started = time.monotonic()
    result = await shell.execute(request("sleep 30", tmp_path, timeout=60, lifetime_ms=5_000))
    assert result.state == "running"
    assert time.monotonic() - started < 3
    await shell.close()


@pytest.mark.asyncio
async def test_handles_belong_to_the_exact_requester_and_agent(tmp_path: Path) -> None:
    """Another requester or agent sees the same error as for a handle that never existed."""
    shell = local_shell()
    shell.grant(60)
    running = await shell.execute(request("sleep 30", tmp_path, timeout=1))
    assert running.handle is not None
    for requester_id, agent_name, handle in (
        ("@other:local", AGENT, running.handle),
        (REQUESTER, "other-agent", running.handle),
        (REQUESTER, AGENT, "shell:missing"),
    ):
        with pytest.raises(DesktopShellError, match=r"^Unknown shell handle\.$"):
            shell.check(requester_id, agent_name, handle)
        with pytest.raises(DesktopShellError, match=r"^Unknown shell handle\.$"):
            shell.kill(requester_id, agent_name, handle)
    assert shell.check(REQUESTER, AGENT, running.handle).state == "running"
    assert shell.kill(REQUESTER, AGENT, running.handle, force=True) == "killed"
    completed = await check_until_completed(shell, running.handle)
    assert completed.exit_code == -signal.SIGKILL
    completed.output.release()
    await shell.close()


@pytest.mark.asyncio
async def test_kill_reports_an_already_completed_handle(tmp_path: Path) -> None:
    """Killing a handle whose process already exited reports completion and keeps its output."""
    shell = local_shell()
    shell.grant(60)
    running = await shell.execute(request("sleep 1.5; printf finished", tmp_path, timeout=1))
    assert running.handle is not None
    for _ in range(600):
        if shell.status()["handles"][0]["state"] == "completed":
            break
        await asyncio.sleep(0.01)
    assert shell.kill(REQUESTER, AGENT, running.handle) == "completed"
    assert completed_output(shell.check(REQUESTER, AGENT, running.handle)) == "finished"
    await shell.close()


@pytest.mark.parametrize("stop", ["revoke", "close", "local_kill"])
@pytest.mark.asyncio
async def test_revoke_close_and_local_kill_stop_handles(tmp_path: Path, pids: Path, stop: str) -> None:
    """Every local stop control kills handle process groups and forgets them."""
    shell = local_shell()
    shell.grant(60)
    running = await shell.execute(request(f"echo $$ > {pids / 'leader.pid'}; sleep 30", tmp_path, timeout=1))
    assert running.handle is not None
    leader = int(await wait_for_file(pids / "leader.pid"))
    if stop == "revoke":
        shell.revoke()
    elif stop == "close":
        await asyncio.wait_for(shell.close(), timeout=2)
    else:
        shell.kill_handle(running.handle)
        with pytest.raises(DesktopShellError, match="Unknown shell handle"):
            shell.kill_handle(running.handle)
    await wait_until_gone(leader)
    assert shell.status()["handles"] == []
    with pytest.raises(DesktopShellError, match="Unknown shell handle"):
        shell.check(REQUESTER, AGENT, running.handle)
    await shell.close()


@pytest.mark.asyncio
async def test_handle_capacity_evicts_completed_handles_before_refusing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retained handles stay bounded: the oldest completed one makes room, running ones never do."""
    monkeypatch.setattr("mindroom.desktop.shell._MAX_HANDLES", 2)
    shell = local_shell()
    shell.grant(60)
    finished = await shell.execute(request("sleep 1.2", tmp_path, request_id="r1", timeout=1))
    running = await shell.execute(request("sleep 30", tmp_path, request_id="r2", timeout=1))
    assert finished.handle is not None
    assert running.handle is not None
    for _ in range(600):
        if {entry["handle"]: entry["state"] for entry in shell.status()["handles"]}[finished.handle] == "completed":
            break
        await asyncio.sleep(0.01)
    third = await shell.execute(request("sleep 30", tmp_path, request_id="r3", timeout=1))
    assert {entry["handle"] for entry in shell.status()["handles"]} == {running.handle, third.handle}
    with pytest.raises(DesktopShellError, match="Too many"):
        await shell.execute(request("touch marker", tmp_path, request_id="r4"))
    assert not (tmp_path / "marker").exists()
    await shell.close()


@pytest.mark.asyncio
async def test_output_is_captured_up_to_the_cap_and_reports_truncation(tmp_path: Path) -> None:
    """Complete output is kept up to 10 MiB and anything beyond is reported as truncated."""
    shell = local_shell()
    shell.grant(60)
    small = await shell.execute(request(f"{shlex.quote(sys.executable)} -c 'print(\"x\" * 100_000)'", tmp_path))
    assert (small.output.size, small.output.truncated) == (100_001, False)
    assert completed_output(small) == "x" * 100_000 + "\n"
    command = (
        f"{shlex.quote(sys.executable)} -c 'import sys; sys.stdout.write(\"y\" * {MAX_SHELL_OUTPUT_BYTES + 65_536})'"
    )
    large = await shell.execute(request(command, tmp_path, request_id="r2"))
    assert large.output.truncated is True
    assert MAX_SHELL_OUTPUT_BYTES - 65_536 < large.output.size <= MAX_SHELL_OUTPUT_BYTES
    assert set(completed_output(large)) == {"y"}
    await shell.close()


@pytest.mark.asyncio
async def test_spool_lives_in_a_private_directory_removed_on_close(tmp_path: Path) -> None:
    """Captured output never lands in the command's directory and the helper directory is removed on close."""
    shell = local_shell()
    shell.grant(60)
    running = await shell.execute(request("printf secret; sleep 30", tmp_path, timeout=1))
    directory = Path(shell._spool_directory())
    assert directory.stat().st_mode & 0o077 == 0
    assert list(tmp_path.iterdir()) == []
    assert running.output.tail(1024) == b"secret"
    await shell.close()
    assert not directory.exists()


@pytest.mark.asyncio
async def test_approve_once_requires_new_decision_and_revoke_pending(tmp_path: Path) -> None:
    """One approval does not authorize a later command; revoke settles it without a process."""
    shell = local_shell()
    first = asyncio.create_task(shell.execute(request("printf first", tmp_path)))
    await wait_pending(shell)
    shell.decide("r1", approved=True)
    assert completed_output(await first) == "first"
    second = asyncio.create_task(shell.execute(request("touch marker", tmp_path, request_id="r2")))
    await wait_pending(shell)
    assert shell.status()["pending"]["request_id"] == "r2"
    shell.revoke()
    with pytest.raises(DesktopShellError, match="did not run"):
        await second
    assert not (tmp_path / "marker").exists()
    await shell.close()


@pytest.mark.asyncio
async def test_wall_clock_rollback_cannot_extend_pending_approval(tmp_path: Path) -> None:
    """Decision uses admitted monotonic deadline even when wall time rolls back."""
    wall = [100.0]
    monotonic = [100.0]
    shell = local_shell(clock=lambda: wall[0], monotonic_clock=lambda: monotonic[0])
    pending = DesktopShellRequest("r1", REQUESTER, AGENT, "touch marker", str(tmp_path), 101000)
    task = asyncio.create_task(shell.execute(pending))
    await wait_pending(shell)
    wall[0] = 1.0
    monotonic[0] = 101.1
    with pytest.raises(DesktopShellError):
        shell.decide("r1", approved=True)
    shell.revoke()
    with pytest.raises(DesktopShellError, match="did not run"):
        await task
    assert not (tmp_path / "marker").exists()
    await shell.close()


@pytest.mark.asyncio
async def test_concurrent_request_rejected_and_close_settles_pending(tmp_path: Path) -> None:
    """Only one command can await approval or its inline wait, and close clears that command."""
    shell = local_shell()
    first = asyncio.create_task(shell.execute(request("touch marker", tmp_path)))
    await wait_pending(shell)
    with pytest.raises(DesktopShellError):
        await shell.execute(request("touch second", tmp_path, request_id="r2"))
    await shell.close()
    with pytest.raises(DesktopShellError, match="did not run"):
        await first
    with pytest.raises(DesktopShellError):
        await shell.execute(request("touch third", tmp_path, request_id="r3"))
    assert not (tmp_path / "marker").exists()


@pytest.mark.asyncio
async def test_cancelling_execute_stops_process_before_delayed_write(tmp_path: Path, pids: Path) -> None:
    """Caller cancellation waits for process group shutdown."""
    shell = local_shell()
    shell.grant(60)
    command = f"echo $$ > {pids / 'leader.pid'}; sleep 2; touch marker"
    task = asyncio.create_task(shell.execute(request(command, tmp_path)))
    leader = int(await wait_for_file(pids / "leader.pid"))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await wait_until_gone(leader)
    assert not (tmp_path / "marker").exists()
    await shell.close()
