"""Persistent browser ownership and lifecycle behavior."""

import asyncio
from pathlib import Path

import pytest

from mindroom.worker_computer.display import WorkerDisplay
from mindroom.worker_computer.protocol import BrowserSession
from mindroom.worker_computer.runtime import ComputerControlError, WorkerComputerRuntime


class FakeDisplay:
    """Display adapter without external processes."""

    display = ":99"
    socket_path = Path("/unused/computer.sock")

    def __init__(self) -> None:
        self.alive = False

    async def start(self) -> None:
        """Mark the fake process ready."""
        self.alive = True

    async def close(self) -> None:
        """Mark the fake process reaped."""
        self.alive = False

    def healthy(self) -> bool:
        """Report observable fake process health."""
        return self.alive


@pytest.mark.asyncio
async def test_takeover_waits_for_action_and_invalidates_queued_actions() -> None:
    """Takeover waits for action and invalidates queued actions."""
    runtime = WorkerComputerRuntime(FakeDisplay())
    started, finish = asyncio.Event(), asyncio.Event()
    page = []

    async def navigate() -> None:
        started.set()
        await finish.wait()
        page.append("opened")

    async def type_text() -> None:
        page.append("typed")

    await runtime.ensure_started()
    await runtime.attach_stream("viewer-1", runtime.status()["generation"])
    active = asyncio.create_task(runtime._run_browser_action(navigate))
    await started.wait()
    queued = asyncio.create_task(runtime._run_browser_action(type_text))
    await asyncio.sleep(0)
    takeover = asyncio.create_task(runtime.take_control("viewer-1"))
    await asyncio.sleep(0)
    assert not takeover.done()
    finish.set()
    await active
    with pytest.raises(ComputerControlError):
        await queued
    await takeover
    with pytest.raises(ComputerControlError, match="user"):
        await runtime._run_browser_action(type_text)
    await runtime.release_control("viewer-1")
    await runtime._run_browser_action(type_text)
    assert page == ["opened", "typed"]
    await runtime.close()


@pytest.mark.asyncio
async def test_stop_changes_generation_and_status_does_not_restart() -> None:
    """Stop changes generation and status does not restart."""
    display = FakeDisplay()
    runtime = WorkerComputerRuntime(display)
    initial = await runtime.ensure_started()
    await runtime.attach_stream("one", initial["generation"])
    await runtime.take_control("one")
    with pytest.raises(ComputerControlError, match="another"):
        await runtime.take_control("two")
    await runtime.release_control("two")
    assert runtime.status()["controller_session_id"] == "one"
    await runtime.stop()
    assert runtime.status()["state"] == "stopped"
    assert runtime.status()["generation"] != initial["generation"]
    assert not display.alive


@pytest.mark.asyncio
async def test_replaced_stream_disconnect_does_not_release_new_stream() -> None:
    """Replaced stream disconnect does not release new stream."""
    runtime = WorkerComputerRuntime(FakeDisplay())
    generation = (await runtime.ensure_started())["generation"]
    old = await runtime.attach_stream("viewer", generation)
    await runtime.take_control("viewer")
    new = await runtime.attach_stream("viewer", generation)
    assert old.is_set()
    await runtime.detach_stream("viewer", old)
    assert runtime.allows_input("viewer", new)
    await runtime.detach_stream("viewer", new)
    assert runtime.status()["controller_session_id"] is None
    await runtime.close()


@pytest.mark.asyncio
async def test_browser_binding_reuses_page_and_closes_on_configuration_change() -> None:
    """Browser binding reuses page and closes on configuration change."""
    runtime = WorkerComputerRuntime(FakeDisplay())
    persisted = []

    def factory(display: str) -> BrowserSession:
        assert display == ":99"
        page = []

        async def execute(text: str) -> list[str]:
            page.append(text)
            return list(page)

        async def close() -> None:
            persisted.extend(page)

        return BrowserSession(execute, close)

    assert await runtime.run_browser_call("scope/config-a", factory, ["one"], {}) == ["one"]
    assert await runtime.run_browser_call("scope/config-a", factory, ["two"], {}) == ["one", "two"]
    before = runtime.status()["generation"]
    assert await runtime.run_browser_call("scope/config-b", factory, ["three"], {}) == ["three"]
    assert persisted == ["one", "two"]
    assert runtime.status()["generation"] != before
    await runtime.stop()
    assert persisted == ["one", "two", "three"]


