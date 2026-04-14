"""Tests for the next-run `compact_context` trigger."""

from __future__ import annotations

import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, get_type_hints
from unittest.mock import AsyncMock, Mock, patch

import pytest
from agno.agent import Agent
from agno.models.base import Model
from agno.models.response import ModelResponse
from agno.run import RunContext
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.session.agent import AgentSession
from agno.session.summary import SessionSummary
from agno.tools.function import Function

from mindroom.agents import create_session_storage, get_agent_session
from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import CompactionConfig, DefaultsConfig, ModelConfig
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.custom_tools.compact_context import CompactContextTools
from mindroom.delivery_gateway import CompactionNoticeRequest
from mindroom.history import prepare_history_for_run
from mindroom.history.runtime import ScopeSessionContext, open_scope_session_context
from mindroom.history.storage import read_scope_state, write_scope_state
from mindroom.history.types import CompactionOutcome, HistoryScope, HistoryScopeState
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from tests.conftest import TEST_PASSWORD, bind_runtime_paths, install_runtime_cache_support, make_event_cache_mock

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path


class FakeModel(Model):
    """Minimal model for tool/runtime tests."""

    def invoke(self, *_args: object, **_kwargs: object) -> ModelResponse:
        """Return one successful fake response."""
        return ModelResponse(content="ok")

    async def ainvoke(self, *_args: object, **_kwargs: object) -> ModelResponse:
        """Return one successful fake async response."""
        return ModelResponse(content="ok")

    def invoke_stream(self, *_args: object, **_kwargs: object) -> Iterator[ModelResponse]:
        """Yield one successful fake streaming response."""
        yield ModelResponse(content="ok")

    async def ainvoke_stream(self, *_args: object, **_kwargs: object) -> AsyncIterator[ModelResponse]:
        """Yield one successful fake async streaming response."""
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


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
        },
    )


def _make_config(tmp_path: Path) -> tuple[Config, RuntimePaths]:
    return _make_config_with_context_window(tmp_path, context_window=48_000)


def _make_config_with_context_window(tmp_path: Path, *, context_window: int | None) -> tuple[Config, RuntimePaths]:
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "test_agent": AgentConfig(
                    display_name="Test Agent",
                ),
            },
            defaults=DefaultsConfig(tools=[]),
            models={"default": ModelConfig(provider="openai", id="test-model", context_window=context_window)},
        ),
        runtime_paths,
    )
    return config, runtime_paths


def _completed_run(run_id: str, *, agent_id: str) -> RunOutput:
    return RunOutput(
        run_id=run_id,
        agent_id=agent_id,
        status=RunStatus.completed,
    )


def _session(session_id: str, *, runs: list[RunOutput] | None = None) -> AgentSession:
    return AgentSession(
        session_id=session_id,
        runs=runs or [],
        created_at=1,
        updated_at=1,
    )


def _agent(*, team_id: str | None = None) -> Agent:
    agent = Agent(id="test_agent", model=FakeModel(id="fake-model", provider="fake"))
    agent.team_id = team_id
    return agent


@contextmanager
def _open_scope_context(
    *,
    agent: Agent,
    agent_name: str,
    session_id: str,
    runtime_paths: RuntimePaths,
    config: Config,
    create_session_if_missing: bool = False,
) -> Iterator[ScopeSessionContext]:
    with open_scope_session_context(
        agent=agent,
        agent_name=agent_name,
        session_id=session_id,
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        create_session_if_missing=create_session_if_missing,
    ) as scope_context:
        assert scope_context is not None
        yield scope_context


@contextmanager
def _patched_scope_context(scope_context: ScopeSessionContext | SimpleNamespace) -> Iterator[object]:
    try:
        yield scope_context
    finally:
        scope_context.storage.close()


