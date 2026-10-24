"""Regression tests for issue #1260: dispatch-path filesystem work must not block the event loop."""

from __future__ import annotations

import asyncio
import threading
from contextvars import ContextVar
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

import mindroom.ai as ai_module
import mindroom.knowledge.utils as knowledge_utils
import mindroom.memory._file_backend as file_backend_module
from mindroom.agent_loop.build import AgentBuild, AgentSurface
from mindroom.agent_loop.prompt import PromptSpec
from mindroom.agent_loop.spec import AgentSpec
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.history.request_messages import TurnMessages
from mindroom.history.types import HistoryPolicy, PreparedHistoryState, ResolvedHistorySettings
from mindroom.hooks import render_transient_context
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest
from mindroom.memory import MemoryPromptParts
from mindroom.memory._file_backend import FileMemoryBackend
from mindroom.timing import DispatchPipelineTiming
from tests.conftest import bind_runtime_paths, make_turn_context, test_runtime_paths
from tests.fakes.scripted_model import ScriptedModel

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


async def _assert_loop_heartbeats_while_pending(task: asyncio.Task) -> None:
    """Tick the loop and assert the task is still parked off-loop."""
    heartbeats = 0
    while heartbeats < 50:
        await asyncio.sleep(0)
        heartbeats += 1
    assert not task.done()


@pytest.mark.asyncio
async def test_agent_knowledge_lookup_runs_off_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow published-index lookup must not stall other event-loop work."""
    gate = threading.Event()
    lookup_started = threading.Event()
    loop_thread_id = threading.get_ident()
    schedule_thread_ids: list[int] = []

    def gated_lookup(*_args: object, **_kwargs: object) -> knowledge_utils.PublishedIndexResolution:
        lookup_started.set()
        assert threading.get_ident() != loop_thread_id
        if not gate.wait(5.0):
            msg = "timed out waiting to release published-index lookup"
            raise TimeoutError(msg)
        return knowledge_utils.get_published_index("docs", config=config, runtime_paths=test_runtime_paths(tmp_path))

    def record_refresh_schedule(
        *_args: object,
        availability: knowledge_utils.KnowledgeAvailability,
        **_kwargs: object,
    ) -> knowledge_utils.KnowledgeAvailability:
        schedule_thread_ids.append(threading.get_ident())
        return availability

    monkeypatch.setattr(knowledge_utils, "_lookup_knowledge_for_base", gated_lookup)
    monkeypatch.setattr(knowledge_utils, "_schedule_refresh_for_availability", record_refresh_schedule)
    config = Config.model_validate(
        {
            "agents": {
                "general": {
                    "display_name": "General",
                    "role": "test",
                    "knowledge_bases": ["docs"],
                },
            },
            "knowledge_bases": {"docs": {"path": str(tmp_path / "docs")}},
        },
    )
    resolve_task = asyncio.create_task(
        knowledge_utils.resolve_agent_knowledge_access_async(
            "general",
            config,
            test_runtime_paths(tmp_path),
            refresh_scheduler=MagicMock(),
        ),
    )
    try:
        assert await asyncio.to_thread(lookup_started.wait, 5.0)
        await _assert_loop_heartbeats_while_pending(resolve_task)
    finally:
        gate.set()

    resolution = await resolve_task
    assert resolution.knowledge is None
    assert resolution.unavailable["docs"].availability is knowledge_utils.KnowledgeAvailability.INITIALIZING
    assert resolution.unavailable["docs"].search_available is False
    assert schedule_thread_ids == [loop_thread_id]


@pytest.mark.asyncio
async def test_agent_file_memory_resolution_runs_off_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """File-memory workspace resolution must not stall other event-loop work."""
    gate = threading.Event()
    resolution_started = threading.Event()
    loop_thread_id = threading.get_ident()

    def gated_file_memory_resolution(*_args: object, **_kwargs: object) -> None:
        resolution_started.set()
        assert threading.get_ident() != loop_thread_id
        if not gate.wait(5.0):
            msg = "timed out waiting to release file-memory resolution"
            raise TimeoutError(msg)

    monkeypatch.setattr(
        knowledge_utils,
        "resolve_agent_file_memory_knowledge",
        gated_file_memory_resolution,
    )
    config = Config.model_validate(
        {
            "agents": {"general": {"display_name": "General", "role": "test", "memory_backend": "file"}},
            "memory": {"search": {"mode": "semantic"}},
        },
    )
    resolve_task = asyncio.create_task(
        knowledge_utils.resolve_agent_knowledge_access_async(
            "general",
            config,
            test_runtime_paths(tmp_path),
        ),
    )
    try:
        assert await asyncio.to_thread(resolution_started.wait, 5.0)
        await _assert_loop_heartbeats_while_pending(resolve_task)
    finally:
        gate.set()

    resolution = await resolve_task
    assert resolution.knowledge is None
    assert resolution.unavailable == {}


@pytest.mark.asyncio
async def test_knowledge_base_lookup_runs_off_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A direct published-index lookup must not stall other event-loop work."""
    gate = threading.Event()
    lookup_started = threading.Event()
    loop_thread_id = threading.get_ident()

    def gated_lookup(*_args: object, **_kwargs: object) -> knowledge_utils.PublishedIndexResolution:
        lookup_started.set()
        assert threading.get_ident() != loop_thread_id
        if not gate.wait(5.0):
            msg = "timed out waiting to release direct published-index lookup"
            raise TimeoutError(msg)
        return knowledge_utils.get_published_index("docs", config=config, runtime_paths=test_runtime_paths(tmp_path))

    monkeypatch.setattr(knowledge_utils, "_lookup_knowledge_for_base", gated_lookup)
    config = Config.model_validate(
        {
            "knowledge_bases": {"docs": {"path": str(tmp_path / "docs")}},
        },
    )
    resolve_task = asyncio.create_task(
        knowledge_utils.resolve_knowledge_base_access_async(
            "docs",
            config,
            test_runtime_paths(tmp_path),
        ),
    )
    try:
        assert await asyncio.to_thread(lookup_started.wait, 5.0)
        await _assert_loop_heartbeats_while_pending(resolve_task)
    finally:
        gate.set()

    resolution = await resolve_task
    assert resolution.knowledge is None
    assert resolution.availability is knowledge_utils.KnowledgeAvailability.INITIALIZING


