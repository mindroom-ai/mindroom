"""Regression tests for issue #1260: dispatch-path filesystem work must not block the event loop."""

from __future__ import annotations

import asyncio
import threading
from contextvars import ContextVar
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from agno.models.message import Message

import mindroom.ai as ai_module
import mindroom.memory._file_backend as file_backend_module
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.history.types import PreparedHistoryState
from mindroom.hooks import render_transient_context
from mindroom.memory import MemoryPromptParts
from mindroom.memory._file_backend import FileMemoryBackend
from mindroom.timing import DispatchPipelineTiming
from tests.conftest import bind_runtime_paths, make_turn_context, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


async def _assert_loop_heartbeats_while_pending(task: asyncio.Task) -> None:
    """Tick the loop and assert the task is still parked off-loop."""
    heartbeats = 0
    while heartbeats < 50:
        await asyncio.sleep(0)
        heartbeats += 1
    assert not task.done()


def _prompt_preparation_config(memory_backend: str = "mem0") -> Config:
    return Config.model_validate(
        {"agents": {"general": {"display_name": "General", "role": "test", "memory_backend": memory_backend}}},
    )


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
        ai_module._prepare_agent_and_prompt(
            make_turn_context("general"),
            prompt="hello",
            runtime_paths=runtime_paths,
            config=config,
        ),
    )
    await asyncio.to_thread(build_started.wait, 5.0)

    # The agent build thread is parked on the gate; the loop must stay live.
    await _assert_loop_heartbeats_while_pending(prepare_task)

    gate.set()
    prepared_run = await prepare_task
    assert prepared_run.agent is built_agent


@pytest.mark.asyncio
@pytest.mark.parametrize("first_finished", ["memory", "agent"])
@pytest.mark.parametrize("memory_backend", ["mem0", "file"])
async def test_prepare_agent_and_prompt_joins_overlapping_branches_before_history(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first_finished: str,
    memory_backend: str,
) -> None:
    """Memory preparation and agent construction overlap, then both join before history."""
    memory_started = asyncio.Event()
    memory_release = asyncio.Event()
    memory_finished = asyncio.Event()
    agent_started = threading.Event()
    agent_release = threading.Event()
    agent_finished = threading.Event()
    prompt_composed = asyncio.Event()
    history_started = asyncio.Event()
    context_marker = ContextVar("prompt_preparation_context", default="missing")
    context_token = context_marker.set("preserved")
    built_agent = MagicMock()
    built_agent.additional_context = "existing context"

    async def gated_memory(*_args: object, **_kwargs: object) -> MemoryPromptParts:
        memory_started.set()
        if not agent_started.wait(1.0):
            msg = "agent construction was not submitted before synchronous memory preparation"
            raise TimeoutError(msg)
        await memory_release.wait()
        memory_finished.set()
        return MemoryPromptParts(session_preamble="session memory", transient_turn_context="turn memory")

    def gated_create_agent(*_args: object, **_kwargs: object) -> MagicMock:
        assert context_marker.get() == "preserved"
        agent_started.set()
        if not agent_release.wait(5.0):
            msg = "timed out waiting to release agent construction"
            raise TimeoutError(msg)
        agent_finished.set()
        return built_agent

    async def prepare_history(*_args: object, **kwargs: object) -> SimpleNamespace:
        history_started.set()
        assert kwargs["agent"] is built_agent
        assert kwargs["prompt"] == "raw prompt\n\nmodel metadata"
        assert len(kwargs["transient_context_messages"]) == 1
        assert kwargs["transient_context_messages"][0].content == render_transient_context(("turn memory",))
        assert kwargs["transient_context_messages"][0].add_to_agent_memory is False
        assert kwargs["resolved_runtime_model"].model_name == "default"
        return SimpleNamespace(
            prepared_history=PreparedHistoryState(),
            replay_plan=None,
            unseen_event_ids=[],
            messages=(Message(role="user", content=kwargs["prompt"]),),
        )

    original_compose = ai_module._compose_current_turn_prompt

    def compose_prompt(*, raw_prompt: str, model_prompt: str | None) -> str:
        assert memory_finished.is_set()
        assert agent_finished.is_set()
        prompt_composed.set()
        return original_compose(
            raw_prompt=raw_prompt,
            model_prompt=model_prompt,
        )

    monkeypatch.setattr(ai_module, "build_memory_prompt_parts", gated_memory)
    monkeypatch.setattr(ai_module, "create_agent", gated_create_agent)
    monkeypatch.setattr(ai_module, "_compose_current_turn_prompt", compose_prompt)
    monkeypatch.setattr(ai_module, "prepare_agent_execution_context", prepare_history)

    config = _prompt_preparation_config(memory_backend)
    pipeline_timing = DispatchPipelineTiming(source_event_id="$event", room_id="!room")
    prepare_task = asyncio.create_task(
        ai_module._prepare_agent_and_prompt(
            make_turn_context("general"),
            prompt="raw prompt",
            runtime_paths=test_runtime_paths(tmp_path),
            config=config,
            model_prompt="model metadata",
            pipeline_timing=pipeline_timing,
        ),
    )
    try:
        await asyncio.wait_for(memory_started.wait(), timeout=1.0)
        assert await asyncio.to_thread(agent_started.wait, 1.0)

        if first_finished == "memory":
            memory_release.set()
            await asyncio.wait_for(memory_finished.wait(), timeout=1.0)
        else:
            agent_release.set()
            assert await asyncio.to_thread(agent_finished.wait, 1.0)
        await asyncio.sleep(0)
        assert not prompt_composed.is_set()
        assert not history_started.is_set()

        memory_release.set()
        agent_release.set()
        prepared_run = await prepare_task
    finally:
        context_marker.reset(context_token)
        memory_release.set()
        agent_release.set()

    assert prepared_run.agent is built_agent
    assert prepared_run.prompt_text == "raw prompt\n\nmodel metadata"
    assert built_agent.additional_context == "existing context\n\nsession memory"
    assert pipeline_timing.metadata["prompt_branches_parallel"] is True
    assert pipeline_timing.metadata["prompt_branches_memory_backend"] == memory_backend
    assert "prompt_branches_start" in pipeline_timing.marks
    assert "prompt_branches_ready" in pipeline_timing.marks


