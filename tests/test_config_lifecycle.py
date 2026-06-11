"""Direct unit tests for the debounced config-reload lifecycle."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.orchestration.config_lifecycle import ConfigReloadLifecycle, _ConfigReloadDrainState
from mindroom.orchestration.config_updates import ConfigUpdatePlan
from mindroom.orchestration.runtime import create_logged_task
from tests.conftest import bind_runtime_paths, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from mindroom.bot import AgentBot, TeamBot


def _make_lifecycle(
    tmp_path: Path,
    *,
    running: bool = True,
    current_config: Config | None = None,
    agent_bots: Mapping[str, AgentBot | TeamBot] | None = None,
    in_flight_response_count: Callable[[], int] | None = None,
) -> ConfigReloadLifecycle:
    """Return a lifecycle wired to stub dependencies."""
    return ConfigReloadLifecycle(
        runtime_paths=test_runtime_paths(tmp_path),
        is_running=lambda: running,
        current_config=lambda: current_config,
        agent_bots=lambda: agent_bots if agent_bots is not None else {},
        in_flight_response_count=in_flight_response_count or (lambda: 0),
        load_initial_config=AsyncMock(return_value=False),
        apply_update_plan=AsyncMock(return_value=True),
    )


def test_drain_state_tracks_wait_warning_force_and_reset() -> None:
    """Drain-state helpers should model wait, warning, force, and reset transitions."""
    state = _ConfigReloadDrainState()

    assert state.waiting_for_idle is False
    assert state.should_reset_for_request(1.0) is False

    state.begin_wait(now=10.0, requested_at=1.0)

    assert state.waiting_for_idle is True
    assert state.should_reset_for_request(1.0) is False
    assert state.should_reset_for_request(2.0) is True
    assert (
        state.should_warn(
            now=10.5,
            warning_after_seconds=1.0,
            warning_interval_seconds=10.0,
        )
        is False
    )
    assert (
        state.should_warn(
            now=11.0,
            warning_after_seconds=1.0,
            warning_interval_seconds=10.0,
        )
        is True
    )

    state.mark_warning(11.0)

    assert (
        state.should_warn(
            now=15.0,
            warning_after_seconds=1.0,
            warning_interval_seconds=10.0,
        )
        is False
    )
    assert (
        state.should_warn(
            now=21.0,
            warning_after_seconds=1.0,
            warning_interval_seconds=10.0,
        )
        is True
    )
    assert state.should_force_reload(now=11.9, force_after_seconds=2.0) is False
    assert state.should_force_reload(now=12.0, force_after_seconds=2.0) is True

    state.reset()

    assert state.waiting_for_idle is False
    assert state.should_reset_for_request(2.0) is False


@pytest.mark.asyncio
async def test_request_reload_is_ignored_until_running(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Reload requests before startup finishes should be dropped."""
    logger_mock = MagicMock()
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle.logger", logger_mock)
    lifecycle = _make_lifecycle(tmp_path, running=False)

    lifecycle.request_reload()

    assert lifecycle._requested_at is None
    assert lifecycle._reload_task is None
    assert any(
        call.args and call.args[0] == "Ignoring config change while startup is still in progress"
        for call in logger_mock.info.call_args_list
    )


@pytest.mark.asyncio
async def test_rapid_requests_coalesce_into_one_reload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Multiple quick reload requests should extend the debounce and apply once."""
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle._CONFIG_RELOAD_DEBOUNCE_SECONDS", 0.05)
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle._CONFIG_RELOAD_IDLE_POLL_SECONDS", 0.01)
    lifecycle = _make_lifecycle(tmp_path)
    lifecycle.update_config = AsyncMock(return_value=True)

    lifecycle.request_reload()
    task = lifecycle._reload_task
    assert task is not None

    await asyncio.sleep(0.02)
    lifecycle.request_reload()

    assert lifecycle._reload_task is task
    await asyncio.wait_for(task, timeout=1)
    lifecycle.update_config.assert_awaited_once()


@pytest.mark.asyncio
async def test_reload_drains_active_responses_before_applying(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A queued reload should wait until in-flight responses finish."""
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle._CONFIG_RELOAD_DEBOUNCE_SECONDS", 0.01)
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle._CONFIG_RELOAD_IDLE_POLL_SECONDS", 0.01)
    active_responses = [1]
    lifecycle = _make_lifecycle(tmp_path, in_flight_response_count=lambda: active_responses[0])
    lifecycle.update_config = AsyncMock(return_value=True)

    lifecycle.request_reload()
    task = lifecycle._reload_task
    assert task is not None

    await asyncio.sleep(0.05)
    lifecycle.update_config.assert_not_awaited()

    active_responses[0] = 0
    await asyncio.wait_for(task, timeout=1)
    lifecycle.update_config.assert_awaited_once()