def _prompt_preparation_config(memory_backend: str = "mem0") -> Config:
    return Config.model_validate(
        {"agents": {"general": {"display_name": "General", "role": "test", "memory_backend": memory_backend}}},
    )


def _build(turn_context: str | None = None) -> AgentBuild:
    """Return a minimal agent build whose prompt carries ``turn_context`` as the build's own context."""
    return AgentBuild(
        spec=AgentSpec(id="general", name="General", model=ScriptedModel([]), system_message=None),
        prompt=PromptSpec(role="test", turn_context=turn_context),
        request_template=ModelRequest(messages=()),
        history_settings=ResolvedHistorySettings(policy=HistoryPolicy(mode="all"), max_tool_calls_from_history=None),
        store=None,
        tool_owners={},
        surface=AgentSurface(mode="standard", cli_shell=False),
    )


def _prepared_execution(
    prompt: str,
    transient: Sequence[Message] = (),
    *,
    unseen_event_ids: Sequence[str] = (),
    prepared_context_tokens: int | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        prepared_history=PreparedHistoryState(prepared_context_tokens=prepared_context_tokens),
        replay_plan=None,
        unseen_event_ids=list(unseen_event_ids),
        turn=TurnMessages(prompt=Message(role="user", content=prompt), transient=tuple(transient)),
    )