@pytest.mark.asyncio
async def test_unopened_stream_cannot_take_control() -> None:
    """Unopened stream cannot take control."""
    runtime = WorkerComputerRuntime(FakeDisplay())
    await runtime.ensure_started()
    with pytest.raises(ComputerControlError, match="stream"):
        await runtime.take_control("missing")
    assert runtime.status()["controller_session_id"] is None
    await runtime.close()


@pytest.mark.asyncio
async def test_dead_display_invalidates_stream_and_generation() -> None:
    """Dead display invalidates stream and generation."""
    display = FakeDisplay()
    runtime = WorkerComputerRuntime(display)
    generation = (await runtime.ensure_started())["generation"]
    stream = await runtime.attach_stream("viewer", generation)
    await runtime.take_control("viewer")
    display.alive = False
    await asyncio.wait_for(stream.wait(), timeout=2)
    assert runtime.status()["state"] == "stopped"
    assert runtime.status()["generation"] != generation
    await runtime.close()


@pytest.mark.asyncio
async def test_release_disconnects_controller_to_reset_held_input() -> None:
    """Closing the RFB client on release lets the display release held keys/buttons."""
    runtime = WorkerComputerRuntime(FakeDisplay())
    generation = (await runtime.ensure_started())["generation"]
    stream = await runtime.attach_stream("controller", generation)
    await runtime.take_control("controller")
    await runtime.release_control("controller")
    assert stream.is_set()
    assert runtime.status()["controller_session_id"] is None
    assert runtime.status()["generation"] == generation
    watcher = await runtime.attach_stream("controller", generation)
    assert not runtime.allows_input("controller", watcher)
    await runtime.close()