@pytest.mark.asyncio
async def test_stuck_drain_warns_then_forces_reload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A wedged drain should warn and then force the reload through."""
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle._CONFIG_RELOAD_DEBOUNCE_SECONDS", 0.01)
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle._CONFIG_RELOAD_IDLE_POLL_SECONDS", 0.01)
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle._CONFIG_RELOAD_DRAIN_WARNING_AFTER_SECONDS", 0.02)
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle._CONFIG_RELOAD_DRAIN_WARNING_INTERVAL_SECONDS", 1.0)
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle._CONFIG_RELOAD_DRAIN_FORCE_AFTER_SECONDS", 0.04)
    logger_mock = MagicMock()
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle.logger", logger_mock)
    lifecycle = _make_lifecycle(tmp_path, in_flight_response_count=lambda: 1)
    lifecycle.update_config = AsyncMock(return_value=True)

    lifecycle.request_reload()
    task = lifecycle._reload_task
    assert task is not None

    await asyncio.wait_for(task, timeout=1)

    lifecycle.update_config.assert_awaited_once()
    assert any(
        call.args and call.args[0] == "Configuration reload still waiting for active responses to finish"
        for call in logger_mock.warning.call_args_list
    )
    assert any(
        call.args and call.args[0] == "Forcing configuration reload while responses are still active"
        for call in logger_mock.error.call_args_list
    )


@pytest.mark.asyncio
async def test_new_request_during_drain_restarts_drain_window(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A newer config change should get a fresh drain timeout window."""
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle._CONFIG_RELOAD_DEBOUNCE_SECONDS", 0.01)
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle._CONFIG_RELOAD_IDLE_POLL_SECONDS", 0.005)
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle._CONFIG_RELOAD_DRAIN_WARNING_AFTER_SECONDS", 1.0)
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle._CONFIG_RELOAD_DRAIN_WARNING_INTERVAL_SECONDS", 1.0)
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle._CONFIG_RELOAD_DRAIN_FORCE_AFTER_SECONDS", 0.12)
    lifecycle = _make_lifecycle(tmp_path, in_flight_response_count=lambda: 1)

    loop = asyncio.get_running_loop()
    started_at = loop.time()
    update_called_at: float | None = None

    async def fake_update_config() -> bool:
        nonlocal update_called_at
        update_called_at = loop.time()
        return True

    lifecycle.update_config = AsyncMock(side_effect=fake_update_config)

    lifecycle.request_reload()
    await asyncio.sleep(0.06)
    lifecycle.request_reload()

    task = lifecycle._reload_task
    assert task is not None
    await asyncio.wait_for(task, timeout=1)

    assert update_called_at is not None
    assert update_called_at - started_at >= 0.16
    lifecycle.update_config.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_update_does_not_strand_queued_reload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed update must not prevent a subsequently queued reload from running."""
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle._CONFIG_RELOAD_DEBOUNCE_SECONDS", 0.01)
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle._CONFIG_RELOAD_IDLE_POLL_SECONDS", 0.01)
    lifecycle = _make_lifecycle(tmp_path)

    call_count = 0

    async def failing_then_succeeding_update() -> bool:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First call fails; queue a new reload during the failure
            lifecycle.request_reload()
            msg = "Simulated config update failure"
            raise RuntimeError(msg)
        return True

    lifecycle.update_config = AsyncMock(side_effect=failing_then_succeeding_update)
    lifecycle.request_reload()
    task = lifecycle._reload_task
    assert task is not None

    await asyncio.wait_for(task, timeout=2)

    assert lifecycle.update_config.await_count == 2


@pytest.mark.asyncio
async def test_config_change_during_update_triggers_second_reload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A config change arriving while an update runs should cause a second reload."""
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle._CONFIG_RELOAD_DEBOUNCE_SECONDS", 0.01)
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle._CONFIG_RELOAD_IDLE_POLL_SECONDS", 0.01)
    lifecycle = _make_lifecycle(tmp_path)

    call_count = 0

    async def update_config_with_second_change() -> bool:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            lifecycle.request_reload()
        return True

    lifecycle.update_config = AsyncMock(side_effect=update_config_with_second_change)
    lifecycle.request_reload()
    task = lifecycle._reload_task
    assert task is not None

    await asyncio.wait_for(task, timeout=2)

    assert lifecycle.update_config.await_count == 2


