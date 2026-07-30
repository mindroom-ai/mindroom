"""Todo poke runtime coordinator tests."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.constants import ORIGINAL_SENDER_KEY
from mindroom.orchestration.todo_poke_runtime import TodoPokeRuntimeCoordinator
from mindroom.orchestrator import _MultiAgentOrchestrator
from tests.conftest import bind_runtime_paths, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.bot import AgentBot, TeamBot
    from mindroom.constants import RuntimePaths


def _config(tmp_path: Path) -> Config:
    runtime_paths = test_runtime_paths(tmp_path)
    return bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code")},
            teams={
                "dev": TeamConfig(
                    display_name="Dev",
                    role="Develop",
                    agents=["code"],
                ),
            },
        ),
        runtime_paths=runtime_paths,
    )


def _coordinator(
    runtime_paths: RuntimePaths,
    config: Config | None,
    bots: dict[str, AgentBot | TeamBot],
) -> TodoPokeRuntimeCoordinator:
    return TodoPokeRuntimeCoordinator(
        runtime_paths=runtime_paths,
        config_provider=lambda: config,
        bot_provider=bots.get,
    )


def _bot(**overrides: object) -> MagicMock:
    bot = MagicMock()
    bot.running = overrides.get("running", True)
    bot.client = overrides.get("client", object())
    bot.in_flight_response_count = overrides.get("in_flight_response_count", 0)
    return bot


def test_idle_check_includes_direct_and_running_team_bots(tmp_path: Path) -> None:
    """Direct activity or activity in any running member team makes the agent busy."""
    direct_bot = _bot()
    team_bot = _bot()
    coordinator = _coordinator(
        test_runtime_paths(tmp_path),
        _config(tmp_path),
        {"code": direct_bot, "dev": team_bot},
    )

    assert coordinator._agent_is_idle("code") is True

    direct_bot.in_flight_response_count = 1
    assert coordinator._agent_is_idle("code") is False

    direct_bot.in_flight_response_count = 0
    team_bot.in_flight_response_count = 1
    assert coordinator._agent_is_idle("code") is False

    team_bot.running = False
    assert coordinator._agent_is_idle("code") is True
    assert coordinator._agent_is_idle("removed") is False


@pytest.mark.asyncio
async def test_router_query_and_send_wiring(tmp_path: Path) -> None:
    """Router I/O supplies schedule scopes and dispatches with the internal requester."""
    router_bot = _bot()
    router_bot._hook_send_message = AsyncMock(return_value="$event")
    coordinator = _coordinator(
        test_runtime_paths(tmp_path),
        _config(tmp_path),
        {"router": router_bot},
    )

    with (
        patch(
            "mindroom.orchestration.todo_poke_runtime.get_pending_schedule_thread_ids_for_room",
            new=AsyncMock(return_value=frozenset({"$scheduled"})),
        ) as schedule_query,
        patch(
            "mindroom.orchestration.todo_poke_runtime.mindroom_user_id",
            return_value="@mindroom_user:localhost",
        ),
    ):
        pending = await coordinator._schedule_query("!room:localhost")
        event_id = await coordinator._send_poke(
            "!room:localhost",
            "@code Todo work is ready.",
            "$thread",
        )

    assert pending == frozenset({"$scheduled"})
    schedule_query.assert_awaited_once_with(router_bot.client, "!room:localhost")
    assert event_id == "$event"
    router_bot._hook_send_message.assert_awaited_once_with(
        "!room:localhost",
        "@code Todo work is ready.",
        "$thread",
        "todo_poke",
        {ORIGINAL_SENDER_KEY: "@mindroom_user:localhost"},
        trigger_dispatch=True,
    )


@pytest.mark.asyncio
async def test_adapters_skip_when_runtime_is_unavailable(tmp_path: Path) -> None:
    """Unavailable idle, schedule, and sender adapters conservatively skip the tick."""
    coordinator = _coordinator(test_runtime_paths(tmp_path), _config(tmp_path), {})

    assert coordinator._agent_is_idle("code") is False
    assert await coordinator._schedule_query("!room:localhost") is None
    assert (
        await coordinator._send_poke(
            "!room:localhost",
            "@code Todo work is ready.",
            "$thread",
        )
        is None
    )


@pytest.mark.asyncio
async def test_schedule_adapter_preserves_read_errors(tmp_path: Path) -> None:
    """Read errors reach the scanner's tested fail-open boundary unchanged."""
    coordinator = _coordinator(
        test_runtime_paths(tmp_path),
        _config(tmp_path),
        {"router": _bot()},
    )

    with (
        patch(
            "mindroom.orchestration.todo_poke_runtime.get_pending_schedule_thread_ids_for_room",
            new=AsyncMock(side_effect=RuntimeError("state unavailable")),
        ),
        pytest.raises(RuntimeError, match="state unavailable"),
    ):
        await coordinator._schedule_query("!room:localhost")


