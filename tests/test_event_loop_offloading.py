"""Regression tests for issue #1260: dispatch-path filesystem work must not block the event loop."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

import mindroom.ai as ai_module
import mindroom.memory._file_backend as file_backend_module
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.history import PreparedHistoryState
from mindroom.memory import MemoryPromptParts
from mindroom.memory._file_backend import FileMemoryBackend
from tests.conftest import bind_runtime_paths, test_runtime_paths

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


@pytest.mark.asyncio
async def test_file_memory_keyword_search_runs_off_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow keyword memory scan (read + score every memory file) must not stall the loop."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={"MATRIX_HOMESERVER": "http://localhost:8008", "MINDROOM_NAMESPACE": ""},
    )
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General")}),
        runtime_paths,
    )
    config.memory.backend = "file"

    gate = threading.Event()
    scan_started = threading.Event()

    def gated_scan(*_args: object, **_kwargs: object) -> list:
        scan_started.set()
        gate.wait()
        return []

    monkeypatch.setattr(file_backend_module, "_search_agent_file_scope_memories", gated_scan)
    backend = FileMemoryBackend(runtime_paths)
    search_task = asyncio.get_running_loop().create_task(
        backend.search("query", "general", tmp_path, config, limit=5),
    )
    await asyncio.to_thread(scan_started.wait, 5.0)

    # The scan thread is parked on the gate; the loop must stay live.
    await _assert_loop_heartbeats_while_pending(search_task)

    gate.set()
    assert await search_task == []
