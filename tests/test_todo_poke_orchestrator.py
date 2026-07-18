"""Orchestrator wiring tests for native todo auto-pokes."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, patch

import pytest

from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.constants import ORIGINAL_SENDER_KEY
from mindroom.orchestrator import _MultiAgentOrchestrator
from tests.conftest import bind_runtime_paths, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


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


def test_todo_poke_idle_check_includes_direct_and_running_team_bots(tmp_path: Path) -> None:
    """Direct activity or activity in any running member team makes the agent busy."""
    config = _config(tmp_path)
    orchestrator = _MultiAgentOrchestrator(runtime_paths=test_runtime_paths(tmp_path))
    orchestrator.config = config
    direct_bot = SimpleNamespace(running=True, in_flight_response_count=0)
    team_bot = SimpleNamespace(running=True, in_flight_response_count=0)
    orchestrator.agent_bots = cast("dict[str, Any]", {"code": direct_bot, "dev": team_bot})

    assert orchestrator._todo_poke_agent_is_idle("code") is True

    direct_bot.in_flight_response_count = 1
    assert orchestrator._todo_poke_agent_is_idle("code") is False

    direct_bot.in_flight_response_count = 0
    team_bot.in_flight_response_count = 1
    assert orchestrator._todo_poke_agent_is_idle("code") is False

    team_bot.running = False
    assert orchestrator._todo_poke_agent_is_idle("code") is True
    assert orchestrator._todo_poke_agent_is_idle("removed") is False


@pytest.mark.asyncio
async def test_todo_poke_router_query_and_send_wiring(tmp_path: Path) -> None:
    """Router I/O supplies schedule scopes and dispatches with the internal requester."""
    config = _config(tmp_path)
    orchestrator = _MultiAgentOrchestrator(runtime_paths=test_runtime_paths(tmp_path))
    orchestrator.config = config
    router_bot = SimpleNamespace(
        running=True,
        client=object(),
        _hook_send_message=AsyncMock(return_value="$event"),
    )
    orchestrator.agent_bots = cast("dict[str, Any]", {"router": router_bot})

    with (
        patch(
            "mindroom.orchestrator.get_pending_schedule_thread_ids_for_room",
            new=AsyncMock(return_value=frozenset({"$scheduled"})),
        ) as schedule_query,
        patch("mindroom.orchestrator.mindroom_user_id", return_value="@mindroom_user:localhost"),
    ):
        pending = await orchestrator._todo_poke_schedule_query("!room:localhost")
        event_id = await orchestrator._send_todo_poke(
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
async def test_todo_poke_worker_lifecycle_survives_reload_and_stops(tmp_path: Path) -> None:
    """Runtime support starts one worker, reuses it on reload, and stops it promptly."""
    config = _config(tmp_path)
    orchestrator = _MultiAgentOrchestrator(runtime_paths=test_runtime_paths(tmp_path))
    orchestrator.config = config

    await orchestrator._sync_todo_poke_worker()
    first_worker = orchestrator._todo_poke_worker
    first_task = orchestrator._todo_poke_task

    assert first_worker is not None
    assert first_task is not None
    assert first_task.done() is False

    await orchestrator._sync_todo_poke_worker()

    assert orchestrator._todo_poke_worker is first_worker
    assert orchestrator._todo_poke_task is first_task

    await orchestrator._stop_todo_poke_worker()

    assert first_task.done() is True
    assert orchestrator._todo_poke_worker is None
    assert orchestrator._todo_poke_task is None


@pytest.mark.asyncio
async def test_todo_poke_worker_restarts_after_task_finishes(tmp_path: Path) -> None:
    """A finished worker task is replaced on the next runtime-support sync."""
    config = _config(tmp_path)
    orchestrator = _MultiAgentOrchestrator(runtime_paths=test_runtime_paths(tmp_path))
    orchestrator.config = config

    await orchestrator._sync_todo_poke_worker()
    first_worker = orchestrator._todo_poke_worker
    first_task = orchestrator._todo_poke_task
    assert first_worker is not None
    assert first_task is not None

    first_worker.stop()
    await first_task
    await orchestrator._sync_todo_poke_worker()

    assert orchestrator._todo_poke_worker is not first_worker
    assert orchestrator._todo_poke_task is not first_task
    await orchestrator._stop_todo_poke_worker()


@pytest.mark.asyncio
async def test_zero_interval_disables_todo_poke_worker(tmp_path: Path) -> None:
    """A zero runtime interval leaves no worker or task running."""
    config = _config(tmp_path)
    runtime_paths = replace(
        test_runtime_paths(tmp_path),
        process_env={"MINDROOM_TODO_POKE_INTERVAL_SECONDS": "0"},
    )
    orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths)
    orchestrator.config = config

    await orchestrator._sync_todo_poke_worker()

    assert orchestrator._todo_poke_worker is None
    assert orchestrator._todo_poke_task is None
