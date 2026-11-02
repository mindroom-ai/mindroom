"""Direct unit tests for the debounced config-reload lifecycle."""

from __future__ import annotations

import asyncio
import hashlib
import threading
from functools import partial
from typing import TYPE_CHECKING, NoReturn
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI

import mindroom.orchestration.config_lifecycle as lifecycle_module
from mindroom.api import config_lifecycle as api_config_lifecycle
from mindroom.api.main import initialize_api_app
from mindroom.config.agent import AgentConfig
from mindroom.config.load_attempt import load_config_attempt
from mindroom.config.main import Config, load_config
from mindroom.config.reload_status import ConfigReloadStatus
from mindroom.config.yaml_includes import source_files_fingerprint
from mindroom.orchestration.config_lifecycle import _ReplacementDrainState
from mindroom.orchestration.runtime import create_logged_task
from mindroom.response_admission import ResponseAdmissionGate
from tests.api.conftest import request_for, save_config
from tests.conftest import (
    SETTLE_TIMEOUT_SECONDS,
    bind_runtime_paths,
    make_config_reload_lifecycle,
    published_config_load,
    test_runtime_paths,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.load_attempt import ConfigLoad
    from mindroom.orchestration.config_updates import ConfigUpdatePlan


def test_replacement_drain_state_tracks_wait_warning_and_force() -> None:
    """Replacement-drain helpers should model wait, warning, and force transitions."""
    state = _ReplacementDrainState()

    assert state.waiting_for_idle is False

    state.begin_wait(now=10.0)

    assert state.waiting_for_idle is True
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
    assert state.should_force_apply(now=11.9, force_after_seconds=2.0) is False
    assert state.should_force_apply(now=12.0, force_after_seconds=2.0) is True


@pytest.mark.asyncio
async def test_config_change_during_startup_reaches_the_dashboard_and_applies_once_startup_publishes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An edit made while MindRoom starts is loaded for the dashboard at once and applied once startup publishes."""
    monkeypatch.setattr(lifecycle_module, "_CONFIG_RELOAD_DEBOUNCE_SECONDS", 0)
    running = False
    config = load_config(test_runtime_paths(tmp_path))
    loads: list[Config] = []

    def load_edit() -> ConfigLoad:
        loads.append(config)
        return published_config_load(config)

    lifecycle = make_config_reload_lifecycle(test_runtime_paths(tmp_path), current_config=config, load_config=load_edit)
    lifecycle.is_running = lambda: running

    lifecycle.request_reload()
    startup_load = lifecycle._reload_task
    assert startup_load is not None
    await asyncio.wait_for(startup_load, timeout=5)
    assert len(loads) == 1
    lifecycle.apply_update_plan.assert_not_awaited()

    running = True
    lifecycle.startup_published(config)
    reload_task = lifecycle._reload_task
    assert reload_task is not None
    await asyncio.wait_for(reload_task, timeout=5)
    assert len(loads) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", [False, True])
async def test_startup_load_keeps_the_watcher_on_include_files_it_read(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    broken: bool,
) -> None:
    """A root edit made while MindRoom starts that adds an include makes the watcher cover that file, valid or not."""
    monkeypatch.setattr(lifecycle_module, "_CONFIG_RELOAD_DEBOUNCE_SECONDS", 0)
    lifecycle = make_config_reload_lifecycle(test_runtime_paths(tmp_path))
    lifecycle.is_running = lambda: False
    config_path = lifecycle.runtime_paths.config_path
    included = config_path.with_name("defaults.yaml")
    included.write_text("enable_streaming: [\n" if broken else "enable_streaming: false\n", encoding="utf-8")
    config_path.write_text("defaults: !include defaults.yaml\n", encoding="utf-8")

    lifecycle.request_reload()
    startup_load = lifecycle._reload_task
    assert startup_load is not None
    await asyncio.wait_for(startup_load, timeout=5)

    assert included.resolve() in {path.resolve() for path in lifecycle.watched_source_files}


@pytest.mark.asyncio
@pytest.mark.parametrize("startup_published", [True, False])
async def test_config_change_after_the_runtime_stopped_loads_nothing(
    tmp_path: Path,
    *,
    startup_published: bool,
) -> None:
    """A file edit during shutdown is neither loaded nor applied, even when startup never published."""
    running = startup_published
    config = load_config(test_runtime_paths(tmp_path))
    lifecycle = make_config_reload_lifecycle(
        test_runtime_paths(tmp_path),
        current_config=config,
        load_config=MagicMock(),
    )
    lifecycle.is_running = lambda: running
    if startup_published:
        lifecycle.startup_published(config)
    await lifecycle.cancel()
    running = False

    lifecycle.request_reload()

    assert lifecycle._reload_task is None
    lifecycle.load_config.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("edited", [False, True])
async def test_startup_publication_reloads_only_when_its_files_changed_since_loading(
    tmp_path: Path,
    *,
    edited: bool,
) -> None:
    """An edit that landed before the watcher saw the config files still reaches the runtime."""
    lifecycle = make_config_reload_lifecycle(test_runtime_paths(tmp_path))
    config_path = lifecycle.runtime_paths.config_path
    config_path.write_text("defaults: {enable_streaming: true}\n", encoding="utf-8")
    config = load_config(lifecycle.runtime_paths)
    if edited:
        config_path.write_text("defaults: {enable_streaming: false}\n", encoding="utf-8")

    lifecycle.startup_published(config)

    assert (lifecycle._reload_task is not None) is edited
    assert lifecycle.status.status == ("pending" if edited else "applied")
    await lifecycle.cancel()


@pytest.mark.asyncio
async def test_rapid_requests_coalesce_into_one_reload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Multiple quick reload requests should extend the debounce and apply once."""
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle._CONFIG_RELOAD_DEBOUNCE_SECONDS", 0.05)
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle._REPLACEMENT_DRAIN_IDLE_POLL_SECONDS", 0.01)
    lifecycle = make_config_reload_lifecycle(test_runtime_paths(tmp_path))
    lifecycle._update_config = AsyncMock(return_value=True)

    lifecycle.request_reload()
    task = lifecycle._reload_task
    assert task is not None

    await asyncio.sleep(0.02)
    lifecycle.request_reload()

    assert lifecycle._reload_task is task
    await asyncio.wait_for(task, timeout=1)
    lifecycle._update_config.assert_awaited_once()


@pytest.mark.asyncio
async def test_reload_drains_active_responses_before_applying(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A queued reload should wait until in-flight responses finish."""
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle._CONFIG_RELOAD_DEBOUNCE_SECONDS", 0.01)
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle._REPLACEMENT_DRAIN_IDLE_POLL_SECONDS", 0.01)
    gate = ResponseAdmissionGate()
    assert gate.admit()
    current_config = Config()
    new_config = Config(defaults={"enable_streaming": False})
    load_config_mock = MagicMock(return_value=published_config_load(new_config))
    lifecycle = make_config_reload_lifecycle(
        test_runtime_paths(tmp_path),
        load_config=load_config_mock,
        current_config=current_config,
        response_admission_gate=gate,
    )

    lifecycle.request_reload()
    task = lifecycle._reload_task
    assert task is not None

    await asyncio.sleep(0.05)
    load_config_mock.assert_called_once()
    lifecycle.apply_update_plan.assert_not_awaited()

    gate.release()
    await asyncio.wait_for(task, timeout=1)
    lifecycle.apply_update_plan.assert_awaited_once()
    assert gate.closed is False


_AGENT1_YAML = "models:\n  default: {provider: openai, id: test-model}\nagents:\n  agent1:\n    display_name: Agent 1\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("apply_fails", [False, True])
async def test_a_reload_ends_the_script_launch_fence_once_published_or_failed(
    tmp_path: Path,
    apply_fails: bool,
) -> None:
    """New script launches read the applied config, so a reload releases them only after publishing it or failing."""
    current_config = Config()
    new_config = Config(defaults={"enable_streaming": False})
    lifecycle = make_config_reload_lifecycle(
        test_runtime_paths(tmp_path),
        current_config=current_config,
        load_config=lambda: published_config_load(new_config),
    )
    applied_when_released: list[Config | None] = []
    lifecycle.finish_reload = AsyncMock(
        side_effect=lambda: applied_when_released.append(lifecycle.fully_applied_config),
    )
    if apply_fails:
        lifecycle.apply_update_plan = AsyncMock(side_effect=RuntimeError("apply failed"))
        with pytest.raises(RuntimeError, match="apply failed"):
            await lifecycle._update_config()
    else:
        await lifecycle._update_config()

    assert applied_when_released == [current_config if apply_fails else new_config]


@pytest.mark.asyncio
async def test_another_update_waiting_for_the_config_lock_starts_after_the_launch_fence_ends(
    tmp_path: Path,
) -> None:
    """A plugin reload queued behind a config reload never shares the launch fence that reload then ends."""
    lifecycle = make_config_reload_lifecycle(
        test_runtime_paths(tmp_path),
        current_config=Config(),
        load_config=lambda: published_config_load(Config(defaults={"enable_streaming": False})),
    )
    applying = asyncio.Event()
    release_apply = asyncio.Event()
    order: list[str] = []

    async def apply_plan(*_args: object) -> bool:
        applying.set()
        await release_apply.wait()
        return True

    async def finish_reload() -> None:
        # The real release runs in its own task, so it yields before it ends the fence.
        await asyncio.sleep(0)
        order.append("fence ended")

    async def queued_update() -> None:
        async with lifecycle.config_update_lock:
            order.append("queued update")

    lifecycle.apply_update_plan = AsyncMock(side_effect=apply_plan)
    lifecycle.finish_reload = AsyncMock(side_effect=finish_reload)
    reload_task = asyncio.create_task(lifecycle._update_config())
    await asyncio.wait_for(applying.wait(), timeout=1)
    queued = asyncio.create_task(queued_update())
    await asyncio.sleep(0)
    release_apply.set()
    await asyncio.wait_for(asyncio.gather(reload_task, queued), timeout=1)

    assert order == ["fence ended", "queued update"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("new_yaml", "waits_for_idle"),
    [
        pytest.param(_AGENT1_YAML + "defaults:\n  enable_streaming: false\n", False, id="restarts-nothing"),
        pytest.param(_AGENT1_YAML + "    role: Changed\n", False, id="live-agent-edit"),
        pytest.param(_AGENT1_YAML.replace("Agent 1", "Agent One"), True, id="restarts-agent"),
        pytest.param(_AGENT1_YAML + "administrators:\n  - '@admin:localhost'\n", True, id="reply-authorization"),
    ],
)
async def test_busy_reload_waits_only_for_restarts_or_reply_authorization_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    new_yaml: str,
    waits_for_idle: bool,
) -> None:
    """A reload that restarts nothing applies during replies and still reports its fingerprint applied."""
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle._REPLACEMENT_DRAIN_IDLE_POLL_SECONDS", 0.01)
    runtime_paths = test_runtime_paths(tmp_path)
    runtime_paths.config_path.write_text(_AGENT1_YAML, encoding="utf-8")
    gate = ResponseAdmissionGate()
    lifecycle = make_config_reload_lifecycle(
        runtime_paths,
        current_config=load_config(runtime_paths),
        agent_bots={"router": MagicMock(), "agent1": MagicMock()},
        response_admission_gate=gate,
    )
    runtime_paths.config_path.write_text(new_yaml, encoding="utf-8")
    observed_gate_states: list[tuple[bool, int]] = []

    async def apply_plan(*_args: object) -> bool:
        observed_gate_states.append((gate.closed, gate.in_flight_response_count))
        return True

    lifecycle.apply_update_plan = AsyncMock(side_effect=apply_plan)

    assert gate.admit()
    update_task = asyncio.create_task(lifecycle._update_config())
    try:
        # A reload that waits for idle must stay pending over a window far below the 600 s force-apply timer;
        # one that does not wait finishes on its own after loading and diffing the config.
        timeout = 0.5 if waits_for_idle else SETTLE_TIMEOUT_SECONDS
        done, _pending = await asyncio.wait({update_task}, timeout=timeout)
        assert bool(done) is not waits_for_idle
    finally:
        gate.release()
    assert await asyncio.wait_for(update_task, timeout=SETTLE_TIMEOUT_SECONDS) is True

    # Admission is closed for the swap itself, over the busy response only when nothing restarts.
    assert observed_gate_states == [(True, 0 if waits_for_idle else 1)]
    # A swap that replaces no runtime must not cancel running exports through the replacement hook.
    assert lifecycle.before_runtime_replacement.await_count == (1 if waits_for_idle else 0)
    assert gate.closed is False
    assert lifecycle.status == ConfigReloadStatus(
        status="applied",
        fingerprint=load_config(runtime_paths).source_fingerprint,
    )


@pytest.mark.asyncio
async def test_reload_does_not_hold_config_lock_while_draining_responses(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Response drain must not block unrelated config-lock owners."""
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle._REPLACEMENT_DRAIN_IDLE_POLL_SECONDS", 0.01)
    gate = ResponseAdmissionGate()
    assert gate.admit()
    current_config = Config()
    new_config = Config(defaults={"enable_streaming": False})
    load_started = threading.Event()

    def observed_load_config() -> ConfigLoad:
        load_started.set()
        return published_config_load(new_config)

    lifecycle = make_config_reload_lifecycle(
        test_runtime_paths(tmp_path),
        load_config=observed_load_config,
        current_config=current_config,
        response_admission_gate=gate,
    )

    update_task = asyncio.create_task(lifecycle._update_config())
    assert await asyncio.to_thread(load_started.wait, 1)
    await asyncio.sleep(0.02)

    await asyncio.wait_for(lifecycle.config_update_lock.acquire(), timeout=0.1)
    lifecycle.config_update_lock.release()
    lifecycle.apply_update_plan.assert_not_awaited()

    gate.release()
    assert await asyncio.wait_for(update_task, timeout=1) is True


@pytest.mark.asyncio
async def test_semantic_noop_reload_does_not_wait_for_active_responses(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A comment-only or formatting-only save must not close response admission."""
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle._CONFIG_RELOAD_DEBOUNCE_SECONDS", 0)
    gate = ResponseAdmissionGate()
    assert gate.admit()
    current_config = Config()
    lifecycle = make_config_reload_lifecycle(
        test_runtime_paths(tmp_path),
        load_config=lambda: published_config_load(Config()),
        current_config=current_config,
        response_admission_gate=gate,
    )

    lifecycle.request_reload()
    task = lifecycle._reload_task
    assert task is not None

    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=0.2)
    finally:
        gate.release()
        await asyncio.gather(task, return_exceptions=True)

    lifecycle.apply_update_plan.assert_not_awaited()
    assert gate.closed is False


@pytest.mark.asyncio
async def test_semantic_noop_reload_records_new_include_sources(
    tmp_path: Path,
) -> None:
    """Equivalent config moved into an include must make that include watchable."""
    runtime_paths = test_runtime_paths(tmp_path)
    runtime_paths.config_path.write_text("defaults:\n  enable_streaming: true\n", encoding="utf-8")
    current_config = load_config(runtime_paths)
    included_defaults = tmp_path / "defaults.yaml"
    included_defaults.write_text("enable_streaming: true\n", encoding="utf-8")
    runtime_paths.config_path.write_text("defaults: !include defaults.yaml\n", encoding="utf-8")
    reloaded_config = load_config(runtime_paths)
    assert current_config.authored_model_dump() == reloaded_config.authored_model_dump()
    lifecycle = make_config_reload_lifecycle(
        runtime_paths,
        current_config=current_config,
        load_config=lambda: published_config_load(reloaded_config),
    )

    assert await lifecycle._update_config() is False

    assert lifecycle.watched_source_files == reloaded_config.source_files
    assert included_defaults.resolve() in lifecycle.watched_source_files
    lifecycle.apply_update_plan.assert_not_awaited()


@pytest.mark.asyncio
async def test_replacement_admission_serializes_config_and_mcp_owners(tmp_path: Path) -> None:
    """Concurrent replacement flows must never share or prematurely reopen gate ownership."""
    gate = ResponseAdmissionGate()
    lifecycle = make_config_reload_lifecycle(test_runtime_paths(tmp_path), response_admission_gate=gate)
    mcp_started = asyncio.Event()
    config_started = asyncio.Event()

    async def apply_mcp_restart() -> None:
        mcp_started.set()
        await asyncio.Future()

    async def apply_config_reload() -> None:
        config_started.set()
        await asyncio.Future()

    mcp_task = asyncio.create_task(
        lifecycle.apply_with_response_admission(
            apply_mcp_restart,
            operation_name="MCP catalog restart",
            request_is_current=lambda: True,
        ),
    )
    await mcp_started.wait()
    config_task = asyncio.create_task(
        lifecycle.apply_with_response_admission(
            apply_config_reload,
            operation_name="configuration reload",
            request_is_current=lambda: True,
        ),
    )
    await asyncio.sleep(0)
    assert gate.closed
    assert not config_started.is_set()
    mcp_task.cancel()
    await asyncio.gather(mcp_task, return_exceptions=True)
    await asyncio.wait_for(config_started.wait(), timeout=1)
    assert gate.closed
    config_task.cancel()
    await asyncio.gather(config_task, return_exceptions=True)
    assert gate.closed is False


@pytest.mark.asyncio
async def test_reload_loads_an_edit_while_another_replacement_drains(tmp_path: Path) -> None:
    """An edit reaches the dashboard while an MCP restart holds admission, so a dashboard save cannot overwrite it."""
    loaded = threading.Event()

    def load_edit() -> ConfigLoad:
        loaded.set()
        return published_config_load(Config(timezone="Europe/Amsterdam"))

    lifecycle = make_config_reload_lifecycle(test_runtime_paths(tmp_path), load_config=load_edit)
    mcp_started = asyncio.Event()
    release_mcp = asyncio.Event()

    async def apply_mcp_restart() -> None:
        mcp_started.set()
        await release_mcp.wait()

    mcp_task = asyncio.create_task(
        lifecycle.apply_with_response_admission(
            apply_mcp_restart,
            operation_name="MCP catalog restart",
            request_is_current=lambda: True,
        ),
    )
    await mcp_started.wait()
    reload_task = asyncio.create_task(lifecycle._update_config())
    loaded_during_drain = await asyncio.to_thread(loaded.wait, 5)
    # Release before asserting, so a reload stuck behind the drain fails instead of hanging.
    release_mcp.set()
    assert loaded_during_drain
    assert not reload_task.done()
    lifecycle.apply_update_plan.assert_not_awaited()

    await mcp_task
    assert await asyncio.wait_for(reload_task, timeout=5) is True
    lifecycle.apply_update_plan.assert_awaited_once()


@pytest.mark.asyncio
async def test_stuck_drain_warns_then_stops_deferring(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A wedged drain should warn, keep waiting, and only stop deferring at the bound."""
    warning_after_seconds = 0.5
    force_after_seconds = 1_000.0
    wait_started_at = 10.0
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle._REPLACEMENT_DRAIN_IDLE_POLL_SECONDS", 0)
    monkeypatch.setattr(
        "mindroom.orchestration.config_lifecycle._REPLACEMENT_DRAIN_WARNING_AFTER_SECONDS",
        warning_after_seconds,
    )
    monkeypatch.setattr(
        "mindroom.orchestration.config_lifecycle._REPLACEMENT_DRAIN_WARNING_INTERVAL_SECONDS",
        1.0,
    )
    monkeypatch.setattr(
        "mindroom.orchestration.config_lifecycle._REPLACEMENT_DRAIN_FORCE_AFTER_SECONDS",
        force_after_seconds,
    )
    logger_mock = MagicMock()
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle.logger", logger_mock)
    lifecycle = make_config_reload_lifecycle(test_runtime_paths(tmp_path))
    drain_state = _ReplacementDrainState()
    loop = MagicMock(spec=asyncio.AbstractEventLoop)
    loop.time.side_effect = [wait_started_at, wait_started_at + 1.0, wait_started_at + force_after_seconds]

    should_defer = await lifecycle._should_defer_replacement_for_active_responses(
        drain_state=drain_state,
        active_response_count=1,
        loop=loop,
        operation_name="configuration reload",
    )
    assert should_defer is True
    logger_mock.info.assert_any_call(
        "Deferring replacement until active responses finish",
        operation="configuration reload",
        active_response_count=1,
    )

    # Past the warning threshold but still inside the bound: warn and keep waiting.
    should_defer = await lifecycle._should_defer_replacement_for_active_responses(
        drain_state=drain_state,
        active_response_count=1,
        loop=loop,
        operation_name="configuration reload",
    )
    assert should_defer is True
    assert any(
        call.args
        and call.args[0] == "Replacement still waiting for active responses to finish"
        and call.kwargs["operation"] == "configuration reload"
        for call in logger_mock.warning.call_args_list
    )
    logger_mock.error.assert_not_called()

    # At the bound: stop deferring so the change cannot be starved forever.
    should_defer = await lifecycle._should_defer_replacement_for_active_responses(
        drain_state=drain_state,
        active_response_count=1,
        loop=loop,
        operation_name="configuration reload",
    )
    assert should_defer is False
    assert any(
        call.args
        and call.args[0] == "Applying replacement while responses are still active"
        and call.kwargs["operation"] == "configuration reload"
        for call in logger_mock.error.call_args_list
    )


@pytest.mark.asyncio
async def test_new_request_during_drain_keeps_waiting_for_idle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A newer config change should not make an active response reload early."""
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle._CONFIG_RELOAD_DEBOUNCE_SECONDS", 0.01)
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle._REPLACEMENT_DRAIN_IDLE_POLL_SECONDS", 0.005)
    logger_mock = MagicMock()
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle.logger", logger_mock)
    gate = ResponseAdmissionGate()
    assert gate.admit()
    current_config = Config()
    new_config = Config(defaults={"enable_streaming": False})
    load_config_mock = MagicMock(return_value=published_config_load(new_config))
    lifecycle = make_config_reload_lifecycle(
        test_runtime_paths(tmp_path),
        load_config=load_config_mock,
        current_config=current_config,
        response_admission_gate=gate,
    )

    lifecycle.request_reload()
    await asyncio.sleep(0.06)
    lifecycle.request_reload()
    await asyncio.sleep(0.06)

    task = lifecycle._reload_task
    assert task is not None
    assert load_config_mock.call_count == 2
    lifecycle.apply_update_plan.assert_not_awaited()

    gate.release()
    await asyncio.wait_for(task, timeout=1)

    lifecycle.apply_update_plan.assert_awaited_once()
    logger_mock.info.assert_any_call("Configuration reload superseded before publication; skipping")


@pytest.mark.asyncio
async def test_failed_update_does_not_strand_queued_reload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed update must not prevent a subsequently queued reload from running."""
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle._CONFIG_RELOAD_DEBOUNCE_SECONDS", 0.01)
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle._REPLACEMENT_DRAIN_IDLE_POLL_SECONDS", 0.01)
    lifecycle = make_config_reload_lifecycle(test_runtime_paths(tmp_path))

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

    lifecycle._update_config = AsyncMock(side_effect=failing_then_succeeding_update)
    lifecycle.request_reload()
    task = lifecycle._reload_task
    assert task is not None

    await asyncio.wait_for(task, timeout=2)

    assert lifecycle._update_config.await_count == 2


@pytest.mark.asyncio
async def test_config_change_during_update_triggers_second_reload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A config change arriving while an update runs should cause a second reload."""
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle._CONFIG_RELOAD_DEBOUNCE_SECONDS", 0.01)
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle._REPLACEMENT_DRAIN_IDLE_POLL_SECONDS", 0.01)
    lifecycle = make_config_reload_lifecycle(test_runtime_paths(tmp_path))

    call_count = 0

    async def update_config_with_second_change() -> bool:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            lifecycle.request_reload()
        return True

    lifecycle._update_config = AsyncMock(side_effect=update_config_with_second_change)
    lifecycle.request_reload()
    task = lifecycle._reload_task
    assert task is not None

    await asyncio.wait_for(task, timeout=2)

    assert lifecycle._update_config.await_count == 2


@pytest.mark.asyncio
async def test_cancel_logs_exception_instead_of_suppressing_silently(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Reload-task cancellation should log unexpected failures and keep shutdown moving."""
    logger_mock = MagicMock()
    monkeypatch.setattr("mindroom.orchestration.runtime.logger", logger_mock)
    lifecycle = make_config_reload_lifecycle(test_runtime_paths(tmp_path))
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
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle._REPLACEMENT_DRAIN_IDLE_POLL_SECONDS", 0.01)
    busy_gate = ResponseAdmissionGate()
    assert busy_gate.admit()
    current_config = Config()
    new_config = Config(defaults={"enable_streaming": False})
    load_config_mock = MagicMock(return_value=published_config_load(new_config))
    lifecycle = make_config_reload_lifecycle(
        test_runtime_paths(tmp_path),
        load_config=load_config_mock,
        current_config=current_config,
        response_admission_gate=busy_gate,
    )

    lifecycle.request_reload()
    task = lifecycle._reload_task
    assert task is not None
    await asyncio.sleep(0.05)

    await lifecycle.cancel()

    assert lifecycle._reload_task is None
    assert lifecycle._requested_at is None
    assert task.done()
    load_config_mock.assert_called_once()
    lifecycle.apply_update_plan.assert_not_awaited()


@pytest.mark.asyncio
async def test_config_load_stays_open_then_apply_waits_for_active_responses(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Loading and planning stay live, while publication still drains active responses."""
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle._CONFIG_RELOAD_DEBOUNCE_SECONDS", 0)
    load_started = threading.Event()
    release_load = threading.Event()
    observed_apply_counts: list[int] = []
    gate = ResponseAdmissionGate()
    assert gate.admit()
    current_config = Config()
    new_config = Config(defaults={"enable_streaming": False})

    def blocked_load() -> ConfigLoad:
        load_started.set()
        assert release_load.wait(timeout=2)
        return published_config_load(new_config)

    lifecycle = make_config_reload_lifecycle(
        test_runtime_paths(tmp_path),
        load_config=blocked_load,
        current_config=current_config,
        response_admission_gate=gate,
    )

    async def apply_plan(*_args: object) -> bool:
        observed_apply_counts.append(gate.in_flight_response_count)
        return True

    lifecycle.apply_update_plan = AsyncMock(side_effect=apply_plan)

    lifecycle.request_reload()
    reload_task = lifecycle._reload_task
    assert reload_task is not None

    try:
        assert await asyncio.to_thread(load_started.wait, 1)
        # Loading and validation do not publish anything, so responses may keep
        # entering on the current config snapshot.
        assert gate.admit() is True
        gate.release()

        release_load.set()
        await asyncio.sleep(0.05)
        lifecycle.apply_update_plan.assert_not_awaited()

        gate.release()
        await asyncio.wait_for(reload_task, timeout=1)

        lifecycle.apply_update_plan.assert_awaited_once()
        assert observed_apply_counts == [0]
        # The gate reopens once the apply completes.
        assert gate.admit() is True
        gate.release()
    finally:
        release_load.set()
        while gate.in_flight_response_count:
            gate.release()
        await asyncio.gather(reload_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_apply_does_not_block_response_drain_started_by_the_apply(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Applying a plan must not stall the response drain that stopping bots performs.

    Stopping a bot drains its detached responses. If the applier held the
    admission gate, a response parked at admission could not finish, so the
    drain would burn its whole timeout and then cancel live work.
    """
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle._CONFIG_RELOAD_DEBOUNCE_SECONDS", 0)
    gate = ResponseAdmissionGate()
    lifecycle = make_config_reload_lifecycle(
        test_runtime_paths(tmp_path),
        current_config=Config(),
        response_admission_gate=gate,
        load_config=lambda: published_config_load(Config(defaults={"enable_streaming": False})),
    )

    async def response_lifecycle() -> str:
        if not gate.admit():
            return "refused"
        gate.release()
        return "admitted"

    async def apply_plan(*_args: object) -> bool:
        # Stand in for the fleet's restart stop, whose bot preparation drains inbox responses.
        task = asyncio.create_task(response_lifecycle())
        _done, pending = await asyncio.wait([task], timeout=1)
        assert not pending, "drain stalled: response could not settle during apply"
        assert task.result() == "refused"
        return True

    lifecycle.apply_update_plan = AsyncMock(side_effect=apply_plan)

    lifecycle.request_reload()
    task = lifecycle._reload_task
    assert task is not None
    await asyncio.wait_for(task, timeout=2)

    lifecycle.apply_update_plan.assert_awaited_once()
    assert gate.closed is False


@pytest.mark.asyncio
async def test_drain_applies_reload_after_force_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A never-idle install must still get its config change applied eventually."""
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle._CONFIG_RELOAD_DEBOUNCE_SECONDS", 0)
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle._REPLACEMENT_DRAIN_IDLE_POLL_SECONDS", 0)
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle._REPLACEMENT_DRAIN_FORCE_AFTER_SECONDS", 0.05)
    gate = ResponseAdmissionGate()
    # A response that never finishes, so the gate is never idle.
    assert gate.admit()
    current_config = Config()
    new_config = Config(defaults={"enable_streaming": False})
    lifecycle = make_config_reload_lifecycle(
        test_runtime_paths(tmp_path),
        load_config=lambda: published_config_load(new_config),
        current_config=current_config,
        response_admission_gate=gate,
    )

    lifecycle.request_reload()
    task = lifecycle._reload_task
    assert task is not None
    await asyncio.wait_for(task, timeout=2)

    lifecycle.apply_update_plan.assert_awaited_once()
    # Admission reopens even though the forced apply ran over a live response.
    assert gate.closed is False


@pytest.mark.asyncio
async def test_update_config_builds_plan_and_dispatches(
    tmp_path: Path,
) -> None:
    """With an active config, update_config should diff configs and dispatch the plan."""
    runtime_paths = test_runtime_paths(tmp_path)
    current_config = bind_runtime_paths(Config(agents={"agent1": AgentConfig(display_name="Agent 1")}), runtime_paths)
    new_config = bind_runtime_paths(
        Config(agents={"agent1": AgentConfig(display_name="Agent One")}),
        runtime_paths,
    )
    lifecycle = make_config_reload_lifecycle(
        runtime_paths,
        load_config=lambda: published_config_load(new_config),
        current_config=current_config,
        agent_bots={"router": MagicMock(), "agent1": MagicMock()},
    )

    assert await lifecycle._update_config() is True

    lifecycle.apply_update_plan.assert_awaited_once()
    dispatched_config, plan = lifecycle.apply_update_plan.await_args.args
    plugin_changes = plan.plugin_changes
    assert dispatched_config is current_config
    assert plan.new_config is new_config
    assert "agent1" in plan.restart
    assert plugin_changes == ()


@pytest.mark.asyncio
async def test_failed_partial_publication_retries_from_last_successful_config(
    tmp_path: Path,
) -> None:
    """A partially published config must not make its retry look unchanged."""
    current_config = Config()
    new_config = Config(defaults={"enable_streaming": False})
    live_config = current_config
    applied_steps: list[tuple[Config, Config]] = []
    lifecycle = make_config_reload_lifecycle(
        test_runtime_paths(tmp_path),
        current_config=current_config,
        load_config=lambda: published_config_load(new_config),
    )
    lifecycle.current_config = lambda: live_config

    async def apply_plan(
        applied_config: Config,
        plan: ConfigUpdatePlan,
    ) -> bool:
        nonlocal live_config
        applied_steps.append((applied_config, plan.new_config))
        live_config = plan.new_config
        if len(applied_steps) == 1:
            msg = "failed after config publication"
            raise RuntimeError(msg)
        return True

    lifecycle.apply_update_plan = AsyncMock(side_effect=apply_plan)

    with pytest.raises(RuntimeError, match="failed after config publication"):
        await lifecycle._update_config()

    assert await lifecycle._update_config() is True
    assert applied_steps == [
        (current_config, new_config),
        (new_config, current_config),
        (current_config, new_config),
    ]


@pytest.mark.asyncio
async def test_failed_partial_publication_allows_rollback_to_last_successful_config(
    tmp_path: Path,
) -> None:
    """Restoring the last-good file must repair a partially published runtime."""
    current_config = Config()
    failed_config = Config(defaults={"enable_streaming": False})
    loaded_config = failed_config
    live_config = current_config
    applied_from: list[Config] = []
    applied_configs: list[Config] = []
    lifecycle = make_config_reload_lifecycle(
        test_runtime_paths(tmp_path),
        current_config=current_config,
        load_config=lambda: published_config_load(loaded_config),
    )
    lifecycle.current_config = lambda: live_config

    async def apply_plan(
        applied_config: Config,
        plan: ConfigUpdatePlan,
    ) -> bool:
        nonlocal live_config
        applied_from.append(applied_config)
        applied_configs.append(plan.new_config)
        live_config = plan.new_config
        if len(applied_configs) <= 2:
            msg = (
                "failed after config publication"
                if len(applied_configs) == 1
                else "failed during partial-publication repair"
            )
            raise RuntimeError(msg)
        return True

    lifecycle.apply_update_plan = AsyncMock(side_effect=apply_plan)

    with pytest.raises(RuntimeError, match="failed after config publication"):
        await lifecycle._update_config()

    loaded_config = current_config
    with pytest.raises(RuntimeError, match="failed during partial-publication repair"):
        await lifecycle._update_config()

    assert await lifecycle._update_config() is True
    assert applied_from == [current_config, failed_config, failed_config]
    assert applied_configs == [failed_config, current_config, current_config]


@pytest.mark.asyncio
async def test_failed_partial_publication_repairs_before_applying_corrected_config(
    tmp_path: Path,
) -> None:
    """A corrected third snapshot must apply only after restoring the last-good state."""
    current_config = Config(agents={"agent1": AgentConfig(display_name="Agent 1", rooms=["lobby"])})
    failed_config = Config(agents={"agent1": AgentConfig(display_name="Agent 1", rooms=["project"])})
    corrected_config = Config(
        agents={"agent1": AgentConfig(display_name="Agent 1", rooms=["lobby"])},
        defaults={"enable_streaming": False},
    )
    loaded_config = failed_config
    live_config = current_config
    applied_steps: list[tuple[Config, Config]] = []
    lifecycle = make_config_reload_lifecycle(
        test_runtime_paths(tmp_path),
        current_config=current_config,
        load_config=lambda: published_config_load(loaded_config),
    )
    lifecycle.current_config = lambda: live_config

    async def apply_plan(
        applied_config: Config,
        plan: ConfigUpdatePlan,
    ) -> bool:
        nonlocal live_config
        applied_steps.append((applied_config, plan.new_config))
        live_config = plan.new_config
        if len(applied_steps) == 1:
            msg = "failed after config publication"
            raise RuntimeError(msg)
        return True

    lifecycle.apply_update_plan = AsyncMock(side_effect=apply_plan)

    with pytest.raises(RuntimeError, match="failed after config publication"):
        await lifecycle._update_config()

    loaded_config = corrected_config
    assert await lifecycle._update_config() is True
    assert applied_steps == [
        (current_config, failed_config),
        (failed_config, current_config),
        (current_config, corrected_config),
    ]


@pytest.mark.asyncio
async def test_update_config_plugin_changes_restart_all_bots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Plugin entry changes restart every kept bot and never a removed one."""
    agents = {"agent1": AgentConfig(display_name="Agent 1"), "agent2": AgentConfig(display_name="Agent 2")}
    current_config = Config(agents={**agents, "gone": AgentConfig(display_name="Gone")})
    new_config = Config(agents=agents, plugins=["plugins/demo"])
    monkeypatch.setattr(
        "mindroom.orchestration.config_lifecycle.plugin_change_paths",
        lambda *_args: ("plugins/demo",),
    )
    lifecycle = make_config_reload_lifecycle(
        test_runtime_paths(tmp_path),
        load_config=lambda: published_config_load(new_config),
        current_config=current_config,
        agent_bots={name: MagicMock() for name in ("router", "agent1", "agent2", "gone")},
    )

    assert await lifecycle._update_config() is True

    _, dispatched_plan = lifecycle.apply_update_plan.await_args.args
    assert dispatched_plan.restart == {"router", "agent1", "agent2"}
    assert dispatched_plan.remove == {"gone"}
    assert dispatched_plan.plugin_changes == ("plugins/demo",)


@pytest.mark.asyncio
async def test_update_config_loads_config_off_event_loop(tmp_path: Path) -> None:
    """update_config must keep slow config loading off the loop so heartbeats keep ticking."""
    gate = threading.Event()
    load_started = threading.Event()
    loaded_config = Config(agents={"general": AgentConfig(display_name="General")})

    def slow_load_config() -> ConfigLoad:
        load_started.set()
        gate.wait()
        return published_config_load(loaded_config)

    lifecycle = make_config_reload_lifecycle(test_runtime_paths(tmp_path), load_config=slow_load_config)

    update_task = asyncio.get_running_loop().create_task(lifecycle._update_config())
    await asyncio.to_thread(load_started.wait, 5.0)

    # The loader thread is parked on the gate; the loop must stay live.
    heartbeats = 0
    while heartbeats < 50:
        await asyncio.sleep(0)
        heartbeats += 1
    assert not update_task.done()

    gate.set()
    assert await update_task is True
    _, plan = lifecycle.apply_update_plan.await_args.args
    assert plan.new_config is loaded_config


@pytest.mark.asyncio
async def test_update_config_waits_for_shared_lock_before_plugin_aware_loading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Config loading must not overlap another plugin/config publication owner."""
    lock = asyncio.Lock()
    current_config = Config()
    new_config = Config(defaults={"enable_streaming": False})
    to_thread_started = asyncio.Event()
    real_to_thread = asyncio.to_thread

    async def observed_to_thread(*args: object, **kwargs: object) -> object:
        to_thread_started.set()
        return await real_to_thread(*args, **kwargs)

    monkeypatch.setattr(lifecycle_module.asyncio, "to_thread", observed_to_thread)
    load_config_mock = MagicMock(return_value=published_config_load(new_config))
    lifecycle = make_config_reload_lifecycle(
        test_runtime_paths(tmp_path),
        current_config=current_config,
        load_config=load_config_mock,
    )
    lifecycle.config_update_lock = lock

    await lock.acquire()
    update_task = asyncio.create_task(lifecycle._update_config())
    await asyncio.sleep(0)
    try:
        assert not to_thread_started.is_set()
        assert not update_task.done()
        lifecycle.apply_update_plan.assert_not_awaited()
    finally:
        lock.release()
    assert await update_task is True
    load_config_mock.assert_called_once()
    lifecycle.apply_update_plan.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stale_edit",
    [
        pytest.param(_AGENT1_YAML.replace("Agent 1", "Stale Edit"), id="valid-stale-edit"),
        pytest.param("agents: [\n", id="broken-stale-edit"),
    ],
)
async def test_reload_that_loses_to_a_dashboard_save_adopts_nothing_and_stays_pending(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stale_edit: str,
) -> None:
    """A dashboard save committed during the reload's load wins; the stale read is neither applied nor failed."""
    runtime_paths = test_runtime_paths(tmp_path)
    runtime_paths.config_path.write_text(_AGENT1_YAML, encoding="utf-8")
    current_config = load_config(runtime_paths)
    api_app = FastAPI()
    initialize_api_app(api_app, runtime_paths)
    assert api_config_lifecycle.load_config_into_app(runtime_paths, api_app).loaded
    runtime_paths.config_path.write_text(stale_edit, encoding="utf-8")
    saved_payload = {
        "models": {"default": {"provider": "openai", "id": "test-model"}},
        "agents": {"agent1": {"display_name": "Saved From Dashboard"}},
    }
    real_load_attempt = api_config_lifecycle.load_config_attempt

    def load_then_lose_to_dashboard_save(paths: object) -> object:
        attempt = real_load_attempt(paths)
        save_config(request_for(api_app), saved_payload)
        return attempt

    monkeypatch.setattr(api_config_lifecycle, "load_config_attempt", load_then_lose_to_dashboard_save)
    lifecycle = make_config_reload_lifecycle(
        runtime_paths,
        current_config=current_config,
        agent_bots={"router": MagicMock(), "agent1": MagicMock()},
        load_config=partial(api_config_lifecycle.load_config_into_app, runtime_paths, api_app),
    )

    await lifecycle._apply_queued_config_reload()

    snapshot = api_config_lifecycle.require_api_state(api_app).snapshot
    assert snapshot.load_error is None
    assert snapshot.runtime_config.agents["agent1"].display_name == "Saved From Dashboard"
    lifecycle.apply_update_plan.assert_not_awaited()
    assert lifecycle.status == ConfigReloadStatus(status="pending")
    assert lifecycle.watched_source_files == frozenset()


@pytest.mark.asyncio
async def test_one_edit_is_loaded_once_and_the_dashboard_holds_the_config_the_runtime_applies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Under `mindroom run` an edit is read once, and the dashboard publishes the very config the runtime applies."""
    runtime_paths = test_runtime_paths(tmp_path)
    runtime_paths.config_path.write_text(_AGENT1_YAML, encoding="utf-8")
    current_config = load_config(runtime_paths)
    api_app = FastAPI()
    initialize_api_app(api_app, runtime_paths)
    real_load_attempt = api_config_lifecycle.load_config_attempt
    load_attempts: list[object] = []

    def counted_load_attempt(paths: object) -> object:
        load_attempts.append(paths)
        return real_load_attempt(paths)

    monkeypatch.setattr(api_config_lifecycle, "load_config_attempt", counted_load_attempt)
    lifecycle = make_config_reload_lifecycle(
        runtime_paths,
        current_config=current_config,
        agent_bots={"router": MagicMock(), "agent1": MagicMock()},
        load_config=partial(api_config_lifecycle.load_config_into_app, runtime_paths, api_app),
    )
    runtime_paths.config_path.write_text(_AGENT1_YAML.replace("Agent 1", "Renamed"), encoding="utf-8")

    assert await lifecycle._update_config() is True

    assert load_attempts == [runtime_paths]
    [(_applied_from, plan)] = [call.args for call in lifecycle.apply_update_plan.await_args_list]
    snapshot = api_config_lifecycle.require_api_state(api_app).snapshot
    assert plan.new_config is snapshot.runtime_config
    assert plan.new_config.agents["agent1"].display_name == "Renamed"


@pytest.mark.asyncio
async def test_failed_reload_load_reports_failure_and_watches_files_it_read(tmp_path: Path) -> None:
    """A load that nothing superseded fails the reload with the files it read and no unproven fingerprint."""
    runtime_paths = test_runtime_paths(tmp_path)
    lifecycle = make_config_reload_lifecycle(runtime_paths, current_config=Config())
    runtime_paths.config_path.write_text("defaults: !include missing.yaml\n", encoding="utf-8")

    await lifecycle._apply_queued_config_reload()

    lifecycle.apply_update_plan.assert_not_awaited()
    assert lifecycle.status == ConfigReloadStatus(status="failed")
    assert lifecycle.watched_source_files == frozenset({runtime_paths.config_path.resolve()})


def test_load_attempt_reports_the_source_a_legacy_access_migration_rewrote(tmp_path: Path) -> None:
    """A load that migrates and rewrites the config reports the rewritten file's fingerprint, not the authored one."""
    runtime_paths = test_runtime_paths(tmp_path)
    config_path = runtime_paths.config_path
    config_path.write_text(f"access_model: room_membership\n{_AGENT1_YAML}", encoding="utf-8")
    authored_digest = hashlib.sha256(config_path.read_bytes()).hexdigest()

    attempt = load_config_attempt(runtime_paths)

    rewritten_digest = hashlib.sha256(config_path.read_bytes()).hexdigest()
    assert rewritten_digest != authored_digest
    assert attempt.loaded
    assert attempt.source_files == frozenset({config_path.resolve()})
    assert attempt.source_fingerprint == source_files_fingerprint(
        config_path,
        {config_path.resolve(): rewritten_digest},
    )


def test_load_attempt_keeps_an_unexpected_error_for_the_reload_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A load failing with a non-config error reports that error instead of raising it or publishing config."""
    runtime_paths = test_runtime_paths(tmp_path)
    runtime_paths.config_path.write_text(_AGENT1_YAML, encoding="utf-8")
    failure = RuntimeError("plugin import failed")

    def fail_validation(*_args: object, **_kwargs: object) -> NoReturn:
        raise failure

    monkeypatch.setattr("mindroom.config.load_attempt.validate_loaded_config_source", fail_validation)

    attempt = load_config_attempt(runtime_paths)

    assert attempt.config is None
    assert attempt.error is failure
    assert attempt.source_files == frozenset({runtime_paths.config_path.resolve()})
    assert attempt.uses_includes is False
    with pytest.raises(RuntimeError, match="plugin import failed"):
        attempt.loaded_config()