@pytest.mark.asyncio
async def test_prepare_agent_and_prompt_builds_agent_off_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow agent build (workspace and context-file I/O) must not stall the loop."""
    gate = threading.Event()
    build_started = threading.Event()
    build = _build()

    def gated_create_agent(*_args: object, **_kwargs: object) -> AgentBuild:
        build_started.set()
        gate.wait()
        return build

    monkeypatch.setattr(ai_module, "create_agent", gated_create_agent)
    monkeypatch.setattr(
        ai_module,
        "build_memory_prompt_parts",
        AsyncMock(return_value=MemoryPromptParts()),
    )
    monkeypatch.setattr(
        ai_module,
        "prepare_agent_execution_context",
        AsyncMock(return_value=_prepared_execution("hello")),
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
    assert prepared_run.build is build


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
    history_started = asyncio.Event()
    context_marker = ContextVar("prompt_preparation_context", default="missing")
    context_token = context_marker.set("preserved")
    build = _build("existing context")

    async def gated_memory(*_args: object, **_kwargs: object) -> MemoryPromptParts:
        memory_started.set()
        if not agent_started.wait(1.0):
            msg = "agent construction was not submitted before synchronous memory preparation"
            raise TimeoutError(msg)
        await memory_release.wait()
        memory_finished.set()
        return MemoryPromptParts(session_preamble="session memory", transient_turn_context="turn memory")

    def gated_create_agent(*_args: object, **_kwargs: object) -> AgentBuild:
        assert context_marker.get() == "preserved"
        agent_started.set()
        if not agent_release.wait(5.0):
            msg = "timed out waiting to release agent construction"
            raise TimeoutError(msg)
        agent_finished.set()
        return build

    async def prepare_history(*_args: object, **kwargs: object) -> SimpleNamespace:
        history_started.set()
        assert memory_finished.is_set()
        assert agent_finished.is_set()
        assert kwargs["build"] is build
        assert kwargs["prompt"] == "raw prompt\n\nmodel metadata"
        assert str(kwargs["system_message"].content).endswith("existing context\n\nsession memory")
        assert len(kwargs["transient_context_messages"]) == 1
        assert kwargs["transient_context_messages"][0].content == render_transient_context(("turn memory",))
        assert kwargs["transient_context_messages"][0].add_to_agent_memory is False
        assert kwargs["resolved_runtime_model"].model_name == "default"
        return _prepared_execution(kwargs["prompt"], kwargs["transient_context_messages"])

    monkeypatch.setattr(ai_module, "build_memory_prompt_parts", gated_memory)
    monkeypatch.setattr(ai_module, "create_agent", gated_create_agent)
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
        assert not history_started.is_set()

        memory_release.set()
        agent_release.set()
        prepared_run = await prepare_task
    finally:
        context_marker.reset(context_token)
        memory_release.set()
        agent_release.set()

    assert prepared_run.build is build
    assert prepared_run.spec.system_message is not None
    assert str(prepared_run.spec.system_message.content).endswith("existing context\n\nsession memory")
    assert prepared_run.prompt_text.startswith("raw prompt\n\nmodel metadata")
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
    build = _build()

    async def prepare_memory(*_args: object, **_kwargs: object) -> MemoryPromptParts:
        nonlocal memory_finished
        await asyncio.sleep(0)
        memory_finished = True
        return MemoryPromptParts()

    def build_agent(*_args: object, **_kwargs: object) -> AgentBuild:
        assert memory_finished
        return build

    monkeypatch.setattr(ai_module, "build_memory_prompt_parts", prepare_memory)
    monkeypatch.setattr(ai_module, "create_agent", build_agent)
    prepare_history = AsyncMock(return_value=_prepared_execution("hello"))
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

    assert prepared.build is build
    assert prepare_history.await_args.kwargs["resolved_runtime_model"].model_name == "default"
    assert pipeline_timing.metadata["prompt_branches_parallel"] is False
    assert pipeline_timing.metadata["prompt_branches_serial_reason"] == "default_workspace_scaffold_pending"


@pytest.mark.asyncio
async def test_serial_agent_build_cancellation_drains_the_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation must wait for a serial build before it propagates."""
    build_started = threading.Event()
    release_build = threading.Event()

    def blocked_create_agent(*_args: object, **_kwargs: object) -> AgentBuild:
        build_started.set()
        assert release_build.wait(timeout=5)
        return _build()

    monkeypatch.setattr(ai_module, "build_memory_prompt_parts", AsyncMock(return_value=MemoryPromptParts()))
    monkeypatch.setattr(ai_module, "create_agent", blocked_create_agent)

    task = asyncio.create_task(
        ai_module._prepare_agent_and_prompt(
            make_turn_context("general"),
            prompt="hello",
            runtime_paths=test_runtime_paths(tmp_path),
            config=_prompt_preparation_config("file"),
        ),
    )
    try:
        assert await asyncio.to_thread(build_started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()

        release_build.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release_build.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_prepare_agent_and_prompt_file_memory_preserves_reusable_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """File-memory overlap reuses the caller-owned build without rebuilding it, and renders its context per turn."""
    reusable_build = _build("caller context")
    create_agent_mock = MagicMock(side_effect=AssertionError("caller-owned build must not be rebuilt"))

    async def prepare_history(*_args: object, **kwargs: object) -> SimpleNamespace:
        assert kwargs["build"] is reusable_build
        assert kwargs["resolved_runtime_model"].model_name == "default"
        transient_messages = kwargs["transient_context_messages"]
        assert [message.content for message in transient_messages] == [render_transient_context(("turn memory",))]
        return _prepared_execution(
            kwargs["prompt"],
            transient_messages,
            unseen_event_ids=["$memory"],
            prepared_context_tokens=42,
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
        reusable_build=reusable_build,
    )

    assert prepared.build is reusable_build
    assert prepared.spec.system_message is not None
    assert str(prepared.spec.system_message.content).endswith("caller context\n\nsession memory")
    # The build's own prompt is never changed by a turn.
    assert reusable_build.prompt.turn_context == "caller context"
    assert prepared.unseen_event_ids == ["$memory"]
    assert prepared.prepared_history.prepared_context_tokens == 42
    create_agent_mock.assert_not_called()


@pytest.mark.asyncio
async def test_parallel_file_prompt_matches_sequential_output_and_bookkeeping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parallel file preparation preserves sequential prompt and history results."""
    history_inputs: list[tuple[str, tuple[object, ...], str]] = []

    async def prepare_memory(*_args: object, **_kwargs: object) -> MemoryPromptParts:
        return MemoryPromptParts(
            session_preamble="session memory",
            transient_turn_context="retrieved memory",
        )

    async def prepare_history(*_args: object, **kwargs: object) -> SimpleNamespace:
        transient_messages = tuple(kwargs["transient_context_messages"])
        history_inputs.append(
            (
                kwargs["prompt"],
                tuple(message.content for message in transient_messages),
                kwargs["resolved_runtime_model"].model_name,
            ),
        )
        return _prepared_execution(
            kwargs["prompt"],
            transient_messages,
            unseen_event_ids=["$one", "$two"],
            prepared_context_tokens=73,
        )

    monkeypatch.setattr(ai_module, "build_memory_prompt_parts", prepare_memory)
    monkeypatch.setattr(ai_module, "create_agent", lambda *_args, **_kwargs: _build("base context"))
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

    def shape(messages: Sequence[Message]) -> list[tuple[str, object, bool]]:
        return [(message.role, message.content, message.add_to_agent_memory) for message in messages]

    assert shape(parallel.turn.in_request_order()) == shape(sequential.turn.in_request_order())
    assert parallel.prompt_text == sequential.prompt_text
    assert parallel.unseen_event_ids == sequential.unseen_event_ids == ["$one", "$two"]
    assert parallel.prepared_history == sequential.prepared_history
    assert parallel.runtime_model_name == sequential.runtime_model_name
    assert parallel.spec.system_message is not None
    assert sequential.spec.system_message is not None
    assert parallel.spec.system_message.content == sequential.spec.system_message.content
    assert str(parallel.spec.system_message.content).endswith("base context\n\nsession memory")
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