@pytest.mark.asyncio
async def test_sync_wires_coordinator_adapters_into_worker(tmp_path: Path) -> None:
    """The composition seam must install the production idle, schedule, and sender adapters."""
    coordinator = _coordinator(test_runtime_paths(tmp_path), _config(tmp_path), {})

    await coordinator.sync()
    worker = coordinator._worker
    assert worker is not None
    try:
        assert worker.deps.idle_check == coordinator._agent_is_idle
        assert worker.deps.schedule_query == coordinator._schedule_query
        assert worker.deps.sender == coordinator._send_poke
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_worker_lifecycle_survives_reload_and_stops(tmp_path: Path) -> None:
    """Sync starts one worker, reuses it on reload, and stops it promptly."""
    coordinator = _coordinator(test_runtime_paths(tmp_path), _config(tmp_path), {})

    await coordinator.sync()
    first_worker = coordinator._worker
    first_task = coordinator._task

    assert first_worker is not None
    assert first_task is not None
    assert first_task.done() is False

    await coordinator.sync()

    assert coordinator._worker is first_worker
    assert coordinator._task is first_task

    await coordinator.stop()

    assert first_task.done() is True
    assert coordinator._worker is None
    assert coordinator._task is None


@pytest.mark.asyncio
async def test_worker_restarts_after_task_finishes(tmp_path: Path) -> None:
    """A finished worker task is replaced on the next sync."""
    coordinator = _coordinator(test_runtime_paths(tmp_path), _config(tmp_path), {})

    await coordinator.sync()
    first_worker = coordinator._worker
    first_task = coordinator._task
    assert first_worker is not None
    assert first_task is not None

    first_worker.stop()
    await first_task
    await coordinator.sync()

    assert coordinator._worker is not first_worker
    assert coordinator._task is not first_task
    await coordinator.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled_by", ["zero-interval", "missing-config"])
async def test_sync_leaves_worker_stopped_when_disabled(tmp_path: Path, disabled_by: str) -> None:
    """A zero runtime interval or missing config leaves no worker or task running."""
    runtime_paths = test_runtime_paths(tmp_path)
    config: Config | None = _config(tmp_path)
    if disabled_by == "zero-interval":
        runtime_paths = replace(
            runtime_paths,
            process_env={"MINDROOM_TODO_POKE_INTERVAL_SECONDS": "0"},
        )
    else:
        config = None
    coordinator = _coordinator(runtime_paths, config, {})

    await coordinator.sync()

    assert coordinator._worker is None
    assert coordinator._task is None


def test_orchestrator_composes_live_coordinator_providers(tmp_path: Path) -> None:
    """Orchestrator wiring must expose live config and bot state to the coordinator."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=test_runtime_paths(tmp_path))
    coordinator = orchestrator._todo_poke_runtime

    assert coordinator.runtime_paths is orchestrator.runtime_paths
    assert coordinator.config_provider() is None

    config = _config(tmp_path)
    orchestrator.config = config
    assert coordinator.config_provider() is config

    sentinel = _bot()
    orchestrator.agent_bots["code"] = sentinel
    assert coordinator.bot_provider("code") is sentinel
    assert coordinator.bot_provider("missing") is None
