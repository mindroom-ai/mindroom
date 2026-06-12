"""Regression tests for issue #1260: dispatch-path filesystem work must not block the event loop."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

import mindroom.ai as ai_module
from mindroom.config.main import Config
from mindroom.history import PreparedHistoryState
from mindroom.memory import MemoryPromptParts
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


async def _assert_loop_heartbeats_while_pending(task: asyncio.Task) -> None:
    """Tick the loop and assert the task is still parked off-loop."""
    heartbeats = 0
    while heartbeats < 50:
        await asyncio.sleep(0)
        heartbeats += 1
    assert not task.done()


@pytest.mark.asyncio
async def test_prepare_agent_and_prompt_builds_agent_off_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow agent build (workspace and context-file I/O) must not stall the loop."""
    gate = threading.Event()
    build_started = threading.Event()
    built_agent = MagicMock()

    def gated_create_agent(*_args: object, **_kwargs: object) -> MagicMock:
        build_started.set()
        gate.wait()
        return built_agent

    monkeypatch.setattr(ai_module, "create_agent", gated_create_agent)
    monkeypatch.setattr(
        ai_module,
        "build_memory_prompt_parts",
        AsyncMock(return_value=MemoryPromptParts()),
    )
    prepared_execution = SimpleNamespace(
        prepared_history=PreparedHistoryState(),
        replay_plan=None,
        unseen_event_ids=(),
        messages=[],
    )
    monkeypatch.setattr(
        ai_module,
        "prepare_agent_execution_context",
        AsyncMock(return_value=prepared_execution),
    )

    config = Config.model_validate({"agents": {"general": {"display_name": "General", "role": "test"}}})
    runtime_paths = test_runtime_paths(tmp_path)
    prepare_task = asyncio.get_running_loop().create_task(
        ai_module._prepare_agent_and_prompt("general", "hello", runtime_paths, config),
    )
    await asyncio.to_thread(build_started.wait, 5.0)

    # The agent build thread is parked on the gate; the loop must stay live.
    await _assert_loop_heartbeats_while_pending(prepare_task)

    gate.set()
    prepared_run = await prepare_task
    assert prepared_run.agent is built_agent