@pytest.fixture(autouse=True)
def _close_test_storages(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Close temporary SQLite handles created directly by compact-context tests."""
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


def test_compact_context_runtime_annotations_resolve_for_agno_registration(tmp_path: Path) -> None:
    """Agno should be able to evaluate tool annotations at runtime."""
    config, runtime_paths = _make_config(tmp_path)
    tool = CompactContextTools(
        agent_name="test_agent",
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=SimpleNamespace(session_id="session-1"),
    )

    function = Function.from_callable(tool.compact_context)

    assert function.name == "compact_context"
    assert "agent" not in function.parameters["properties"]
    assert "run_context" not in function.parameters["properties"]
    get_type_hints(CompactContextTools.compact_context)
    get_type_hints(CompactContextTools._resolve_active_compaction_settings)


@pytest.mark.asyncio
async def test_compact_context_sets_force_flag_for_agent_scope(tmp_path: Path) -> None:
    """Schedule agent-scope compaction for the next reply."""
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    storage.upsert_session(_session("session-1", runs=[_completed_run("run-1", agent_id="test_agent")]))

    tool = CompactContextTools(
        agent_name="test_agent",
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=SimpleNamespace(session_id="session-1"),
    )

    result = await tool.compact_context(agent=_agent())

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    state = read_scope_state(persisted, HistoryScope(kind="agent", scope_id="test_agent"))
    assert state.force_compact_before_next_run is True
    assert result == "Compaction scheduled for the next reply in this conversation scope."


@pytest.mark.asyncio
async def test_compact_context_requires_compaction_window(tmp_path: Path) -> None:
    """Manual compaction should fail fast when no usable model window is configured."""
    config, runtime_paths = _make_config_with_context_window(tmp_path, context_window=None)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    storage.upsert_session(_session("session-1", runs=[_completed_run("run-1", agent_id="test_agent")]))

    tool = CompactContextTools(
        agent_name="test_agent",
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=SimpleNamespace(session_id="session-1"),
    )

    result = await tool.compact_context(agent=_agent())

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    state = read_scope_state(persisted, HistoryScope(kind="agent", scope_id="test_agent"))
    assert state.force_compact_before_next_run is False
    assert result == (
        "Error: Compaction is unavailable for this scope because no context_window is configured on the active model."
    )


@pytest.mark.asyncio
async def test_compact_context_closes_scope_storage_after_budget_error(tmp_path: Path) -> None:
    """Temporary scope storage should always be closed after manual validation fails."""
    config, runtime_paths = _make_config_with_context_window(tmp_path, context_window=None)
    storage = SimpleNamespace(upsert_session=Mock(), close=Mock())
    scope_context = SimpleNamespace(
        scope=HistoryScope(kind="agent", scope_id="test_agent"),
        storage=storage,
        session=_session("session-1", runs=[_completed_run("run-1", agent_id="test_agent")]),
    )
    tool = CompactContextTools(
        agent_name="test_agent",
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=SimpleNamespace(session_id="session-1"),
    )

    with (
        patch(
            "mindroom.custom_tools.compact_context.open_scope_session_context",
            return_value=_patched_scope_context(scope_context),
        ),
        patch.object(
            tool,
            "_resolve_active_compaction_settings",
            return_value=(
                SimpleNamespace(model_name="default", context_window=None),
                config.get_entity_compaction_config("test_agent"),
            ),
        ),
    ):
        result = await tool.compact_context(agent=_agent())

    assert result == (
        "Error: Compaction is unavailable for this scope because no context_window is configured on the active model."
    )
    storage.close.assert_called_once_with()


@pytest.mark.asyncio
async def test_compact_context_closes_scope_storage_after_success(tmp_path: Path) -> None:
    """Temporary scope storage should be closed after a successful scheduling request."""
    config, runtime_paths = _make_config(tmp_path)
    storage = SimpleNamespace(upsert_session=Mock(), close=Mock())
    scope_context = SimpleNamespace(
        scope=HistoryScope(kind="agent", scope_id="test_agent"),
        storage=storage,
        session=_session("session-1", runs=[_completed_run("run-1", agent_id="test_agent")]),
    )
    tool = CompactContextTools(
        agent_name="test_agent",
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=SimpleNamespace(session_id="session-1"),
    )

    with (
        patch(
            "mindroom.custom_tools.compact_context.open_scope_session_context",
            return_value=_patched_scope_context(scope_context),
        ),
        patch.object(
            tool,
            "_resolve_active_compaction_settings",
            return_value=(
                SimpleNamespace(model_name="default", context_window=48_000),
                config.get_entity_compaction_config("test_agent"),
            ),
        ),
    ):
        result = await tool.compact_context(agent=_agent())

    assert result == "Compaction scheduled for the next reply in this conversation scope."
    storage.upsert_session.assert_called_once_with(scope_context.session)
    storage.close.assert_called_once_with()


@pytest.mark.asyncio
async def test_compact_context_requires_positive_summary_input_budget(tmp_path: Path) -> None:
    """Manual compaction should fail fast when the compaction model cannot fit any summary input."""
    config, runtime_paths = _make_config_with_context_window(tmp_path, context_window=4096)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    storage.upsert_session(_session("session-1", runs=[_completed_run("run-1", agent_id="test_agent")]))

    tool = CompactContextTools(
        agent_name="test_agent",
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=SimpleNamespace(session_id="session-1"),
    )

    result = await tool.compact_context(agent=_agent())

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    state = read_scope_state(persisted, HistoryScope(kind="agent", scope_id="test_agent"))
    assert state.force_compact_before_next_run is False
    assert result == (
        "Error: Compaction is unavailable for this scope because the active compaction model leaves no "
        "usable summary input budget after reserve and prompt overhead."
    )


@pytest.mark.asyncio
async def test_compact_context_can_use_compaction_model_window_when_active_model_has_none(tmp_path: Path) -> None:
    """Manual compaction should work when only the selected compaction model declares a context window."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"test_agent": AgentConfig(display_name="Test Agent")},
            defaults=DefaultsConfig(
                tools=[],
                compaction=CompactionConfig(model="summary-model"),
            ),
            models={
                "default": ModelConfig(provider="openai", id="test-model", context_window=None),
                "summary-model": ModelConfig(provider="openai", id="summary-model", context_window=32_000),
            },
        ),
        runtime_paths,
    )
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1", agent_id="test_agent"),
            _completed_run("run-2", agent_id="test_agent"),
            _completed_run("run-3", agent_id="test_agent"),
            _completed_run("run-4", agent_id="test_agent"),
        ],
    )
    storage.upsert_session(session)

    tool = CompactContextTools(
        agent_name="test_agent",
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=SimpleNamespace(session_id="session-1"),
    )

    result = await tool.compact_context(agent=_agent())
    assert result == "Compaction scheduled for the next reply in this conversation scope."

    with (
        patch(
            "mindroom.ai.get_model_instance",
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
            agent=_agent(),
            agent_name="test_agent",
            full_prompt="Current question",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
        )

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.summary == "merged summary"
    state = read_scope_state(persisted, HistoryScope(kind="agent", scope_id="test_agent"))
    assert state.force_compact_before_next_run is False
    assert len(prepared.compaction_outcomes) == 1
    outcome = prepared.compaction_outcomes[0]
    assert outcome.window_tokens == 0
    assert outcome.history_budget_tokens is None
    assert outcome.to_notice_metadata()["version"] == 1
    assert outcome.to_notice_metadata()["window_tokens"] == 0
    assert "history budget" not in outcome.format_notice()
    assert "/ 0 " not in outcome.format_notice()


@pytest.mark.asyncio
async def test_send_compaction_notice_omits_zero_breakdown_fields_in_html_body(tmp_path: Path) -> None:
    """Bot notices should reuse the zero-filtered notice text for both plain and HTML bodies."""
    config, runtime_paths = _make_config(tmp_path)
    bot = AgentBot(
        agent_user=AgentMatrixUser(
            agent_name="test_agent",
            password=TEST_PASSWORD,
            display_name="Test Agent",
            user_id="@mindroom_test_agent:localhost",
        ),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths,
        rooms=["!room:localhost"],
    )
    bot.client = AsyncMock()
    install_runtime_cache_support(bot)
    outcome = CompactionOutcome(
        mode="auto",
        session_id="session-1",
        scope="agent:test_agent",
        summary="Merged summary",
        summary_model="summary-model",
        before_tokens=30_000,
        after_tokens=12_000,
        window_tokens=100_000,
        threshold_tokens=80_000,
        reserve_tokens=4_096,
        runs_before=20,
        runs_after=8,
        compacted_run_count=12,
        compacted_at="2026-01-01T00:00:00Z",
        notify=True,
        history_budget_tokens=100_000,
        role_instructions_tokens=0,
        tool_definition_tokens=0,
        current_prompt_tokens=62,
    )

    with patch("mindroom.delivery_gateway.send_message", new=AsyncMock(return_value="$notice")) as mock_send:
        event_id = await bot._delivery_gateway.send_compaction_notice(
            CompactionNoticeRequest(
                target=MessageTarget.resolve("!room:localhost", None, "$incoming"),
                main_response_event_id="$reply",
                outcome=outcome,
            ),
        )

    assert event_id == "$notice"
    assert mock_send.await_args is not None
    sent_content = mock_send.await_args.args[2]
    assert sent_content["io.mindroom.compaction"]["version"] == 2
    assert sent_content["io.mindroom.compaction"]["history_budget_tokens"] == 100_000
    assert sent_content["body"] == outcome.format_notice()
    assert sent_content["body"] == (
        "\U0001f4e6 Compacted 12 runs: 30,000 \u2192 12,000 / 100,000 history budget\n   Overhead: 62 prompt"
    )
    assert sent_content["formatted_body"] == (
        "<em>\U0001f4e6 Compacted 12 runs: 30,000 \u2192 12,000 / 100,000 history budget<br/>"
        "   Overhead: 62 prompt</em>"
    )


@pytest.mark.asyncio
async def test_compact_context_sets_force_flag_for_team_scope_only(tmp_path: Path) -> None:
    """Only the team scope should receive the forced-compaction flag."""
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    storage.upsert_session(_session("session-1", runs=[_completed_run("run-1", agent_id="test_agent")]))

    tool = CompactContextTools(
        agent_name="test_agent",
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=SimpleNamespace(session_id="session-1"),
    )

    team_agent = _agent(team_id="team-123")
    with _open_scope_context(
        agent=team_agent,
        agent_name="test_agent",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        create_session_if_missing=True,
    ) as team_context:
        assert team_context.session is not None
        team_context.storage.upsert_session(team_context.session)
    await tool.compact_context(agent=team_agent)

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    direct_state = read_scope_state(persisted, HistoryScope(kind="agent", scope_id="test_agent"))
    with _open_scope_context(
        agent=team_agent,
        agent_name="test_agent",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
    ) as reloaded_team_context:
        assert reloaded_team_context.session is not None
        team_state = read_scope_state(reloaded_team_context.session, HistoryScope(kind="team", scope_id="team-123"))
    assert direct_state.force_compact_before_next_run is False
    assert team_state.force_compact_before_next_run is True


@pytest.mark.asyncio
async def test_prepare_history_for_run_clears_forced_flag_when_no_visible_runs(tmp_path: Path) -> None:
    """Forced compaction clears itself when the scope has no visible runs to compact."""
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session("session-1")
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    write_scope_state(
        session,
        scope,
        HistoryScopeState(force_compact_before_next_run=True),
    )
    storage.upsert_session(session)

    summary_mock = AsyncMock()
    with (
        patch(
            "mindroom.ai.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch(
            "mindroom.history.compaction._generate_compaction_summary",
            new=summary_mock,
        ),
    ):
        prepared = await prepare_history_for_run(
            agent=_agent(),
            agent_name="test_agent",
            full_prompt="Current question",
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
    state = read_scope_state(persisted, scope)
    assert state.force_compact_before_next_run is False
    assert prepared.compaction_outcomes == []
    summary_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_prepare_history_for_run_forced_compaction_compacts_single_run(tmp_path: Path) -> None:
    """Forced compaction of a single run produces a summary and clears the flag."""
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1", agent_id="test_agent"),
        ],
    )
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    write_scope_state(
        session,
        scope,
        HistoryScopeState(force_compact_before_next_run=True),
    )
    storage.upsert_session(session)

    agent = _agent()
    with (
        patch(
            "mindroom.ai.get_model_instance",
            return_value=FakeModel(id="summary-model", provider="fake"),
        ),
        patch(
            "mindroom.history.compaction._generate_compaction_summary",
            new=AsyncMock(
                return_value=SessionSummary(summary="single run summary", updated_at=datetime.now(UTC)),
            ),
        ),
    ):
        prepared = await prepare_history_for_run(
            agent=agent,
            agent_name="test_agent",
            full_prompt="Current question",
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
    assert persisted.summary.summary == "single run summary"
    assert persisted.runs == []
    state = read_scope_state(persisted, scope)
    assert state.force_compact_before_next_run is False
    assert state.last_compacted_run_count == 1
    assert len(prepared.compaction_outcomes) == 1


@pytest.mark.asyncio
async def test_compact_context_persists_pending_force_flag_across_stale_run_save(tmp_path: Path) -> None:
    """Current-run session saves should not erase a compact_context request before the next run."""
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run("run-1", agent_id="test_agent"),
            _completed_run("run-2", agent_id="test_agent"),
        ],
    )
    storage.upsert_session(session)

    tool = CompactContextTools(
        agent_name="test_agent",
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=SimpleNamespace(session_id="session-1"),
    )
    live_session_state: dict[str, object] = {}
    run_context = RunContext(run_id="run-123", session_id="session-1", session_state=live_session_state)
    stale_live_session = _session(
        "session-1",
        runs=[
            _completed_run("run-1", agent_id="test_agent"),
            _completed_run("run-2", agent_id="test_agent"),
        ],
    )
    stale_live_session.metadata = {}
    stale_live_session.session_data = {"session_state": live_session_state}

    result = await tool.compact_context(agent=_agent(), run_context=run_context)
    assert result == "Compaction scheduled for the next reply in this conversation scope."
    assert run_context.session_state is live_session_state
    storage.upsert_session(stale_live_session)

    with (
        patch(
            "mindroom.ai.get_model_instance",
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
            agent=_agent(),
            agent_name="test_agent",
            full_prompt="Current question",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            storage=storage,
        )

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    state = read_scope_state(persisted, HistoryScope(kind="agent", scope_id="test_agent"))
    assert state.force_compact_before_next_run is False
    assert persisted.summary is not None
    assert persisted.summary.summary == "merged summary"
    assert len(prepared.compaction_outcomes) == 1


@pytest.mark.asyncio
async def test_compact_context_uses_stable_team_scope_storage(tmp_path: Path) -> None:
    """Team-scoped compaction should persist through the stable team session store."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "alpha": AgentConfig(display_name="Alpha"),
                "beta": AgentConfig(display_name="Beta"),
            },
            defaults=DefaultsConfig(tools=[]),
            models={"default": ModelConfig(provider="openai", id="test-model", context_window=48_000)},
        ),
        runtime_paths,
    )
    legacy_storage = create_session_storage("alpha", config, runtime_paths, execution_identity=None)
    legacy_storage.upsert_session(_session("session-1", runs=[_completed_run("run-1", agent_id="alpha")]))

    tool = CompactContextTools(
        agent_name="beta",
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=SimpleNamespace(session_id="session-1"),
    )
    agent = Agent(id="beta", model=FakeModel(id="fake-model", provider="fake"))
    agent.team_id = "team-123"
    agent.__dict__["_mindroom_team_scope_owner_agent_name"] = "alpha"

    result = await tool.compact_context(agent=agent)

    with _open_scope_context(
        agent=agent,
        agent_name="beta",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
    ) as team_context:
        assert team_context.session is not None
        team_state = read_scope_state(team_context.session, HistoryScope(kind="team", scope_id="team-123"))
    assert team_state.force_compact_before_next_run is True
    assert result == "Compaction scheduled for the next reply in this conversation scope."


@pytest.mark.asyncio
async def test_compact_context_uses_active_team_model_from_runtime_context(tmp_path: Path) -> None:
    """Team-scoped compaction should honor the actual per-run model override."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"test_agent": AgentConfig(display_name="Test Agent")},
            teams={
                "team_123": TeamConfig(
                    display_name="Test Team",
                    role="Coordinate work",
                    agents=["test_agent"],
                ),
            },
            defaults=DefaultsConfig(tools=[]),
            models={
                "default": ModelConfig(provider="openai", id="default-model", context_window=None),
                "large": ModelConfig(provider="openai", id="large-model", context_window=48_000),
            },
        ),
        runtime_paths,
    )
    tool = CompactContextTools(
        agent_name="test_agent",
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=SimpleNamespace(session_id="session-1"),
    )
    team_agent = _agent(team_id="team_123")
    with _open_scope_context(
        agent=team_agent,
        agent_name="test_agent",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        create_session_if_missing=True,
    ) as team_context:
        assert team_context.session is not None
        team_context.session.runs = [_completed_run("run-1", agent_id="test_agent")]
        team_context.storage.upsert_session(team_context.session)

    runtime_context = ToolRuntimeContext(
        agent_name="test_agent",
        room_id="!room:localhost",
        thread_id="thread-1",
        resolved_thread_id="thread-1",
        requester_id="@alice:localhost",
        client=SimpleNamespace(),
        config=config,
        runtime_paths=runtime_paths,
        event_cache=make_event_cache_mock(),
        active_model_name="large",
        session_id="session-1",
    )

    with tool_runtime_context(runtime_context):
        result = await tool.compact_context(agent=team_agent)

    with _open_scope_context(
        agent=team_agent,
        agent_name="test_agent",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
    ) as reloaded_team_context:
        assert reloaded_team_context.session is not None
        team_state = read_scope_state(reloaded_team_context.session, HistoryScope(kind="team", scope_id="team_123"))
    assert team_state.force_compact_before_next_run is True
    assert result == "Compaction scheduled for the next reply in this conversation scope."