@pytest.mark.asyncio
async def test_prepare_agent_and_prompt_keeps_cold_default_workspace_serial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cold default workspace stays serial so construction cannot change memory reads."""
    memory_finished = False
    built_agent = MagicMock()
    built_agent.additional_context = ""

    async def prepare_memory(*_args: object, **_kwargs: object) -> MemoryPromptParts:
        nonlocal memory_finished
        await asyncio.sleep(0)
        memory_finished = True
        return MemoryPromptParts()

    def build_agent(*_args: object, **_kwargs: object) -> MagicMock:
        assert memory_finished
        return built_agent

    monkeypatch.setattr(ai_module, "build_memory_prompt_parts", prepare_memory)
    monkeypatch.setattr(ai_module, "create_agent", build_agent)
    prepare_history = AsyncMock(
        return_value=SimpleNamespace(
            prepared_history=PreparedHistoryState(),
            replay_plan=None,
            unseen_event_ids=[],
            messages=(Message(role="user", content="hello"),),
        ),
    )
    monkeypatch.setattr(
        ai_module,
        "prepare_agent_execution_context",
        prepare_history,
    )

    config = Config.model_validate(
        {
            "agents": {
                "mind": {
                    "display_name": "Mind",
                    "role": "test",
                    "memory_backend": "file",
                    "context_files": [
                        "SOUL.md",
                        "AGENTS.md",
                        "USER.md",
                        "IDENTITY.md",
                        "TOOLS.md",
                        "HEARTBEAT.md",
                    ],
                },
            },
        },
    )
    pipeline_timing = DispatchPipelineTiming(source_event_id="$event", room_id="!room")
    prepared = await ai_module._prepare_agent_and_prompt(
        make_turn_context("mind"),
        prompt="hello",
        runtime_paths=test_runtime_paths(tmp_path),
        config=config,
        pipeline_timing=pipeline_timing,
    )

    assert prepared.agent is built_agent
    assert prepare_history.await_args.kwargs["resolved_runtime_model"].model_name == "default"
    assert pipeline_timing.metadata["prompt_branches_parallel"] is False
    assert pipeline_timing.metadata["prompt_branches_serial_reason"] == "default_workspace_scaffold_pending"


@pytest.mark.asyncio
async def test_prepare_agent_and_prompt_file_memory_preserves_reusable_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """File-memory overlap reuses caller-owned agent without rebuilding it."""
    reusable_agent = MagicMock()
    reusable_agent.additional_context = "caller context"
    create_agent_mock = MagicMock(side_effect=AssertionError("caller-owned agent must not be rebuilt"))

    async def prepare_history(*_args: object, **kwargs: object) -> SimpleNamespace:
        assert kwargs["agent"] is reusable_agent
        assert kwargs["resolved_runtime_model"].model_name == "default"
        transient_messages = kwargs["transient_context_messages"]
        assert [message.content for message in transient_messages] == [render_transient_context(("turn memory",))]
        return SimpleNamespace(
            prepared_history=PreparedHistoryState(prepared_context_tokens=42),
            replay_plan=None,
            unseen_event_ids=["$memory"],
            messages=(
                *transient_messages,
                Message(role="user", content=kwargs["prompt"]),
            ),
        )

    monkeypatch.setattr(
        ai_module,
        "build_memory_prompt_parts",
        AsyncMock(
            return_value=MemoryPromptParts(
                session_preamble="session memory",
                transient_turn_context="turn memory",
            ),
        ),
    )
    monkeypatch.setattr(ai_module, "create_agent", create_agent_mock)
    monkeypatch.setattr(ai_module, "prepare_agent_execution_context", prepare_history)

    prepared = await ai_module._prepare_agent_and_prompt(
        make_turn_context("general"),
        prompt="hello",
        runtime_paths=test_runtime_paths(tmp_path),
        config=_prompt_preparation_config("file"),
        reusable_agent=reusable_agent,
    )

    assert prepared.agent is reusable_agent
    assert reusable_agent.additional_context == "caller context\n\nsession memory"
    assert prepared.unseen_event_ids == ["$memory"]
    assert prepared.prepared_history.prepared_context_tokens == 42
    create_agent_mock.assert_not_called()


@pytest.mark.asyncio
async def test_parallel_file_prompt_matches_sequential_output_and_bookkeeping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parallel file preparation preserves sequential prompt and history results."""
    built_agents: list[MagicMock] = []
    history_inputs: list[tuple[str, tuple[str, ...], str]] = []

    async def prepare_memory(*_args: object, **_kwargs: object) -> MemoryPromptParts:
        return MemoryPromptParts(
            session_preamble="session memory",
            transient_turn_context="retrieved memory",
        )

    def build_agent(*_args: object, **_kwargs: object) -> MagicMock:
        agent = MagicMock()
        agent.additional_context = "base context"
        built_agents.append(agent)
        return agent

    async def prepare_history(*_args: object, **kwargs: object) -> SimpleNamespace:
        transient_messages = tuple(kwargs["transient_context_messages"])
        history_inputs.append(
            (
                kwargs["prompt"],
                tuple(message.content for message in transient_messages),
                kwargs["resolved_runtime_model"].model_name,
            ),
        )
        return SimpleNamespace(
            prepared_history=PreparedHistoryState(prepared_context_tokens=73),
            replay_plan=None,
            unseen_event_ids=["$one", "$two"],
            messages=(
                Message(role="system", content="stable system context"),
                *transient_messages,
                Message(role="user", content=kwargs["prompt"]),
            ),
        )

    monkeypatch.setattr(ai_module, "build_memory_prompt_parts", prepare_memory)
    monkeypatch.setattr(ai_module, "create_agent", build_agent)
    monkeypatch.setattr(ai_module, "prepare_agent_execution_context", prepare_history)

    config = _prompt_preparation_config("file")
    runtime_paths = test_runtime_paths(tmp_path)
    monkeypatch.setattr(ai_module, "agent_build_can_overlap_file_memory", lambda *_args: False)
    sequential = await ai_module._prepare_agent_and_prompt(
        make_turn_context("general"),
        prompt="raw prompt",
        runtime_paths=runtime_paths,
        config=config,
        model_prompt="model metadata",
    )
    monkeypatch.setattr(ai_module, "agent_build_can_overlap_file_memory", lambda *_args: True)
    parallel = await ai_module._prepare_agent_and_prompt(
        make_turn_context("general"),
        prompt="raw prompt",
        runtime_paths=runtime_paths,
        config=config,
        model_prompt="model metadata",
    )

    assert [(message.role, message.content, message.add_to_agent_memory) for message in parallel.messages] == [
        (message.role, message.content, message.add_to_agent_memory) for message in sequential.messages
    ]
    assert parallel.prompt_text == sequential.prompt_text
    assert parallel.unseen_event_ids == sequential.unseen_event_ids == ["$one", "$two"]
    assert parallel.prepared_history == sequential.prepared_history
    assert parallel.runtime_model_name == sequential.runtime_model_name
    assert [agent.additional_context for agent in built_agents] == [
        "base context\n\nsession memory",
        "base context\n\nsession memory",
    ]
    assert history_inputs == [
        (
            "raw prompt\n\nmodel metadata",
            (render_transient_context(("retrieved memory",)),),
            "default",
        ),
        (
            "raw prompt\n\nmodel metadata",
            (render_transient_context(("retrieved memory",)),),
            "default",
        ),
    ]


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
    assert (await search_task).results == []
