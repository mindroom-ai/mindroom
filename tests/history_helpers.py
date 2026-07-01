"""Shared helpers for the Agno history test modules."""
# ruff: noqa: D102, ANN201, TC002, TC003

from __future__ import annotations

import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agno.agent import Agent
from agno.models.base import Model
from agno.models.message import Message
from agno.models.response import ModelResponse
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.summary import SessionSummary
from agno.session.team import TeamSession

from mindroom.agent_storage import create_session_storage
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import CompactionConfig, CompactionOverrideConfig, DefaultsConfig, ModelConfig
from mindroom.config.plugin import PluginEntryConfig
from mindroom.constants import (
    RuntimePaths,
    resolve_runtime_paths,
)
from mindroom.history.storage import (
    write_scope_state,
)
from mindroom.history.types import (
    CompactionLifecycleFailure,
    CompactionLifecycleProgress,
    CompactionLifecycleStart,
    CompactionOutcome,
    HistoryScope,
    HistoryScopeState,
)
from mindroom.hooks import (
    HookRegistry,
)
from mindroom.tool_system.runtime_context import ToolRuntimeContext
from tests.conftest import (
    FakeModel,
    bind_runtime_paths,
    make_conversation_cache_mock,
    make_event_cache_mock,
)

_DEFAULT_TEST_COMPACTION = CompactionConfig()


@dataclass
class RecordingModel(Model):
    """Model that records the final prompt message list."""

    seen_messages: list[Message] = field(default_factory=list)

    def invoke(self, *_args: object, **kwargs: object) -> ModelResponse:
        messages = kwargs.get("messages")
        if isinstance(messages, list):
            self.seen_messages = list(messages)
        return ModelResponse(content="ok")

    async def ainvoke(self, *_args: object, **kwargs: object) -> ModelResponse:
        messages = kwargs.get("messages")
        if isinstance(messages, list):
            self.seen_messages = list(messages)
        return ModelResponse(content="ok")

    def invoke_stream(self, *_args: object, **_kwargs: object):
        yield ModelResponse(content="ok")

    async def ainvoke_stream(self, *_args: object, **_kwargs: object):
        yield ModelResponse(content="ok")

    def _parse_provider_response(self, response: ModelResponse, *_args: object, **_kwargs: object) -> ModelResponse:
        return response

    def _parse_provider_response_delta(
        self,
        response: ModelResponse,
        *_args: object,
        **_kwargs: object,
    ) -> ModelResponse:
        return response


@dataclass
class RecordingCompactionLifecycle:
    """Lifecycle test double that records foreground compaction notice ordering."""

    events: list[object] = field(default_factory=list)
    start_event_id: str | None = "$compaction"

    async def start(self, event: CompactionLifecycleStart) -> str | None:
        self.events.append(event)
        return self.start_event_id

    async def complete_success(self, outcome: CompactionOutcome) -> None:
        self.events.append(outcome)

    async def progress(self, event: CompactionLifecycleProgress) -> None:
        self.events.append(event)

    async def complete_failure(self, event: CompactionLifecycleFailure) -> None:
        self.events.append(event)


@dataclass
class FailingStartCompactionLifecycle(RecordingCompactionLifecycle):
    """Lifecycle test double whose initial notice delivery fails."""

    async def start(self, event: CompactionLifecycleStart) -> str | None:
        self.events.append(event)
        message = "matrix unavailable"
        raise RuntimeError(message)


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
        },
    )


def _make_config(
    tmp_path: Path,
    *,
    num_history_runs: int | None = None,
    num_history_messages: int | None = None,
    compaction: CompactionOverrideConfig | None = None,
    defaults_compaction: CompactionConfig | None = _DEFAULT_TEST_COMPACTION,
    context_window: int | None = 48_000,
    models: dict[str, ModelConfig] | None = None,
) -> tuple[Config, RuntimePaths]:
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "test_agent": AgentConfig(
                    display_name="Test Agent",
                    num_history_runs=num_history_runs,
                    num_history_messages=num_history_messages,
                    compaction=compaction,
                ),
            },
            defaults=DefaultsConfig(tools=[], compaction=defaults_compaction),
            models=(
                models
                if models is not None
                else {
                    "default": ModelConfig(
                        provider="openai",
                        id="test-model",
                        context_window=context_window,
                    ),
                }
            ),
        ),
        runtime_paths,
    )
    return config, runtime_paths


