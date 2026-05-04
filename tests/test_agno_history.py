"""Tests for native Agno history replay and destructive compaction."""
# ruff: noqa: D102, D103, ANN201, TC003

from __future__ import annotations

import ast
import asyncio
import inspect
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agno.agent import Agent
from agno.media import Image
from agno.models.anthropic.claude import Claude as AnthropicClaude
from agno.models.base import Model
from agno.models.message import Message
from agno.models.response import ModelResponse
from agno.models.vertexai.claude import Claude as VertexClaude
from agno.run import RunContext
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.summary import SessionSummary
from agno.session.team import TeamSession
from agno.team import Team
from agno.team._tools import _determine_tools_for_model as determine_team_tools_for_model
from agno.tools import Toolkit
from agno.tools.function import Function
from defusedxml.ElementTree import fromstring

from mindroom.agent_storage import create_session_storage, get_agent_session
from mindroom.agents import create_agent
from mindroom.ai import _prepare_agent_and_prompt
from mindroom.background_tasks import _get_background_task_count, wait_for_background_tasks
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import CompactionConfig, CompactionOverrideConfig, DefaultsConfig, ModelConfig
from mindroom.config.plugin import PluginEntryConfig
from mindroom.constants import (
    MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS,
    MINDROOM_COMPACTION_METADATA_KEY,
    RuntimePaths,
    resolve_runtime_paths,
)
from mindroom.execution_preparation import (
    PreparedExecutionContext,
    prepare_agent_execution_context,
    prepare_bound_team_execution_context,
    prepare_bound_team_run_context,
)
from mindroom.history import PreparedHistoryState, prepare_history_for_run
from mindroom.history.compaction import (
    _compaction_summary_request_model,
    _emit_compaction_hook,
    _generate_compaction_summary,
    _generate_compaction_summary_with_retry,
    _persist_compaction_progress,
    _rewrite_working_session_for_compaction,
    compact_scope_history,
    effective_summary_input_budget_tokens,
    estimate_prompt_visible_history_tokens,
    estimate_session_summary_tokens,
)
from mindroom.history.compaction_provider_request import (
    CompactionProviderRequest,
    build_agent_compaction_provider_request,
    estimate_agent_static_tokens,
    estimate_tool_definition_tokens,
)
from mindroom.history.policy import classify_compaction_decision, resolve_history_execution_plan
from mindroom.history.runtime import (
    apply_replay_plan,
    estimate_preparation_static_tokens_for_team,
    finalize_history_preparation,
    open_bound_scope_session_context,
    open_scope_session_context,
    plan_replay_that_fits,
    prepare_bound_scope_history,
    prepare_scope_history,
)
from mindroom.history.storage import (
    read_scope_seen_event_ids,
    read_scope_state,
    update_scope_seen_event_ids,
    write_scope_state,
)
from mindroom.history.types import (
    CompactionLifecycleFailure,
    CompactionLifecycleProgress,
    CompactionLifecycleStart,
    CompactionLifecycleSuccess,
    CompactionOutcome,
    HistoryPolicy,
    HistoryScope,
    HistoryScopeState,
    ResolvedHistoryExecutionPlan,
    ResolvedHistorySettings,
    ResolvedReplayPlan,
)
from mindroom.hooks import (
    BUILTIN_EVENT_NAMES,
    EVENT_COMPACTION_AFTER,
    EVENT_COMPACTION_BEFORE,
    CompactionHookContext,
    HookRegistry,
    build_hook_matrix_admin,
    hook,
)
from mindroom.hooks.types import RESERVED_EVENT_NAMESPACES, default_timeout_ms_for_event, validate_event_name
from mindroom.memory import MemoryPromptParts
from mindroom.prepared_conversation_chain import (
    CompactionSummaryRequest,
    PreparedConversationChain,
    build_compaction_summary_request,
    build_matrix_prompt_with_thread_history,
    build_persisted_run_chain,
    build_warm_cache_compaction_summary_request,
    estimate_history_messages_tokens,
    strip_stale_anthropic_replay_fields,
)
from mindroom.teams import TeamMode, _create_team_instance
from mindroom.thread_utils import create_session_id
from mindroom.token_budget import estimate_text_tokens, stable_serialize
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from mindroom.vertex_claude_prompt_cache import install_vertex_claude_prompt_cache_hook
from tests.conftest import bind_runtime_paths, make_conversation_cache_mock, make_event_cache_mock, make_visible_message

_DEFAULT_TEST_COMPACTION = CompactionConfig()


