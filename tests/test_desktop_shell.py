"""Real subprocess checks for local desktop shell approval."""

import asyncio
import sys
import time
from pathlib import Path

import pytest

from mindroom.desktop.shell import DesktopShell, DesktopShellError, DesktopShellRequest


def request(command: str, cwd: Path, *, request_id: str = "r1", timeout: int = 30) -> DesktopShellRequest:
    """Build one short-lived request for real subprocess tests."""
    return DesktopShellRequest(
        request_id,
        "@user:local",
        "agent",
        command,
        str(cwd),
        int(time.time() * 1000) + 3000,
        timeout,
    )


async def wait_pending(shell: DesktopShell) -> None:
    """Bound waiting for asynchronous admission without relying on timing."""
    for _ in range(100):
        if shell.status()["pending"] is not None:
            return
        await asyncio.sleep(0.01)
    pytest.fail("approval request never appeared")


@pytest.mark.asyncio
async def test_command_starts_only_after_matching_approval(tmp_path: Path) -> None:
    """Wrong IDs cannot approve and no process starts before local consent."""
    shell = DesktopShell()
    marker = tmp_path / "marker"
    task = asyncio.create_task(shell.execute(request("printf approved > marker", tmp_path)))
    await wait_pending(shell)
    assert not marker.exists()
    with pytest.raises(DesktopShellError):
        shell.decide("wrong", approved=True)
    assert not marker.exists()
    shell.decide("r1", approved=True)
    result = await task
    assert marker.read_text() == "approved"
    assert result["exit_code"] == 0
    assert shell.status()["pending"] is None
    await shell.close()


@pytest.mark.asyncio
async def test_rejection_and_expiry_never_start_process(tmp_path: Path) -> None:
    """Denied, replayed, and expired requests leave no side effects."""
    shell = DesktopShell()
    marker = tmp_path / "marker"
    task = asyncio.create_task(shell.execute(request("touch marker", tmp_path)))
    await wait_pending(shell)
    shell.decide("r1", approved=False)
    with pytest.raises(DesktopShellError):
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
    shell = DesktopShell(clock=lambda: 100.0, monotonic_clock=lambda: now[0])
    shell.grant(60)
    first = DesktopShellRequest("r1", "user", "agent", "printf yes", str(tmp_path), 110000)
    assert (await shell.execute(first))["stdout"] == "yes"
    now[0] += 61
    second = DesktopShellRequest("r2", "user", "agent", "printf no", str(tmp_path), 110000)
    task = asyncio.create_task(shell.execute(second))
    await wait_pending(shell)
    shell.decide("r2", approved=True)
    assert (await task)["stdout"] == "no"
    await shell.close()


@pytest.mark.asyncio
async def test_revoke_stops_running_process_group(tmp_path: Path) -> None:
    """Revocation kills an active command before its delayed write."""
    shell = DesktopShell()
    shell.grant(60)
    marker = tmp_path / "marker"
    task = asyncio.create_task(shell.execute(request("sleep 2; touch marker", tmp_path)))
    for _ in range(100):
        if shell.status()["active_request_id"]:
            break
        await asyncio.sleep(0.01)
    assert shell.status()["active_request_id"] == "r1"
    await shell.revoke()
    result = await task
    assert result["cancelled"] is True
    assert not marker.exists()
    await shell.close()


@pytest.mark.asyncio
async def test_timeout_and_output_caps(tmp_path: Path) -> None:
    """Timeouts settle and both streams drain beyond capped replies."""
    shell = DesktopShell()
    shell.grant(60)
    slow = await shell.execute(request("sleep 2", tmp_path, timeout=1))
    assert slow["timed_out"] is True
    command = f'{sys.executable} -c \'import sys;sys.stdout.write("x"*20000);sys.stderr.write("y"*20000)\''
    output = await shell.execute(request(command, tmp_path, request_id="r2"))
    assert len(output["stdout"]) == 16384
    assert len(output["stderr"]) == 16384
    assert output["truncated"] is True
    await shell.close()


@pytest.mark.asyncio
async def test_timeout_stops_child_after_shell_parent_exits(tmp_path: Path) -> None:
    """A child holding pipes cannot outlive the overall timeout."""
    shell = DesktopShell()
    shell.grant(60)
    started = time.monotonic()
    result = await shell.execute(request("sleep 3 &", tmp_path, timeout=1))
    assert result["timed_out"] is True
    assert time.monotonic() - started < 2.5
    await shell.close()


@pytest.mark.asyncio
async def test_approve_once_requires_new_decision_and_revoke_pending(tmp_path: Path) -> None:
    """One approval does not authorize a later command; revoke settles it."""
    shell = DesktopShell()
    first = asyncio.create_task(shell.execute(request("printf first", tmp_path)))
    await wait_pending(shell)
    shell.decide("r1", approved=True)
    assert (await first)["stdout"] == "first"
    second = asyncio.create_task(shell.execute(request("printf second", tmp_path, request_id="r2")))
    await wait_pending(shell)
    assert shell.status()["pending"]["request_id"] == "r2"
    await shell.revoke()
    assert (await second)["cancelled"] is True
    await shell.close()


@pytest.mark.asyncio
async def test_wall_clock_rollback_cannot_extend_pending_approval(tmp_path: Path) -> None:
    """Decision uses admitted monotonic deadline even when wall time rolls back."""
    wall = [100.0]
    monotonic = [100.0]
    shell = DesktopShell(clock=lambda: wall[0], monotonic_clock=lambda: monotonic[0])
    pending = DesktopShellRequest("r1", "user", "agent", "touch marker", str(tmp_path), 101000)
    task = asyncio.create_task(shell.execute(pending))
    await wait_pending(shell)
    wall[0] = 1.0
    monotonic[0] = 101.1
    with pytest.raises(DesktopShellError):
        shell.decide("r1", approved=True)
    await shell.revoke()
    assert (await task)["cancelled"] is True
    assert not (tmp_path / "marker").exists()
    await shell.close()


@pytest.mark.asyncio
async def test_concurrent_request_rejected_and_close_settles_pending(tmp_path: Path) -> None:
    """Only one command can be admitted, and close clears that command."""
    shell = DesktopShell()
    first = asyncio.create_task(shell.execute(request("touch marker", tmp_path)))
    await wait_pending(shell)
    with pytest.raises(DesktopShellError):
        await shell.execute(request("touch second", tmp_path, request_id="r2"))
    await shell.close()
    assert (await first)["cancelled"] is True
    with pytest.raises(DesktopShellError):
        await shell.execute(request("touch third", tmp_path, request_id="r3"))
    assert not (tmp_path / "marker").exists()


@pytest.mark.asyncio
async def test_cancelling_execute_stops_process_before_delayed_write(tmp_path: Path) -> None:
    """Caller cancellation waits for process group shutdown."""
    shell = DesktopShell()
    shell.grant(60)
    task = asyncio.create_task(shell.execute(request("sleep 2; touch marker", tmp_path)))
    for _ in range(100):
        if shell.status()["active_request_id"]:
            break
        await asyncio.sleep(0.01)
    assert shell.status()["active_request_id"] == "r1"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.05)
    assert not (tmp_path / "marker").exists()
    await shell.close()
