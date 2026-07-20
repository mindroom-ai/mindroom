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
import mindroom.execution_preparation as execution_preparation_module
import mindroom.memory._file_backend as file_backend_module
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.final_delivery import FinalDeliveryOutcome
from mindroom.history.runtime import ScopeSessionContext
from mindroom.history.types import HistoryScope, PreparedHistoryState
from mindroom.logging_config import get_logger
from mindroom.memory import MemoryPromptParts
from mindroom.memory._file_backend import FileMemoryBackend
from mindroom.post_response_effects import PostResponseEffectsDeps, ResponseOutcome, apply_post_response_effects
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
        location_marker=None,
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
async def test_prepare_agent_and_prompt_joins_overlapping_mem0_branches_before_history(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first_finished: str,
) -> None:
    """Mem0 preparation and agent construction overlap, then both join before history."""
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
        assert kwargs["prompt"] == "raw prompt"
        assert kwargs["current_message_suffix"] == "model metadata"
        assert len(kwargs["transient_context_messages"]) == 1
        assert kwargs["transient_context_messages"][0].content == "turn memory"
        assert kwargs["transient_context_messages"][0].add_to_agent_memory is False
        assert kwargs["resolved_runtime_model"].model_name == "default"
        return SimpleNamespace(
            prepared_history=PreparedHistoryState(),
            replay_plan=None,
            unseen_event_ids=[],
            messages=(
                Message(
                    role="user",
                    content=f"{kwargs['prompt']}\n\n{kwargs['current_message_suffix']}",
                ),
            ),
            location_marker=None,
        )

    original_tail = ai_module.model_prompt_tail_after_raw_prompt

    def compose_prompt(*, raw_prompt: str, model_prompt: str | None) -> str:
        assert memory_finished.is_set()
        assert agent_finished.is_set()
        prompt_composed.set()
        return original_tail(
            raw_prompt=raw_prompt,
            model_prompt=model_prompt,
        )

    monkeypatch.setattr(ai_module, "build_memory_prompt_parts", gated_memory)
    monkeypatch.setattr(ai_module, "create_agent", gated_create_agent)
    monkeypatch.setattr(ai_module, "model_prompt_tail_after_raw_prompt", compose_prompt)
    monkeypatch.setattr(ai_module, "prepare_agent_execution_context", prepare_history)

    config = _prompt_preparation_config()
    prepare_task = asyncio.create_task(
        ai_module._prepare_agent_and_prompt(
            make_turn_context("general"),
            prompt="raw prompt",
            runtime_paths=test_runtime_paths(tmp_path),
            config=config,
            model_prompt="model metadata",
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


@pytest.mark.asyncio
async def test_prepare_agent_and_prompt_keeps_file_memory_before_agent_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """File-memory preparation stays serial so workspace scaffolding cannot change its read."""
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
            location_marker=None,
        ),
    )
    monkeypatch.setattr(
        ai_module,
        "prepare_agent_execution_context",
        prepare_history,
    )

    config = _prompt_preparation_config("file")
    prepared = await ai_module._prepare_agent_and_prompt(
        make_turn_context("general"),
        prompt="hello",
        runtime_paths=test_runtime_paths(tmp_path),
        config=config,
    )

    assert prepared.agent is built_agent
    assert prepare_history.await_args.kwargs["resolved_runtime_model"] is None


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


@pytest.mark.asyncio
async def test_location_marker_lookup_runs_off_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The trusted-marker storage read must not stall the loop while SQLite blocks."""
    gate = threading.Event()
    read_started = threading.Event()

    def gated_get_agent_session(_storage: object, _session_id: str) -> None:
        read_started.set()
        gate.wait()

    monkeypatch.setattr(execution_preparation_module, "get_agent_session", gated_get_agent_session)
    scope_context = ScopeSessionContext(
        scope=HistoryScope(kind="agent", scope_id="general"),
        storage=MagicMock(),
        session=None,
        session_id="session-1",
    )

    lookup_task = asyncio.get_running_loop().create_task(
        execution_preparation_module._resolve_current_location_context(
            location_item_text="at_home: true",
            scope_context=scope_context,
        ),
    )
    await asyncio.to_thread(read_started.wait, 5.0)

    # The storage read thread is parked on the gate; the loop must stay live.
    await _assert_loop_heartbeats_while_pending(lookup_task)

    gate.set()
    location_block, marker = await lookup_task
    assert marker == "📍 Home"
    assert '<item key="location"' in location_block


@pytest.mark.asyncio
async def test_response_event_persistence_runs_off_event_loop() -> None:
    """The post-delivery wrap loads and rewrites a whole session; SQLite must not stall the loop."""
    gate = threading.Event()
    persist_started = threading.Event()
    persisted: list[tuple[str, str, str | None]] = []

    def blocking_persist(run_id: str, event_id: str, body: str | None) -> None:
        persist_started.set()
        gate.wait()
        persisted.append((run_id, event_id, body))

    effects_task = asyncio.get_running_loop().create_task(
        apply_post_response_effects(
            FinalDeliveryOutcome(
                terminal_status="completed",
                event_id="$response",
                is_visible_response=True,
                final_visible_body="ok",
                body_is_run_output=True,
            ),
            ResponseOutcome(response_run_id="run-1", run_succeeded=True),
            PostResponseEffectsDeps(
                logger=get_logger("tests.persist_offload"),
                persist_response_event_id=blocking_persist,
            ),
        ),
    )
    await asyncio.to_thread(persist_started.wait, 5.0)

    # The persistence thread is parked on the gate; the loop must stay live.
    await _assert_loop_heartbeats_while_pending(effects_task)

    gate.set()
    await effects_task
    assert persisted == [("run-1", "$response", "ok")]