def test_agno_forked_request_module_has_no_mindroom_imports() -> None:
    module_path = Path("src/mindroom/history/agno_forked_request.py")
    tree = ast.parse(module_path.read_text())
    imports = [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
    from_imports = [
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module is not None
    ]
    assert not any(name == "mindroom" or name.startswith("mindroom.") for name in imports + from_imports)


def _build_test_summary_request(
    *,
    previous_summary: str | None,
    compacted_runs: Sequence[RunOutput | TeamRunOutput],
    max_input_tokens: int,
    history_settings: ResolvedHistorySettings | None = None,
) -> tuple[CompactionSummaryRequest | None, list[RunOutput | TeamRunOutput]]:
    return build_compaction_summary_request(
        previous_summary=previous_summary,
        compacted_runs=compacted_runs,
        history_settings=history_settings
        or ResolvedHistorySettings(policy=HistoryPolicy(mode="all"), max_tool_calls_from_history=None),
        max_input_tokens=max_input_tokens,
    )


def _included_summary_run_count(
    previous_summary: str | None,
    compacted_runs: Sequence[RunOutput | TeamRunOutput],
    budget: int,
    *,
    history_settings: ResolvedHistorySettings | None = None,
) -> int:
    return len(
        _build_test_summary_request(
            previous_summary=previous_summary,
            compacted_runs=compacted_runs,
            history_settings=history_settings,
            max_input_tokens=budget,
        )[1],
    )


def _summary_messages(content: str = "Current prompt") -> list[Message]:
    return [Message(role="user", content=content)]


def test_prepare_scope_history_boundary_does_not_accept_execution_identity() -> None:
    assert "execution_identity" not in inspect.signature(prepare_agent_execution_context).parameters
    assert "execution_identity" not in inspect.signature(prepare_bound_team_execution_context).parameters
    assert "execution_identity" not in inspect.signature(prepare_bound_team_run_context).parameters
    assert "execution_identity" not in inspect.signature(prepare_bound_scope_history).parameters
    assert "execution_identity" not in inspect.signature(prepare_scope_history).parameters


@dataclass
class FakeModel(Model):
    """Minimal model for deterministic agent creation tests."""

    def invoke(self, *_args: object, **_kwargs: object) -> ModelResponse:
        return ModelResponse(content="ok")

    async def ainvoke(self, *_args: object, **_kwargs: object) -> ModelResponse:
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
class RecordingModel(Model):
    """Model that records the final prompt message list."""

    seen_messages: list[Message] = field(default_factory=list)
    seen_tools: list[Any] | None = None
    seen_tool_choice: Any | None = None

    def invoke(self, *_args: object, **kwargs: object) -> ModelResponse:
        messages = kwargs.get("messages")
        if isinstance(messages, list):
            self.seen_messages = list(messages)
        tools = kwargs.get("tools")
        self.seen_tools = list(tools) if isinstance(tools, list) else None
        self.seen_tool_choice = kwargs.get("tool_choice")
        return ModelResponse(content="ok")

    async def ainvoke(self, *_args: object, **kwargs: object) -> ModelResponse:
        messages = kwargs.get("messages")
        if isinstance(messages, list):
            self.seen_messages = list(messages)
        tools = kwargs.get("tools")
        self.seen_tools = list(tools) if isinstance(tools, list) else None
        self.seen_tool_choice = kwargs.get("tool_choice")
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

    async def complete_success(self, event: CompactionLifecycleSuccess) -> None:
        self.events.append(event)

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
def _close_test_storages(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Close temporary SQLite handles created directly by Agno history tests."""
    storages: list[object] = []
    module = sys.modules[__name__]
    original_create_session_storage = create_session_storage

    def _tracked_create_session_storage(*args: object, **kwargs: object) -> object:
        storage = original_create_session_storage(*args, **kwargs)
        storages.append(storage)
        return storage

    monkeypatch.setattr(module, "create_session_storage", _tracked_create_session_storage)
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


def test_estimate_agent_static_tokens_includes_tool_definitions() -> None:
    def search_docs(query: str, limit: int = 5) -> str:
        """Search the engineering docs for a matching answer."""
        return f"{query}:{limit}"

    def export_notes(title: str, include_metadata: bool = False) -> str:
        """Export the current working notes as markdown with full metadata attached."""
        return f"{title}:{include_metadata}"

    toolkit = Toolkit(
        name="docs",
        tools=[search_docs],
        instructions="Always cite the relevant document section when using search_docs.",
        add_instructions=True,
    )
    export_tool = Function(
        name="export_notes",
        entrypoint=export_notes,
    )
    agent_with_tools = _agent()
    agent_with_tools.role = "Engineer"
    agent_with_tools.instructions = ["Stay concise."]
    agent_with_tools.tools = [toolkit, export_tool]

    baseline_agent = _agent()
    baseline_agent.role = agent_with_tools.role
    baseline_agent.instructions = list(agent_with_tools.instructions)

    expected_export_tool = export_tool.model_copy(deep=True)
    expected_export_tool.process_entrypoint(strict=False)
    expected_payloads = [
        {
            "name": "search_docs",
            "description": "Search the engineering docs for a matching answer.",
            "parameters": Function.from_callable(search_docs).parameters,
        },
        {
            "name": "export_notes",
            "description": "Export the current working notes as markdown with full metadata attached.",
            "parameters": expected_export_tool.parameters,
        },
    ]
    tool_tokens = estimate_tool_definition_tokens(agent_with_tools)
    assert tool_tokens == (
        len(stable_serialize(expected_payloads)) // 4
        + estimate_text_tokens("Always cite the relevant document section when using search_docs.")
    )
    assert estimate_tool_definition_tokens(baseline_agent) == 0
    assert estimate_agent_static_tokens(agent_with_tools, "Current prompt") > estimate_agent_static_tokens(
        baseline_agent,
        "Current prompt",
    )
    assert estimate_agent_static_tokens(agent_with_tools, "Current prompt") >= (
        estimate_agent_static_tokens(baseline_agent, "Current prompt") + tool_tokens
    )


def test_estimate_agent_static_tokens_uses_real_system_message_builder() -> None:
    @dataclass
    class PromptAwareModel(FakeModel):
        def get_instructions_for_model(self, tools: list[Any] | None = None) -> list[str] | None:
            _ = tools
            return ["Follow provider guidance."]

        def get_system_message_for_model(self, tools: list[Any] | None = None) -> str | None:
            _ = tools
            return "Provider system message."

    agent = _agent(model=PromptAwareModel(id="fake-model", provider="fake"))
    agent.role = "Engineer"
    agent.instructions = ["Stay concise."]
    agent.markdown = True

    session = AgentSession(
        session_id="history-budget",
        agent_id=agent.id,
        user_id="history-budget-user",
    )
    run_context = RunContext(
        run_id="history-budget",
        session_id="history-budget",
        user_id="history-budget-user",
        session_state={},
    )
    system_message = agent.get_system_message(
        session=session,
        run_context=run_context,
        tools=None,
        add_session_state_to_context=False,
    )
    assert system_message is not None
    assert system_message.content is not None

    expected_tokens = estimate_text_tokens("Current prompt") + estimate_text_tokens(str(system_message.content))
    assert estimate_agent_static_tokens(agent, "Current prompt") == expected_tokens
    assert estimate_agent_static_tokens(agent, "Current prompt") > estimate_text_tokens("Current prompt")


def test_estimate_tool_definition_tokens_processes_functions_with_custom_parameters() -> None:
    def sync_calendar_event(title: str, include_attendees: bool = False) -> str:
        """Sync the current event draft into the shared calendar."""
        return f"{title}:{include_attendees}"

    custom_tool = Function(
        name="sync_calendar_event",
        entrypoint=sync_calendar_event,
        parameters={
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Calendar event title.",
                },
            },
            "required": ["title"],
        },
    )
    agent = _agent()
    agent.tools = [custom_tool]

    expected_tool = custom_tool.model_copy(deep=True)
    expected_tool.process_entrypoint(strict=False)

    assert expected_tool.description == "Sync the current event draft into the shared calendar."
    assert expected_tool.parameters["additionalProperties"] is False
    assert (
        estimate_tool_definition_tokens(agent)
        == len(
            stable_serialize(
                [
                    {
                        "name": "sync_calendar_event",
                        "description": expected_tool.description,
                        "parameters": expected_tool.parameters,
                    },
                ],
            ),
        )
        // 4
    )


def test_estimate_tool_definition_tokens_ignores_empty_toolkit() -> None:
    agent = _agent()
    agent.tools = [Toolkit(name="empty")]

    assert estimate_tool_definition_tokens(agent) == 0


def test_create_agent_enables_agno_native_history_replay(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path, num_history_runs=2)

    with patch("mindroom.model_loading.get_model_instance", return_value=FakeModel(id="fake-model", provider="fake")):
        agent = create_agent(
            "test_agent",
            config,
            runtime_paths,
            execution_identity=None,
            include_interactive_questions=False,
        )

    assert agent.add_history_to_context is True
    assert agent.num_history_runs == 2
    assert agent.num_history_messages is None
    assert agent.store_history_messages is False


def test_create_agent_uses_active_model_override(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"test_agent": AgentConfig(display_name="Test Agent", model="default")},
            defaults=DefaultsConfig(tools=[]),
            models={
                "default": ModelConfig(provider="openai", id="default-model"),
                "large": ModelConfig(provider="openai", id="large-model"),
            },
        ),
        runtime_paths,
    )
    with patch(
        "mindroom.model_loading.get_model_instance",
        return_value=FakeModel(id="fake-model", provider="fake"),
    ) as mock_get:
        create_agent(
            "test_agent",
            config,
            runtime_paths,
            execution_identity=None,
            active_model_name="large",
            include_interactive_questions=False,
        )

    assert mock_get.call_args is not None
    assert mock_get.call_args.args[2] == "large"


@pytest.mark.asyncio
async def test_prepare_history_for_run_detects_persisted_team_history(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path)
    agent = _agent()
    agent.team_id = "team-123"
    with open_scope_session_context(
        agent=agent,
        agent_name="test_agent",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        create_session_if_missing=True,
    ) as scope_context:
        assert scope_context is not None
        assert scope_context.scope == HistoryScope(kind="team", scope_id="team-123")
        session = _team_session(
            "session-1",
            team_id="team-123",
            runs=[_completed_team_run("team-1", team_id="team-123")],
            summary=SessionSummary(summary="team summary", updated_at=datetime.now(UTC)),
        )
        scope_context.storage.upsert_session(session)

    prepared = await prepare_history_for_run(
        agent=agent,
        agent_name="test_agent",
        full_prompt="Current prompt",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
    )

    assert prepared.replays_persisted_history is True
    assert prepared.compaction_outcomes == []


@pytest.mark.asyncio
async def test_prepare_history_for_run_forced_compaction_rewrites_session(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True, model="summary"),
        context_window=64_000,
        models={
            "default": ModelConfig(provider="openai", id="test-model", context_window=64_000),
            "summary": ModelConfig(provider="openai", id="summary-model", context_window=64_000),
        },
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1"),
            _completed_run("run-2"),
            _completed_run("run-3"),
            _completed_run("run-4"),
        ],
    )
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    write_scope_state(session, scope, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(session)

    agent = _agent(db=storage)
    with (
        patch(
            "mindroom.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch(
            "mindroom.history.compaction._generate_compaction_summary",
            new=AsyncMock(
                return_value=SessionSummary(
                    summary="merged summary",
                    updated_at=datetime.now(UTC),
                ),
            ),
        ),
    ):
        prepared = await prepare_history_for_run(
            agent=agent,
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
        )

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.summary == "merged summary"
    assert persisted.runs == []

    state = read_scope_state(persisted, scope)
    assert state.last_summary_model == "summary-model"
    assert state.last_compacted_run_count == 4
    assert state.force_compact_before_next_run is False
    assert state.last_compacted_at is not None

    assert prepared.replays_persisted_history is True
    assert len(prepared.compaction_outcomes) == 1
    assert prepared.compaction_outcomes[0].summary == "merged summary"


@pytest.mark.asyncio
async def test_prepare_history_for_run_required_compaction_starts_lifecycle_before_summary_request(
    tmp_path: Path,
) -> None:
    """Foreground compaction should make the visible lifecycle notice before the summary call blocks."""
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True, model="summary"),
        context_window=64_000,
        models={
            "default": ModelConfig(provider="openai", id="test-model", context_window=64_000),
            "summary": ModelConfig(provider="openai", id="summary-model", context_window=64_000),
        },
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1"),
            _completed_run("run-2"),
        ],
    )
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    write_scope_state(session, scope, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(session)
    lifecycle = RecordingCompactionLifecycle()

    async def _summary_after_notice(*_args: object, **_kwargs: object) -> SessionSummary:
        assert isinstance(lifecycle.events[0], CompactionLifecycleStart)
        return SessionSummary(summary="merged summary", updated_at=datetime.now(UTC))

    with (
        patch(
            "mindroom.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch(
            "mindroom.history.compaction._generate_compaction_summary",
            new=AsyncMock(side_effect=_summary_after_notice),
        ),
    ):
        prepared = await prepare_history_for_run(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
            compaction_lifecycle=lifecycle,
        )

    assert len(prepared.compaction_outcomes) == 1
    assert prepared.compaction_outcomes[0].lifecycle_notice_event_id == "$compaction"
    assert prepared.compaction_decision.mode == "required"
    assert prepared.compaction_reply_outcome == "success"
    assert isinstance(lifecycle.events[0], CompactionLifecycleStart)
    assert isinstance(lifecycle.events[1], CompactionLifecycleSuccess)
    assert lifecycle.events[1].notice_event_id == "$compaction"


@pytest.mark.asyncio
async def test_prepare_history_for_run_required_compaction_edits_failure_when_model_load_fails(
    tmp_path: Path,
) -> None:
    """Required compaction should surface model-load failure in the lifecycle and continue."""
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True, model="summary"),
        context_window=64_000,
        models={
            "default": ModelConfig(provider="openai", id="test-model", context_window=64_000),
            "summary": ModelConfig(provider="openai", id="summary-model", context_window=64_000),
        },
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1"),
            _completed_run("run-2"),
        ],
    )
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    write_scope_state(session, scope, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(session)
    lifecycle = RecordingCompactionLifecycle()

    with patch("mindroom.model_loading.get_model_instance", side_effect=ValueError("bad summary model")):
        prepared = await prepare_history_for_run(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
            compaction_lifecycle=lifecycle,
        )

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert read_scope_state(persisted, scope).force_compact_before_next_run is False
    assert prepared.compaction_outcomes == []
    assert prepared.compaction_decision.mode == "required"
    assert prepared.compaction_reply_outcome == "failed"
    assert len(lifecycle.events) == 2
    assert isinstance(lifecycle.events[0], CompactionLifecycleStart)
    assert isinstance(lifecycle.events[1], CompactionLifecycleFailure)
    assert lifecycle.events[1].notice_event_id == "$compaction"
    assert lifecycle.events[1].failure_reason == "bad summary model"


@pytest.mark.asyncio
async def test_prepare_history_for_run_required_compaction_edits_failure_when_cancelled(
    tmp_path: Path,
) -> None:
    """Cancellation should not leave the visible compaction notice stuck as running."""
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True, model="summary"),
        context_window=64_000,
        models={
            "default": ModelConfig(provider="openai", id="test-model", context_window=64_000),
            "summary": ModelConfig(provider="openai", id="summary-model", context_window=64_000),
        },
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1"),
            _completed_run("run-2"),
        ],
    )
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    write_scope_state(session, scope, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(session)
    lifecycle = RecordingCompactionLifecycle()

    with (
        patch(
            "mindroom.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch("mindroom.history.runtime._run_scope_compaction", new=AsyncMock(side_effect=asyncio.CancelledError)),
        pytest.raises(asyncio.CancelledError),
    ):
        await prepare_history_for_run(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
            compaction_lifecycle=lifecycle,
        )

    assert len(lifecycle.events) == 2
    assert isinstance(lifecycle.events[0], CompactionLifecycleStart)
    assert isinstance(lifecycle.events[1], CompactionLifecycleFailure)
    assert lifecycle.events[1].notice_event_id == "$compaction"
    assert lifecycle.events[1].status == "failed"
    assert lifecycle.events[1].failure_reason == "CancelledError"


@pytest.mark.asyncio
async def test_prepare_history_for_run_required_compaction_classifies_provider_timeout(
    tmp_path: Path,
) -> None:
    """Provider TimeoutError should use the timeout lifecycle outcome even with an empty message."""
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1"),
            _completed_run("run-2"),
        ],
    )
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    write_scope_state(session, scope, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(session)
    lifecycle = RecordingCompactionLifecycle()

    with (
        patch(
            "mindroom.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch("mindroom.history.compaction._generate_compaction_summary", new=AsyncMock(side_effect=TimeoutError)),
    ):
        prepared = await prepare_history_for_run(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
            compaction_lifecycle=lifecycle,
        )

    assert prepared.compaction_outcomes == []
    assert prepared.compaction_reply_outcome == "timeout"
    assert isinstance(lifecycle.events[1], CompactionLifecycleFailure)
    assert lifecycle.events[1].status == "timeout"
    assert lifecycle.events[1].failure_reason == "TimeoutError"


@pytest.mark.asyncio
async def test_compaction_call_timeout_raises_runtime_error() -> None:
    class _SlowSummaryModel(FakeModel):
        async def aresponse(self, *_args: object, **_kwargs: object) -> ModelResponse:
            await asyncio.sleep(0.05)
            return ModelResponse(content="merged summary")

    with (
        patch("mindroom.history.compaction.MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS", 0.01),
        pytest.raises(RuntimeError, match=r"compaction summary timed out after 0.01s"),
    ):
        await _generate_compaction_summary(
            model=_SlowSummaryModel(id="summary-model", provider="fake"),
            messages=_summary_messages(),
        )


@pytest.mark.asyncio
async def test_compaction_call_timeout_returns_without_waiting_for_cancellation_cleanup() -> None:
    class _SlowToUnwindSummaryModel(FakeModel):
        def __init__(self, *, model_id: str, provider: str) -> None:
            super().__init__(id=model_id, provider=provider)
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()
            self.finished = asyncio.Event()

        async def aresponse(self, *_args: object, **_kwargs: object) -> ModelResponse:
            self.started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                self.cancelled.set()
                await asyncio.sleep(0.05)
                raise
            finally:
                self.finished.set()
            raise AssertionError

    model = _SlowToUnwindSummaryModel(model_id="summary-model", provider="fake")
    start = asyncio.get_running_loop().time()

    with (
        patch("mindroom.history.compaction.MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS", 0.01),
        pytest.raises(RuntimeError, match=r"compaction summary timed out after 0.01s"),
    ):
        await _generate_compaction_summary(
            model=model,
            messages=_summary_messages(),
        )

    assert asyncio.get_running_loop().time() - start < 0.04
    await asyncio.wait_for(model.started.wait(), timeout=0.1)
    await asyncio.wait_for(model.cancelled.wait(), timeout=0.1)
    await asyncio.wait_for(model.finished.wait(), timeout=0.2)


@pytest.mark.asyncio
async def test_compaction_call_timeout_raises_even_when_provider_returns_after_cancel() -> None:
    class _SwallowingCancelSummaryModel(FakeModel):
        def __init__(self, *, model_id: str, provider: str) -> None:
            super().__init__(id=model_id, provider=provider)
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()
            self.finished = asyncio.Event()
            self.release_after_cancel = asyncio.Event()

        async def aresponse(self, *_args: object, **_kwargs: object) -> ModelResponse:
            self.started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                self.cancelled.set()
                await self.release_after_cancel.wait()
                return ModelResponse(content="merged summary")
            finally:
                self.finished.set()
            raise AssertionError

    model = _SwallowingCancelSummaryModel(model_id="summary-model", provider="fake")

    with (
        patch("mindroom.history.compaction.MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS", 0.01),
        pytest.raises(RuntimeError, match=r"compaction summary timed out after 0.01s"),
    ):
        await _generate_compaction_summary(
            model=model,
            messages=_summary_messages(),
        )

    await asyncio.wait_for(model.started.wait(), timeout=0.1)
    await asyncio.wait_for(model.cancelled.wait(), timeout=0.1)
    assert not model.finished.is_set()
    model.release_after_cancel.set()
    await asyncio.wait_for(model.finished.wait(), timeout=0.2)


@pytest.mark.asyncio
async def test_compaction_provider_timeout_propagates_unchanged() -> None:
    class _ProviderTimeoutModel(FakeModel):
        async def aresponse(self, *_args: object, **_kwargs: object) -> ModelResponse:
            msg = "provider timeout"
            raise TimeoutError(msg)

    with pytest.raises(TimeoutError, match="provider timeout"):
        await _generate_compaction_summary(
            model=_ProviderTimeoutModel(id="summary-model", provider="fake"),
            messages=_summary_messages(),
        )


def _tool_payload() -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": "search_docs",
            "description": "Search docs.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Search query."}},
                "required": ["query"],
            },
        },
    }


def _closure_references_object(function: object, target: object) -> bool:
    stack = [function]
    seen: set[int] = set()
    while stack:
        current = stack.pop()
        current_id = id(current)
        if current_id in seen:
            continue
        seen.add(current_id)
        if current is target:
            return True
        if inspect.ismethod(current) and current.__self__ is target:
            return True
        closure = getattr(current, "__closure__", None)
        if closure is None:
            continue
        for cell in closure:
            value = cell.cell_contents
            if value is target:
                return True
            if inspect.ismethod(value) or inspect.isfunction(value):
                stack.append(value)
    return False


def test_compaction_summary_anthropic_model_forwards_tool_choice_none_without_mutating_model() -> None:
    tool_payload = _tool_payload()
    model = AnthropicClaude(id="claude-test", api_key="test-key")

    request_model = _compaction_summary_request_model(
        model,
        tools=[tool_payload],
        tool_choice="none",
    )

    assert request_model is not model
    assert model.request_params is None
    kwargs = request_model._prepare_request_kwargs("", tools=[tool_payload], messages=[])
    assert kwargs["tool_choice"] == {"type": "none"}
    assert "tools" in kwargs


def test_compaction_summary_vertex_claude_copy_rebinds_prompt_cache_hook() -> None:
    tool_payload = _tool_payload()
    model = VertexClaude(id="claude-test", project_id="test-project", region="us-central1")
    install_vertex_claude_prompt_cache_hook(model)

    request_model = _compaction_summary_request_model(
        model,
        tools=[tool_payload],
        tool_choice="none",
    )

    assert request_model is not model
    assert request_model.request_params == {"tool_choice": {"type": "none"}}
    assert _closure_references_object(request_model.ainvoke, request_model)
    assert not _closure_references_object(request_model.ainvoke, model)


@pytest.mark.asyncio
async def test_agent_compaction_provider_request_uses_previous_summary_from_system_context_once() -> None:
    agent = _agent()
    agent.add_session_summary_to_context = True
    agent.role = "Engineer"
    session = _session(
        "session-1",
        summary=SessionSummary(summary="Existing durable summary", updated_at=datetime.now(UTC)),
    )
    chain = PreparedConversationChain(
        messages=(Message(role="user", content="New work"), Message(role="assistant", content="New result")),
        rendered_text="New work\nNew result",
        source="persisted_runs",
        source_run_ids=("run-1",),
        estimated_tokens=10,
    )
    summary_request = build_warm_cache_compaction_summary_request(
        chain,
        previous_summary="Existing durable summary",
    )

    provider_request = await build_agent_compaction_provider_request(summary_request, session, agent=agent)

    assert "Existing durable summary" in str(provider_request.messages[0].content)
    assert "Existing durable summary" not in str(provider_request.messages[-1].content)
    assert "Already included in the system context" in str(provider_request.messages[-1].content)


def test_compaction_summary_request_converts_media_tool_call_group_to_plain_messages() -> None:
    tool_payload = {
        "id": "call-1",
        "type": "function",
        "function": {"name": "render_chart", "arguments": "{}"},
    }
    request, included_runs = build_compaction_summary_request(
        previous_summary=None,
        compacted_runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="user", content="Render the chart."),
                    Message(role="assistant", content=None, tool_calls=[tool_payload]),
                    Message(
                        role="tool",
                        content="Chart rendered.",
                        tool_call_id="call-1",
                        images=[
                            Image(
                                id="chart-image",
                                content=b"chart-bytes-that-should-not-be-replayed",
                                format="png",
                                mime_type="image/png",
                            ),
                        ],
                    ),
                    Message(role="assistant", content="The chart is ready."),
                ],
            ),
        ],
        history_settings=ResolvedHistorySettings(
            policy=HistoryPolicy(mode="all"),
            max_tool_calls_from_history=None,
        ),
        max_input_tokens=4_000,
    )

    assert request is not None
    assert included_runs
    assert [message.role for message in request.chain.messages] == ["user", "assistant", "user", "assistant"]
    assert all(not message.tool_calls for message in request.chain.messages)
    assert all(message.tool_call_id is None for message in request.chain.messages)
    assert "Tool calls:" in str(request.chain.messages[1].content)
    assert "Chart rendered." in str(request.chain.messages[2].content)
    assert "images:" in str(request.chain.messages[2].content)
    assert "chart-bytes-that-should-not-be-replayed" not in str(request.chain.messages[2].content)


@pytest.mark.asyncio
async def test_compaction_summary_rejects_provider_request_that_exceeds_budget() -> None:
    class _NoCallSummaryModel(FakeModel):
        async def aresponse(self, *_args: object, **_kwargs: object) -> ModelResponse:
            raise AssertionError

    history_settings = ResolvedHistorySettings(
        policy=HistoryPolicy(mode="all"),
        max_tool_calls_from_history=None,
    )
    run = _completed_run("run-1")
    summary_request, included_runs = build_compaction_summary_request(
        previous_summary=None,
        compacted_runs=[run],
        history_settings=history_settings,
        max_input_tokens=1_000,
    )
    assert summary_request is not None
    assert included_runs

    async def oversized_provider_request(
        request: CompactionSummaryRequest,
        _session: AgentSession | TeamSession,
    ) -> CompactionProviderRequest:
        return CompactionProviderRequest(
            messages=(Message(role="system", content="static prompt " * 1_000), *request.messages),
        )

    with pytest.raises(RuntimeError, match="provider-visible compaction request is too large"):
        await _generate_compaction_summary_with_retry(
            model=_NoCallSummaryModel(id="summary-model", provider="fake"),
            session=_session("session-1", runs=[run]),
            previous_summary=None,
            compactable_runs=[run],
            initial_summary_request=summary_request,
            initial_included_runs=included_runs,
            summary_input_budget=1_000,
            session_id="session-1",
            scope=HistoryScope(kind="agent", scope_id="test_agent"),
            history_settings=history_settings,
            provider_request_builder=oversized_provider_request,
        )


@pytest.mark.asyncio
async def test_compaction_summary_rebuilds_chunk_for_provider_request_overhead() -> None:
    history_settings = ResolvedHistorySettings(
        policy=HistoryPolicy(mode="all"),
        max_tool_calls_from_history=None,
    )
    first_run = _completed_run(
        "run-1",
        messages=[
            Message(role="user", content="first user " + "u" * 600),
            Message(role="assistant", content="first answer " + "a" * 600),
        ],
    )
    second_run = _completed_run(
        "run-2",
        messages=[
            Message(role="user", content="second user " + "v" * 600),
            Message(role="assistant", content="second answer " + "b" * 600),
        ],
    )
    summary_request, included_runs = build_compaction_summary_request(
        previous_summary=None,
        compacted_runs=[first_run, second_run],
        history_settings=history_settings,
        max_input_tokens=1_300,
    )
    assert summary_request is not None
    assert included_runs == [first_run, second_run]

    async def provider_request_with_static_overhead(
        request: CompactionSummaryRequest,
        _session: AgentSession | TeamSession,
    ) -> CompactionProviderRequest:
        return CompactionProviderRequest(
            messages=(Message(role="system", content="static prompt " * 160), *request.messages),
        )

    model = RecordingModel(id="summary-model", provider="fake")
    chunk = await _generate_compaction_summary_with_retry(
        model=model,
        session=_session("session-1", runs=[first_run, second_run]),
        previous_summary=None,
        compactable_runs=[first_run, second_run],
        initial_summary_request=summary_request,
        initial_included_runs=included_runs,
        summary_input_budget=1_300,
        session_id="session-1",
        scope=HistoryScope(kind="agent", scope_id="test_agent"),
        history_settings=history_settings,
        provider_request_builder=provider_request_with_static_overhead,
    )

    assert chunk.included_runs == [first_run]
    assert any("first user" in str(message.content) for message in model.seen_messages)
    assert not any("second user" in str(message.content) for message in model.seen_messages)


def test_effective_summary_input_budget_caps_per_chunk() -> None:
    assert effective_summary_input_budget_tokens(100_000, 256_000) == 32_000
    assert effective_summary_input_budget_tokens(10_000, 256_000) == 10_000
    assert effective_summary_input_budget_tokens(100_000, 12_000) == 3_000
    assert effective_summary_input_budget_tokens(1_500, 1_000) == 1_500
    assert effective_summary_input_budget_tokens(100_000, None) == 100_000


@pytest.mark.asyncio
async def test_rewrite_retries_summary_with_smaller_chunk_after_timeout(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    working_session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="user", content="u" * 8_000),
                    Message(role="assistant", content="a" * 8_000),
                ],
            ),
        ],
    )
    summary_inputs: list[list[Message]] = []

    async def fake_summary(*, messages: list[Message], **_kwargs: object) -> SessionSummary:
        summary_inputs.append(messages)
        if len(summary_inputs) == 1:
            msg = f"compaction summary timed out after {MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS}s"
            raise RuntimeError(msg)
        return SessionSummary(summary="merged summary", updated_at=datetime.now(UTC))

    with patch(
        "mindroom.history.compaction._generate_compaction_summary",
        new=AsyncMock(side_effect=fake_summary),
    ):
        rewrite_result = await _rewrite_working_session_for_compaction(
            storage=storage,
            persisted_session=working_session,
            working_session=working_session,
            summary_model=FakeModel(id="summary-model", provider="fake"),
            summary_model_name="summary-model",
            session_id="session-1",
            scope=scope,
            state=HistoryScopeState(force_compact_before_next_run=True),
            history_settings=ResolvedHistorySettings(
                policy=HistoryPolicy(mode="all"),
                max_tool_calls_from_history=None,
            ),
            available_history_budget=None,
            summary_input_budget=8_000,
            compaction_context_window=16_000,
            before_tokens=0,
            runs_before=1,
            threshold_tokens=None,
            lifecycle_notice_event_id=None,
            progress_callback=None,
            collect_compaction_hook_messages=False,
        )

    assert rewrite_result is not None
    assert len(summary_inputs) == 2
    assert estimate_history_messages_tokens(summary_inputs[1]) < estimate_history_messages_tokens(summary_inputs[0])


@pytest.mark.asyncio
async def test_compaction_summary_cancels_model_task_when_outer_call_is_cancelled() -> None:
    class _BlockingSummaryModel(FakeModel):
        def __init__(self, *, model_id: str, provider: str) -> None:
            super().__init__(id=model_id, provider=provider)
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()
            self.response_task: asyncio.Task[object] | None = None

        async def aresponse(self, *_args: object, **_kwargs: object) -> ModelResponse:
            self.response_task = asyncio.current_task()
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
            raise AssertionError

    model = _BlockingSummaryModel(model_id="summary-model", provider="fake")
    summary_task = asyncio.create_task(
        _generate_compaction_summary(
            model=model,
            messages=_summary_messages(),
        ),
    )

    await asyncio.wait_for(model.started.wait(), timeout=0.1)
    summary_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(summary_task, timeout=0.1)

    await asyncio.wait_for(model.cancelled.wait(), timeout=0.1)
    assert model.response_task is not None
    assert model.response_task.done() is True
    assert model.response_task.cancelled() is True


@pytest.mark.asyncio
async def test_compaction_summary_outer_cancellation_returns_without_waiting_for_provider_cleanup() -> None:
    class _SlowCancelCleanupSummaryModel(FakeModel):
        def __init__(self, *, model_id: str, provider: str) -> None:
            super().__init__(id=model_id, provider=provider)
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()
            self.finished = asyncio.Event()

        async def aresponse(self, *_args: object, **_kwargs: object) -> ModelResponse:
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                await asyncio.sleep(0.05)
                raise
            finally:
                self.finished.set()
            raise AssertionError

    model = _SlowCancelCleanupSummaryModel(model_id="summary-model", provider="fake")
    summary_task = asyncio.create_task(
        _generate_compaction_summary(
            model=model,
            messages=_summary_messages(),
        ),
    )

    await asyncio.wait_for(model.started.wait(), timeout=0.1)
    summary_task.cancel()
    start = asyncio.get_running_loop().time()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(summary_task, timeout=0.02)

    assert asyncio.get_running_loop().time() - start < 0.04
    await asyncio.wait_for(model.cancelled.wait(), timeout=0.1)
    await asyncio.wait_for(model.finished.wait(), timeout=0.2)


@pytest.mark.asyncio
async def test_compaction_summary_outer_cancellation_wins_over_provider_cleanup_error() -> None:
    class _CleanupErrorSummaryModel(FakeModel):
        def __init__(self, *, model_id: str, provider: str) -> None:
            super().__init__(id=model_id, provider=provider)
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()
            self.response_task: asyncio.Task[object] | None = None

        async def aresponse(self, *_args: object, **_kwargs: object) -> ModelResponse:
            self.response_task = asyncio.current_task()
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                msg = "provider cleanup failed"
                raise RuntimeError(msg) from None
            raise AssertionError

    model = _CleanupErrorSummaryModel(model_id="summary-model", provider="fake")
    summary_task = asyncio.create_task(
        _generate_compaction_summary(
            model=model,
            messages=_summary_messages(),
        ),
    )

    await asyncio.wait_for(model.started.wait(), timeout=0.1)
    summary_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(summary_task, timeout=0.1)

    await asyncio.wait_for(model.cancelled.wait(), timeout=0.1)
    assert model.response_task is not None
    assert model.response_task.done() is True
    with pytest.raises(RuntimeError, match="provider cleanup failed"):
        model.response_task.result()


@pytest.mark.asyncio
async def test_compaction_timeout_cleanup_detaches_after_grace_window() -> None:
    class _DetachedTimeoutCleanupSummaryModel(FakeModel):
        def __init__(self, *, model_id: str, provider: str) -> None:
            super().__init__(id=model_id, provider=provider)
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()
            self.release_cleanup = asyncio.Event()
            self.finished = asyncio.Event()

        async def aresponse(self, *_args: object, **_kwargs: object) -> ModelResponse:
            self.started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                self.cancelled.set()
                await self.release_cleanup.wait()
                return ModelResponse(content="merged summary")
            finally:
                self.finished.set()
            raise AssertionError

    model = _DetachedTimeoutCleanupSummaryModel(model_id="summary-model", provider="fake")

    with (
        patch("mindroom.history.compaction.MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS", 0.01),
        patch("mindroom.history.compaction._COMPACTION_CANCEL_DRAIN_TIMEOUT_SECONDS", 0.01),
        pytest.raises(RuntimeError, match=r"compaction summary timed out after 0.01s"),
    ):
        await _generate_compaction_summary(
            model=model,
            messages=_summary_messages(),
        )

    await asyncio.wait_for(model.started.wait(), timeout=0.1)
    await asyncio.wait_for(model.cancelled.wait(), timeout=0.1)
    await asyncio.sleep(0)
    assert _get_background_task_count() == 0
    model.release_cleanup.set()
    await asyncio.wait_for(model.finished.wait(), timeout=0.2)
    await wait_for_background_tasks(timeout=0.1)


@pytest.mark.asyncio
async def test_compaction_call_timeout_falls_back_in_runtime(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class _SlowSummaryModel(FakeModel):
        async def aresponse(self, *_args: object, **_kwargs: object) -> ModelResponse:
            await asyncio.sleep(0.05)
            return ModelResponse(content="merged summary")

    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True, model="summary"),
        context_window=64_000,
        models={
            "default": ModelConfig(provider="openai", id="test-model", context_window=64_000),
            "summary": ModelConfig(provider="openai", id="summary-model", context_window=64_000),
        },
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1"),
            _completed_run("run-2"),
            _completed_run("run-3"),
            _completed_run("run-4"),
        ],
    )
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    write_scope_state(session, scope, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(session)

    with (
        patch(
            "mindroom.model_loading.get_model_instance",
            return_value=_SlowSummaryModel(id="summary-model", provider="fake"),
        ),
        patch("mindroom.history.compaction.MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS", 0.01),
    ):
        prepared = await prepare_history_for_run(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
        )
        await wait_for_background_tasks(timeout=0.2)

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is None
    assert len(persisted.runs) == 4
    assert read_scope_state(persisted, scope).force_compact_before_next_run is False
    assert prepared.compaction_outcomes == []
    assert prepared.compaction_reply_outcome == "timeout"
    captured = capsys.readouterr()
    assert "Compaction failed; continuing without compaction" in captured.out
    assert "Timed-out compaction request" not in captured.out


def test_compaction_hook_events_are_registered() -> None:
    assert EVENT_COMPACTION_BEFORE in BUILTIN_EVENT_NAMES
    assert EVENT_COMPACTION_AFTER in BUILTIN_EVENT_NAMES
    assert validate_event_name(EVENT_COMPACTION_BEFORE) == EVENT_COMPACTION_BEFORE
    assert validate_event_name(EVENT_COMPACTION_AFTER) == EVENT_COMPACTION_AFTER
    assert "compaction" in RESERVED_EVENT_NAMESPACES
    assert default_timeout_ms_for_event(EVENT_COMPACTION_BEFORE) == 15000
    assert default_timeout_ms_for_event(EVENT_COMPACTION_AFTER) == 5000


@pytest.mark.asyncio
async def test_prepare_history_for_run_emits_compaction_before_and_after_hooks(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1"),
            _completed_run("run-2"),
        ],
    )
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    write_scope_state(session, scope, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(session)

    observed: list[tuple[str, list[str], int, int | None, str | None]] = []

    @hook(EVENT_COMPACTION_BEFORE, priority=10)
    async def before_first(ctx: CompactionHookContext) -> None:
        persisted = get_agent_session(storage, "session-1")
        assert persisted is not None
        assert [run.run_id for run in persisted.runs or []] == ["run-1", "run-2"]
        observed.append(
            (
                ctx.event_name,
                ctx.scope.key,
                [str(message.content) for message in ctx.messages],
                ctx.token_count_before,
                ctx.token_count_after,
                ctx.compaction_summary,
            ),
        )

    @hook(EVENT_COMPACTION_BEFORE, priority=20)
    async def before_second(ctx: CompactionHookContext) -> None:
        observed.append((f"{ctx.event_name}:second", [], 0, None, None))

    @hook(EVENT_COMPACTION_AFTER)
    async def after(ctx: CompactionHookContext) -> None:
        observed.append(
            (
                ctx.event_name,
                ctx.scope.key,
                [str(message.content) for message in ctx.messages],
                ctx.token_count_before,
                ctx.token_count_after,
                ctx.compaction_summary,
            ),
        )

    registry = HookRegistry.from_plugins([_plugin("compaction-hooks", [before_first, before_second, after])])
    agent = _agent(db=storage)
    runtime_context = _hook_runtime_context(
        config=config,
        runtime_paths=runtime_paths,
        registry=registry,
        session_id="session-1",
    )

    with (
        tool_runtime_context(runtime_context),
        patch(
            "mindroom.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch(
            "mindroom.history.compaction._generate_compaction_summary",
            new=AsyncMock(return_value=SessionSummary(summary="merged summary", updated_at=datetime.now(UTC))),
        ),
    ):
        prepared = await prepare_history_for_run(
            agent=agent,
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
        )

    assert len(prepared.compaction_outcomes) == 1
    assert observed[0] == (
        "compaction:before",
        "agent:test_agent",
        ["run-1 question", "run-1 answer", "run-2 question", "run-2 answer"],
        observed[0][3],
        None,
        None,
    )
    assert observed[1] == ("compaction:before:second", [], 0, None, None)
    assert observed[2] == (
        "compaction:after",
        "agent:test_agent",
        ["run-1 question", "run-1 answer", "run-2 question", "run-2 answer"],
        observed[2][3],
        prepared.compaction_outcomes[0].after_tokens,
        "merged summary",
    )
    assert observed[0][3] == prepared.compaction_outcomes[0].before_tokens
    assert observed[2][3] == prepared.compaction_outcomes[0].before_tokens


@pytest.mark.asyncio
async def test_compact_scope_history_emits_before_hook_for_each_persisted_chunk(tmp_path: Path) -> None:
    """Every destructive compaction chunk should expose raw messages before persistence."""
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    first_run = _completed_run(
        "run-1",
        messages=[
            Message(role="user", content="u" * 200),
            Message(role="assistant", content="a" * 200),
        ],
    )
    second_run = _completed_run(
        "run-2",
        messages=[
            Message(role="user", content="v" * 200),
            Message(role="assistant", content="b" * 200),
        ],
    )
    session = _session("session-1", runs=[first_run, second_run])
    storage.upsert_session(session)
    history_settings = ResolvedHistorySettings(
        policy=HistoryPolicy(mode="all"),
        max_tool_calls_from_history=None,
    )
    summary_input_budget = next(
        budget
        for budget in range(1, 5_000)
        if len(
            _build_test_summary_request(
                previous_summary=None,
                compacted_runs=[first_run, second_run],
                max_input_tokens=budget,
            )[1],
        )
        == 1
        and len(
            _build_test_summary_request(
                previous_summary="merged summary",
                compacted_runs=[second_run],
                max_input_tokens=budget,
            )[1],
        )
        == 1
    )
    observed: list[tuple[str, list[str], list[str]]] = []

    @hook(EVENT_COMPACTION_BEFORE)
    async def before(ctx: CompactionHookContext) -> None:
        persisted = get_agent_session(storage, "session-1")
        assert persisted is not None
        observed.append(
            (
                ctx.event_name,
                [run.run_id for run in persisted.runs or []],
                [str(message.content) for message in ctx.messages],
            ),
        )

    @hook(EVENT_COMPACTION_AFTER)
    async def after(ctx: CompactionHookContext) -> None:
        persisted = get_agent_session(storage, "session-1")
        assert persisted is not None
        observed.append(
            (
                ctx.event_name,
                [run.run_id for run in persisted.runs or []],
                [str(message.content) for message in ctx.messages],
            ),
        )

    registry = HookRegistry.from_plugins([_plugin("compaction-hooks", [before, after])])
    runtime_context = _hook_runtime_context(
        config=config,
        runtime_paths=runtime_paths,
        registry=registry,
        session_id="session-1",
    )

    with (
        tool_runtime_context(runtime_context),
        patch(
            "mindroom.history.compaction._generate_compaction_summary",
            new=AsyncMock(return_value=SessionSummary(summary="merged summary", updated_at=datetime.now(UTC))),
        ),
    ):
        _state, outcome = await compact_scope_history(
            storage=storage,
            session=session,
            scope=scope,
            state=HistoryScopeState(),
            history_settings=history_settings,
            available_history_budget=1,
            summary_input_budget=summary_input_budget,
            compaction_context_window=16_000,
            summary_model=FakeModel(id="summary-model", provider="fake"),
            summary_model_name="summary-model",
            active_context_window=16_000,
            replay_window_tokens=16_000,
            threshold_tokens=1,
            reserve_tokens=0,
        )

    assert outcome is not None
    assert observed == [
        ("compaction:before", ["run-1", "run-2"], ["u" * 200, "a" * 200]),
        ("compaction:before", ["run-2"], ["v" * 200, "b" * 200]),
        ("compaction:after", [], ["u" * 200, "a" * 200, "v" * 200, "b" * 200]),
    ]


@pytest.mark.asyncio
async def test_prepare_history_for_run_does_not_emit_compaction_hooks_for_no_op_branch(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session("session-1", runs=[_completed_run("run-1")])
    storage.upsert_session(session)

    observed: list[str] = []

    @hook(EVENT_COMPACTION_BEFORE)
    async def before(_ctx: CompactionHookContext) -> None:
        observed.append("before")

    @hook(EVENT_COMPACTION_AFTER)
    async def after(_ctx: CompactionHookContext) -> None:
        observed.append("after")

    registry = HookRegistry.from_plugins([_plugin("compaction-hooks", [before, after])])
    runtime_context = _hook_runtime_context(
        config=config,
        runtime_paths=runtime_paths,
        registry=registry,
        session_id="session-1",
    )
    lifecycle = RecordingCompactionLifecycle()

    with tool_runtime_context(runtime_context):
        prepared = await prepare_history_for_run(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
            compaction_lifecycle=lifecycle,
        )

    assert prepared.compaction_outcomes == []
    assert observed == []
    assert lifecycle.events == []


@pytest.mark.asyncio
async def test_prepare_history_for_run_does_not_collect_compaction_messages_without_hooks(tmp_path: Path) -> None:
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1"),
            _completed_run("run-2"),
        ],
    )

    config, runtime_paths, storage, _scope, runtime_context = _forced_compaction_context(
        tmp_path,
        session=session,
        registry=HookRegistry.empty(),
    )

    with (
        tool_runtime_context(runtime_context),
        patch(
            "mindroom.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch(
            "mindroom.history.compaction._generate_compaction_summary",
            new=AsyncMock(return_value=SessionSummary(summary="merged summary", updated_at=datetime.now(UTC))),
        ),
        patch(
            "mindroom.history.compaction.build_persisted_run_chain",
            side_effect=AssertionError("compaction messages should not be collected without hooks"),
        ),
    ):
        prepared = await prepare_history_for_run(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
        )

    assert len(prepared.compaction_outcomes) == 1


@pytest.mark.asyncio
async def test_prepare_history_for_run_does_not_emit_compaction_hooks_when_rewrite_returns_none(
    tmp_path: Path,
) -> None:
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1"),
            _completed_run("run-2"),
        ],
    )

    observed: list[str] = []

    @hook(EVENT_COMPACTION_BEFORE)
    async def before(_ctx: CompactionHookContext) -> None:
        observed.append("before")

    @hook(EVENT_COMPACTION_AFTER)
    async def after(_ctx: CompactionHookContext) -> None:
        observed.append("after")

    registry = HookRegistry.from_plugins([_plugin("compaction-hooks", [before, after])])
    config, runtime_paths, storage, scope, runtime_context = _forced_compaction_context(
        tmp_path,
        session=session,
        registry=registry,
    )

    with (
        tool_runtime_context(runtime_context),
        patch(
            "mindroom.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch(
            "mindroom.history.compaction._rewrite_working_session_for_compaction",
            new=AsyncMock(return_value=None),
        ),
    ):
        prepared = await prepare_history_for_run(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
        )

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is None
    assert len(persisted.runs or []) == 2
    assert read_scope_state(persisted, scope).force_compact_before_next_run is False
    assert prepared.compaction_outcomes == []
    assert observed == []


@pytest.mark.asyncio
async def test_prepare_history_for_run_applies_compaction_hook_agent_and_room_scopes(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1"),
            _completed_run("run-2"),
        ],
    )
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    write_scope_state(session, scope, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(session)

    observed: list[str] = []

    @hook(EVENT_COMPACTION_BEFORE, agents=["test_agent"], rooms=["!room:localhost"])
    async def matching(ctx: CompactionHookContext) -> None:
        observed.append(f"{ctx.scope.key}:{ctx.agent_name}:{ctx.room_id}:{ctx.thread_id}")

    @hook(EVENT_COMPACTION_BEFORE, agents=["other_agent"], rooms=["!room:localhost"])
    async def wrong_agent(ctx: CompactionHookContext) -> None:
        observed.append(f"wrong-agent:{ctx.agent_name}")

    @hook(EVENT_COMPACTION_BEFORE, agents=["test_agent"], rooms=["!elsewhere:localhost"])
    async def wrong_room(ctx: CompactionHookContext) -> None:
        observed.append(f"wrong-room:{ctx.room_id}")

    registry = HookRegistry.from_plugins([_plugin("compaction-hooks", [matching, wrong_agent, wrong_room])])
    runtime_context = _hook_runtime_context(
        config=config,
        runtime_paths=runtime_paths,
        registry=registry,
        session_id="session-1",
    )

    with (
        tool_runtime_context(runtime_context),
        patch(
            "mindroom.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch(
            "mindroom.history.compaction._generate_compaction_summary",
            new=AsyncMock(return_value=SessionSummary(summary="merged summary", updated_at=datetime.now(UTC))),
        ),
    ):
        prepared = await prepare_history_for_run(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
        )

    assert len(prepared.compaction_outcomes) == 1
    assert observed == ["agent:test_agent:test_agent:!room:localhost:$thread"]


@pytest.mark.asyncio
async def test_compaction_hooks_use_team_scope_agent_name(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    observed: list[str] = []
    saw_matrix_admin: list[bool] = []

    @hook(EVENT_COMPACTION_BEFORE, agents=["team_general"], rooms=["!room:localhost"])
    async def matching(ctx: CompactionHookContext) -> None:
        saw_matrix_admin.append(ctx.matrix_admin is not None)
        observed.append(f"{ctx.scope.key}:{ctx.agent_name}:{ctx.room_id}:{ctx.thread_id}")

    registry = HookRegistry.from_plugins([_plugin("compaction-hooks", [matching])])
    client = AsyncMock()
    runtime_context = ToolRuntimeContext(
        agent_name="router",
        room_id="!room:localhost",
        thread_id="$thread",
        resolved_thread_id="$thread",
        requester_id="@user:localhost",
        client=client,
        config=config,
        runtime_paths=runtime_paths,
        event_cache=make_event_cache_mock(),
        conversation_cache=make_conversation_cache_mock(),
        session_id="session-1",
        hook_registry=registry,
        correlation_id="corr-compaction",
        matrix_admin=build_hook_matrix_admin(client, runtime_paths),
    )

    with tool_runtime_context(runtime_context):
        await _emit_compaction_hook(
            event_name=EVENT_COMPACTION_BEFORE,
            scope=HistoryScope(kind="team", scope_id="team_general"),
            messages=[Message(role="user", content="hello")],
            session_id="session-1",
            token_count_before=10,
            token_count_after=None,
            compaction_summary=None,
        )

    assert observed == ["team:team_general:team_general:!room:localhost:$thread"]
    assert saw_matrix_admin == [True]


@pytest.mark.asyncio
async def test_compaction_hooks_continue_after_timeout(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1"),
            _completed_run("run-2"),
        ],
    )
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    write_scope_state(session, scope, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(session)

    observed: list[str] = []

    @hook(EVENT_COMPACTION_BEFORE, priority=10, timeout_ms=10)
    async def slow_before(_ctx: CompactionHookContext) -> None:
        observed.append("slow")
        await asyncio.sleep(0.05)

    @hook(EVENT_COMPACTION_BEFORE, priority=20)
    async def fast_before(ctx: CompactionHookContext) -> None:
        observed.append(f"fast:{ctx.session_id}")

    registry = HookRegistry.from_plugins([_plugin("compaction-hooks", [slow_before, fast_before])])
    runtime_context = _hook_runtime_context(
        config=config,
        runtime_paths=runtime_paths,
        registry=registry,
        session_id="session-1",
    )

    with (
        tool_runtime_context(runtime_context),
        patch(
            "mindroom.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch(
            "mindroom.history.compaction._generate_compaction_summary",
            new=AsyncMock(return_value=SessionSummary(summary="merged summary", updated_at=datetime.now(UTC))),
        ),
    ):
        prepared = await prepare_history_for_run(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
        )

    assert len(prepared.compaction_outcomes) == 1
    assert observed == ["slow", "fast:session-1"]


@pytest.mark.asyncio
async def test_compaction_hooks_continue_after_runtime_error(tmp_path: Path) -> None:
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1"),
            _completed_run("run-2"),
        ],
    )

    observed: list[str] = []

    @hook(EVENT_COMPACTION_BEFORE, priority=10)
    async def failing(_ctx: CompactionHookContext) -> None:
        observed.append("failed")
        msg = "hook failed"
        raise RuntimeError(msg)

    @hook(EVENT_COMPACTION_BEFORE, priority=20)
    async def fast(ctx: CompactionHookContext) -> None:
        observed.append(f"fast:{ctx.session_id}")

    registry = HookRegistry.from_plugins([_plugin("compaction-hooks", [failing, fast])])
    config, runtime_paths, storage, _scope, runtime_context = _forced_compaction_context(
        tmp_path,
        session=session,
        registry=registry,
    )

    with (
        tool_runtime_context(runtime_context),
        patch(
            "mindroom.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch(
            "mindroom.history.compaction._generate_compaction_summary",
            new=AsyncMock(return_value=SessionSummary(summary="merged summary", updated_at=datetime.now(UTC))),
        ),
    ):
        prepared = await prepare_history_for_run(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
        )

    assert len(prepared.compaction_outcomes) == 1
    assert observed == ["failed", "fast:session-1"]


@pytest.mark.asyncio
async def test_prepare_history_for_run_uses_provided_storage_without_reopening_scope_context(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session("session-1", runs=[_completed_run("run-1")])
    storage.upsert_session(session)

    with patch("mindroom.history.runtime.open_scope_session_context") as mock_open_scope_context:
        prepared = await prepare_history_for_run(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
        )

    mock_open_scope_context.assert_not_called()
    assert prepared.replay_plan is not None


@pytest.mark.asyncio
async def test_prepare_history_for_run_keeps_thread_session_compaction_isolated(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    room_session_id = create_session_id("!room:localhost", None)
    thread_session_id = create_session_id("!room:localhost", "$thread-1")
    room_session = _session(
        room_session_id,
        runs=[
            _completed_run("room-1"),
            _completed_run("room-2"),
            _completed_run("room-3"),
        ],
    )
    thread_session = _session(
        thread_session_id,
        runs=[
            _completed_run("thread-1"),
            _completed_run("thread-2"),
            _completed_run("thread-3"),
            _completed_run("thread-4"),
        ],
    )
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    write_scope_state(thread_session, scope, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(room_session)
    storage.upsert_session(thread_session)

    with (
        patch(
            "mindroom.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch(
            "mindroom.history.compaction._generate_compaction_summary",
            new=AsyncMock(
                return_value=SessionSummary(
                    summary="thread summary",
                    updated_at=datetime.now(UTC),
                ),
            ),
        ),
    ):
        prepared = await prepare_history_for_run(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id=thread_session_id,
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=thread_session,
        )

    persisted_room = get_agent_session(storage, room_session_id)
    persisted_thread = get_agent_session(storage, thread_session_id)
    assert persisted_room is not None
    assert persisted_thread is not None
    assert persisted_room.summary is None
    assert [run.run_id for run in persisted_room.runs] == ["room-1", "room-2", "room-3"]
    assert persisted_thread.summary is not None
    assert persisted_thread.summary.summary == "thread summary"
    assert persisted_thread.runs == []
    assert len(prepared.compaction_outcomes) == 1
    outcome = prepared.compaction_outcomes[0]
    assert outcome.session_id == thread_session_id
    assert outcome.scope == scope.key
    assert outcome.to_notice_metadata()["session_id"] == thread_session_id
    assert outcome.to_notice_metadata()["scope"] == scope.key


@pytest.mark.asyncio
async def test_prepare_history_for_run_forced_compaction_finishes_selected_runs_across_multiple_passes(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="user", content="u" * 200),
                    Message(role="assistant", content="a" * 200),
                ],
            ),
            _completed_run(
                "run-2",
                messages=[
                    Message(role="user", content="u" * 200),
                    Message(role="assistant", content="a" * 200),
                ],
            ),
            _completed_run(
                "run-3",
                messages=[
                    Message(role="user", content="u" * 200),
                    Message(role="assistant", content="a" * 200),
                ],
            ),
        ],
    )
    storage.upsert_session(session)
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    write_scope_state(session, scope, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(session)
    history_settings = ResolvedHistorySettings(
        policy=HistoryPolicy(mode="all"),
        max_tool_calls_from_history=None,
    )
    visible_runs = list(session.runs or [])
    first_summary_text = "first pass summary"
    second_summary_text = "final summary"

    summary_input_budget = next(
        budget
        for budget in range(1, 10_000)
        if _included_summary_run_count(None, visible_runs, budget) == 2
        and _included_summary_run_count(first_summary_text, visible_runs[2:], budget) == 1
    )
    after_first_session = _session(
        "session-1",
        runs=visible_runs[2:],
        summary=SessionSummary(summary=first_summary_text, updated_at=datetime.now(UTC)),
    )
    replay_budget = estimate_prompt_visible_history_tokens(
        session=after_first_session,
        scope=scope,
        history_settings=history_settings,
    )
    assert (
        estimate_prompt_visible_history_tokens(
            session=session,
            scope=scope,
            history_settings=history_settings,
        )
        > replay_budget
    )

    execution_plan = ResolvedHistoryExecutionPlan(
        authored_compaction_config=True,
        authored_compaction_enabled=True,
        destructive_compaction_available=True,
        explicit_compaction_model=True,
        compaction_model_name="summary-model",
        compaction_context_window=4_096,
        replay_window_tokens=64_000,
        trigger_threshold_tokens=1,
        reserve_tokens=0,
        static_prompt_tokens=0,
        replay_budget_tokens=replay_budget,
        summary_input_budget_tokens=summary_input_budget,
    )

    summary_mock = AsyncMock(
        side_effect=[
            SessionSummary(summary=first_summary_text, updated_at=datetime.now(UTC)),
            SessionSummary(summary=second_summary_text, updated_at=datetime.now(UTC)),
        ],
    )
    with (
        patch(
            "mindroom.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch(
            "mindroom.history.compaction._generate_compaction_summary",
            new=summary_mock,
        ),
    ):
        prepared = await prepare_history_for_run(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
            history_settings=history_settings,
            execution_plan=execution_plan,
        )

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.summary == second_summary_text
    assert persisted.runs == []
    state = read_scope_state(persisted, scope)
    assert state.last_compacted_run_count == 3
    assert summary_mock.await_count == 2
    assert len(prepared.compaction_outcomes) == 1
    assert prepared.compaction_outcomes[0].compacted_run_count == 3
    assert prepared.compaction_outcomes[0].runs_after == 0


@pytest.mark.asyncio
async def test_prepare_history_for_run_auto_compaction_runs_to_completion_before_reply(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="user", content="u" * 200),
                    Message(role="assistant", content="a" * 200),
                ],
            ),
            _completed_run(
                "run-2",
                messages=[
                    Message(role="user", content="u" * 200),
                    Message(role="assistant", content="a" * 200),
                ],
            ),
            _completed_run(
                "run-3",
                messages=[
                    Message(role="user", content="u" * 200),
                    Message(role="assistant", content="a" * 200),
                ],
            ),
        ],
    )
    storage.upsert_session(session)
    history_settings = ResolvedHistorySettings(
        policy=HistoryPolicy(mode="all"),
        max_tool_calls_from_history=None,
    )
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    visible_runs = list(session.runs or [])
    first_summary_text = "first pass summary"
    second_summary_text = "second pass summary"

    summary_input_budget = next(
        budget
        for budget in range(1, 10_000)
        if _included_summary_run_count(None, visible_runs, budget) == 2
        and _included_summary_run_count(first_summary_text, visible_runs[2:], budget) == 1
    )

    execution_plan = ResolvedHistoryExecutionPlan(
        authored_compaction_config=True,
        authored_compaction_enabled=True,
        destructive_compaction_available=True,
        explicit_compaction_model=True,
        compaction_model_name="summary-model",
        compaction_context_window=4_096,
        replay_window_tokens=64_000,
        trigger_threshold_tokens=1,
        reserve_tokens=0,
        static_prompt_tokens=0,
        replay_budget_tokens=1,
        summary_input_budget_tokens=summary_input_budget,
    )

    summary_mock = AsyncMock(
        side_effect=[
            SessionSummary(summary=first_summary_text, updated_at=datetime.now(UTC)),
            SessionSummary(summary=second_summary_text, updated_at=datetime.now(UTC)),
        ],
    )
    with (
        patch(
            "mindroom.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch(
            "mindroom.history.compaction._generate_compaction_summary",
            new=summary_mock,
        ),
    ):
        prepared = await prepare_history_for_run(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
            history_settings=history_settings,
            execution_plan=execution_plan,
        )

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.summary == second_summary_text
    assert persisted.runs == []
    assert summary_mock.await_count == 2
    assert len(prepared.compaction_outcomes) == 1
    state = read_scope_state(persisted, scope)
    assert state.last_compacted_run_count == 3


@pytest.mark.asyncio
async def test_prepare_history_for_run_auto_compaction_stops_when_history_fits(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="user", content="u" * 200),
                    Message(role="assistant", content="a" * 200),
                ],
            ),
            _completed_run(
                "run-2",
                messages=[
                    Message(role="user", content="u" * 200),
                    Message(role="assistant", content="a" * 200),
                ],
            ),
            _completed_run(
                "run-3",
                messages=[
                    Message(role="user", content="u" * 200),
                    Message(role="assistant", content="a" * 200),
                ],
            ),
        ],
    )
    storage.upsert_session(session)
    history_settings = ResolvedHistorySettings(
        policy=HistoryPolicy(mode="all"),
        max_tool_calls_from_history=None,
    )
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    visible_runs = list(session.runs or [])
    first_summary_text = "first pass summary"
    second_summary_text = "second pass summary"
    third_summary_text = "third pass summary"

    summary_input_budget = next(
        budget
        for budget in range(1, 10_000)
        if len(
            _build_test_summary_request(
                previous_summary=None,
                compacted_runs=visible_runs,
                history_settings=history_settings,
                max_input_tokens=budget,
            )[1],
        )
        == 1
        and len(
            _build_test_summary_request(
                previous_summary=first_summary_text,
                compacted_runs=visible_runs[1:],
                history_settings=history_settings,
                max_input_tokens=budget,
            )[1],
        )
        == 1
    )
    after_first_session = _session(
        "session-1",
        runs=visible_runs[1:],
        summary=SessionSummary(summary=first_summary_text, updated_at=datetime.now(UTC)),
    )
    replay_budget = estimate_prompt_visible_history_tokens(
        session=after_first_session,
        scope=scope,
        history_settings=history_settings,
    )

    execution_plan = ResolvedHistoryExecutionPlan(
        authored_compaction_config=True,
        authored_compaction_enabled=True,
        destructive_compaction_available=True,
        explicit_compaction_model=True,
        compaction_model_name="summary-model",
        compaction_context_window=4_096,
        replay_window_tokens=64_000,
        trigger_threshold_tokens=1,
        reserve_tokens=0,
        static_prompt_tokens=0,
        replay_budget_tokens=replay_budget,
        summary_input_budget_tokens=summary_input_budget,
    )
    summary_mock = AsyncMock(
        side_effect=[
            SessionSummary(summary=first_summary_text, updated_at=datetime.now(UTC)),
            SessionSummary(summary=second_summary_text, updated_at=datetime.now(UTC)),
            SessionSummary(summary=third_summary_text, updated_at=datetime.now(UTC)),
        ],
    )
    lifecycle = RecordingCompactionLifecycle()

    with (
        patch(
            "mindroom.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch(
            "mindroom.history.compaction._generate_compaction_summary",
            new=summary_mock,
        ),
    ):
        prepared = await prepare_history_for_run(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
            history_settings=history_settings,
            execution_plan=execution_plan,
            compaction_lifecycle=lifecycle,
        )

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.summary == first_summary_text
    assert [run.run_id for run in persisted.runs or []] == ["run-2", "run-3"]
    assert summary_mock.await_count == 1
    assert len(prepared.compaction_outcomes) == 1
    assert prepared.compaction_outcomes[0].compacted_run_count == 1
    state = read_scope_state(persisted, scope)
    assert state.last_compacted_run_count == 1
    progress_events = [event for event in lifecycle.events if isinstance(event, CompactionLifecycleProgress)]
    assert len(progress_events) == 1
    assert progress_events[0].runs_remaining == 0


@pytest.mark.asyncio
async def test_prepare_history_for_run_persists_successful_compaction_chunks_before_later_failure(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="user", content="u" * 200),
                    Message(role="assistant", content="a" * 200),
                ],
            ),
            _completed_run(
                "run-2",
                messages=[
                    Message(role="user", content="u" * 200),
                    Message(role="assistant", content="a" * 200),
                ],
            ),
            _completed_run(
                "run-3",
                messages=[
                    Message(role="user", content="u" * 200),
                    Message(role="assistant", content="a" * 200),
                ],
            ),
        ],
    )
    storage.upsert_session(session)
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    write_scope_state(session, scope, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(session)
    history_settings = ResolvedHistorySettings(
        policy=HistoryPolicy(mode="all"),
        max_tool_calls_from_history=None,
    )
    visible_runs = list(session.runs or [])
    first_summary_text = "first pass summary"

    summary_input_budget = next(
        budget
        for budget in range(1, 10_000)
        if _included_summary_run_count(None, visible_runs, budget) == 2
        and _included_summary_run_count(first_summary_text, visible_runs[2:], budget) == 1
    )
    after_first_session = _session(
        "session-1",
        runs=visible_runs[2:],
        summary=SessionSummary(summary=first_summary_text, updated_at=datetime.now(UTC)),
    )
    replay_budget = estimate_prompt_visible_history_tokens(
        session=after_first_session,
        scope=scope,
        history_settings=history_settings,
    )

    execution_plan = ResolvedHistoryExecutionPlan(
        authored_compaction_config=True,
        authored_compaction_enabled=True,
        destructive_compaction_available=True,
        explicit_compaction_model=True,
        compaction_model_name="summary-model",
        compaction_context_window=4_096,
        replay_window_tokens=64_000,
        trigger_threshold_tokens=1,
        reserve_tokens=0,
        static_prompt_tokens=0,
        replay_budget_tokens=replay_budget,
        summary_input_budget_tokens=summary_input_budget,
    )
    summary_mock = AsyncMock(
        side_effect=[
            SessionSummary(summary=first_summary_text, updated_at=datetime.now(UTC)),
            RuntimeError("summary failed"),
        ],
    )

    with (
        patch(
            "mindroom.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch(
            "mindroom.history.compaction._generate_compaction_summary",
            new=summary_mock,
        ),
    ):
        prepared = await prepare_history_for_run(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
            history_settings=history_settings,
            execution_plan=execution_plan,
        )

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.summary == first_summary_text
    assert [run.run_id for run in persisted.runs or []] == ["run-3"]
    assert summary_mock.await_count == 2
    assert read_scope_state(persisted, scope).force_compact_before_next_run is False
    assert prepared.compaction_outcomes == []


@pytest.mark.asyncio
async def test_prepare_history_for_run_reuses_completed_auto_compaction(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="user", content="u" * 200),
                    Message(role="assistant", content="a" * 200),
                ],
            ),
            _completed_run(
                "run-2",
                messages=[
                    Message(role="user", content="u" * 200),
                    Message(role="assistant", content="a" * 200),
                ],
            ),
            _completed_run(
                "run-3",
                messages=[
                    Message(role="user", content="u" * 200),
                    Message(role="assistant", content="a" * 200),
                ],
            ),
            _completed_run(
                "run-4",
                messages=[
                    Message(role="user", content="u" * 200),
                    Message(role="assistant", content="a" * 200),
                ],
            ),
        ],
    )
    storage.upsert_session(session)

    summary_mock = AsyncMock(
        return_value=SessionSummary(summary="all runs summary", updated_at=datetime.now(UTC)),
    )
    with (
        patch(
            "mindroom.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch(
            "mindroom.history.compaction._generate_compaction_summary",
            new=summary_mock,
        ),
    ):
        first_prepared = await prepare_history_for_run(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
            available_history_budget=1,
        )
        persisted_before_second = get_agent_session(storage, "session-1")
        assert persisted_before_second is not None
        second_prepared = await prepare_history_for_run(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=persisted_before_second,
            available_history_budget=1,
        )

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.summary == "all runs summary"
    assert persisted.runs == []
    assert summary_mock.await_count == 1
    assert len(first_prepared.compaction_outcomes) == 1
    assert second_prepared.compaction_outcomes == []


@pytest.mark.asyncio
async def test_prepare_history_for_run_uses_context_window_guard_without_authored_compaction(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(tmp_path, context_window=600)
    config.defaults.compaction = None
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="user", content="u" * 400),
                    Message(role="assistant", content="a" * 400),
                ],
            ),
            _completed_run(
                "run-2",
                messages=[
                    Message(role="user", content="u" * 400),
                    Message(role="assistant", content="a" * 400),
                ],
            ),
            _completed_run(
                "run-3",
                messages=[
                    Message(role="user", content="u" * 400),
                    Message(role="assistant", content="a" * 400),
                ],
            ),
        ],
    )
    storage.upsert_session(session)
    agent = _agent(db=storage)
    prepared = await prepare_history_for_run(
        agent=agent,
        agent_name="test_agent",
        full_prompt="Current prompt",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        storage=storage,
        session=session,
    )
    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is None
    assert [run.run_id for run in persisted.runs] == ["run-1", "run-2", "run-3"]
    assert prepared.compaction_outcomes == []
    assert prepared.replay_plan is not None
    assert prepared.replay_plan.mode == "limited"
    assert prepared.replay_plan.add_history_to_context is True
    assert prepared.replay_plan.num_history_runs == 2
    assert prepared.replay_plan.num_history_messages is None


@pytest.mark.asyncio
async def test_prepare_history_for_run_context_window_guard_preserves_custom_system_message_role(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(tmp_path, context_window=40)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="developer", content="d" * 120),
                    Message(role="user", content="u" * 15),
                    Message(role="assistant", content="a" * 15),
                ],
            ),
            _completed_run(
                "run-2",
                messages=[
                    Message(role="developer", content="d" * 120),
                    Message(role="user", content="u" * 15),
                    Message(role="assistant", content="a" * 15),
                ],
            ),
        ],
    )
    storage.upsert_session(session)
    agent = _agent(db=storage)

    prepared = await prepare_history_for_run(
        agent=agent,
        agent_name="test_agent",
        full_prompt="Current prompt",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        storage=storage,
        session=session,
        history_settings=ResolvedHistorySettings(
            policy=HistoryPolicy(mode="all"),
            max_tool_calls_from_history=None,
            system_message_role="developer",
        ),
        static_prompt_tokens=0,
        available_history_budget=10,
    )

    assert prepared.replay_plan is not None
    assert prepared.replay_plan.mode == "limited"
    assert prepared.replay_plan.add_history_to_context is True
    assert prepared.replay_plan.num_history_runs == 1
    assert prepared.replay_plan.num_history_messages is None


@pytest.mark.asyncio
async def test_prepare_history_for_run_compaction_failure_clears_force_flag(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1"),
            _completed_run("run-2"),
            _completed_run("run-3"),
            _completed_run("run-4"),
        ],
    )
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    write_scope_state(session, scope, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(session)

    with (
        patch(
            "mindroom.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch(
            "mindroom.history.compaction._generate_compaction_summary",
            new=AsyncMock(side_effect=RuntimeError("summary failed")),
        ),
    ):
        prepared = await prepare_history_for_run(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
        )

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is None
    assert [run.run_id for run in persisted.runs] == ["run-1", "run-2", "run-3", "run-4"]

    state = read_scope_state(persisted, scope)
    assert state.force_compact_before_next_run is False
    assert state.last_summary_model is None
    assert state.last_compacted_run_count is None

    assert prepared.compaction_outcomes == []
    assert prepared.replays_persisted_history is True


@pytest.mark.asyncio
async def test_prepare_history_for_run_without_context_window_skips_auto_compaction(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True, threshold_tokens=10),
        context_window=None,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1"),
            _completed_run("run-2"),
            _completed_run("run-3"),
        ],
    )
    storage.upsert_session(session)

    prepared = await prepare_history_for_run(
        agent=_agent(db=storage),
        agent_name="test_agent",
        full_prompt="Current prompt",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        storage=storage,
        session=session,
    )

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is None
    assert [run.run_id for run in persisted.runs] == ["run-1", "run-2", "run-3"]
    assert prepared.compaction_outcomes == []
    assert prepared.replays_persisted_history is True


@pytest.mark.asyncio
async def test_prepare_history_for_run_authored_compaction_still_plans_safe_replay_when_compaction_unavailable(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=600,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="user", content="u" * 400),
                    Message(role="assistant", content="a" * 400),
                ],
            ),
            _completed_run(
                "run-2",
                messages=[
                    Message(role="user", content="u" * 400),
                    Message(role="assistant", content="a" * 400),
                ],
            ),
        ],
    )
    storage.upsert_session(session)

    agent = _agent(db=storage)
    prepared = await prepare_history_for_run(
        agent=agent,
        agent_name="test_agent",
        full_prompt="Current prompt",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        storage=storage,
        session=session,
    )

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is None
    assert [run.run_id for run in persisted.runs] == ["run-1", "run-2"]
    assert prepared.compaction_outcomes == []
    assert prepared.replay_plan is not None
    assert prepared.replay_plan.mode == "limited"
    assert prepared.replay_plan.add_history_to_context is True
    assert prepared.replay_plan.num_history_runs == 1
    assert prepared.replay_plan.num_history_messages is None


@pytest.mark.asyncio
async def test_prepare_history_for_run_without_authored_compaction_and_no_window_skips_warning(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path, context_window=None)
    config.defaults.compaction = None
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1"),
            _completed_run("run-2"),
        ],
    )
    storage.upsert_session(session)

    with patch("mindroom.history.runtime.logger.warning") as mock_warning:
        prepared = await prepare_history_for_run(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
        )

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is None
    assert [run.run_id for run in persisted.runs] == ["run-1", "run-2"]
    assert prepared.compaction_outcomes == []
    assert prepared.replays_persisted_history is True
    assert mock_warning.call_args_list == []


@pytest.mark.asyncio
async def test_prepare_history_for_run_with_disabled_compaction_and_no_window_skips_warning(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=False),
        context_window=None,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1"),
            _completed_run("run-2"),
        ],
    )
    storage.upsert_session(session)

    with patch("mindroom.history.runtime.logger.warning") as mock_warning:
        prepared = await prepare_history_for_run(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
        )

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is None
    assert [run.run_id for run in persisted.runs] == ["run-1", "run-2"]
    assert prepared.compaction_outcomes == []
    assert prepared.replays_persisted_history is True
    assert mock_warning.call_args_list == []


@pytest.mark.asyncio
async def test_prepare_history_for_run_warns_once_when_authored_compaction_is_unavailable(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=None,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1"),
            _completed_run("run-2"),
        ],
    )
    storage.upsert_session(session)

    with patch("mindroom.history.runtime.logger.warning") as mock_warning:
        await prepare_history_for_run(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
        )

    assert len(mock_warning.call_args_list) == 1


def test_compaction_summary_request_advances_past_oversized_oldest_run() -> None:
    big_run = _completed_run(
        "run-big",
        messages=[
            Message(role="user", content="u" * 800),
            Message(role="assistant", content="a" * 800),
        ],
    )
    small_run = _completed_run("run-small")

    request, included_runs = _build_test_summary_request(
        previous_summary=None,
        compacted_runs=[big_run, small_run],
        max_input_tokens=420,
    )

    assert request is not None
    assert [run.run_id for run in included_runs] == ["run-big"]
    assert request.chain.source_run_ids == ("run-big",)
    assert request.chain.messages[0].content == "Run truncated to fit compaction budget."
    assert request.estimated_tokens <= 420


def test_compaction_summary_request_skips_oversized_run_when_only_truncation_note_fits() -> None:
    run = _completed_run(
        "run-too-large",
        messages=[
            Message(role="user", content="u" * 1_000),
            Message(role="assistant", content="a" * 1_000),
        ],
    )

    request, included_runs = _build_test_summary_request(
        previous_summary=None,
        compacted_runs=[run],
        max_input_tokens=205,
    )

    assert request is None
    assert included_runs == []


def test_compaction_summary_request_stays_within_budget_after_summary_instruction() -> None:
    run = _completed_run(
        "run-near-limit",
        messages=[
            Message(role="user", content="x" * 6_800),
        ],
    )

    request, included_runs = _build_test_summary_request(
        previous_summary=None,
        compacted_runs=[run],
        max_input_tokens=1_900,
    )

    assert request is not None
    assert included_runs == [run]
    assert request.estimated_tokens <= 1_900


def test_compaction_summary_request_oversized_tool_call_run_uses_plain_budgeted_excerpt() -> None:
    run = _completed_run(
        "run-tool",
        messages=[
            Message(role="user", content="Use the tool."),
            Message(
                role="assistant",
                content=None,
                tool_calls=[
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "shell", "arguments": "x" * 4_000},
                    },
                ],
            ),
            Message(role="tool", content="tool result", tool_call_id="call-1"),
        ],
    )

    request, included_runs = _build_test_summary_request(
        previous_summary=None,
        compacted_runs=[run],
        max_input_tokens=700,
    )

    assert request is not None
    assert included_runs == [run]
    assert request.estimated_tokens <= 700
    assert all(message.role != "tool" for message in request.chain.messages)
    assert all(not message.tool_calls for message in request.chain.messages)
    assert all(message.tool_call_id is None for message in request.chain.messages)


def test_warm_cache_compaction_request_rejects_dangling_tool_calls_before_summary_instruction() -> None:
    chain = PreparedConversationChain(
        messages=(
            Message(
                role="assistant",
                content="Calling a tool",
                tool_calls=[
                    {"id": "call-1", "type": "function", "function": {"name": "shell", "arguments": "{}"}},
                ],
            ),
        ),
        rendered_text="Calling a tool",
        source="persisted_runs",
        source_run_ids=("run-tool",),
        estimated_tokens=1,
    )

    with pytest.raises(ValueError, match="tool result adjacency"):
        build_warm_cache_compaction_summary_request(chain, previous_summary=None)


def test_compaction_summary_request_oversized_run_preserves_messages_before_tool_schema() -> None:
    root_request = "Look into how the automatic memory flush in mindroom is supposed to work."
    run = _completed_run(
        "run-big-metadata",
        messages=[
            Message(role="user", content=root_request),
            Message(role="assistant", content="I will investigate."),
        ],
    )
    run.metadata = {
        "matrix_event_id": "$root",
        "thread_id": "$root",
        "tools_schema": [{"name": f"tool_{index}", "description": "x" * 2000} for index in range(30)],
    }

    request, included_runs = _build_test_summary_request(
        previous_summary=None,
        compacted_runs=[run],
        max_input_tokens=360,
    )

    assert request is not None
    assert [included_run.run_id for included_run in included_runs] == ["run-big-metadata"]
    assert root_request in request.rendered_text
    assert "tools_schema" not in request.rendered_text
    assert request.estimated_tokens <= 360


def test_compaction_summary_request_skips_when_previous_summary_cannot_be_preserved() -> None:
    run = _completed_run("run-1")

    request, included_runs = _build_test_summary_request(
        previous_summary="existing durable summary " * 50,
        compacted_runs=[run],
        max_input_tokens=50,
    )

    assert request is None
    assert included_runs == []


def test_compaction_summary_request_excludes_persisted_system_prompt() -> None:
    run = _completed_run(
        "run-1",
        messages=[
            Message(role="system", content="Persisted system prompt that should not be summarized"),
            Message(role="user", content="user request"),
            Message(role="assistant", content="assistant answer"),
        ],
    )

    request, included_runs = _build_test_summary_request(
        previous_summary=None,
        compacted_runs=[run],
        max_input_tokens=1_000,
    )

    assert request is not None
    assert included_runs == [run]
    assert "Persisted system prompt" not in request.rendered_text
    assert "user request" in request.rendered_text
    assert "assistant answer" in request.rendered_text


def test_compaction_summary_request_honors_tool_call_history_limit() -> None:
    run = _completed_run(
        "run-1",
        messages=[
            Message(role="user", content="use tools"),
            Message(
                role="assistant",
                content="first tool",
                tool_calls=[{"id": "call-1", "type": "function", "function": {"name": "first", "arguments": "{}"}}],
            ),
            Message(role="tool", content="first result", tool_call_id="call-1"),
            Message(
                role="assistant",
                content="second tool",
                tool_calls=[{"id": "call-2", "type": "function", "function": {"name": "second", "arguments": "{}"}}],
            ),
            Message(role="tool", content="second result", tool_call_id="call-2"),
        ],
    )

    request, included_runs = _build_test_summary_request(
        previous_summary=None,
        compacted_runs=[run],
        history_settings=ResolvedHistorySettings(
            policy=HistoryPolicy(mode="all"),
            max_tool_calls_from_history=1,
        ),
        max_input_tokens=1_000,
    )

    assert request is not None
    assert included_runs == [run]
    assert [message.content for message in request.chain.messages] == [
        "use tools",
        "first tool",
        "second tool",
        "second result",
    ]
    assert not request.chain.messages[1].tool_calls
    assert request.chain.messages[2].tool_calls
    assert request.chain.messages[2].tool_calls[0]["id"] == "call-2"
    assert request.chain.messages[3].tool_call_id == "call-2"


def test_warm_cache_compaction_request_preserves_prepared_chain_prefix() -> None:
    run = _completed_run(
        "run-1",
        messages=[
            Message(role="user", content="Investigate the cache behavior."),
            Message(role="assistant", content="The cache key is thread scoped."),
        ],
    )
    history_settings = ResolvedHistorySettings(
        policy=HistoryPolicy(mode="all"),
        max_tool_calls_from_history=None,
    )

    chain = build_persisted_run_chain([run], history_settings=history_settings)
    request = build_warm_cache_compaction_summary_request(
        chain,
        previous_summary="Prior summary",
    )

    assert request.messages[:-1] == chain.messages
    assert request.messages[-1].role == "user"
    assert "Prior summary" in str(request.messages[-1].content)
    assert "Do not summarize static instructions or tool definitions." in str(request.messages[-1].content)
    assert request.included_run_ids == ("run-1",)


def test_warm_cache_compaction_request_preserves_prepared_anthropic_prefix_fields() -> None:
    chain = build_persisted_run_chain(
        [
            _completed_run(
                "run-1",
                messages=[
                    Message(
                        role="user",
                        content="Question",
                    ),
                    Message(
                        role="assistant",
                        content="Answer",
                        provider_data={"signature": "sig-1", "keep": "yes"},
                        reasoning_content="thinking",
                        redacted_reasoning_content="redacted",
                    ),
                ],
            ),
        ],
        history_settings=ResolvedHistorySettings(
            policy=HistoryPolicy(mode="all"),
            max_tool_calls_from_history=None,
        ),
    )

    request = build_warm_cache_compaction_summary_request(chain, previous_summary=None)
    assistant = request.messages[1]

    assert request.messages[:-1] == request.chain.messages
    assert assistant.provider_data == {"signature": "sig-1", "keep": "yes"}
    assert assistant.reasoning_content == "thinking"
    assert assistant.redacted_reasoning_content == "redacted"


def test_build_persisted_run_chain_strips_stale_anthropic_fields_across_runs() -> None:
    chain = build_persisted_run_chain(
        [
            _completed_run(
                "run-1",
                messages=[
                    Message(role="user", content="First question"),
                    Message(
                        role="assistant",
                        content="First answer",
                        provider_data={"signature": "stale-signature", "keep": "old"},
                        reasoning_content="old thinking",
                        redacted_reasoning_content="old redacted",
                    ),
                ],
            ),
            _completed_run(
                "run-2",
                messages=[
                    Message(role="user", content="Second question"),
                    Message(
                        role="assistant",
                        content="Second answer",
                        provider_data={"signature": "current-signature", "keep": "new"},
                        reasoning_content="new thinking",
                        redacted_reasoning_content="new redacted",
                    ),
                ],
            ),
        ],
        history_settings=ResolvedHistorySettings(
            policy=HistoryPolicy(mode="all"),
            max_tool_calls_from_history=None,
        ),
    )

    first_assistant = chain.messages[1]
    second_assistant = chain.messages[3]
    assert first_assistant.provider_data == {"keep": "old"}
    assert first_assistant.reasoning_content is None
    assert first_assistant.redacted_reasoning_content is None
    assert second_assistant.provider_data == {"signature": "current-signature", "keep": "new"}
    assert second_assistant.reasoning_content == "new thinking"
    assert second_assistant.redacted_reasoning_content == "new redacted"


def test_build_persisted_run_chain_replaces_media_payloads_with_metadata_snapshots() -> None:
    chain = build_persisted_run_chain(
        [
            _completed_run(
                "run-1",
                messages=[
                    Message(
                        role="user",
                        content="Please inspect this image.",
                        images=[
                            Image(
                                id="image-1",
                                content=b"raw-image-bytes-that-should-not-be-replayed",
                                format="png",
                                mime_type="image/png",
                            ),
                        ],
                    ),
                    Message(role="assistant", content="I inspected it."),
                ],
            ),
        ],
        history_settings=ResolvedHistorySettings(
            policy=HistoryPolicy(mode="all"),
            max_tool_calls_from_history=None,
        ),
    )

    replay_message = chain.messages[0]
    assert replay_message.images is None
    assert "Please inspect this image." in str(replay_message.content)
    assert "images:" in str(replay_message.content)
    assert "raw-image-bytes-that-should-not-be-replayed" not in str(replay_message.content)


@pytest.mark.asyncio
async def test_agent_compaction_provider_request_does_not_mutate_live_replay_settings_during_await() -> None:
    agent = _agent()
    agent.add_history_to_context = False
    agent.num_history_runs = 2
    agent.num_history_messages = 3
    agent.max_tool_calls_from_history = 1
    session = _session("session-1")
    chain = PreparedConversationChain(
        messages=(Message(role="user", content="Past message"),),
        rendered_text="Past message",
        source="persisted_runs",
        source_run_ids=("run-1",),
        estimated_tokens=10,
    )
    summary_request = build_warm_cache_compaction_summary_request(chain, previous_summary=None)
    started = asyncio.Event()
    release = asyncio.Event()

    async def observed_aget_tools(**_kwargs: object) -> list[object]:
        started.set()
        await release.wait()
        return []

    agent.aget_tools = observed_aget_tools  # type: ignore[method-assign]
    request_task = asyncio.create_task(
        build_agent_compaction_provider_request(summary_request, session, agent=agent),
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        assert agent.add_history_to_context is False
        assert agent.num_history_runs == 2
        assert agent.num_history_messages == 3
        assert agent.max_tool_calls_from_history == 1
    finally:
        release.set()
        await request_task


@pytest.mark.asyncio
async def test_compact_scope_history_sends_prepared_chain_summary_request(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="user", content="First question"),
                    Message(role="assistant", content="First answer"),
                ],
            ),
        ],
        summary=SessionSummary(summary="Existing durable summary", updated_at=datetime.now(UTC)),
    )
    write_scope_state(session, scope, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(session)
    observed_messages: list[Message] = []

    async def fake_generate_compaction_summary(*, messages: list[Message], **_kwargs: object) -> SessionSummary:
        observed_messages[:] = messages
        return SessionSummary(summary="Merged summary", updated_at=datetime.now(UTC))

    with patch(
        "mindroom.history.compaction._generate_compaction_summary",
        new=fake_generate_compaction_summary,
    ):
        await compact_scope_history(
            storage=storage,
            session=session,
            scope=scope,
            state=HistoryScopeState(force_compact_before_next_run=True),
            history_settings=ResolvedHistorySettings(
                policy=HistoryPolicy(mode="all"),
                max_tool_calls_from_history=None,
            ),
            available_history_budget=1,
            summary_input_budget=4_000,
            compaction_context_window=16_000,
            summary_model=FakeModel(id="summary-model", provider="fake"),
            summary_model_name="summary-model",
            active_context_window=16_000,
            replay_window_tokens=16_000,
            threshold_tokens=1,
            reserve_tokens=0,
        )

    assert [message.content for message in observed_messages[:-1]] == ["First question", "First answer"]
    assert "Existing durable summary" in str(observed_messages[-1].content)
    assert "Return only the summary text." in str(observed_messages[-1].content)


@pytest.mark.asyncio
async def test_prepare_history_for_run_default_compaction_model_reuses_live_agent_request_prefix(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="user", content="First question"),
                    Message(role="assistant", content="First answer"),
                ],
            ),
        ],
        summary=SessionSummary(summary="Existing durable summary", updated_at=datetime.now(UTC)),
    )
    write_scope_state(session, scope, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(session)

    def search_docs(query: str) -> str:
        """Search docs."""
        return query

    recording_model = RecordingModel(id="recording-model", provider="fake")
    live_agent = _agent(model=recording_model, db=storage, num_history_runs=10)
    live_agent.role = "Engineer"
    live_agent.instructions = ["Use project terminology exactly."]
    live_agent.tools = [Function.from_callable(search_docs)]
    live_agent.add_session_summary_to_context = True

    with patch("mindroom.model_loading.get_model_instance", return_value=recording_model):
        await prepare_history_for_run(
            agent=live_agent,
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
            active_model_name="default",
            active_context_window=64_000,
        )

    assert recording_model.seen_messages[0].role == "system"
    assert "Engineer" in str(recording_model.seen_messages[0].content)
    assert "Existing durable summary" in str(recording_model.seen_messages[0].content)
    assert [message.content for message in recording_model.seen_messages[1:3]] == ["First question", "First answer"]
    assert "Return only the summary text." in str(recording_model.seen_messages[-1].content)
    assert recording_model.seen_tools is not None
    assert any(
        isinstance(tool, dict)
        and tool.get("type") == "function"
        and isinstance(tool.get("function"), dict)
        and tool["function"].get("name") == "search_docs"
        for tool in recording_model.seen_tools
    )
    assert recording_model.seen_tool_choice == "none"


@pytest.mark.asyncio
async def test_prepare_history_for_run_explicit_different_compaction_model_keeps_chain_only_request(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True, model="summary"),
        context_window=64_000,
        models={
            "default": ModelConfig(provider="openai", id="test-model", context_window=64_000),
            "summary": ModelConfig(provider="openai", id="summary-model", context_window=64_000),
        },
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="user", content="First question"),
                    Message(
                        role="assistant",
                        content="I will search docs.",
                        tool_calls=[
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "search_docs", "arguments": '{"query":"cache"}'},
                            },
                        ],
                    ),
                    Message(role="tool", content="Tool result text", tool_call_id="call-1"),
                    Message(role="assistant", content="First answer"),
                ],
            ),
        ],
        summary=SessionSummary(summary="Existing durable summary", updated_at=datetime.now(UTC)),
    )
    write_scope_state(session, scope, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(session)

    def search_docs(query: str) -> str:
        """Search docs."""
        return query

    active_model = RecordingModel(id="active-model", provider="fake")
    summary_model = RecordingModel(id="summary-model", provider="fake")
    live_agent = _agent(model=active_model, db=storage, num_history_runs=10)
    live_agent.role = "Engineer"
    live_agent.instructions = ["Use project terminology exactly."]
    live_agent.tools = [Function.from_callable(search_docs)]
    live_agent.add_session_summary_to_context = True

    with patch("mindroom.model_loading.get_model_instance", return_value=summary_model):
        await prepare_history_for_run(
            agent=live_agent,
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
            active_model_name="default",
            active_context_window=64_000,
        )

    assert [message.role for message in summary_model.seen_messages[:-1]] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert [message.content for message in summary_model.seen_messages[:-1]] == [
        "First question",
        'I will search docs.\nTool calls: [{"function":{"arguments":"{\\"query\\":\\"cache\\"}","name":"search_docs"},"id":"call-1","type":"function"}]',
        "Tool result for call-1:\nTool result text",
        "First answer",
    ]
    assert summary_model.seen_messages[0].role == "user"
    assert all(not message.tool_calls for message in summary_model.seen_messages)
    assert all(message.tool_call_id is None for message in summary_model.seen_messages)
    assert "Return only the summary text." in str(summary_model.seen_messages[-1].content)
    assert not summary_model.seen_tools


def test_estimate_prompt_visible_history_tokens_uses_agno_message_limit_selection() -> None:
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="system", content="Persisted system"),
                    Message(role="user", content="old user"),
                    Message(
                        role="assistant",
                        content="old assistant",
                        tool_calls=[
                            {"id": "call-1", "type": "function", "function": {"name": "tool", "arguments": "{}"}},
                        ],
                    ),
                    Message(role="tool", content="old tool"),
                ],
            ),
            _completed_run(
                "run-2",
                messages=[
                    Message(role="user", content="new user"),
                    Message(role="assistant", content="new assistant"),
                ],
            ),
        ],
    )
    history_settings = ResolvedHistorySettings(
        policy=HistoryPolicy(mode="messages", limit=3),
        max_tool_calls_from_history=None,
    )

    estimated_tokens = estimate_prompt_visible_history_tokens(
        session=session,
        scope=HistoryScope(kind="agent", scope_id="test_agent"),
        history_settings=history_settings,
    )

    expected_messages = [
        Message(role="user", content="new user"),
        Message(role="assistant", content="new assistant"),
    ]
    assert estimated_tokens == estimate_history_messages_tokens(expected_messages)


def test_estimate_prompt_visible_history_tokens_honors_custom_system_message_role() -> None:
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="developer", content="Persisted developer prompt"),
                    Message(role="user", content="old user"),
                    Message(role="assistant", content="old assistant"),
                ],
            ),
            _completed_run(
                "run-2",
                messages=[
                    Message(role="user", content="new user"),
                    Message(role="assistant", content="new assistant"),
                ],
            ),
        ],
    )
    history_settings = ResolvedHistorySettings(
        policy=HistoryPolicy(mode="messages", limit=3),
        max_tool_calls_from_history=None,
        system_message_role="developer",
    )

    estimated_tokens = estimate_prompt_visible_history_tokens(
        session=session,
        scope=HistoryScope(kind="agent", scope_id="test_agent"),
        history_settings=history_settings,
    )

    expected_messages = [
        Message(role="assistant", content="old assistant"),
        Message(role="user", content="new user"),
        Message(role="assistant", content="new assistant"),
    ]
    assert estimated_tokens == estimate_history_messages_tokens(expected_messages)


def test_estimate_prompt_visible_history_tokens_counts_summary_after_compaction_removes_all_runs() -> None:
    session = _session(
        "session-1",
        summary=SessionSummary(summary="merged summary", updated_at=datetime.now(UTC)),
    )
    history_settings = ResolvedHistorySettings(
        policy=HistoryPolicy(mode="messages", limit=3),
        max_tool_calls_from_history=None,
    )

    estimated_tokens = estimate_prompt_visible_history_tokens(
        session=session,
        scope=HistoryScope(kind="agent", scope_id="test_agent"),
        history_settings=history_settings,
    )

    expected_wrapper = (
        "Here is a brief summary of your previous interactions:\n\n"
        "<summary_of_previous_interactions>\n"
        "merged summary\n"
        "</summary_of_previous_interactions>\n\n"
        "Note: this information is from previous interactions and may be outdated. "
        "You should ALWAYS prefer information from this conversation over the past summary.\n\n"
    )

    assert estimate_session_summary_tokens("merged summary") == estimate_text_tokens(expected_wrapper)
    assert estimated_tokens == estimate_text_tokens(expected_wrapper)
    assert estimated_tokens > 0


def test_estimate_session_summary_tokens_none() -> None:
    assert estimate_session_summary_tokens(None) == 0


def test_estimate_session_summary_tokens_empty() -> None:
    assert estimate_session_summary_tokens("") == 0
    assert estimate_session_summary_tokens("   ") == 0


def test_strip_stale_anthropic_replay_fields_returns_zero_without_user_messages() -> None:
    assistant = Message(
        role="assistant",
        content="assistant",
        provider_data={"signature": "sig-1", "keep": "yes"},
        reasoning_content="thinking",
        redacted_reasoning_content="redacted",
    )

    assert strip_stale_anthropic_replay_fields([assistant]) == 0
    assert assistant.provider_data == {"signature": "sig-1", "keep": "yes"}
    assert assistant.reasoning_content == "thinking"
    assert assistant.redacted_reasoning_content == "redacted"


def test_strip_stale_anthropic_replay_fields_preserves_single_turn_after_last_user() -> None:
    assistant = Message(
        role="assistant",
        content="assistant",
        provider_data={"signature": "sig-1"},
        reasoning_content="thinking",
        redacted_reasoning_content="redacted",
    )
    messages = [
        Message(role="user", content="question"),
        assistant,
    ]

    assert strip_stale_anthropic_replay_fields(messages) == 0
    assert assistant.provider_data == {"signature": "sig-1"}
    assert assistant.reasoning_content == "thinking"
    assert assistant.redacted_reasoning_content == "redacted"


def test_strip_stale_anthropic_replay_fields_strips_old_assistants_and_preserves_current_turn() -> None:
    old_assistant = Message(
        role="assistant",
        content="old assistant",
        provider_data={"signature": "sig-old", "keep": "yes"},
        reasoning_content="old thinking",
        redacted_reasoning_content="old redacted",
    )
    current_assistant = Message(
        role="assistant",
        content="current assistant",
        provider_data={"signature": "sig-current"},
        reasoning_content="current thinking",
        redacted_reasoning_content="current redacted",
    )
    messages = [
        Message(role="user", content="old user"),
        old_assistant,
        Message(role="user", content="current user"),
        current_assistant,
    ]

    assert strip_stale_anthropic_replay_fields(messages) == 1
    assert old_assistant.provider_data == {"keep": "yes"}
    assert old_assistant.reasoning_content is None
    assert old_assistant.redacted_reasoning_content is None
    assert current_assistant.provider_data == {"signature": "sig-current"}
    assert current_assistant.reasoning_content == "current thinking"
    assert current_assistant.redacted_reasoning_content == "current redacted"


def test_strip_stale_anthropic_replay_fields_preserves_tool_chain_after_last_user() -> None:
    tool_assistant = Message(
        role="assistant",
        content="tool call",
        provider_data={"signature": "sig-tool"},
        reasoning_content="thinking",
        redacted_reasoning_content="redacted",
        tool_calls=[
            {"id": "call-1", "type": "function", "function": {"name": "tool", "arguments": "{}"}},
        ],
    )
    final_assistant = Message(
        role="assistant",
        content="final answer",
        provider_data={"signature": "sig-final"},
        reasoning_content="more thinking",
        redacted_reasoning_content="more redacted",
    )
    messages = [
        Message(role="user", content="question"),
        tool_assistant,
        Message(role="tool", content="tool result", tool_call_id="call-1"),
        final_assistant,
    ]

    assert strip_stale_anthropic_replay_fields(messages) == 0
    assert tool_assistant.provider_data == {"signature": "sig-tool"}
    assert tool_assistant.reasoning_content == "thinking"
    assert tool_assistant.redacted_reasoning_content == "redacted"
    assert final_assistant.provider_data == {"signature": "sig-final"}
    assert final_assistant.reasoning_content == "more thinking"
    assert final_assistant.redacted_reasoning_content == "more redacted"


def test_strip_stale_anthropic_replay_fields_ignores_reasoning_without_signature() -> None:
    assistant = Message(
        role="assistant",
        content="assistant",
        provider_data={"keep": "yes"},
        reasoning_content="thinking",
        redacted_reasoning_content="redacted",
    )
    messages = [
        Message(role="user", content="old user"),
        assistant,
        Message(role="user", content="current user"),
    ]

    assert strip_stale_anthropic_replay_fields(messages) == 0
    assert assistant.provider_data == {"keep": "yes"}
    assert assistant.reasoning_content == "thinking"
    assert assistant.redacted_reasoning_content == "redacted"


@pytest.mark.asyncio
async def test_rewrite_working_session_for_compaction_strips_stale_replay_fields_from_remaining_runs(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    remaining_run = _completed_run(
        "run-2",
        messages=[
            Message(role="user", content="old user"),
            Message(
                role="assistant",
                content="old assistant",
                provider_data={"signature": "sig-old", "keep": "yes"},
                reasoning_content="old thinking",
                redacted_reasoning_content="old redacted",
            ),
            Message(role="user", content="current user"),
            Message(
                role="assistant",
                content="current assistant",
                provider_data={"signature": "sig-current"},
                reasoning_content="current thinking",
                redacted_reasoning_content="current redacted",
            ),
        ],
    )
    working_session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="user", content="u" * 200),
                    Message(role="assistant", content="a" * 200),
                ],
            ),
            remaining_run,
        ],
    )
    summary_text = "merged summary " * 40
    summary_input_budget = next(
        budget
        for budget in range(1, 10_000)
        if len(
            _build_test_summary_request(
                previous_summary=None,
                compacted_runs=list(working_session.runs or []),
                max_input_tokens=budget,
            )[1],
        )
        == 1
        and _build_test_summary_request(
            previous_summary=summary_text,
            compacted_runs=[remaining_run],
            max_input_tokens=budget,
        )[1]
        == []
    )

    with patch(
        "mindroom.history.compaction._generate_compaction_summary",
        new=AsyncMock(return_value=SessionSummary(summary=summary_text, updated_at=datetime.now(UTC))),
    ):
        rewrite_result = await _rewrite_working_session_for_compaction(
            storage=storage,
            persisted_session=working_session,
            working_session=working_session,
            summary_model=FakeModel(id="summary-model", provider="fake"),
            summary_model_name="summary-model",
            session_id="session-1",
            scope=scope,
            state=HistoryScopeState(),
            history_settings=ResolvedHistorySettings(
                policy=HistoryPolicy(mode="all"),
                max_tool_calls_from_history=None,
            ),
            available_history_budget=1,
            summary_input_budget=summary_input_budget,
            compaction_context_window=16_000,
            before_tokens=0,
            runs_before=2,
            threshold_tokens=None,
            lifecycle_notice_event_id=None,
            progress_callback=None,
            collect_compaction_hook_messages=False,
        )
    assert rewrite_result is not None
    assert rewrite_result.compacted_run_count == 1
    assert [run.run_id for run in working_session.runs] == ["run-2"]
    remaining_messages = working_session.runs[0].messages or []
    assert remaining_messages[1].provider_data == {"keep": "yes"}
    assert remaining_messages[1].reasoning_content is None
    assert remaining_messages[1].redacted_reasoning_content is None
    assert remaining_messages[3].provider_data == {"signature": "sig-current"}
    assert remaining_messages[3].reasoning_content == "current thinking"
    assert remaining_messages[3].redacted_reasoning_content == "current redacted"


@pytest.mark.asyncio
async def test_rewrite_working_session_for_compaction_ignores_runs_without_stable_ids(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    unremovable_run = RunOutput(
        run_id=None,
        agent_id="test_agent",
        status=RunStatus.completed,
        messages=[
            Message(role="user", content="question"),
            Message(role="assistant", content="answer"),
        ],
    )
    working_session = _session("session-1", runs=[unremovable_run])

    with patch(
        "mindroom.history.compaction._generate_compaction_summary",
        new=AsyncMock(return_value=SessionSummary(summary="summary", updated_at=datetime.now(UTC))),
    ) as mock_generate:
        rewrite_result = await _rewrite_working_session_for_compaction(
            storage=storage,
            persisted_session=working_session,
            working_session=working_session,
            summary_model=FakeModel(id="summary-model", provider="fake"),
            summary_model_name="summary-model",
            session_id="session-1",
            scope=scope,
            state=HistoryScopeState(force_compact_before_next_run=True),
            history_settings=ResolvedHistorySettings(
                policy=HistoryPolicy(mode="all"),
                max_tool_calls_from_history=None,
            ),
            available_history_budget=1,
            summary_input_budget=16_000,
            compaction_context_window=16_000,
            before_tokens=0,
            runs_before=1,
            threshold_tokens=None,
            lifecycle_notice_event_id=None,
            progress_callback=None,
            collect_compaction_hook_messages=False,
        )

    assert rewrite_result is None
    assert mock_generate.await_count == 0
    assert working_session.summary is None
    assert working_session.runs == [unremovable_run]


@pytest.mark.asyncio
async def test_compact_scope_history_persists_sanitized_remaining_runs(tmp_path: Path) -> None:
    """Final compaction persist should copy sanitized remaining runs onto the latest session."""
    config, _runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, _runtime_paths, execution_identity=None)
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    remaining_run = _completed_run(
        "run-2",
        messages=[
            Message(role="user", content="old user"),
            Message(
                role="assistant",
                content="old assistant",
                provider_data={"signature": "sig-old", "keep": "yes"},
                reasoning_content="old thinking",
                redacted_reasoning_content="old redacted",
            ),
            Message(role="user", content="current user"),
            Message(
                role="assistant",
                content="current assistant",
                provider_data={"signature": "sig-current"},
                reasoning_content="current thinking",
                redacted_reasoning_content="current redacted",
            ),
        ],
    )
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="user", content="u" * 200),
                    Message(role="assistant", content="a" * 200),
                ],
            ),
            remaining_run,
        ],
    )
    storage.upsert_session(session)
    summary_text = "merged summary " * 40
    summary_input_budget = next(
        budget
        for budget in range(1, 10_000)
        if len(
            _build_test_summary_request(
                previous_summary=None,
                compacted_runs=list(session.runs or []),
                max_input_tokens=budget,
            )[1],
        )
        == 1
        and _build_test_summary_request(
            previous_summary=summary_text,
            compacted_runs=[remaining_run],
            max_input_tokens=budget,
        )[1]
        == []
    )

    with patch(
        "mindroom.history.compaction._generate_compaction_summary",
        new=AsyncMock(return_value=SessionSummary(summary=summary_text, updated_at=datetime.now(UTC))),
    ):
        _state, outcome = await compact_scope_history(
            storage=storage,
            session=session,
            scope=scope,
            state=HistoryScopeState(),
            history_settings=ResolvedHistorySettings(
                policy=HistoryPolicy(mode="all"),
                max_tool_calls_from_history=None,
            ),
            available_history_budget=1,
            summary_input_budget=summary_input_budget,
            compaction_context_window=16_000,
            summary_model=FakeModel(id="summary-model", provider="fake"),
            summary_model_name="summary-model",
            active_context_window=16_000,
            replay_window_tokens=16_000,
            threshold_tokens=1,
            reserve_tokens=0,
        )

    assert outcome is not None
    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert [run.run_id for run in persisted.runs or []] == ["run-2"]
    remaining_messages = (persisted.runs or [])[0].messages or []
    assert remaining_messages[1].provider_data == {"keep": "yes"}
    assert remaining_messages[1].reasoning_content is None
    assert remaining_messages[1].redacted_reasoning_content is None
    assert remaining_messages[3].provider_data == {"signature": "sig-current"}
    assert remaining_messages[3].reasoning_content == "current thinking"
    assert remaining_messages[3].redacted_reasoning_content == "current redacted"


@pytest.mark.asyncio
async def test_rewrite_working_session_emits_progress_after_persisted_chunks(tmp_path: Path) -> None:
    """Visible compaction should update progress after each durable non-final chunk."""
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    first_run = _completed_run(
        "run-1",
        messages=[
            Message(role="user", content="u" * 200),
            Message(role="assistant", content="a" * 200),
        ],
    )
    second_run = _completed_run(
        "run-2",
        messages=[
            Message(role="user", content="v" * 200),
            Message(role="assistant", content="b" * 200),
        ],
    )
    working_session = _session("session-1", runs=[first_run, second_run])
    storage.upsert_session(working_session)
    history_settings = ResolvedHistorySettings(
        policy=HistoryPolicy(mode="all"),
        max_tool_calls_from_history=None,
    )
    before_tokens = estimate_prompt_visible_history_tokens(
        session=working_session,
        scope=scope,
        history_settings=history_settings,
    )
    summary_input_budget = next(
        budget
        for budget in range(1, 5_000)
        if len(
            _build_test_summary_request(
                previous_summary=None,
                compacted_runs=[first_run, second_run],
                max_input_tokens=budget,
            )[1],
        )
        == 1
        and len(
            _build_test_summary_request(
                previous_summary="merged summary",
                compacted_runs=[second_run],
                max_input_tokens=budget,
            )[1],
        )
        == 1
    )
    progress_events: list[CompactionLifecycleProgress] = []

    async def record_progress(event: CompactionLifecycleProgress) -> None:
        persisted = get_agent_session(storage, "session-1")
        assert persisted is not None
        assert persisted.summary is not None
        assert [run.run_id for run in persisted.runs or []] == ["run-2"]
        progress_events.append(event)

    with patch(
        "mindroom.history.compaction._generate_compaction_summary",
        new=AsyncMock(return_value=SessionSummary(summary="merged summary", updated_at=datetime.now(UTC))),
    ):
        rewrite_result = await _rewrite_working_session_for_compaction(
            storage=storage,
            persisted_session=working_session,
            working_session=working_session,
            summary_model=FakeModel(id="summary-model", provider="fake"),
            summary_model_name="summary-model",
            session_id="session-1",
            scope=scope,
            state=HistoryScopeState(),
            history_settings=history_settings,
            available_history_budget=1,
            summary_input_budget=summary_input_budget,
            compaction_context_window=16_000,
            before_tokens=before_tokens,
            runs_before=2,
            threshold_tokens=None,
            lifecycle_notice_event_id="$notice",
            progress_callback=record_progress,
            collect_compaction_hook_messages=False,
        )

    assert rewrite_result is not None
    assert rewrite_result.compacted_run_count == 2
    assert len(progress_events) == 1
    assert progress_events[0].notice_event_id == "$notice"
    assert progress_events[0].mode == "auto"
    assert progress_events[0].session_id == "session-1"
    assert progress_events[0].scope == "agent:test_agent"
    assert progress_events[0].summary_model == "summary-model"
    assert progress_events[0].before_tokens == before_tokens
    assert progress_events[0].compacted_run_count == 1
    assert progress_events[0].runs_before == 2
    assert progress_events[0].runs_remaining == 1


@pytest.mark.asyncio
async def test_prepare_bound_agents_for_run_prepares_team_scope_once(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path)
    owner_agent = _agent(agent_id="alpha", name="Alpha")
    peer_agent = _agent(agent_id="beta", name="Beta")

    def team_lookup(topic: str, include_links: bool = False) -> str:
        """Look up team context for a topic before delegating work."""
        return f"{topic}:{include_links}"

    toolkit = Toolkit(
        name="team_docs",
        tools=[team_lookup],
        instructions="Use the team docs tool before delegating factual questions.",
        add_instructions=True,
    )
    team = Team(
        members=[owner_agent, peer_agent],
        model=FakeModel(id="fake-model", provider="fake"),
        id="team_alpha+beta",
        name="Pair",
        role="Verbose team role",
        tools=[toolkit],
        get_member_information_tool=True,
    )

    prepared_tools = determine_team_tools_for_model(
        team,
        model=team.model,
        run_response=TeamRunOutput(
            run_id="history-budget",
            team_id=team.id,
            session_id="history-budget",
            session_state={},
        ),
        run_context=RunContext(run_id="history-budget", session_id="history-budget", session_state={}),
        team_run_context={},
        session=TeamSession(session_id="history-budget", team_id=team.id),
        check_mcp_tools=False,
    )
    expected_payloads = [
        {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": tool.parameters,
        }
        for tool in prepared_tools
    ]
    previous_tool_instructions = team._tool_instructions
    try:
        team._tool_instructions = [toolkit.instructions]
        system_message = team.get_system_message(
            session=TeamSession(session_id="history-budget", team_id=team.id),
            tools=prepared_tools,
            add_session_state_to_context=False,
        )
    finally:
        team._tool_instructions = previous_tool_instructions
    expected_static_prompt_tokens = estimate_text_tokens("Current prompt")
    if system_message is not None and system_message.content is not None:
        expected_static_prompt_tokens += estimate_text_tokens(str(system_message.content))
    expected_static_prompt_tokens += len(stable_serialize(expected_payloads)) // 4

    with (
        patch(
            "mindroom.history.runtime.prepare_scope_history",
            new=AsyncMock(return_value=MagicMock()),
        ) as mock_prepare,
        patch(
            "tests.test_agno_history.finalize_history_preparation",
            return_value=PreparedHistoryState(replays_persisted_history=True),
        ) as mock_finalize,
        open_bound_scope_session_context(
            agents=[peer_agent, owner_agent],
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
        ) as scope_context,
    ):
        prepared_scope_history = await prepare_bound_scope_history(
            agents=[peer_agent, owner_agent],
            team=team,
            full_prompt="Current prompt",
            runtime_paths=runtime_paths,
            config=config,
            scope_context=scope_context,
        )
        prepared = finalize_history_preparation(
            prepared_scope_history=prepared_scope_history,
            config=config,
        )

    assert prepared.replays_persisted_history is True
    assert mock_finalize.call_count == 1
    assert mock_prepare.await_count == 1
    assert mock_prepare.await_args.kwargs["agent"] is owner_agent
    assert mock_prepare.await_args.kwargs["agent_name"] == "alpha"
    assert mock_prepare.await_args.kwargs["scope"] == HistoryScope(kind="team", scope_id="team_alpha+beta")
    assert (
        estimate_preparation_static_tokens_for_team(team, full_prompt="Current prompt") == expected_static_prompt_tokens
    )
    assert mock_prepare.await_args.kwargs["static_prompt_tokens"] == expected_static_prompt_tokens


def test_estimate_preparation_static_tokens_for_team_includes_agentic_state_tool() -> None:
    owner_agent = _agent(agent_id="alpha", name="Alpha")
    peer_agent = _agent(agent_id="beta", name="Beta")
    team = Team(
        members=[owner_agent, peer_agent],
        model=FakeModel(id="fake-model", provider="fake"),
        id="team_alpha+beta",
        name="Pair",
        role="Stateful team role",
        enable_agentic_state=True,
    )
    budget_session_id = "history-budget"
    session = TeamSession(session_id=budget_session_id, team_id=team.id)
    prepared_tools = determine_team_tools_for_model(
        team,
        model=team.model,
        run_response=TeamRunOutput(
            run_id=budget_session_id,
            team_id=team.id,
            session_id=budget_session_id,
            session_state={},
        ),
        run_context=RunContext(
            run_id=budget_session_id,
            session_id=budget_session_id,
            session_state={},
        ),
        team_run_context={},
        session=session,
        check_mcp_tools=False,
    )
    expected_payloads = [
        {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": tool.parameters,
        }
        for tool in prepared_tools
    ]
    assert any(tool["name"] == "update_session_state" for tool in expected_payloads)

    previous_tool_instructions = team._tool_instructions
    try:
        team._tool_instructions = []
        system_message = team.get_system_message(
            session=session,
            tools=prepared_tools,
            add_session_state_to_context=False,
        )
    finally:
        team._tool_instructions = previous_tool_instructions

    expected_static_prompt_tokens = estimate_text_tokens("Current prompt")
    if system_message is not None and system_message.content is not None:
        expected_static_prompt_tokens += estimate_text_tokens(str(system_message.content))
    expected_static_prompt_tokens += len(stable_serialize(expected_payloads)) // 4

    assert (
        estimate_preparation_static_tokens_for_team(team, full_prompt="Current prompt") == expected_static_prompt_tokens
    )


def test_estimate_preparation_static_tokens_for_team_preserves_tool_instructions() -> None:
    owner_agent = _agent(agent_id="alpha", name="Alpha")
    peer_agent = _agent(agent_id="beta", name="Beta")
    team = Team(
        members=[owner_agent, peer_agent],
        model=FakeModel(id="fake-model", provider="fake"),
        id="team_alpha+beta",
        name="Pair",
        role="Stateful team role",
        enable_agentic_state=True,
    )
    team._tool_instructions = ["keep me"]

    estimate_preparation_static_tokens_for_team(team, full_prompt="Current prompt")

    assert team._tool_instructions == ["keep me"]


def test_create_team_instance_enables_native_team_history_and_disables_members(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "alpha": AgentConfig(display_name="Alpha", num_history_messages=100),
                "zeta": AgentConfig(display_name="Zeta", num_history_messages=1),
            },
            teams={
                "pair": TeamConfig(
                    display_name="Pair",
                    role="Test team",
                    agents=["alpha", "zeta"],
                    num_history_messages=2,
                ),
            },
            defaults=DefaultsConfig(tools=[]),
            models={
                "default": ModelConfig(
                    provider="openai",
                    id="test-model",
                    context_window=2_000,
                ),
            },
        ),
        runtime_paths,
    )
    alpha = _agent(agent_id="alpha", name="Alpha")
    zeta = _agent(agent_id="zeta", name="Zeta")

    with patch("mindroom.model_loading.get_model_instance", return_value=FakeModel(id="fake-model", provider="fake")):
        team = _create_team_instance(
            agents=[alpha, zeta],
            mode=TeamMode.COORDINATE,
            config=config,
            runtime_paths=runtime_paths,
            team_display_name="Team-alpha-zeta",
            fallback_team_id="Team-alpha-zeta",
            configured_team_name="pair",
        )

    assert alpha.add_history_to_context is False
    assert zeta.add_history_to_context is False
    assert team.add_history_to_context is True
    assert team.num_history_messages == 2
    assert team.store_history_messages is False


def test_create_team_instance_preserves_all_history_mode(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "alpha": AgentConfig(display_name="Alpha"),
                "zeta": AgentConfig(display_name="Zeta"),
            },
            teams={
                "pair": TeamConfig(
                    display_name="Pair",
                    role="Test team",
                    agents=["alpha", "zeta"],
                ),
            },
            defaults=DefaultsConfig(tools=[]),
            models={
                "default": ModelConfig(
                    provider="openai",
                    id="test-model",
                    context_window=2_000,
                ),
            },
        ),
        runtime_paths,
    )
    alpha = _agent(agent_id="alpha", name="Alpha")
    zeta = _agent(agent_id="zeta", name="Zeta")

    with patch("mindroom.model_loading.get_model_instance", return_value=FakeModel(id="fake-model", provider="fake")):
        team = _create_team_instance(
            agents=[alpha, zeta],
            mode=TeamMode.COORDINATE,
            config=config,
            runtime_paths=runtime_paths,
            team_display_name="Team-alpha-zeta",
            fallback_team_id="Team-alpha-zeta",
            configured_team_name="pair",
        )

    assert team.num_history_runs is None
    assert team.num_history_messages is None


def test_get_entity_compaction_config_merges_authored_overrides(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "test_agent": AgentConfig(
                    display_name="Test Agent",
                    compaction=CompactionOverrideConfig(
                        threshold_percent=0.6,
                    ),
                ),
            },
            defaults=DefaultsConfig(
                tools=[],
                compaction=CompactionConfig(
                    enabled=False,
                    threshold_tokens=12_000,
                    reserve_tokens=2_048,
                    model="summary-model",
                ),
            ),
            models={
                "default": ModelConfig(
                    provider="openai",
                    id="test-model",
                    context_window=48_000,
                ),
                "summary-model": ModelConfig(
                    provider="openai",
                    id="summary-model-id",
                    context_window=32_000,
                ),
            },
        ),
        runtime_paths,
    )

    resolved = config.get_entity_compaction_config("test_agent")

    assert resolved.enabled is True
    assert resolved.threshold_tokens is None
    assert resolved.threshold_percent == 0.6
    assert resolved.reserve_tokens == 2_048
    assert resolved.model == "summary-model"


def test_authored_empty_defaults_compaction_enables_destructive_compaction(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    config = Config.validate_with_runtime(
        {
            "agents": {
                "test_agent": {
                    "display_name": "Test Agent",
                },
            },
            "defaults": {
                "tools": [],
                "compaction": {},
            },
            "models": {
                "default": {
                    "provider": "openai",
                    "id": "test-model",
                    "context_window": 48_000,
                },
            },
        },
        runtime_paths,
    )

    execution_plan = resolve_history_execution_plan(
        config=config,
        compaction_config=config.get_entity_compaction_config("test_agent"),
        has_authored_compaction_config=config.has_authored_entity_compaction_config("test_agent"),
        active_model_name="default",
        active_context_window=48_000,
        static_prompt_tokens=2_000,
    )

    assert execution_plan.authored_compaction_enabled is True


def test_omitted_defaults_compaction_enables_destructive_compaction(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    config = Config.validate_with_runtime(
        {
            "agents": {
                "test_agent": {
                    "display_name": "Test Agent",
                },
            },
            "defaults": {
                "tools": [],
            },
            "models": {
                "default": {
                    "provider": "openai",
                    "id": "test-model",
                    "context_window": 48_000,
                },
            },
        },
        runtime_paths,
    )

    execution_plan = resolve_history_execution_plan(
        config=config,
        compaction_config=config.get_entity_compaction_config("test_agent"),
        has_authored_compaction_config=config.has_authored_entity_compaction_config("test_agent"),
        active_model_name="default",
        active_context_window=48_000,
        static_prompt_tokens=2_000,
    )

    assert execution_plan.authored_compaction_enabled is True


def test_empty_agent_compaction_override_stays_disabled_with_disabled_defaults(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    config = Config.validate_with_runtime(
        {
            "agents": {
                "test_agent": {
                    "display_name": "Test Agent",
                    "compaction": {},
                },
            },
            "defaults": {
                "tools": [],
                "compaction": {
                    "enabled": False,
                },
            },
            "models": {
                "default": {
                    "provider": "openai",
                    "id": "test-model",
                    "context_window": 48_000,
                },
            },
        },
        runtime_paths,
    )

    execution_plan = resolve_history_execution_plan(
        config=config,
        compaction_config=config.get_entity_compaction_config("test_agent"),
        has_authored_compaction_config=config.has_authored_entity_compaction_config("test_agent"),
        active_model_name="default",
        active_context_window=48_000,
        static_prompt_tokens=2_000,
    )

    assert execution_plan.authored_compaction_enabled is False


def test_validate_compaction_model_references_does_not_emit_availability_warnings(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    with patch("mindroom.config.main.logger.warning") as mock_warning:
        bind_runtime_paths(
            Config(
                agents={
                    "test_agent": AgentConfig(
                        display_name="Test Agent",
                        compaction=CompactionOverrideConfig(enabled=True),
                    ),
                },
                defaults=DefaultsConfig(tools=[]),
                models={
                    "default": ModelConfig(
                        provider="openai",
                        id="test-model",
                        context_window=None,
                    ),
                },
            ),
            runtime_paths,
        )

    assert mock_warning.call_args_list == []


def test_validate_compaction_model_references_rejects_explicit_model_without_context_window(
    tmp_path: Path,
) -> None:
    runtime_paths = _runtime_paths(tmp_path)

    with pytest.raises(
        ValueError,
        match=r"Explicit compaction\.model requires a model with context_window: agents\.test_agent\.compaction\.model -> summary-model",
    ):
        bind_runtime_paths(
            Config(
                agents={
                    "test_agent": AgentConfig(
                        display_name="Test Agent",
                        compaction=CompactionOverrideConfig(enabled=True, model="summary-model"),
                    ),
                },
                defaults=DefaultsConfig(tools=[]),
                models={
                    "default": ModelConfig(
                        provider="openai",
                        id="test-model",
                        context_window=48_000,
                    ),
                    "summary-model": ModelConfig(
                        provider="openai",
                        id="summary-model-id",
                        context_window=None,
                    ),
                },
            ),
            runtime_paths,
        )


def test_validate_compaction_model_references_rejects_disabled_explicit_model_without_context_window(
    tmp_path: Path,
) -> None:
    runtime_paths = _runtime_paths(tmp_path)

    with pytest.raises(
        ValueError,
        match=r"Explicit compaction\.model requires a model with context_window",
    ):
        bind_runtime_paths(
            Config(
                defaults=DefaultsConfig(
                    tools=[],
                    compaction=CompactionConfig(
                        enabled=False,
                        model="summary-model",
                    ),
                ),
                models={
                    "default": ModelConfig(
                        provider="openai",
                        id="test-model",
                        context_window=48_000,
                    ),
                    "summary-model": ModelConfig(
                        provider="openai",
                        id="summary-model-id",
                        context_window=None,
                    ),
                },
            ),
            runtime_paths,
        )


def test_authored_model_dump_preserves_explicit_compaction_model_clear(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "test_agent": AgentConfig(
                    display_name="Test Agent",
                    compaction=CompactionOverrideConfig(enabled=True, model=None),
                ),
            },
            models={
                "default": ModelConfig(
                    provider="openai",
                    id="test-model",
                    context_window=48_000,
                ),
            },
        ),
        runtime_paths,
    )

    assert config.authored_model_dump()["agents"]["test_agent"]["compaction"] == {
        "enabled": True,
        "model": None,
    }


def test_get_entity_compaction_config_inherits_disabled_defaults_for_pure_model_clear(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "test_agent": AgentConfig(
                    display_name="Test Agent",
                    compaction=CompactionOverrideConfig(model=None),
                ),
            },
            defaults=DefaultsConfig(
                tools=[],
                compaction=CompactionConfig(
                    enabled=False,
                    model="summary-model",
                ),
            ),
            models={
                "default": ModelConfig(
                    provider="openai",
                    id="test-model",
                    context_window=48_000,
                ),
                "summary-model": ModelConfig(
                    provider="openai",
                    id="summary-model-id",
                    context_window=32_000,
                ),
            },
        ),
        runtime_paths,
    )

    compaction_config = config.get_entity_compaction_config("test_agent")

    assert compaction_config.enabled is False
    assert compaction_config.model is None


def test_resolve_history_execution_plan_uses_compaction_model_window_only_for_summary_budget(
    tmp_path: Path,
) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "test_agent": AgentConfig(
                    display_name="Test Agent",
                    compaction=CompactionOverrideConfig(model="summary-model"),
                ),
            },
            defaults=DefaultsConfig(tools=[]),
            models={
                "default": ModelConfig(
                    provider="openai",
                    id="test-model",
                    context_window=None,
                ),
                "summary-model": ModelConfig(
                    provider="openai",
                    id="summary-model-id",
                    context_window=32_000,
                ),
            },
        ),
        runtime_paths,
    )

    execution_plan = resolve_history_execution_plan(
        config=config,
        compaction_config=config.get_entity_compaction_config("test_agent"),
        has_authored_compaction_config=config.has_authored_entity_compaction_config("test_agent"),
        active_model_name="default",
        active_context_window=None,
        static_prompt_tokens=2_000,
    )

    assert execution_plan.compaction_context_window == 32_000
    assert execution_plan.replay_window_tokens is None
    assert execution_plan.summary_input_budget_tokens is not None
    assert execution_plan.replay_budget_tokens is None
    assert execution_plan.destructive_compaction_available is True


def test_resolve_runtime_model_uses_room_override_for_team(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"test_agent": AgentConfig(display_name="Test Agent")},
            teams={
                "team_123": TeamConfig(
                    display_name="Test Team",
                    role="Coordinate work",
                    agents=["test_agent"],
                    model="default",
                ),
            },
            defaults=DefaultsConfig(tools=[]),
            room_models={"lobby": "large"},
            models={
                "default": ModelConfig(provider="openai", id="default-model", context_window=None),
                "large": ModelConfig(provider="openai", id="large-model", context_window=32_000),
            },
        ),
        runtime_paths,
    )
    monkeypatch.setattr("mindroom.matrix.rooms.get_room_alias_from_id", lambda *_args: "lobby")

    runtime_model = config.resolve_runtime_model(
        entity_name="team_123",
        room_id="!room:localhost",
        runtime_paths=runtime_paths,
    )

    assert runtime_model.model_name == "large"
    assert runtime_model.context_window == 32_000


def test_resolve_runtime_model_uses_room_override_for_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"test_agent": AgentConfig(display_name="Test Agent", model="default")},
            defaults=DefaultsConfig(tools=[]),
            room_models={"lobby": "large"},
            models={
                "default": ModelConfig(provider="openai", id="default-model", context_window=None),
                "large": ModelConfig(provider="openai", id="large-model", context_window=48_000),
            },
        ),
        runtime_paths,
    )
    monkeypatch.setattr("mindroom.matrix.rooms.get_room_alias_from_id", lambda *_args: "lobby")

    runtime_model = config.resolve_runtime_model(
        entity_name="test_agent",
        room_id="!room:localhost",
        runtime_paths=runtime_paths,
    )

    assert runtime_model.model_name == "large"
    assert runtime_model.context_window == 48_000


def test_resolve_history_execution_plan_marks_non_positive_summary_budget_unavailable(tmp_path: Path) -> None:
    config, _runtime_paths_value = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=4_096,
    )

    execution_plan = resolve_history_execution_plan(
        config=config,
        compaction_config=config.get_entity_compaction_config("test_agent"),
        has_authored_compaction_config=config.has_authored_entity_compaction_config("test_agent"),
        active_model_name="default",
        active_context_window=4_096,
        static_prompt_tokens=500,
    )

    assert execution_plan.summary_input_budget_tokens == 0
    assert execution_plan.destructive_compaction_available is False
    assert execution_plan.unavailable_reason == "non_positive_summary_input_budget"


def test_resolve_history_execution_plan_keeps_replay_headroom_when_compaction_disabled(
    tmp_path: Path,
) -> None:
    config, _runtime_paths_value = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(
            enabled=False,
            threshold_tokens=100,
        ),
        context_window=1_000,
    )

    execution_plan = resolve_history_execution_plan(
        config=config,
        compaction_config=config.get_entity_compaction_config("test_agent"),
        has_authored_compaction_config=config.has_authored_entity_compaction_config("test_agent"),
        active_model_name="default",
        active_context_window=1_000,
        static_prompt_tokens=10,
    )

    assert execution_plan.trigger_threshold_tokens is None
    assert execution_plan.replay_budget_tokens == 490


def test_classify_compaction_decision_forced_compaction_takes_priority() -> None:
    execution_plan = ResolvedHistoryExecutionPlan(
        authored_compaction_config=True,
        authored_compaction_enabled=True,
        destructive_compaction_available=True,
        explicit_compaction_model=True,
        compaction_model_name="summary-model",
        compaction_context_window=32_000,
        replay_window_tokens=32_000,
        trigger_threshold_tokens=24_000,
        reserve_tokens=16_384,
        static_prompt_tokens=2_000,
        replay_budget_tokens=10_000,
        summary_input_budget_tokens=5_000,
    )

    decision = classify_compaction_decision(
        plan=execution_plan,
        force_compact_before_next_run=True,
        current_history_tokens=None,
    )

    assert decision.mode == "required"
    assert decision.reason == "forced"


def test_classify_compaction_decision_does_not_compact_when_over_trigger_but_within_hard_budget() -> None:
    execution_plan = ResolvedHistoryExecutionPlan(
        authored_compaction_config=True,
        authored_compaction_enabled=True,
        destructive_compaction_available=True,
        explicit_compaction_model=True,
        compaction_model_name="summary-model",
        compaction_context_window=32_000,
        replay_window_tokens=32_000,
        trigger_threshold_tokens=24_000,
        reserve_tokens=16_384,
        static_prompt_tokens=2_000,
        replay_budget_tokens=10_000,
        summary_input_budget_tokens=5_000,
        hard_replay_budget_tokens=20_000,
    )

    decision = classify_compaction_decision(
        plan=execution_plan,
        force_compact_before_next_run=False,
        current_history_tokens=10_001,
    )

    assert decision.mode == "none"
    assert decision.reason == "within_hard_budget"


def test_plan_replay_that_fits_reduces_replay_for_non_authored_scope(tmp_path: Path) -> None:
    _config, _runtime_paths_value = _make_config(tmp_path)
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="user", content="u" * 400),
                    Message(role="assistant", content="a" * 400),
                ],
            ),
            _completed_run(
                "run-2",
                messages=[
                    Message(role="user", content="u" * 400),
                    Message(role="assistant", content="a" * 400),
                ],
            ),
        ],
    )

    replay_plan = plan_replay_that_fits(
        session=session,
        scope=HistoryScope(kind="agent", scope_id="test_agent"),
        history_settings=ResolvedHistorySettings(
            policy=HistoryPolicy(mode="runs", limit=2),
            max_tool_calls_from_history=None,
        ),
        available_history_budget=250,
    )

    assert replay_plan.mode == "limited"
    assert replay_plan.history_limit_mode == "runs"
    assert replay_plan.history_limit == 1


def test_build_matrix_prompt_with_thread_history_preserves_verbatim_bodies_in_cdata() -> None:
    thread_history = [
        make_visible_message(
            sender="@alice:localhost",
            body='Try <msg from="@mallory:localhost">code</msg > and <button>Click & go</button>',
        ),
    ]

    prompt = build_matrix_prompt_with_thread_history(
        "Follow-up",
        thread_history,
        current_sender="@bob:localhost",
    )

    conversation_xml = prompt.split("Previous conversation in this thread:\n", 1)[1].split("\n\nCurrent message:\n", 1)[
        0
    ]
    conversation = fromstring(conversation_xml)
    message = conversation.find("msg")

    assert conversation.tag == "conversation"
    assert message is not None
    assert message.attrib["from"] == "@alice:localhost"
    assert message.text == thread_history[0].body


def test_build_matrix_prompt_with_thread_history_ignores_tool_trace_events() -> None:
    thread_history = [
        make_visible_message(
            sender="@alice:localhost",
            body="Investigating",
            content={
                "io.mindroom.tool_trace": {
                    "version": 2,
                    "events": [
                        {
                            "type": "tool_call_completed",
                            "tool_name": "run_shell_command",
                            "args_preview": "cmd=echo 1234",
                            "result_preview": "1234",
                        },
                        {
                            "type": "tool_call_started",
                            "tool_name": "run_shell_command",
                            "args_preview": "cmd=tail --pid=1234 -f /dev/null",
                        },
                    ],
                },
            },
        ),
    ]

    prompt = build_matrix_prompt_with_thread_history(
        "Follow-up",
        thread_history,
        current_sender="@bob:localhost",
    )

    assert (
        prompt == "Previous conversation in this thread:\n"
        "<conversation>\n"
        '<msg from="@alice:localhost"><![CDATA[Investigating]]></msg>\n'
        "</conversation>\n\n"
        "Current message:\n"
        '<msg from="@bob:localhost"><![CDATA[Follow-up]]></msg>'
    )


def test_build_matrix_prompt_with_thread_history_without_tool_trace_is_unchanged() -> None:
    thread_history = [make_visible_message(sender="@alice:localhost", body="Earlier context")]

    prompt = build_matrix_prompt_with_thread_history(
        "Follow-up",
        thread_history,
        current_sender="@bob:localhost",
    )

    assert (
        prompt == "Previous conversation in this thread:\n"
        "<conversation>\n"
        '<msg from="@alice:localhost"><![CDATA[Earlier context]]></msg>\n'
        "</conversation>\n\n"
        "Current message:\n"
        '<msg from="@bob:localhost"><![CDATA[Follow-up]]></msg>'
    )


@pytest.mark.asyncio
async def test_prepare_agent_and_prompt_budgets_persisted_replay_against_primary_prompt(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(tmp_path)
    live_agent = _agent()
    live_agent.role = "Verbose role " + ("r" * 200)
    thread_history = [
        make_visible_message(sender="alice", body="Earlier context"),
        make_visible_message(sender="bob", body="More context"),
    ]

    with (
        patch("mindroom.ai.create_agent", return_value=live_agent),
        patch("mindroom.ai.build_memory_prompt_parts", new=AsyncMock(return_value=MemoryPromptParts())),
        patch(
            "mindroom.execution_preparation.prepare_scope_history",
            new=AsyncMock(return_value=MagicMock()),
        ) as mock_prepare,
        patch(
            "mindroom.execution_preparation.finalize_history_preparation",
            return_value=PreparedHistoryState(),
        ),
    ):
        await _prepare_agent_and_prompt(
            "test_agent",
            "Current prompt",
            runtime_paths,
            config,
            thread_history=thread_history,
        )

    assert mock_prepare.await_args is not None
    assert mock_prepare.await_args.kwargs["static_prompt_tokens"] == estimate_agent_static_tokens(
        live_agent,
        "Current prompt",
    )


@pytest.mark.asyncio
async def test_prepare_agent_and_prompt_uses_room_resolved_agent_model_for_execution_and_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"test_agent": AgentConfig(display_name="Test Agent", model="default")},
            defaults=DefaultsConfig(tools=[]),
            room_models={"lobby": "large"},
            models={
                "default": ModelConfig(provider="openai", id="default-model", context_window=None),
                "large": ModelConfig(provider="openai", id="large-model", context_window=48_000),
            },
        ),
        runtime_paths,
    )
    monkeypatch.setattr("mindroom.matrix.rooms.get_room_alias_from_id", lambda *_args: "lobby")
    live_agent = _agent()

    with (
        patch("mindroom.ai.create_agent", return_value=live_agent) as mock_create_agent,
        patch("mindroom.ai.build_memory_prompt_parts", new=AsyncMock(return_value=MemoryPromptParts())),
        patch(
            "mindroom.execution_preparation.prepare_scope_history",
            new=AsyncMock(return_value=MagicMock()),
        ) as mock_prepare,
        patch(
            "mindroom.execution_preparation.finalize_history_preparation",
            return_value=PreparedHistoryState(),
        ),
    ):
        await _prepare_agent_and_prompt(
            "test_agent",
            "Current prompt",
            runtime_paths,
            config,
            room_id="!room:localhost",
        )

    assert mock_create_agent.call_args is not None
    assert mock_create_agent.call_args.kwargs["active_model_name"] == "large"
    assert mock_prepare.await_args is not None
    assert mock_prepare.await_args.kwargs["active_model_name"] == "large"
    assert mock_prepare.await_args.kwargs["active_context_window"] == 48_000


@pytest.mark.asyncio
async def test_prepare_agent_and_prompt_uses_thread_history_when_persisted_replay_is_disabled(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path)
    live_agent = _agent()
    thread_history = [
        make_visible_message(sender="alice", body="Earlier context"),
        make_visible_message(sender="bob", body="More context"),
    ]

    with (
        patch("mindroom.ai.create_agent", return_value=live_agent),
        patch("mindroom.ai.build_memory_prompt_parts", new=AsyncMock(return_value=MemoryPromptParts())),
        patch(
            "mindroom.execution_preparation.prepare_scope_history",
            new=AsyncMock(return_value=MagicMock()),
        ),
        patch(
            "mindroom.execution_preparation.finalize_history_preparation",
            return_value=PreparedHistoryState(replays_persisted_history=False),
        ),
    ):
        prepared_run = await _prepare_agent_and_prompt(
            "test_agent",
            "Current prompt",
            runtime_paths,
            config,
            thread_history=thread_history,
        )

    prepared_agent = prepared_run.agent
    full_prompt = prepared_run.prompt_text
    prepared = prepared_run.prepared_history
    assert prepared_agent is live_agent
    assert prepared.replays_persisted_history is False
    assert full_prompt == "alice: Earlier context\n\nbob: More context\n\nCurrent prompt"


@pytest.mark.asyncio
async def test_prepare_agent_and_prompt_caps_thread_fallback_to_active_window(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        defaults_compaction=CompactionConfig(reserve_tokens=0),
        context_window=16,
    )
    live_agent = _agent()
    thread_history = [
        make_visible_message(sender="alice", body="Old context " + ("o" * 120)),
        make_visible_message(sender="bob", body="Recent context"),
    ]

    def fake_estimate_preparation_static_tokens(
        agent: Agent,
        *,
        full_prompt: str,
    ) -> int:
        assert agent is live_agent
        return estimate_text_tokens(full_prompt)

    with (
        patch("mindroom.ai.create_agent", return_value=live_agent),
        patch("mindroom.ai.build_memory_prompt_parts", new=AsyncMock(return_value=MemoryPromptParts())),
        patch(
            "mindroom.execution_preparation.estimate_preparation_static_tokens",
            side_effect=fake_estimate_preparation_static_tokens,
        ),
        patch(
            "mindroom.execution_preparation.prepare_scope_history",
            new=AsyncMock(return_value=MagicMock()),
        ),
        patch(
            "mindroom.execution_preparation.finalize_history_preparation",
            return_value=PreparedHistoryState(replays_persisted_history=False),
        ),
    ):
        prepared_run = await _prepare_agent_and_prompt(
            "test_agent",
            "Current prompt",
            runtime_paths,
            config,
            thread_history=thread_history,
        )

    assert prepared_run.prompt_text == "bob: Recent context\n\nCurrent prompt"
    assert estimate_text_tokens(prepared_run.prompt_text) <= 16


@pytest.mark.asyncio
async def test_prepare_agent_and_prompt_uses_full_thread_fallback_for_threaded_missing_replay(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(tmp_path)
    live_agent = _agent()
    thread_history = [
        make_visible_message(sender="@alice:localhost", body="Original question", event_id="$root"),
        make_visible_message(sender="@bot:localhost", body="Prior diagnosis", event_id="$agent-reply"),
        make_visible_message(sender="@alice:localhost", body="What was that?", event_id="$current"),
        make_visible_message(sender="@carol:localhost", body="Later reaction", event_id="$later"),
    ]

    with (
        patch.object(
            Config,
            "get_ids",
            return_value={"test_agent": SimpleNamespace(full_id="@bot:localhost")},
        ),
        patch("mindroom.ai.create_agent", return_value=live_agent),
        patch("mindroom.ai.build_memory_prompt_parts", new=AsyncMock(return_value=MemoryPromptParts())),
        patch(
            "mindroom.execution_preparation.prepare_scope_history",
            new=AsyncMock(return_value=MagicMock()),
        ),
        patch(
            "mindroom.execution_preparation.finalize_history_preparation",
            return_value=PreparedHistoryState(replays_persisted_history=False),
        ),
    ):
        prepared_run = await _prepare_agent_and_prompt(
            "test_agent",
            "What was that?",
            runtime_paths,
            config,
            thread_history=thread_history,
            reply_to_event_id="$current",
            current_sender_id="@alice:localhost",
        )

    assert prepared_run.prepared_history.replays_persisted_history is False
    assert prepared_run.prompt_text == (
        "@alice:localhost: Original question\n\n"
        "Prior diagnosis\n\n"
        'Current message:\n<msg from="@alice:localhost"><![CDATA[What was that?]]></msg>'
    )
    assert "Later reaction" not in prepared_run.prompt_text


@pytest.mark.asyncio
async def test_prepare_agent_and_prompt_trims_oversized_full_thread_fallback(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(tmp_path, context_window=1_000)
    live_agent = _agent()
    thread_history = [
        make_visible_message(
            sender="@alice:localhost",
            body="obsolete context " + ("x" * 20_000),
            event_id="$old",
        ),
        make_visible_message(
            sender="@bob:localhost",
            body="Recent context to keep.",
            event_id="$recent",
        ),
    ]

    with (
        patch("mindroom.ai.create_agent", return_value=live_agent),
        patch("mindroom.ai.build_memory_prompt_parts", new=AsyncMock(return_value=MemoryPromptParts())),
        patch(
            "mindroom.execution_preparation.prepare_scope_history",
            new=AsyncMock(return_value=MagicMock()),
        ),
        patch(
            "mindroom.execution_preparation.finalize_history_preparation",
            return_value=PreparedHistoryState(replays_persisted_history=False),
        ),
    ):
        prepared_run = await _prepare_agent_and_prompt(
            "test_agent",
            "Current prompt",
            runtime_paths,
            config,
            thread_history=thread_history,
        )

    assert prepared_run.prepared_history.replays_persisted_history is False
    assert "Recent context to keep." in prepared_run.prompt_text
    assert "obsolete context" not in prepared_run.prompt_text


@pytest.mark.asyncio
async def test_prepare_agent_and_prompt_skips_thread_fallback_for_summary_only_replay(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[],
        summary=SessionSummary(summary="Compacted summary", updated_at=datetime.now(UTC)),
    )
    storage.upsert_session(session)
    live_agent = _agent()
    thread_history = [
        make_visible_message(sender="@alice:localhost", body="Original context", event_id="$root"),
        make_visible_message(sender="@bot:localhost", body="Prior answer", event_id="$agent-reply"),
    ]

    with (
        open_scope_session_context(
            agent=live_agent,
            agent_name="test_agent",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
        ) as scope_context,
        patch("mindroom.ai.create_agent", return_value=live_agent),
        patch("mindroom.ai.build_memory_prompt_parts", new=AsyncMock(return_value=MemoryPromptParts())),
    ):
        prepared_run = await _prepare_agent_and_prompt(
            "test_agent",
            "Current prompt",
            runtime_paths,
            config,
            session_id="session-1",
            scope_context=scope_context,
            thread_history=thread_history,
        )

    assert prepared_run.prepared_history.replays_persisted_history is True
    assert prepared_run.prompt_text == "Current prompt"
    assert "Original context" not in prepared_run.prompt_text
    assert "Prior answer" not in prepared_run.prompt_text


@pytest.mark.asyncio
async def test_prepare_agent_and_prompt_keeps_matrix_current_sender_when_persisted_replay_is_enabled(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(tmp_path)
    live_agent = _agent()

    with (
        patch("mindroom.ai.create_agent", return_value=live_agent),
        patch("mindroom.ai.build_memory_prompt_parts", new=AsyncMock(return_value=MemoryPromptParts())),
        patch(
            "mindroom.execution_preparation.prepare_scope_history",
            new=AsyncMock(return_value=MagicMock()),
        ),
        patch(
            "mindroom.execution_preparation.finalize_history_preparation",
            return_value=PreparedHistoryState(replays_persisted_history=True),
        ),
    ):
        prepared_run = await _prepare_agent_and_prompt(
            "test_agent",
            "Current prompt",
            runtime_paths,
            config,
            current_sender_id="@alice:localhost",
        )

    prepared_agent = prepared_run.agent
    full_prompt = prepared_run.prompt_text
    prepared = prepared_run.prepared_history
    assert prepared_agent is live_agent
    assert prepared.replays_persisted_history is True
    assert full_prompt == 'Current message:\n<msg from="@alice:localhost"><![CDATA[Current prompt]]></msg>'


def _make_test_compaction_outcome() -> CompactionOutcome:
    return CompactionOutcome(
        mode="auto",
        session_id="session-1",
        scope="agent:test_agent",
        summary="Merged summary",
        summary_model="summary-model",
        before_tokens=30_000,
        after_tokens=12_000,
        window_tokens=128_000,
        threshold_tokens=96_000,
        reserve_tokens=4_096,
        runs_before=20,
        runs_after=8,
        compacted_run_count=12,
        compacted_at="2026-01-01T00:00:00Z",
    )


@pytest.mark.asyncio
async def test_prepare_agent_and_prompt_syncs_enriched_compaction_outcomes_back_to_collector(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(tmp_path)

    def search_docs(query: str) -> str:
        return query

    live_agent = _agent()
    live_agent.role = "Engineer"
    live_agent.instructions = ["Keep the response concise."]
    live_agent.tools = [Function.from_callable(search_docs)]

    original_outcome = _make_test_compaction_outcome()
    collector = [original_outcome]
    prepared_execution = PreparedExecutionContext(
        messages=(Message(role="user", content="Current prompt"),),
        replay_plan=None,
        unseen_event_ids=[],
        replays_persisted_history=False,
        compaction_outcomes=[original_outcome],
    )

    with (
        patch("mindroom.ai.build_memory_prompt_parts", new=AsyncMock(return_value=MemoryPromptParts())),
        patch("mindroom.ai.create_agent", return_value=live_agent),
        patch(
            "mindroom.ai.prepare_agent_execution_context",
            new=AsyncMock(return_value=prepared_execution),
        ),
    ):
        prepared_run = await _prepare_agent_and_prompt(
            "test_agent",
            "Current prompt",
            runtime_paths,
            config,
            compaction_outcomes_collector=collector,
        )

    prepared = prepared_run.prepared_history
    assert collector[0] is prepared.compaction_outcomes[0]
    assert collector[0] is not original_outcome
    assert collector[0].role_instructions_tokens is not None
    assert collector[0].tool_definition_tokens is not None
    assert collector[0].current_prompt_tokens is not None


@pytest.mark.asyncio
async def test_prepare_agent_and_prompt_populates_empty_collector_with_enriched_compaction_outcomes(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(tmp_path)

    def search_docs(query: str) -> str:
        return query

    live_agent = _agent()
    live_agent.role = "Engineer"
    live_agent.instructions = ["Keep the response concise."]
    live_agent.tools = [Function.from_callable(search_docs)]

    original_outcome = _make_test_compaction_outcome()
    collector: list[CompactionOutcome] = []
    prepared_execution = PreparedExecutionContext(
        messages=(Message(role="user", content="Current prompt"),),
        replay_plan=None,
        unseen_event_ids=[],
        replays_persisted_history=False,
        compaction_outcomes=[original_outcome],
    )

    with (
        patch("mindroom.ai.build_memory_prompt_parts", new=AsyncMock(return_value=MemoryPromptParts())),
        patch("mindroom.ai.create_agent", return_value=live_agent),
        patch(
            "mindroom.ai.prepare_agent_execution_context",
            new=AsyncMock(return_value=prepared_execution),
        ),
    ):
        prepared_run = await _prepare_agent_and_prompt(
            "test_agent",
            "Current prompt",
            runtime_paths,
            config,
            compaction_outcomes_collector=collector,
        )

    prepared = prepared_run.prepared_history
    assert len(collector) == 1
    assert collector[0] is prepared.compaction_outcomes[0]
    assert collector[0] is not original_outcome
    assert collector[0].role_instructions_tokens is not None
    assert collector[0].tool_definition_tokens is not None
    assert collector[0].current_prompt_tokens is not None


@pytest.mark.asyncio
async def test_prepare_agent_and_prompt_enriches_compaction_outcomes_without_collector(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(tmp_path)

    def search_docs(query: str) -> str:
        return query

    live_agent = _agent()
    live_agent.role = "Engineer"
    live_agent.instructions = ["Keep the response concise."]
    live_agent.tools = [Function.from_callable(search_docs)]

    original_outcome = _make_test_compaction_outcome()
    prepared_execution = PreparedExecutionContext(
        messages=(Message(role="user", content="Current prompt"),),
        replay_plan=None,
        unseen_event_ids=[],
        replays_persisted_history=False,
        compaction_outcomes=[original_outcome],
    )

    with (
        patch("mindroom.ai.build_memory_prompt_parts", new=AsyncMock(return_value=MemoryPromptParts())),
        patch("mindroom.ai.create_agent", return_value=live_agent),
        patch(
            "mindroom.ai.prepare_agent_execution_context",
            new=AsyncMock(return_value=prepared_execution),
        ),
    ):
        prepared_run = await _prepare_agent_and_prompt(
            "test_agent",
            "Current prompt",
            runtime_paths,
            config,
            compaction_outcomes_collector=None,
        )

    prepared = prepared_run.prepared_history
    assert len(prepared.compaction_outcomes) == 1
    assert prepared.compaction_outcomes[0] is not original_outcome
    assert prepared.compaction_outcomes[0].role_instructions_tokens is not None
    assert prepared.compaction_outcomes[0].tool_definition_tokens is not None
    assert prepared.compaction_outcomes[0].current_prompt_tokens is not None


@pytest.mark.asyncio
async def test_prepare_agent_and_prompt_omits_zero_breakdown_segments_in_notice(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(tmp_path)
    live_agent = _agent()
    live_agent.role = ""
    live_agent.instructions = []
    live_agent.tools = None

    original_outcome = _make_test_compaction_outcome()
    prepared_execution = PreparedExecutionContext(
        messages=(Message(role="user", content="x" * 248),),
        replay_plan=None,
        unseen_event_ids=[],
        replays_persisted_history=False,
        compaction_outcomes=[original_outcome],
    )

    with (
        patch("mindroom.ai.build_memory_prompt_parts", new=AsyncMock(return_value=MemoryPromptParts())),
        patch("mindroom.ai.create_agent", return_value=live_agent),
        patch(
            "mindroom.ai.prepare_agent_execution_context",
            new=AsyncMock(return_value=prepared_execution),
        ),
    ):
        prepared_run = await _prepare_agent_and_prompt(
            "test_agent",
            "Current prompt",
            runtime_paths,
            config,
            compaction_outcomes_collector=None,
        )

    prepared = prepared_run.prepared_history
    outcome = prepared.compaction_outcomes[0]
    assert outcome.role_instructions_tokens == 0
    assert outcome.tool_definition_tokens == 0
    assert outcome.current_prompt_tokens == 62
    notice = outcome.format_notice()
    assert "0 instructions" not in notice
    assert "0 tools" not in notice
    assert "62 prompt" in notice


@pytest.mark.asyncio
async def test_prepare_agent_and_prompt_keeps_empty_collector_when_no_compaction_outcomes(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(tmp_path)
    live_agent = _agent()
    collector: list[CompactionOutcome] = []
    prepared_execution = PreparedExecutionContext(
        messages=(Message(role="user", content="Current prompt"),),
        replay_plan=None,
        unseen_event_ids=[],
        replays_persisted_history=False,
        compaction_outcomes=[],
    )

    with (
        patch("mindroom.ai.build_memory_prompt_parts", new=AsyncMock(return_value=MemoryPromptParts())),
        patch("mindroom.ai.create_agent", return_value=live_agent),
        patch(
            "mindroom.ai.prepare_agent_execution_context",
            new=AsyncMock(return_value=prepared_execution),
        ),
    ):
        prepared_run = await _prepare_agent_and_prompt(
            "test_agent",
            "Current prompt",
            runtime_paths,
            config,
            compaction_outcomes_collector=collector,
        )

    prepared = prepared_run.prepared_history
    assert collector == []
    assert prepared.compaction_outcomes == []


@pytest.mark.asyncio
async def test_prepare_history_for_run_forced_compaction_without_budget_clears_flag(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=None,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1"),
            _completed_run("run-2"),
        ],
    )
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    write_scope_state(session, scope, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(session)

    prepared = await prepare_history_for_run(
        agent=_agent(db=storage),
        agent_name="test_agent",
        full_prompt="Current prompt",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        storage=storage,
        session=session,
    )

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is None
    assert [run.run_id for run in persisted.runs] == ["run-1", "run-2"]
    assert read_scope_state(persisted, scope).force_compact_before_next_run is False
    assert prepared.compaction_outcomes == []


@pytest.mark.asyncio
async def test_prepare_history_for_run_without_budget_returns_configured_replay_plan(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        num_history_runs=2,
        context_window=None,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1"),
            _completed_run("run-2"),
        ],
    )
    storage.upsert_session(session)

    prepared = await prepare_history_for_run(
        agent=_agent(db=storage, num_history_runs=2),
        agent_name="test_agent",
        full_prompt="Current prompt",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        storage=storage,
        session=session,
    )

    assert prepared.replay_plan == ResolvedReplayPlan(
        mode="configured",
        estimated_tokens=estimate_prompt_visible_history_tokens(
            session=session,
            scope=HistoryScope(kind="agent", scope_id="test_agent"),
            history_settings=ResolvedHistorySettings(
                policy=HistoryPolicy(mode="runs", limit=2),
                max_tool_calls_from_history=None,
            ),
        ),
        add_history_to_context=True,
        num_history_runs=2,
        num_history_messages=None,
    )
    assert prepared.replays_persisted_history is True


@pytest.mark.asyncio
async def test_prepare_history_for_run_tracks_disabled_replay_separately_from_session_persistence(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        num_history_runs=2,
        context_window=500,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="user", content="u" * 800),
                    Message(role="assistant", content="a" * 800),
                ],
            ),
            _completed_run(
                "run-2",
                messages=[
                    Message(role="user", content="u" * 800),
                    Message(role="assistant", content="a" * 800),
                ],
            ),
        ],
    )
    storage.upsert_session(session)

    prepared = await prepare_history_for_run(
        agent=_agent(db=storage, num_history_runs=2),
        agent_name="test_agent",
        full_prompt="Current prompt",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        storage=storage,
        session=session,
    )

    assert prepared.replay_plan is not None
    assert prepared.replay_plan.mode == "disabled"
    assert prepared.replays_persisted_history is False


@pytest.mark.asyncio
async def test_prepare_history_for_run_forced_compaction_uses_summary_replay_when_no_runs_fit(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="user", content="u" * 200),
                    Message(role="assistant", content="a" * 200),
                ],
            ),
            _completed_run(
                "run-2",
                messages=[
                    Message(role="user", content="u" * 200),
                    Message(role="assistant", content="a" * 200),
                ],
            ),
        ],
    )
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    write_scope_state(session, scope, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(session)

    agent = _agent(db=storage)
    with (
        patch(
            "mindroom.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch(
            "mindroom.history.compaction._generate_compaction_summary",
            new=AsyncMock(
                return_value=SessionSummary(summary="merged summary", updated_at=datetime.now(UTC)),
            ),
        ),
    ):
        prepared = await prepare_history_for_run(
            agent=agent,
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
            available_history_budget=1,
        )

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.summary == "merged summary"
    assert persisted.runs == []
    state = read_scope_state(persisted, scope)
    assert state.last_compacted_run_count == 2
    assert state.force_compact_before_next_run is False
    assert len(prepared.compaction_outcomes) == 1
    assert prepared.compaction_outcomes[0].runs_after == 0
    assert prepared.compaction_outcomes[0].summary == "merged summary"
    assert prepared.replay_plan is not None
    assert prepared.replay_plan.mode == "disabled"
    assert prepared.replay_plan.estimated_tokens > 0
    assert prepared.replays_persisted_history is True


def test_plan_replay_that_fits_disables_replay_when_no_history_fits_budget() -> None:
    available_history_budget = estimate_text_tokens("budget")
    agent = _agent()
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="user", content="u" * 600),
                    Message(role="assistant", content="a" * 600),
                ],
            ),
        ],
    )

    replay_plan = plan_replay_that_fits(
        session=session,
        scope=HistoryScope(kind="agent", scope_id="test_agent"),
        history_settings=ResolvedHistorySettings(
            policy=HistoryPolicy(mode="all"),
            max_tool_calls_from_history=None,
        ),
        available_history_budget=available_history_budget,
    )
    apply_replay_plan(target=agent, replay_plan=replay_plan)

    assert replay_plan.mode == "disabled"
    assert agent.add_history_to_context is False
    assert agent.num_history_runs is None
    assert agent.num_history_messages is None


def test_scope_seen_event_ids_survive_scope_state_writes(tmp_path: Path) -> None:
    _config, _runtime_paths_value = _make_config(tmp_path)
    scope = HistoryScope(kind="team", scope_id="team-123")
    session = _session("session-1")

    assert update_scope_seen_event_ids(session, scope, ["event-1"]) is True
    write_scope_state(session, scope, HistoryScopeState(force_compact_before_next_run=True))

    assert read_scope_seen_event_ids(session, scope) == {"event-1"}


def test_scope_seen_event_ids_include_persisted_response_event_ids(tmp_path: Path) -> None:
    _config, _runtime_paths_value = _make_config(tmp_path)
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    run = _completed_run("run-1")
    run.metadata = {
        "matrix_seen_event_ids": ["question-1"],
        "matrix_response_event_id": "answer-1",
    }
    session = _session("session-1", runs=[run])

    assert read_scope_seen_event_ids(session, scope) == {"question-1", "answer-1"}


def test_scope_states_do_not_bleed_between_scopes(tmp_path: Path) -> None:
    _config, _runtime_paths_value = _make_config(tmp_path)
    agent_scope = HistoryScope(kind="agent", scope_id="test_agent")
    team_scope = HistoryScope(kind="team", scope_id="team-123")
    session = _session("session-1")

    write_scope_state(session, agent_scope, HistoryScopeState(force_compact_before_next_run=True))
    write_scope_state(session, team_scope, HistoryScopeState(last_summary_model="summary-model"))

    assert read_scope_state(session, agent_scope).force_compact_before_next_run is True
    assert read_scope_state(session, agent_scope).last_summary_model is None
    assert read_scope_state(session, team_scope).force_compact_before_next_run is False
    assert read_scope_state(session, team_scope).last_summary_model == "summary-model"


def test_legacy_scope_state_metadata_is_ignored(tmp_path: Path) -> None:
    _config, _runtime_paths_value = _make_config(tmp_path)
    agent_scope = HistoryScope(kind="agent", scope_id="test_agent")
    session = _session(
        "session-1",
        metadata={
            MINDROOM_COMPACTION_METADATA_KEY: {
                "version": 1,
                "force_compact_before_next_run": True,
            },
        },
    )

    assert read_scope_state(session, agent_scope).force_compact_before_next_run is False

    write_scope_state(session, agent_scope, HistoryScopeState(force_compact_before_next_run=True))

    assert session.metadata == {
        MINDROOM_COMPACTION_METADATA_KEY: {
            "version": 2,
            "states": {
                agent_scope.key: {
                    "force_compact_before_next_run": True,
                },
            },
        },
    }


def test_scope_seen_event_ids_do_not_bleed_between_scopes(tmp_path: Path) -> None:
    _config, _runtime_paths_value = _make_config(tmp_path)
    agent_scope = HistoryScope(kind="agent", scope_id="test_agent")
    team_scope = HistoryScope(kind="team", scope_id="team-123")
    session = _session(
        "session-1",
        runs=[
            RunOutput(
                run_id="agent-run",
                agent_id="test_agent",
                status=RunStatus.completed,
                metadata={"matrix_seen_event_ids": ["agent-event"]},
            ),
            TeamRunOutput(
                run_id="team-run",
                team_id="team-123",
                status=RunStatus.completed,
                metadata={"matrix_seen_event_ids": ["team-event"]},
            ),
        ],
    )
    update_scope_seen_event_ids(session, team_scope, ["preserved-team-event"])

    assert read_scope_seen_event_ids(session, agent_scope) == {"agent-event"}
    assert read_scope_seen_event_ids(session, team_scope) == {"team-event", "preserved-team-event"}


def test_compaction_progress_preserves_newer_seen_event_ids(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    persisted_session = _session("session-1")
    working_session = _session("session-1")
    latest_session = _session("session-1")
    update_scope_seen_event_ids(working_session, scope, ["compacted-event"])
    update_scope_seen_event_ids(latest_session, scope, ["newer-event"])
    storage.upsert_session(latest_session)

    _persist_compaction_progress(
        storage=storage,
        persisted_session=persisted_session,
        working_session=working_session,
        compacted_run_ids=set(),
    )

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert read_scope_seen_event_ids(persisted, scope) == {"compacted-event", "newer-event"}


@pytest.mark.asyncio
async def test_prepare_history_for_run_compaction_preserves_seen_event_ids(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(enabled=True),
        context_window=64_000,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            RunOutput(
                run_id="run-1",
                agent_id="test_agent",
                status=RunStatus.completed,
                metadata={
                    "matrix_seen_event_ids": ["event-1", "event-2"],
                    "matrix_response_event_id": "response-1",
                },
            ),
            RunOutput(
                run_id="run-2",
                agent_id="test_agent",
                status=RunStatus.completed,
                metadata={
                    "matrix_seen_event_ids": ["event-3"],
                    "matrix_response_event_id": "response-2",
                },
            ),
            RunOutput(
                run_id="run-3",
                agent_id="test_agent",
                status=RunStatus.completed,
                metadata={
                    "matrix_seen_event_ids": ["event-4"],
                    "matrix_response_event_id": "response-3",
                },
            ),
            RunOutput(
                run_id="run-4",
                agent_id="test_agent",
                status=RunStatus.completed,
                metadata={
                    "matrix_seen_event_ids": ["event-5"],
                    "matrix_response_event_id": "response-4",
                },
            ),
        ],
    )
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    write_scope_state(session, scope, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(session)

    with (
        patch(
            "mindroom.model_loading.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch(
            "mindroom.history.compaction._generate_compaction_summary",
            new=AsyncMock(
                return_value=SessionSummary(summary="merged summary", updated_at=datetime.now(UTC)),
            ),
        ),
    ):
        await prepare_history_for_run(
            agent=_agent(db=storage),
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
        )

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert read_scope_seen_event_ids(persisted, scope) == {
        "event-1",
        "event-2",
        "event-3",
        "event-4",
        "event-5",
        "response-1",
        "response-2",
        "response-3",
        "response-4",
    }


@pytest.mark.asyncio
async def test_native_agno_replays_recent_raw_history_without_persisting_replay(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    storage.upsert_session(
        _session(
            "session-1",
            runs=[
                _completed_run("run-1"),
                _completed_run("run-2"),
            ],
            summary=SessionSummary(summary="stored summary", updated_at=datetime.now(UTC)),
        ),
    )
    model = RecordingModel(id="recording-model", provider="fake")
    agent = _agent(
        model=model,
        db=storage,
        num_history_runs=1,
    )

    response = await agent.arun("Current prompt", session_id="session-1")

    assert response.content == "ok"
    assert [message.role for message in model.seen_messages[:2]] == ["user", "assistant"]
    assert "stored summary" not in str(model.seen_messages)
    assert [message.content for message in model.seen_messages[:2]] == [
        "run-2 question",
        "run-2 answer",
    ]
    assert [message.from_history for message in model.seen_messages[:2]] == [True, True]
    assert model.seen_messages[-1].role == "user"
    assert model.seen_messages[-1].content == "Current prompt"

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    latest_run = persisted.runs[-1]
    assert isinstance(latest_run, RunOutput)
    assert [message.content for message in latest_run.messages or []] == [
        "Current prompt",
        "ok",
    ]
    assert all(message.from_history is False for message in latest_run.messages or [])
    assert latest_run.additional_input in (None, [])


@pytest.mark.asyncio
async def test_prepare_agent_and_prompt_uses_native_history_with_unseen_thread_context(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path, num_history_runs=1)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[_completed_run("run-1"), _completed_run("run-2")],
        summary=SessionSummary(summary="stored summary", updated_at=datetime.now(UTC)),
    )
    update_scope_seen_event_ids(session, HistoryScope(kind="agent", scope_id="test_agent"), ["event-1"])
    storage.upsert_session(session)

    recording_model = RecordingModel(id="recording-model", provider="fake")
    live_agent = _agent(model=recording_model, db=storage, num_history_runs=1)

    with open_scope_session_context(
        agent=live_agent,
        agent_name="test_agent",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
    ) as scope_context:
        assert scope_context is not None
        with (
            patch("mindroom.ai.create_agent", return_value=live_agent),
            patch("mindroom.ai.build_memory_prompt_parts", new=AsyncMock(return_value=MemoryPromptParts())),
        ):
            prepared_run = await _prepare_agent_and_prompt(
                "test_agent",
                "Current prompt",
                runtime_paths,
                config,
                scope_context=scope_context,
                thread_history=[
                    make_visible_message(event_id="event-1", sender="alice", body="Already seen"),
                    make_visible_message(event_id="event-2", sender="alice", body="Fresh follow-up"),
                    make_visible_message(event_id="event-3", sender="alice", body="Current message body"),
                ],
                reply_to_event_id="event-3",
            )

    agent = prepared_run.agent
    full_prompt = prepared_run.prompt_text
    unseen_event_ids = prepared_run.unseen_event_ids
    prepared = prepared_run.prepared_history
    assert unseen_event_ids == ["event-2"]
    assert prepared.replays_persisted_history is True
    assert "Fresh follow-up" in full_prompt
    assert "Already seen" not in full_prompt
    assert "stored summary" not in full_prompt
    assert "<history_context>" not in full_prompt

    response = await agent.arun(prepared_run.run_input, session_id="session-1")

    assert response.content == "ok"
    assert [message.role for message in recording_model.seen_messages[:2]] == ["user", "assistant"]
    assert "stored summary" not in str(recording_model.seen_messages)
    assert [message.content for message in recording_model.seen_messages[:2]] == [
        "run-2 question",
        "run-2 answer",
    ]

    unseen_user_message = recording_model.seen_messages[-2]
    assert unseen_user_message.role == "user"
    assert unseen_user_message.content == "alice: Fresh follow-up"

    final_user_message = recording_model.seen_messages[-1]
    assert final_user_message.role == "user"
    assert final_user_message.content == "Current prompt"


@pytest.mark.asyncio
async def test_prepare_agent_and_prompt_keeps_prior_request_message_prefix_byte_identical(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(tmp_path, num_history_runs=10)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    recording_model = RecordingModel(id="recording-model", provider="fake")
    recorded_requests: list[list[dict[str, str]]] = []

    prompt_parts_by_prompt = {
        "First prompt": MemoryPromptParts(
            session_preamble="[File memory entrypoint (agent)]\nStable MEMORY.md",
            turn_context="turn context one",
        ),
        "Second prompt": MemoryPromptParts(
            session_preamble="[File memory entrypoint (agent)]\nStable MEMORY.md",
            turn_context="turn context two",
        ),
        "Third prompt": MemoryPromptParts(
            session_preamble="[File memory entrypoint (agent)]\nStable MEMORY.md",
            turn_context="turn context three",
        ),
    }

    async def fake_build_memory_prompt_parts(
        prompt: str,
        *_args: object,
        **_kwargs: object,
    ) -> MemoryPromptParts:
        return prompt_parts_by_prompt[prompt]

    def create_agent_stub(*_args: object, **_kwargs: object) -> Agent:
        return _agent(
            model=recording_model,
            db=storage,
            num_history_runs=10,
        )

    with (
        patch(
            "mindroom.ai.create_agent",
            side_effect=create_agent_stub,
        ),
        patch(
            "mindroom.ai.build_memory_prompt_parts",
            new=AsyncMock(side_effect=fake_build_memory_prompt_parts),
        ),
    ):
        for prompt in ("First prompt", "Second prompt", "Third prompt"):
            prepared_run = await _prepare_agent_and_prompt(
                "test_agent",
                prompt,
                runtime_paths,
                config,
                session_id="session-1",
            )
            await prepared_run.agent.arun(prepared_run.run_input, session_id="session-1")
            recorded_requests.append(
                [
                    {
                        "role": message.role,
                        "content": str(message.content),
                    }
                    for message in recording_model.seen_messages
                ],
            )

    second_request = recorded_requests[1]
    third_request = recorded_requests[2]

    assert stable_serialize(second_request) == stable_serialize(third_request[: len(second_request)])
    assert third_request[-1]["content"] == "Third prompt\n\nturn context three"


@pytest.mark.asyncio
async def test_prepare_agent_and_prompt_strips_timestamped_current_turn_duplication_from_model_prompt(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(tmp_path, num_history_runs=10)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    recording_model = RecordingModel(id="recording-model", provider="fake")
    recorded_requests: list[list[dict[str, str]]] = []

    prompt_parts_by_prompt = {
        "First prompt": MemoryPromptParts(
            session_preamble="[File memory entrypoint (agent)]\nStable MEMORY.md",
            turn_context="turn context one",
        ),
        "Second prompt": MemoryPromptParts(
            session_preamble="[File memory entrypoint (agent)]\nStable MEMORY.md",
            turn_context="turn context two",
        ),
        "Third prompt": MemoryPromptParts(
            session_preamble="[File memory entrypoint (agent)]\nStable MEMORY.md",
            turn_context="turn context three",
        ),
    }
    model_prompt_by_prompt = {
        "First prompt": (
            "[2026-03-20 08:15 PDT] First prompt\n\n"
            "Available attachment IDs: att_1. Use tool calls to inspect or process them."
        ),
        "Second prompt": (
            "[2026-03-20 08:16 PDT] Second prompt\n\n"
            "Available attachment IDs: att_2. Use tool calls to inspect or process them."
        ),
        "Third prompt": (
            "[2026-03-20 08:17 PDT] Third prompt\n\n"
            "Available attachment IDs: att_3. Use tool calls to inspect or process them."
        ),
    }

    async def fake_build_memory_prompt_parts(
        prompt: str,
        *_args: object,
        **_kwargs: object,
    ) -> MemoryPromptParts:
        return prompt_parts_by_prompt[prompt]

    def create_agent_stub(*_args: object, **_kwargs: object) -> Agent:
        return _agent(
            model=recording_model,
            db=storage,
            num_history_runs=10,
        )

    with (
        patch(
            "mindroom.ai.create_agent",
            side_effect=create_agent_stub,
        ),
        patch(
            "mindroom.ai.build_memory_prompt_parts",
            new=AsyncMock(side_effect=fake_build_memory_prompt_parts),
        ),
    ):
        for prompt in ("First prompt", "Second prompt", "Third prompt"):
            prepared_run = await _prepare_agent_and_prompt(
                "test_agent",
                prompt,
                runtime_paths,
                config,
                session_id="session-1",
                model_prompt=model_prompt_by_prompt[prompt],
            )
            await prepared_run.agent.arun(prepared_run.run_input, session_id="session-1")
            recorded_requests.append(
                [
                    {
                        "role": message.role,
                        "content": str(message.content),
                    }
                    for message in recording_model.seen_messages
                ],
            )

    second_request = recorded_requests[1]
    third_request = recorded_requests[2]

    assert stable_serialize(second_request) == stable_serialize(third_request[: len(second_request)])
    assert third_request[-1]["content"] == (
        "Third prompt\n\n"
        "turn context three\n\n"
        "Available attachment IDs: att_3. Use tool calls to inspect or process them."
    )