@pytest.mark.asyncio
async def test_cancel_logs_exception_instead_of_suppressing_silently(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Reload-task cancellation should log unexpected failures and keep shutdown moving."""
    logger_mock = MagicMock()
    monkeypatch.setattr("mindroom.orchestration.runtime.logger", logger_mock)
    lifecycle = _make_lifecycle(tmp_path)
    started = asyncio.Event()

    async def fail_during_cancel() -> None:
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError as err:
            msg = "boom"
            raise RuntimeError(msg) from err

    lifecycle._reload_task = create_logged_task(
        fail_during_cancel(),
        name="config_reload",
        failure_message="config_reload failed",
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    await lifecycle.cancel()

    assert lifecycle._reload_task is None
    assert any(
        call.args
        and call.args[0] == "Detached task failed while being cancelled"
        and call.kwargs.get("task_name") == "config_reload"
        for call in logger_mock.debug.call_args_list
    )
    logger_mock.exception.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_clears_queued_reload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cancelling should stop the queued reload before it applies."""
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle._CONFIG_RELOAD_DEBOUNCE_SECONDS", 0.01)
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle._CONFIG_RELOAD_IDLE_POLL_SECONDS", 0.01)
    lifecycle = _make_lifecycle(tmp_path, in_flight_response_count=lambda: 1)
    lifecycle.update_config = AsyncMock(return_value=True)

    lifecycle.request_reload()
    task = lifecycle._reload_task
    assert task is not None
    await asyncio.sleep(0.05)

    await lifecycle.cancel()

    assert lifecycle._reload_task is None
    assert lifecycle._requested_at is None
    assert task.done()
    lifecycle.update_config.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_config_delegates_initial_load(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Without an active config, update_config should hand off to the initial loader."""
    new_config = Config()
    monkeypatch.setattr(
        "mindroom.orchestration.config_lifecycle.load_config",
        lambda *_args, **_kwargs: new_config,
    )
    lifecycle = _make_lifecycle(tmp_path, current_config=None)

    assert await lifecycle.update_config() is False

    lifecycle.load_initial_config.assert_awaited_once_with(new_config)
    lifecycle.apply_update_plan.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_config_builds_plan_and_dispatches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """With an active config, update_config should diff configs and dispatch the plan."""
    runtime_paths = test_runtime_paths(tmp_path)
    current_config = bind_runtime_paths(Config(agents={"agent1": AgentConfig(display_name="Agent 1")}), runtime_paths)
    new_config = bind_runtime_paths(
        Config(agents={"agent1": AgentConfig(display_name="Agent 1", role="changed role")}),
        runtime_paths,
    )
    monkeypatch.setattr(
        "mindroom.orchestration.config_lifecycle.load_config",
        lambda *_args, **_kwargs: new_config,
    )
    lifecycle = _make_lifecycle(
        tmp_path,
        current_config=current_config,
        agent_bots={"router": MagicMock(), "agent1": MagicMock()},
    )

    assert await lifecycle.update_config() is True

    lifecycle.load_initial_config.assert_not_awaited()
    lifecycle.apply_update_plan.assert_awaited_once()
    dispatched_config, plan, plugin_changes = lifecycle.apply_update_plan.await_args.args
    assert dispatched_config is current_config
    assert plan.new_config is new_config
    assert "agent1" in plan.entities_to_restart
    assert plugin_changes == ()


@pytest.mark.asyncio
async def test_update_config_plugin_changes_restart_all_bots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Plugin entry changes should expand the plan to restart every managed bot."""
    current_config = Config()
    new_config = Config()
    plan = ConfigUpdatePlan(
        new_config=new_config,
        changed_mcp_servers=set(),
        configured_entities={"router"},
        entities_to_restart={"agent1"},
        new_entities=set(),
        removed_entities=set(),
        mindroom_user_changed=False,
        matrix_room_access_changed=False,
        matrix_space_changed=False,
        authorization_changed=False,
    )
    monkeypatch.setattr(
        "mindroom.orchestration.config_lifecycle.load_config",
        lambda *_args, **_kwargs: new_config,
    )
    monkeypatch.setattr(
        "mindroom.orchestration.config_lifecycle.plugin_change_paths",
        lambda *_args: ("plugins/demo",),
    )
    monkeypatch.setattr(
        "mindroom.orchestration.config_lifecycle.build_config_update_plan",
        lambda **_kwargs: plan,
    )
    lifecycle = _make_lifecycle(
        tmp_path,
        current_config=current_config,
        agent_bots={"router": MagicMock(), "agent1": MagicMock(), "agent2": MagicMock()},
    )

    assert await lifecycle.update_config() is True

    _, dispatched_plan, plugin_changes = lifecycle.apply_update_plan.await_args.args
    assert dispatched_plan.entities_to_restart == {"router", "agent1", "agent2"}
    assert plugin_changes == ("plugins/demo",)