@pytest.mark.asyncio
async def test_display_timeout_reaps_started_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A never-ready display cannot leave a live subprocess after startup fails."""
    real_spawn = asyncio.create_subprocess_exec
    children = []

    async def spawn(*_args: object, **_kwargs: object) -> asyncio.subprocess.Process:
        process = await real_spawn("sleep", "60")
        children.append(process)
        return process

    monkeypatch.setattr("mindroom.worker_computer.display.shutil.which", lambda _name: "/display")
    monkeypatch.setattr("mindroom.worker_computer.display.asyncio.create_subprocess_exec", spawn)
    display = WorkerDisplay(tmp_path, readiness_timeout=0.02)
    with pytest.raises(TimeoutError):
        await display.start()
    assert not display.healthy()
    assert children[0].returncode is not None
    assert not display.socket_path.exists()


@pytest.mark.asyncio
async def test_timed_out_action_invalidates_uncertain_browser_state() -> None:
    """A timed-out navigation cannot leave a live computer with uncertain page state."""
    runtime = WorkerComputerRuntime(FakeDisplay())
    generation = (await runtime.ensure_started())["generation"]

    async def stalled_action() -> None:
        async with asyncio.timeout(0.01):
            await asyncio.Event().wait()

    with pytest.raises(TimeoutError):
        await runtime._run_browser_action(stalled_action)
    assert runtime.status()["state"] == "stopped"
    assert runtime.status()["generation"] != generation


@pytest.mark.asyncio
async def test_stale_control_generation_cannot_stop_replacement_runtime() -> None:
    """A delayed control request must not mutate a newer computer generation."""
    runtime = WorkerComputerRuntime(FakeDisplay())
    original = await runtime.ensure_started()
    await runtime.stop()
    replacement = await runtime.ensure_started()
    with pytest.raises(ComputerControlError):
        await runtime.stop(generation=original["generation"])
    assert runtime.status() == replacement
    await runtime.close()


class DeferredDisplayChild:
    """Process boundary exposing termination and reaping independently."""

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminated = False
        self.waiting = asyncio.Event()
        self.exit_allowed = asyncio.Event()

    def terminate(self) -> None:
        """Record the signal without pretending the child has exited."""
        self.terminated = True

    def kill(self) -> None:
        """Allow a forcefully killed child to exit."""
        self.exit_allowed.set()

    async def wait(self) -> int:
        """Complete reaping only when the external child exits."""
        self.waiting.set()
        await self.exit_allowed.wait()
        self.returncode = 0
        return 0


@pytest.mark.asyncio
@pytest.mark.parametrize("via_runtime", [False, True])
async def test_display_teardown_drains_both_children_after_repeated_cancellation(
    tmp_path: Path,
    via_runtime: bool,
) -> None:
    """Cancellation at either child wait cannot abandon another display child."""
    initial_tasks = asyncio.all_tasks()
    display = WorkerDisplay(tmp_path)
    server, manager = DeferredDisplayChild(), DeferredDisplayChild()
    display._children = [server, manager]
    runtime = WorkerComputerRuntime(display)
    await runtime.ensure_started()
    closing = asyncio.create_task(runtime.stop() if via_runtime else display.close())
    await asyncio.wait_for(manager.waiting.wait(), timeout=1)
    restarting = asyncio.create_task(runtime.ensure_started() if via_runtime else display.start())
    try:
        for _ in range(3):
            closing.cancel()
            await asyncio.sleep(0)
        assert not closing.done(), "Display teardown escaped with unreaped children"
        if restarting is not None:
            assert not restarting.done()
            restarting.cancel()
            await asyncio.gather(restarting, return_exceptions=True)
        manager.exit_allowed.set()
        await asyncio.wait_for(server.waiting.wait(), timeout=1)
        for _ in range(3):
            closing.cancel()
            await asyncio.sleep(0)
        assert not closing.done()
    finally:
        manager.exit_allowed.set()
        server.exit_allowed.set()
        if restarting is not None:
            restarting.cancel()
            await asyncio.gather(restarting, return_exceptions=True)
        await asyncio.gather(closing, return_exceptions=True)
        await runtime.close()
    assert closing.cancelled()
    assert server.terminated
    assert manager.terminated
    assert server.returncode == manager.returncode == 0
    assert not display._children
    assert not (asyncio.all_tasks() - initial_tasks)


@pytest.mark.asyncio
async def test_monitor_can_finish_its_own_teardown() -> None:
    """Display failure cleanup cannot cancel the monitor awaiting that cleanup."""
    initial_tasks = asyncio.all_tasks()
    display = FakeDisplay()
    runtime = WorkerComputerRuntime(display)
    await runtime.ensure_started()
    monitor = runtime._monitor
    assert monitor is not None
    display.alive = False
    await asyncio.wait_for(asyncio.shield(monitor), timeout=1)
    assert not monitor.cancelled()
    assert runtime.status()["state"] == "stopped"
    await runtime.close()
    assert not (asyncio.all_tasks() - initial_tasks)


@pytest.mark.asyncio
async def test_display_cleanup_error_still_reaps_other_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreapable child stays owned while other children still get reaped."""
    display = WorkerDisplay(tmp_path)
    server, manager = DeferredDisplayChild(), DeferredDisplayChild()
    display._children = [server, manager]
    server.exit_allowed.set()

    async def failed_wait() -> int:
        msg = "wait failed"
        raise RuntimeError(msg)

    monkeypatch.setattr(manager, "wait", failed_wait)
    try:
        with pytest.raises(ExceptionGroup, match="display children"):
            await display.close()
        assert server.returncode == 0
        assert display._children == [manager]
    finally:
        monkeypatch.undo()
        manager.exit_allowed.set()
        await display.close()
    assert not display._children


@pytest.mark.asyncio
async def test_runtime_retries_failed_browser_cleanup_before_restart() -> None:
    """A failed browser close keeps ownership and blocks replacement until retry."""
    display = FakeDisplay()
    runtime = WorkerComputerRuntime(display)
    resources = {"old browser"}
    allow_cleanup = False

    async def execute() -> str:
        return "ready"

    async def close() -> None:
        if not allow_cleanup:
            msg = "close failed"
            raise RuntimeError(msg)
        resources.clear()

    await runtime.run_browser_call("binding", lambda _: BrowserSession(execute, close), [], {})
    try:
        with pytest.raises(ExceptionGroup, match="computer resources"):
            await runtime.stop()
        assert not display.healthy()
        with pytest.raises(ExceptionGroup, match="computer resources"):
            await runtime.ensure_started()
        assert not display.healthy()
    finally:
        allow_cleanup = True
        await runtime.ensure_started()
        await runtime.close()
    assert not resources
