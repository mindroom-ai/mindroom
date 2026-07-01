"""Tests for compaction config/plan resolution, decision classification, and replay planning."""
# ruff: noqa: D103, TC003

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from agno.models.message import Message
from agno.session.summary import SessionSummary

from mindroom.agent_storage import create_session_storage, get_agent_session
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import CompactionConfig, CompactionOverrideConfig, DefaultsConfig, ModelConfig
from mindroom.history.compaction import (
    estimate_prompt_visible_history_tokens,
)
from mindroom.history.policy import (
    classify_compaction_decision,
    context_budget_after_reserve,
    resolve_history_execution_plan,
)
from mindroom.history.runtime import (
    _plan_replay_that_fits,
    apply_replay_plan,
)
from mindroom.history.storage import (
    read_scope_state,
    write_scope_state,
)
from mindroom.history.types import (
    HistoryPolicy,
    HistoryScope,
    HistoryScopeState,
    ResolvedHistoryExecutionPlan,
    ResolvedHistorySettings,
    ResolvedReplayPlan,
)
from mindroom.token_budget import estimate_text_tokens
from tests.conftest import (
    FakeModel,
    bind_runtime_paths,
    prepare_history_for_run_for_test,
)
from tests.history_helpers import (  # noqa: F401
    _agent,
    _close_test_storages,
    _completed_run,
    _make_config,
    _runtime_paths,
    _session,
)


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
    prepared = await prepare_history_for_run_for_test(
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
        prepared = await prepare_history_for_run_for_test(
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
        prepared = await prepare_history_for_run_for_test(
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
        await prepare_history_for_run_for_test(
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
    monkeypatch.setattr("mindroom.matrix.state.get_room_alias_from_id", lambda *_args: "lobby")

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
    monkeypatch.setattr("mindroom.matrix.state.get_room_alias_from_id", lambda *_args: "lobby")

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


@pytest.mark.parametrize(
    ("context_window_tokens", "reserve_tokens", "spent_tokens", "expected"),
    [
        (1_000, 100, 25, 875),
        (1_000, 800, 10, 490),
        (1_000, 100, 2_000, 0),
        (0, 100, 10, 0),
        (-10, 5, 3, 0),
    ],
)
def test_context_budget_after_reserve_preserves_replay_budget_bounds(
    context_window_tokens: int,
    reserve_tokens: int,
    spent_tokens: int,
    expected: int,
) -> None:
    assert context_budget_after_reserve(context_window_tokens, reserve_tokens, spent_tokens) == expected


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
        hard_replay_budget_tokens=10_000,
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

    scope = HistoryScope(kind="agent", scope_id="test_agent")
    history_settings = ResolvedHistorySettings(
        policy=HistoryPolicy(mode="runs", limit=2),
        max_tool_calls_from_history=None,
    )
    replay_plan = _plan_replay_that_fits(
        session=session,
        scope=scope,
        history_settings=history_settings,
        available_history_budget=250,
        current_history_tokens=estimate_prompt_visible_history_tokens(
            session=session,
            scope=scope,
            history_settings=history_settings,
        ),
    )

    assert replay_plan.mode == "limited"
    assert replay_plan.num_history_runs == 1
    assert replay_plan.num_history_messages is None


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

    prepared = await prepare_history_for_run_for_test(
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

    prepared = await prepare_history_for_run_for_test(
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

    prepared = await prepare_history_for_run_for_test(
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
            "mindroom.history.compaction.generate_compaction_summary",
            new=AsyncMock(
                return_value=SessionSummary(summary="merged summary", updated_at=datetime.now(UTC)),
            ),
        ),
    ):
        prepared = await prepare_history_for_run_for_test(
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

    scope = HistoryScope(kind="agent", scope_id="test_agent")
    history_settings = ResolvedHistorySettings(
        policy=HistoryPolicy(mode="all"),
        max_tool_calls_from_history=None,
    )
    replay_plan = _plan_replay_that_fits(
        session=session,
        scope=scope,
        history_settings=history_settings,
        available_history_budget=available_history_budget,
        current_history_tokens=estimate_prompt_visible_history_tokens(
            session=session,
            scope=scope,
            history_settings=history_settings,
        ),
    )
    apply_replay_plan(target=agent, replay_plan=replay_plan)

    assert replay_plan.mode == "disabled"
    assert agent.add_history_to_context is False
    assert agent.num_history_runs is None
    assert agent.num_history_messages is None
