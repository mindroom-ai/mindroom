"""Tests for supervising workspace automation runtime tasks."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

import pytest

from mindroom.config.main import Config
from mindroom.config.models import WorkspaceAutomationPolicyConfig
from mindroom.constants import ROUTER_AGENT_NAME, RuntimePaths, resolve_runtime_paths
from mindroom.hooks import HookRegistry
from mindroom.workspace_automations.actions import WorkspaceAutomationActionResult
from mindroom.workspace_automations.executor import ShellCheckResult
from mindroom.workspace_automations.models import (
    LoadedWorkspaceAutomation,
    WorkspaceAutomationAction,
    WorkspaceAutomationCheck,
    WorkspaceAutomationLoadResult,
    WorkspaceAutomationTrigger,
)
from mindroom.workspace_automations.service import WorkspaceAutomationService
from mindroom.workspace_automations.targets import WorkspaceAutomationTarget

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path


@dataclass(frozen=True)
class _SleepRequest:
    delay: float
    future: asyncio.Future[None]


class _ControlledClock:
    def __init__(self, now: datetime) -> None:
        self.now = now
        self.requests: list[_SleepRequest] = []

    def current_time(self) -> datetime:
        return self.now

    async def sleep(self, delay: float) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.requests.append(_SleepRequest(delay=delay, future=future))
        await future

    async def wait_for_sleep_count(self, expected_count: int) -> None:
        for _ in range(100):
            if len(self.requests) >= expected_count:
                return
            await asyncio.sleep(0)
        msg = f"Timed out waiting for {expected_count} sleep request(s); got {len(self.requests)}"
        raise AssertionError(msg)

    def resolve_next_sleep(self) -> None:
        request = self.requests.pop(0)
        self.now += timedelta(seconds=request.delay)
        request.future.set_result(None)


class _TaskRecorder:
    def __init__(self) -> None:
        self.tasks: list[asyncio.Task[None]] = []

    def __call__(
        self,
        awaitable: Awaitable[None],
        *,
        name: str,
    ) -> asyncio.Task[None]:
        task = asyncio.create_task(awaitable, name=name)
        self.tasks.append(task)
        return task


class _RouterBot:
    client = object()


@pytest.fixture
def runtime_paths(tmp_path: Path) -> RuntimePaths:
    """Create isolated runtime paths for service tests."""
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
        },
    )


def _config(runtime_paths: RuntimePaths, *, enabled: bool = True) -> Config:
    return Config.validate_with_runtime(
        {
            "memory": {"backend": "none"},
            "agents": {
                "ops": {
                    "display_name": "Ops",
                    "rooms": ["Lobby"],
                    "workspace_automations": {
                        "enabled": enabled,
                        "allowed_actions": ["agent_message"],
                    },
                },
            },
        },
        runtime_paths,
    )


def _target(workspace_root: Path, *, enabled: bool = True) -> WorkspaceAutomationTarget:
    return WorkspaceAutomationTarget(
        agent_name="ops",
        agent_configured_rooms=("Lobby",),
        policy=WorkspaceAutomationPolicyConfig(
            enabled=enabled,
            allowed_actions=["agent_message"],
        ),
        agent_runtime=cast("Any", object()),
        workspace_root=workspace_root,
    )


def _automation(
    workspace_root: Path,
    *,
    automation_id: str = "urgent_email_poll",
    schedule: str = "* * * * *",
    trigger_exit_code: int | None = 42,
) -> LoadedWorkspaceAutomation:
    return LoadedWorkspaceAutomation(
        agent_name="ops",
        automation_id=automation_id,
        workspace_root=workspace_root,
        file_path=workspace_root / ".mindroom" / "automations.yaml",
        schedule=schedule,
        check=WorkspaceAutomationCheck(
            type="shell",
            command="./scripts/check_urgent_email.sh",
            timeout_seconds=20,
            tail=100,
        ),
        trigger=WorkspaceAutomationTrigger(exit_code=trigger_exit_code) if trigger_exit_code is not None else None,
        action=WorkspaceAutomationAction(
            type="agent_message",
            room="Lobby",
            thread_id="$thread",
            message="Urgent email condition matched.",
        ),
    )


def _check_result(automation_id: str = "urgent_email_poll", *, exit_code: int | None = 42) -> ShellCheckResult:
    return ShellCheckResult(
        automation_id=automation_id,
        ok=exit_code == 0,
        exit_code=exit_code,
        stdout="urgent email",
        stderr="",
        raw_output="urgent email",
        timed_out=False,
        error=None,
    )


def _bot_provider(agent_name: str) -> object | None:
    assert agent_name == ROUTER_AGENT_NAME
    return _RouterBot()


@pytest.mark.asyncio
async def test_start_scans_targets_and_schedules_loaded_automation(
    runtime_paths: RuntimePaths,
    tmp_path: Path,
) -> None:
    """Starting the service should scan configured targets and supervise loaded automations."""
    config = _config(runtime_paths)
    automation = _automation(tmp_path)
    task_recorder = _TaskRecorder()
    target_calls: list[tuple[Config, RuntimePaths]] = []
    loader_calls: list[tuple[str, Path]] = []
    clock = _ControlledClock(datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC))

    def target_loader(actual_config: Config, actual_runtime_paths: RuntimePaths) -> list[WorkspaceAutomationTarget]:
        target_calls.append((actual_config, actual_runtime_paths))
        return [_target(tmp_path)]

    def automation_loader(**kwargs: object) -> WorkspaceAutomationLoadResult:
        loader_calls.append((cast("str", kwargs["agent_name"]), cast("Path", kwargs["workspace_root"])))
        return WorkspaceAutomationLoadResult(automations=(automation,))

    service = WorkspaceAutomationService(
        target_loader=target_loader,
        automation_loader=automation_loader,
        now=clock.current_time,
        sleep=clock.sleep,
        task_factory=task_recorder,
        scan_interval_seconds=None,
    )

    await service.start(config, runtime_paths, HookRegistry.empty(), _bot_provider, object())

    loaded = service.list_loaded()
    assert [(item.agent_name, item.automation_id, item.workspace_root) for item in loaded] == [
        ("ops", "urgent_email_poll", str(tmp_path)),
    ]
    assert target_calls == [(config, runtime_paths)]
    assert loader_calls == [("ops", tmp_path)]
    assert len(task_recorder.tasks) == 1

    await service.shutdown()


@pytest.mark.asyncio
async def test_scan_interval_rescans_loaded_targets(
    runtime_paths: RuntimePaths,
    tmp_path: Path,
) -> None:
    """The background scanner should rescan after the configured scan interval."""
    config = _config(runtime_paths)
    clock = _ControlledClock(datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC))
    target_call_count = 0

    def target_loader(_config: Config, _runtime_paths: RuntimePaths) -> list[WorkspaceAutomationTarget]:
        nonlocal target_call_count
        target_call_count += 1
        return [_target(tmp_path)]

    def automation_loader(**_kwargs: object) -> WorkspaceAutomationLoadResult:
        return WorkspaceAutomationLoadResult()

    service = WorkspaceAutomationService(
        target_loader=target_loader,
        automation_loader=automation_loader,
        now=clock.current_time,
        sleep=clock.sleep,
        scan_interval_seconds=12.5,
    )

    await service.start(config, runtime_paths, HookRegistry.empty(), _bot_provider, object())
    await clock.wait_for_sleep_count(1)

    assert target_call_count == 1
    assert clock.requests[0].delay == 12.5

    clock.resolve_next_sleep()
    for _ in range(100):
        if target_call_count == 2:
            break
        await asyncio.sleep(0)
    assert target_call_count == 2

    await service.shutdown()


@pytest.mark.asyncio
async def test_cron_due_run_executes_check_trigger_action_and_writes_state(
    runtime_paths: RuntimePaths,
    tmp_path: Path,
) -> None:
    """A due cron run should check, match, act, and persist its latest status."""
    config = _config(runtime_paths)
    automation = _automation(tmp_path)
    clock = _ControlledClock(datetime(2026, 1, 1, 0, 0, 59, tzinfo=UTC))
    conversation_cache = object()
    action_event = asyncio.Event()
    bot_provider_calls: list[str] = []
    sender_builder_calls: list[tuple[object, Config, RuntimePaths, object]] = []
    action_calls: list[tuple[Config, object | None]] = []

    def target_loader(_config: Config, _runtime_paths: RuntimePaths) -> list[WorkspaceAutomationTarget]:
        return [_target(tmp_path)]

    def automation_loader(**_kwargs: object) -> WorkspaceAutomationLoadResult:
        return WorkspaceAutomationLoadResult(automations=(automation,))

    def bot_provider(agent_name: str) -> object | None:
        bot_provider_calls.append(agent_name)
        return _RouterBot()

    def message_sender_builder(
        client: object,
        actual_config: Config,
        actual_runtime_paths: RuntimePaths,
        *,
        conversation_cache: object,
    ) -> Callable[..., Awaitable[str | None]]:
        sender_builder_calls.append((client, actual_config, actual_runtime_paths, conversation_cache))

        async def sender(*_args: object, **_kwargs: object) -> str:
            return "$sent"

        return sender

    async def check_runner(**_kwargs: object) -> ShellCheckResult:
        return _check_result()

    async def action_runner(**kwargs: object) -> WorkspaceAutomationActionResult:
        action_calls.append((cast("Config", kwargs["config"]), kwargs["message_sender"]))
        action_event.set()
        return WorkspaceAutomationActionResult("urgent_email_poll", "agent_message", ok=True, event_id="$sent")

    service = WorkspaceAutomationService(
        target_loader=target_loader,
        automation_loader=automation_loader,
        check_runner=check_runner,
        action_runner=action_runner,
        message_sender_builder=message_sender_builder,
        now=clock.current_time,
        sleep=clock.sleep,
        scan_interval_seconds=None,
    )

    await service.start(config, runtime_paths, HookRegistry.empty(), bot_provider, conversation_cache)
    await clock.wait_for_sleep_count(1)
    clock.resolve_next_sleep()
    await asyncio.wait_for(action_event.wait(), timeout=1)

    assert bot_provider_calls == [ROUTER_AGENT_NAME]
    assert sender_builder_calls == [(_RouterBot.client, config, runtime_paths, conversation_cache)]
    assert action_calls[0][0] is config
    assert action_calls[0][1] is not None

    state_path = runtime_paths.storage_root / "workspace_automations" / "state.json"
    for _ in range(100):
        if state_path.exists():
            break
        await asyncio.sleep(0.01)
    assert state_path.exists()
    state = json.loads(state_path.read_text(encoding="utf-8"))
    records = list(state["automations"].values())
    assert records[0]["agent_name"] == "ops"
    assert records[0]["automation_id"] == "urgent_email_poll"
    assert records[0]["last_status"] == "action_succeeded"
    assert records[0]["last_exit_code"] == 42
    assert records[0]["last_event_id"] == "$sent"

    await service.shutdown()


@pytest.mark.asyncio
async def test_cron_due_run_skips_action_when_trigger_does_not_match(
    runtime_paths: RuntimePaths,
    tmp_path: Path,
) -> None:
    """The service should not run actions for checks whose trigger is not matched."""
    config = _config(runtime_paths)
    automation = _automation(tmp_path, trigger_exit_code=99)
    clock = _ControlledClock(datetime(2026, 1, 1, 0, 0, 59, tzinfo=UTC))

    async def check_runner(**_kwargs: object) -> ShellCheckResult:
        return _check_result(exit_code=42)

    async def action_runner(**_kwargs: object) -> WorkspaceAutomationActionResult:
        msg = "action_runner should not be called when trigger does not match"
        raise AssertionError(msg)

    service = WorkspaceAutomationService(
        target_loader=lambda _config, _runtime_paths: [_target(tmp_path)],
        automation_loader=lambda **_kwargs: WorkspaceAutomationLoadResult(automations=(automation,)),
        check_runner=check_runner,
        action_runner=action_runner,
        now=clock.current_time,
        sleep=clock.sleep,
        scan_interval_seconds=None,
    )

    await service.start(config, runtime_paths, HookRegistry.empty(), _bot_provider, object())
    await clock.wait_for_sleep_count(1)
    clock.resolve_next_sleep()
    for _ in range(100):
        if service.list_loaded()[0].last_status == "not_matched":
            break
        await asyncio.sleep(0)

    loaded = service.list_loaded()
    assert loaded[0].last_status == "not_matched"
    assert loaded[0].last_exit_code == 42

    await service.shutdown()


@pytest.mark.asyncio
async def test_refresh_replaces_config_without_duplicate_tasks(
    runtime_paths: RuntimePaths,
    tmp_path: Path,
) -> None:
    """Refreshing with the same automation key should update context without spawning a duplicate loop."""
    old_config = _config(runtime_paths)
    new_config = _config(runtime_paths)
    automation = _automation(tmp_path)
    clock = _ControlledClock(datetime(2026, 1, 1, 0, 0, 59, tzinfo=UTC))
    task_recorder = _TaskRecorder()
    action_event = asyncio.Event()
    action_configs: list[Config] = []

    async def check_runner(**_kwargs: object) -> ShellCheckResult:
        return _check_result()

    async def action_runner(**kwargs: object) -> WorkspaceAutomationActionResult:
        action_configs.append(cast("Config", kwargs["config"]))
        action_event.set()
        return WorkspaceAutomationActionResult("urgent_email_poll", "agent_message", ok=True)

    service = WorkspaceAutomationService(
        target_loader=lambda _config, _runtime_paths: [_target(tmp_path)],
        automation_loader=lambda **_kwargs: WorkspaceAutomationLoadResult(automations=(automation,)),
        check_runner=check_runner,
        action_runner=action_runner,
        now=clock.current_time,
        sleep=clock.sleep,
        task_factory=task_recorder,
        scan_interval_seconds=None,
    )

    await service.start(old_config, runtime_paths, HookRegistry.empty(), _bot_provider, object())
    await clock.wait_for_sleep_count(1)

    await service.refresh(new_config, HookRegistry.empty(), _bot_provider, object())

    assert len(task_recorder.tasks) == 1

    clock.resolve_next_sleep()
    await asyncio.wait_for(action_event.wait(), timeout=1)

    assert action_configs == [new_config]

    await service.shutdown()


@pytest.mark.asyncio
async def test_refresh_cancels_loaded_automation_when_policy_is_disabled(
    runtime_paths: RuntimePaths,
    tmp_path: Path,
) -> None:
    """A config refresh that disables policy should cancel the loaded automation loop."""
    enabled_config = _config(runtime_paths, enabled=True)
    disabled_config = _config(runtime_paths, enabled=False)
    automation = _automation(tmp_path)
    clock = _ControlledClock(datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC))
    task_recorder = _TaskRecorder()

    def target_loader(actual_config: Config, _runtime_paths: RuntimePaths) -> list[WorkspaceAutomationTarget]:
        if not actual_config.get_agent_workspace_automation_policy("ops").enabled:
            return []
        return [_target(tmp_path)]

    service = WorkspaceAutomationService(
        target_loader=target_loader,
        automation_loader=lambda **_kwargs: WorkspaceAutomationLoadResult(automations=(automation,)),
        now=clock.current_time,
        sleep=clock.sleep,
        task_factory=task_recorder,
        scan_interval_seconds=None,
    )

    await service.start(enabled_config, runtime_paths, HookRegistry.empty(), _bot_provider, object())
    await clock.wait_for_sleep_count(1)

    await service.refresh(disabled_config, HookRegistry.empty(), _bot_provider, object())

    assert service.list_loaded() == ()
    assert task_recorder.tasks[0].done()

    await service.shutdown()


@pytest.mark.asyncio
async def test_refresh_cancels_loaded_automation_when_file_is_deleted(
    runtime_paths: RuntimePaths,
    tmp_path: Path,
) -> None:
    """A scan that no longer loads an existing key should cancel that automation loop."""
    config = _config(runtime_paths)
    automation = _automation(tmp_path)
    clock = _ControlledClock(datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC))
    task_recorder = _TaskRecorder()
    loader_results = [
        WorkspaceAutomationLoadResult(automations=(automation,)),
        WorkspaceAutomationLoadResult(),
    ]

    def automation_loader(**_kwargs: object) -> WorkspaceAutomationLoadResult:
        return loader_results.pop(0)

    service = WorkspaceAutomationService(
        target_loader=lambda _config, _runtime_paths: [_target(tmp_path)],
        automation_loader=automation_loader,
        now=clock.current_time,
        sleep=clock.sleep,
        task_factory=task_recorder,
        scan_interval_seconds=None,
    )

    await service.start(config, runtime_paths, HookRegistry.empty(), _bot_provider, object())
    await clock.wait_for_sleep_count(1)

    await service.refresh(config, HookRegistry.empty(), _bot_provider, object())

    assert service.list_loaded() == ()
    assert task_recorder.tasks[0].done()

    await service.shutdown()


@pytest.mark.asyncio
async def test_shutdown_cancels_tasks_and_clears_loaded_automations(
    runtime_paths: RuntimePaths,
    tmp_path: Path,
) -> None:
    """Shutdown should cancel supervised tasks cleanly and clear runtime state."""
    config = _config(runtime_paths)
    automation = _automation(tmp_path)
    clock = _ControlledClock(datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC))
    task_recorder = _TaskRecorder()

    service = WorkspaceAutomationService(
        target_loader=lambda _config, _runtime_paths: [_target(tmp_path)],
        automation_loader=lambda **_kwargs: WorkspaceAutomationLoadResult(automations=(automation,)),
        now=clock.current_time,
        sleep=clock.sleep,
        task_factory=task_recorder,
        scan_interval_seconds=None,
    )

    await service.start(config, runtime_paths, HookRegistry.empty(), _bot_provider, object())
    await clock.wait_for_sleep_count(1)

    await service.shutdown()

    assert service.list_loaded() == ()
    assert all(task.done() for task in task_recorder.tasks)
