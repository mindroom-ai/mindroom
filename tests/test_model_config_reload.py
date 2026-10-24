"""Model reloads reach the next response without replacing Matrix bots."""

from __future__ import annotations

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
from mindroom.history.runtime import resolve_agent_preparation_inputs
from mindroom.matrix.users import AgentMatrixUser
from mindroom.orchestrator import _MultiAgentOrchestrator
from mindroom.providers.openai.chat import ChatAdapter
from mindroom.providers.registry import build_model_provider
from mindroom.teams.members import BuiltTeam, build_team_spec, materialize_exact_team_members
from mindroom.teams.spec import TeamMode
from mindroom.token_budget import compute_compaction_input_budget
from tests.bot_helpers import make_test_agent_bot, make_test_team_bot
from tests.conftest import TEST_PASSWORD, bind_runtime_paths, orchestrator_runtime_paths, write_config_yaml
from tests.identity_helpers import entity_ids
from tests.providers.bare_adapter import bare_adapter

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.agent_loop.build import AgentBuild
    from mindroom.history.types import ResolvedHistoryExecutionPlan
    from mindroom.llm.provider import ModelProvider


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
        bot = make_test_team_bot(**kwargs) if name == "team" else make_test_agent_bot(**kwargs)
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


def _materialize(runtime: _MultiAgentOrchestrator) -> tuple[AgentBuild, AgentBuild, BuiltTeam, ModelProvider]:
    """Build the next standalone agent, team member, team, and router model."""
    config = runtime.config
    assert config is not None
    agent = create_agent("general", config, runtime.runtime_paths, execution_identity=None)
    members = materialize_exact_team_members(
        ["general"],
        config=config,
        runtime_paths=runtime.runtime_paths,
        execution_identity=None,
    )
    team = build_team_spec(
        members=members,
        mode=TeamMode.COORDINATE,
        config=config,
        runtime_paths=runtime.runtime_paths,
        scope_context=None,
        execution_identity=None,
        model_name=config.resolve_entity("team").model_name,
        configured_team_name="team",
    )
    router = build_model_provider(config, runtime.runtime_paths, config.router.model)
    return agent, members.builds[0], team, router


def _temperature(model: ModelProvider) -> float | None:
    """Return the temperature the model's requests send, from its OpenAI Chat Completions route."""
    adapter = bare_adapter(model)
    assert isinstance(adapter, ChatAdapter)
    return adapter.settings.temperature


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
    old_agent, old_member, old_team, old_router = _materialize(runtime)
    assert old_agent.spec.model.identity.id == "default-model"
    before = resolve_agent_preparation_inputs(
        agent_name="general",
        history_settings=old_agent.history_settings,
        config=old,
        static_prompt_tokens=0,
    ).execution_plan
    assert before.replay_window_tokens == 4000
    assert not before.text_compaction_available

    new = old.model_copy(deep=True)
    new.models["default"].context_window = 64000
    if not context_only:
        new.models["default"].id = "updated-model"
        new.models["default"].extra_kwargs = {"api_key": "test-placeholder", "temperature": 0.7}
        new.models["other"].id = "updated-team-model"
    await _reload(runtime, new)

    assert runtime.config is not None
    agent, member, team, router = _materialize(runtime)
    assert agent is not old_agent
    assert member is not old_member
    assert team is not old_team
    assert router is not old_router
    expected_id = "default-model" if context_only else "updated-model"
    for model in (agent.spec.model, member.spec.model, router):
        assert model.identity.id == expected_id
        assert _temperature(model) == (0.1 if context_only else 0.7)
    assert team.spec.model.identity.id == ("other-model" if context_only else "updated-team-model")
    after = resolve_agent_preparation_inputs(
        agent_name="general",
        history_settings=agent.history_settings,
        config=runtime.config,
        static_prompt_tokens=0,
    ).execution_plan
    assert after.replay_window_tokens == 64000
    assert after.summary_input_budget_tokens == compute_compaction_input_budget(
        64000,
        reserve_tokens=after.reserve_tokens,
    )
    assert after.text_compaction_available


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
    assert (
        build_model_provider(runtime.config, runtime.runtime_paths, model_name).identity.id == "updated-summary-model"
    )
    for name in ("general", "team"):
        after = _history_plan(runtime.config, name)
        assert after.replay_window_tokens == before[name].replay_window_tokens
        if model_field == "model":
            assert after.summary_input_budget_tokens == compute_compaction_input_budget(
                96000,
                reserve_tokens=after.reserve_tokens,
            )
            assert after.summary_input_budget_tokens is not None
            assert before[name].summary_input_budget_tokens is not None
            assert after.summary_input_budget_tokens > before[name].summary_input_budget_tokens
        else:
            assert after.summary_input_budget_tokens == before[name].summary_input_budget_tokens
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
    assert plan.summary_input_budget_tokens == compute_compaction_input_budget(
        64000,
        reserve_tokens=plan.reserve_tokens,
    )
    assert plan.text_compaction_available


@pytest.mark.asyncio
async def test_agent_and_team_edits_reach_the_next_response_without_restarting_bots(
    runtime: _MultiAgentOrchestrator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Instruction, model, tool, and team mode edits reach running bots and the config:reloaded hook."""
    emit_config_reloaded = AsyncMock()
    monkeypatch.setattr(runtime, "_emit_config_reloaded", emit_config_reloaded)
    old = runtime.config
    assert old is not None
    new = old.model_copy(deep=True)
    new.agents["general"].instructions = ["Answer in one sentence."]
    new.agents["general"].model = "other"
    new.agents["general"].tools = ["calculator"]
    new.teams["team"].mode = "collaborate"
    await _reload(runtime, new)

    agent, _member, _team, _router = _materialize(runtime)
    assert agent.spec.model.identity.id == "other-model"
    assert "Answer in one sentence." in agent.prompt.instructions
    assert "calculator" in {toolkit.name for toolkit in agent.spec.toolkits}
    assert runtime.agent_bots["team"].config.teams["team"].mode == "collaborate"
    assert emit_config_reloaded.await_args.kwargs["changed_entities"] == {"general", "team"}