@pytest.mark.asyncio
async def test_compact_context_uses_room_resolved_team_model_when_runtime_model_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Team-scoped compaction should reuse the room-aware team model resolver."""
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
                "large": ModelConfig(provider="openai", id="large-model", context_window=48_000),
            },
        ),
        runtime_paths,
    )
    monkeypatch.setattr("mindroom.matrix.rooms.get_room_alias_from_id", lambda *_args: "lobby")

    tool = CompactContextTools(
        agent_name="test_agent",
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=SimpleNamespace(session_id="session-1"),
    )
    team_agent = _agent(team_id="team_123")
    with _open_scope_context(
        agent=team_agent,
        agent_name="test_agent",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        create_session_if_missing=True,
    ) as team_context:
        assert team_context.session is not None
        team_context.session.runs = [_completed_run("run-1", agent_id="test_agent")]
        team_context.storage.upsert_session(team_context.session)

    runtime_context = ToolRuntimeContext(
        agent_name="test_agent",
        room_id="!room:localhost",
        thread_id="thread-1",
        resolved_thread_id="thread-1",
        requester_id="@alice:localhost",
        client=SimpleNamespace(),
        config=config,
        runtime_paths=runtime_paths,
        event_cache=make_event_cache_mock(),
        active_model_name=None,
        session_id="session-1",
    )

    with tool_runtime_context(runtime_context):
        result = await tool.compact_context(agent=team_agent)

    with _open_scope_context(
        agent=team_agent,
        agent_name="test_agent",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
    ) as reloaded_team_context:
        assert reloaded_team_context.session is not None
        team_state = read_scope_state(reloaded_team_context.session, HistoryScope(kind="team", scope_id="team_123"))
    assert team_state.force_compact_before_next_run is True
    assert result == "Compaction scheduled for the next reply in this conversation scope."


@pytest.mark.asyncio
async def test_compact_context_uses_room_resolved_agent_model_when_runtime_model_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent-scoped compaction should reuse the room-aware runtime model resolver."""
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "test_agent": AgentConfig(
                    display_name="Test Agent",
                    model="default",
                ),
            },
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

    tool = CompactContextTools(
        agent_name="test_agent",
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=SimpleNamespace(session_id="session-1"),
    )
    with _open_scope_context(
        agent=_agent(),
        agent_name="test_agent",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        create_session_if_missing=True,
    ) as scope_context:
        assert scope_context.session is not None
        scope_context.session.runs = [_completed_run("run-1", agent_id="test_agent")]
        scope_context.storage.upsert_session(scope_context.session)

    runtime_context = ToolRuntimeContext(
        agent_name="test_agent",
        room_id="!room:localhost",
        thread_id="thread-1",
        resolved_thread_id="thread-1",
        requester_id="@alice:localhost",
        client=SimpleNamespace(),
        config=config,
        runtime_paths=runtime_paths,
        event_cache=make_event_cache_mock(),
        active_model_name=None,
        session_id="session-1",
    )

    with tool_runtime_context(runtime_context):
        result = await tool.compact_context(agent=_agent())

    with _open_scope_context(
        agent=_agent(),
        agent_name="test_agent",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
    ) as reloaded_scope_context:
        assert reloaded_scope_context.session is not None
        agent_state = read_scope_state(
            reloaded_scope_context.session,
            HistoryScope(kind="agent", scope_id="test_agent"),
        )
    assert agent_state.force_compact_before_next_run is True
    assert result == "Compaction scheduled for the next reply in this conversation scope."