def _completed_run(
    run_id: str,
    *,
    agent_id: str = "test_agent",
    messages: list[Message] | None = None,
) -> RunOutput:
    return RunOutput(
        run_id=run_id,
        agent_id=agent_id,
        status=RunStatus.completed,
        messages=messages
        or [
            Message(role="user", content=f"{run_id} question"),
            Message(role="assistant", content=f"{run_id} answer"),
        ],
    )


def _completed_team_run(
    run_id: str,
    *,
    team_id: str,
    messages: list[Message] | None = None,
) -> TeamRunOutput:
    return TeamRunOutput(
        run_id=run_id,
        team_id=team_id,
        status=RunStatus.completed,
        messages=messages
        or [
            Message(role="user", content=f"{run_id} team question"),
            Message(role="assistant", content=f"{run_id} team answer"),
        ],
    )


def _session(
    session_id: str,
    *,
    agent_id: str = "test_agent",
    runs: list[RunOutput | TeamRunOutput] | None = None,
    metadata: dict[str, object] | None = None,
    summary: SessionSummary | None = None,
) -> AgentSession:
    return AgentSession(
        session_id=session_id,
        agent_id=agent_id,
        runs=runs or [],
        metadata=metadata,
        summary=summary,
        created_at=1,
        updated_at=1,
    )


@pytest.fixture(autouse=True)
def _close_test_storages(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Close temporary SQLite handles created directly by Agno history tests."""
    storages: list[object] = []
    original_create_session_storage = create_session_storage

    def _tracked_create_session_storage(*args: object, **kwargs: object) -> object:
        storage = original_create_session_storage(*args, **kwargs)
        storages.append(storage)
        return storage

    for module in {sys.modules[__name__], request.module}:
        monkeypatch.setattr(module, "create_session_storage", _tracked_create_session_storage, raising=False)
    yield

    seen_storage_ids: set[int] = set()
    for storage in storages:
        storage_id = id(storage)
        if storage_id in seen_storage_ids:
            continue
        seen_storage_ids.add(storage_id)
        storage.close()


def _team_session(
    session_id: str,
    *,
    team_id: str,
    runs: list[RunOutput | TeamRunOutput] | None = None,
    metadata: dict[str, object] | None = None,
    summary: SessionSummary | None = None,
) -> TeamSession:
    return TeamSession(
        session_id=session_id,
        team_id=team_id,
        runs=runs or [],
        metadata=metadata,
        summary=summary,
        created_at=1,
        updated_at=1,
    )


def _agent(
    *,
    agent_id: str = "test_agent",
    name: str = "Test Agent",
    model: Model | None = None,
    db: object | None = None,
    num_history_runs: int | None = None,
    num_history_messages: int | None = None,
) -> Agent:
    return Agent(
        id=agent_id,
        name=name,
        model=model or FakeModel(id="fake-model", provider="fake"),
        db=db,
        add_history_to_context=True,
        num_history_runs=num_history_runs,
        num_history_messages=num_history_messages,
        store_history_messages=False,
    )


def _hook_runtime_context(
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    registry: HookRegistry,
    session_id: str,
    thread_id: str | None = "$thread",
) -> ToolRuntimeContext:
    return ToolRuntimeContext(
        agent_name="test_agent",
        room_id="!room:localhost",
        thread_id=thread_id,
        resolved_thread_id=thread_id,
        requester_id="@user:localhost",
        client=AsyncMock(),
        config=config,
        runtime_paths=runtime_paths,
        event_cache=make_event_cache_mock(),
        conversation_cache=make_conversation_cache_mock(),
        session_id=session_id,
        hook_registry=registry,
        correlation_id="corr-compaction",
    )


def _plugin(name: str, callbacks: list[object]) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        discovered_hooks=tuple(callbacks),
        entry_config=PluginEntryConfig(path=f"./plugins/{name}"),
        plugin_order=0,
    )


def _forced_compaction_context(
    tmp_path: Path,
    *,
    session: AgentSession,
    registry: HookRegistry | None = None,
    context_window: int = 64_000,
) -> tuple[Config, RuntimePaths, object, HistoryScope, ToolRuntimeContext]:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=context_window,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    write_scope_state(session, scope, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(session)
    runtime_context = _hook_runtime_context(
        config=config,
        runtime_paths=runtime_paths,
        registry=registry or HookRegistry.empty(),
        session_id=session.session_id,
    )
    return config, runtime_paths, storage, scope, runtime_context
