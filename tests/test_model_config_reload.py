"""Model reloads reach the next response without replacing Matrix bots."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from mindroom.agents import create_agent
from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import CompactionConfig, CompactionOverrideConfig, DefaultsConfig, ModelConfig
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.history.policy import resolve_history_execution_plan
from mindroom.history.runtime import close_team_runtime_state_dbs, resolve_agent_preparation_inputs
from mindroom.matrix.users import AgentMatrixUser
from mindroom.model_loading import get_model_instance
from mindroom.orchestrator import _MultiAgentOrchestrator
from mindroom.teams import TeamMode, build_materialized_team_instance, materialize_exact_team_members
from tests.bot_helpers import make_test_agent_bot, make_test_team_bot
from tests.conftest import TEST_PASSWORD, bind_runtime_paths, orchestrator_runtime_paths, write_config_yaml
from tests.identity_helpers import entity_ids

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path

    from agno.agent import Agent
    from agno.models.base import Model
    from agno.team import Team

    from mindroom.history.types import ResolvedHistoryExecutionPlan


@pytest_asyncio.fixture
async def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_MultiAgentOrchestrator]:
    """Run config publication against real bots, forbidding restarts and Matrix presence I/O."""
    runtime_paths = orchestrator_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General", tools=[])},
            teams={"team": TeamConfig(display_name="Team", role="Coordinate", agents=["general"], model="other")},
            defaults=DefaultsConfig(tools=[], compaction=CompactionConfig()),
            models={
                name: ModelConfig(
                    provider="openai",
                    api="chat_completions",
                    id=f"{name}-model",
                    context_window=4000 if name == "default" else 64000,
                    extra_kwargs={"api_key": "test-placeholder", "temperature": 0.1},
                )
                for name in ("default", "other", "summary", "fallback")
            },
        ),
        runtime_paths,
    )
    orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths)
    orchestrator.config = config
    matrix_ids = entity_ids(config, runtime_paths)
    for name in ("general", "team", ROUTER_AGENT_NAME):
        user = AgentMatrixUser(
            agent_name=name,
            user_id=matrix_ids[name].full_id,
            display_name=name,
            password=TEST_PASSWORD,
        )
        kwargs = {
            "agent_user": user,
            "storage_path": runtime_paths.storage_root,
            "config": config,
            "runtime_paths": runtime_paths,
        }
        bot = make_test_team_bot(**kwargs, team_model="other") if name == "team" else make_test_agent_bot(**kwargs)
        bot.orchestrator = orchestrator
        bot.running = True
        orchestrator.agent_bots[name] = bot
    monkeypatch.setattr(AgentBot, "_set_presence_with_model_info", AsyncMock())
    monkeypatch.setattr(
        orchestrator,
        "_restart_changed_entities",
        AsyncMock(side_effect=AssertionError("Model-only reload must preserve Matrix bots")),
    )
    try:
        yield orchestrator
    finally:
        await orchestrator.stop()


async def _reload(runtime: _MultiAgentOrchestrator, config: Config) -> None:
    """Load edited YAML through the production reload lifecycle and retain every bot."""
    bots = dict(runtime.agent_bots)
    write_config_yaml(config, runtime.runtime_paths.config_path)
    assert await runtime.config_reload._update_config() is False
    assert runtime.config is not None
    assert runtime.config.authored_model_dump() == config.authored_model_dump()
    for name, bot in bots.items():
        assert runtime.agent_bots[name] is bot
        assert bot.running
        assert bot.config is runtime.config


@contextmanager
def _materialize(runtime: _MultiAgentOrchestrator) -> Iterator[tuple[Agent, Agent, Team, Model]]:
    """Build the next standalone, member, coordinator, and routing models."""
    config = runtime.config
    assert config is not None
    agent = create_agent("general", config, runtime.runtime_paths, execution_identity=None)
    agents = [agent]
    try:
        members = materialize_exact_team_members(
            ["general"],
            config=config,
            runtime_paths=runtime.runtime_paths,
            execution_identity=None,
        )
        agents.extend(members.agents)
        member = members.agents[0]
        team = build_materialized_team_instance(
            requested_agent_names=["general"],
            agents=members.agents,
            mode=TeamMode.COORDINATE,
            config=config,
            runtime_paths=runtime.runtime_paths,
            scope_context=None,
            model_name=config.resolve_entity("team").model_name,
            configured_team_name="team",
            execution_identity=None,
        )
        router_model = get_model_instance(config, runtime.runtime_paths, config.router.model)
        yield agent, member, team, router_model
    finally:
        close_team_runtime_state_dbs(agents=agents, team_db=None)


def _history_plan(config: Config, entity_name: str) -> ResolvedHistoryExecutionPlan:
    """Resolve effective model and compaction budgets for one response scope."""
    entity = config.resolve_entity(entity_name)
    model = config.resolve_runtime_model(entity_name=entity_name)
    return resolve_history_execution_plan(
        config=config,
        compaction_config=entity.compaction_config,
        has_authored_compaction_config=entity.has_authored_compaction_config,
        active_model_name=model.model_name,
        active_context_window=model.context_window,
        static_prompt_tokens=0,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("context_only", [True, False])
async def test_model_reload_updates_next_response_without_restarting_bots(
    runtime: _MultiAgentOrchestrator,
    *,
    context_only: bool,
) -> None:
    """Model fields and replay budgets propagate through real YAML reloads and model factories."""
    old = runtime.config
    assert old is not None
    with _materialize(runtime) as (old_agent, old_member, old_team, old_router):
        assert old_agent.model is not None
        assert old_agent.model.id == "default-model"
        before = resolve_agent_preparation_inputs(
            agent=old_agent,
            agent_name="general",
            full_prompt="",
            config=old,
            static_prompt_tokens=0,
        ).execution_plan
        assert before.replay_window_tokens == 4000
        assert not before.destructive_compaction_available

        new = old.model_copy(deep=True)
        new.models["default"].context_window = 64000
        if not context_only:
            new.models["default"].id = "updated-model"
            new.models["default"].extra_kwargs = {"api_key": "test-placeholder", "temperature": 0.7}
            new.models["other"].id = "updated-team-model"
        await _reload(runtime, new)

        assert runtime.config is not None
        with _materialize(runtime) as (agent, member, team, router):
            assert agent is not old_agent
            assert member is not old_member
            assert team is not old_team
            assert router is not old_router
            expected_id = "default-model" if context_only else "updated-model"
            for model in (agent.model, member.model, router):
                assert model is not None
                assert model.id == expected_id
                assert model.temperature == (0.1 if context_only else 0.7)
            assert team.model is not None
            assert team.model.id == ("other-model" if context_only else "updated-team-model")
            after = resolve_agent_preparation_inputs(
                agent=agent,
                agent_name="general",
                full_prompt="",
                config=runtime.config,
                static_prompt_tokens=0,
            ).execution_plan
            assert after.replay_window_tokens == 64000
            assert after.compaction_context_window == 64000
            assert after.destructive_compaction_available


@pytest.mark.asyncio
@pytest.mark.parametrize("model_field", ["model", "fallback_model"])
@pytest.mark.parametrize("inherited", [True, False])
async def test_compaction_model_reload_updates_effective_budgets(
    runtime: _MultiAgentOrchestrator,
    model_field: str,
    *,
    inherited: bool,
) -> None:
    """Explicit and inherited summary/fallback definitions update both agent and team scopes."""
    old = runtime.config
    assert old is not None
    compaction = {"model": "summary", "fallback_model": "fallback"}
    if inherited:
        old.defaults.compaction = CompactionConfig.model_validate(compaction)
    else:
        old.agents["general"].compaction = CompactionOverrideConfig.model_validate(compaction)
        old.teams["team"].compaction = CompactionOverrideConfig.model_validate(compaction)
    before = {name: _history_plan(old, name) for name in ("general", "team")}
    model_name = compaction[model_field]
    new = old.model_copy(deep=True)
    new.models[model_name].context_window = 96000
    new.models[model_name].id = "updated-summary-model"
    await _reload(runtime, new)

    assert runtime.config is not None
    assert get_model_instance(runtime.config, runtime.runtime_paths, model_name).id == "updated-summary-model"
    for name in ("general", "team"):
        after = _history_plan(runtime.config, name)
        assert after.replay_window_tokens == before[name].replay_window_tokens
        if model_field == "model":
            assert after.compaction_context_window == 96000
            assert after.summary_input_budget_tokens is not None
            assert before[name].summary_input_budget_tokens is not None
            assert after.summary_input_budget_tokens > before[name].summary_input_budget_tokens
        else:
            assert after.compaction_context_window == before[name].compaction_context_window
            assert after.compaction_fallback_summary_input_budget_tokens is not None
            assert before[name].compaction_fallback_summary_input_budget_tokens is not None
            assert (
                after.compaction_fallback_summary_input_budget_tokens
                > before[name].compaction_fallback_summary_input_budget_tokens
            )


@pytest.mark.asyncio
async def test_context_window_reload_preserves_explicit_replay_cap(runtime: _MultiAgentOrchestrator) -> None:
    """Increasing provider capacity does not remove an authored, smaller replay cap."""
    old = runtime.config
    assert old is not None
    old.defaults.compaction = CompactionConfig(replay_window_tokens=3000)
    new = old.model_copy(deep=True)
    new.models["default"].context_window = 64000
    await _reload(runtime, new)

    assert runtime.config is not None
    plan = _history_plan(runtime.config, "general")
    assert plan.replay_window_tokens == 3000
    assert plan.compaction_context_window == 64000
    assert plan.destructive_compaction_available
