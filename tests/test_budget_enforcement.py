"""Over-budget requesters' replies use the configured fallback model."""
# ruff: noqa: D103

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import nio
import pytest
from agno.run.team import RunContentEvent as TeamContentEvent
from agno.run.team import TeamRunOutput
from agno.team import Team as AgnoTeam
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mindroom.api import openai_compat
from mindroom.api.main import initialize_api_app
from mindroom.background_tasks import wait_for_background_tasks
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.budgets import BudgetsConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, ModelPricing, RouterConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.custom_tools import dynamic_workflow as dynamic_workflow_module
from mindroom.custom_tools.delegate import DelegateTools
from mindroom.delegation.lifecycle import prepare_child_turn
from mindroom.synthetic_model import SyntheticModel
from mindroom.teams import TeamMode, TeamTurnModelSelection
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.conftest import patch_response_runner_module, unwrap_extracted_collaborator
from tests.identity_helpers import persist_entity_accounts
from tests.response_runner_helpers import _bot, _noop_typing, _plain_request, _target
from tests.test_delegate_tools import _delegate_runtime_context, _make_config, _runtime_paths
from tests.test_dynamic_workflows import _fake_stream_agent, _make_context, _make_multi_agent_context

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path


def _budget(config: Config, *, monthly_limit_usd: float | None = 0) -> None:
    """Price the agent's model and cap every requester at ``monthly_limit_usd``."""
    config.models["default"].pricing = ModelPricing(input=5, output=30)
    config.models["luna"] = ModelConfig(
        provider="openai",
        id="gpt-6-luna",
        pricing=ModelPricing(input=0.2, output=1.25),
    )
    config.budgets = BudgetsConfig(fallback_model="luna", monthly_limit_usd=monthly_limit_usd)


@pytest.mark.asyncio
async def test_agent_reply_uses_fallback_for_over_budget_requester(tmp_path: Path) -> None:
    coordinator = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    _budget(coordinator.deps.runtime.config)

    runtime = await coordinator.prepare_response_runtime(_plain_request(_target()))

    assert runtime.active_model_name == "luna"


@pytest.mark.asyncio
async def test_agent_reply_keeps_its_model_within_budget(tmp_path: Path) -> None:
    coordinator = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    _budget(coordinator.deps.runtime.config, monthly_limit_usd=None)

    runtime = await coordinator.prepare_response_runtime(_plain_request(_target()))

    assert runtime.active_model_name == "default"


@pytest.mark.asyncio
async def test_scheduled_model_is_budgeted_too(tmp_path: Path) -> None:
    coordinator = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    config = coordinator.deps.runtime.config
    _budget(config)
    config.models["large"] = ModelConfig(provider="openai", id="gpt-6-astra", pricing=ModelPricing(input=5, output=30))

    runtime = await coordinator.prepare_response_runtime(replace(_plain_request(_target()), scheduled_model="large"))

    assert runtime.active_model_name == "luna"


@pytest.mark.asyncio
async def test_completed_response_asks_budgets_to_count_the_new_spend(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    assert bot.client is not None
    bot.client.room_send.return_value = nio.RoomSendResponse(event_id="$response", room_id="!room:localhost")
    orchestrator = MagicMock(knowledge_refresh_scheduler=None)
    coordinator.deps.runtime.orchestrator = orchestrator
    model = SyntheticModel(id="synthetic", min_response_chars=10, max_response_chars=10, chars_per_second=0)

    with (
        patch("mindroom.model_loading.get_model_instance", return_value=model),
        patch_response_runner_module(typing_indicator=_noop_typing, should_use_streaming=AsyncMock(return_value=False)),
    ):
        await coordinator.generate_response(_plain_request(_target()))
        assert await wait_for_background_tasks(5, owner=coordinator.deps.runtime)

    orchestrator.budgets.response_finished.assert_called_once_with()


class _Spent:
    """Monitor stand-in that reports one month-to-date spend for every requester."""

    def __init__(self, spend_usd: float) -> None:
        self._spend_usd = spend_usd

    def spend_usd(self, _user_id: str) -> float:
        return self._spend_usd


def _delegation_config() -> Config:
    config = _make_config(
        {
            "leader": AgentConfig(display_name="Leader", delegate_to=["child"]),
            "child": AgentConfig(display_name="Child"),
        },
    )
    config.models["default"].pricing = ModelPricing(input=5, output=30)
    config.models["luna"] = ModelConfig(
        provider="openai",
        id="gpt-6-luna",
        pricing=ModelPricing(input=0.2, output=1.25),
    )
    config.budgets = BudgetsConfig(fallback_model="luna", monthly_limit_usd=10)
    return config


def _owner() -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name="leader",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="parent-session",
    )


@pytest.mark.parametrize(("spend", "expected"), [(12.0, "luna"), (3.0, "default")])
def test_delegated_child_model_follows_the_owners_budget(tmp_path: Path, spend: float, expected: str) -> None:
    child = prepare_child_turn(
        "leader",
        "child",
        "Do the work",
        owner=_owner(),
        config=_delegation_config(),
        runtime_paths=_runtime_paths(tmp_path),
        depth=0,
        budget_monitor=_Spent(spend),  # type: ignore[arg-type]
    )

    assert child.model_name == expected


@pytest.mark.asyncio
async def test_direct_delegation_reads_the_orchestrators_budget_monitor(tmp_path: Path) -> None:
    config = _delegation_config()
    runtime_paths = _runtime_paths(tmp_path)
    tools = DelegateTools("leader", ["child"], runtime_paths, config, execution_identity=_owner())
    context = replace(
        _delegate_runtime_context(config, runtime_paths, execution_identity=_owner()),
        orchestrator=MagicMock(budgets=_Spent(12.0)),
    )

    with (
        tool_runtime_context(context),
        patch("mindroom.ai.ai_response", new_callable=AsyncMock, return_value="Child completed.") as response,
    ):
        await tools.run_subagent(agent_name="child", task="Do the work")

    assert response.await_args.args[0].active_model_name == "luna"


def _openai_config() -> Config:
    config = Config(
        agents={
            "general": AgentConfig(display_name="GeneralAgent", rooms=[]),
            "code": AgentConfig(display_name="CodeAgent", model="local", rooms=[]),
        },
        teams={
            "super_team": TeamConfig(
                display_name="Super Team",
                role="Team",
                agents=["general", "code"],
                model="default",
            ),
        },
        models={
            "default": ModelConfig(provider="ollama", id="test-model", pricing=ModelPricing(input=5, output=30)),
            "local": ModelConfig(provider="ollama", id="local-model"),
            "luna": ModelConfig(provider="ollama", id="cheap-model", pricing=ModelPricing(input=0.2, output=1.25)),
        },
        router=RouterConfig(model="default"),
        budgets=BudgetsConfig(fallback_model="luna", monthly_limit_usd=0),
    )
    for entity in (config.agents["general"], config.agents["code"], config.teams["super_team"]):
        entity.access = ResponderAccessConfig(users=["@alice:localhost"])
    return config


@contextmanager
def _openai_client(tmp_path: Path, config: Config, *, authenticated: bool) -> Iterator[TestClient]:
    process_env = (
        {
            "OPENAI_COMPAT_API_KEYS": "alice-key",
            "OPENAI_COMPAT_API_KEY_REQUESTERS": json.dumps({"alice-key": "@alice:localhost"}),
        }
        if authenticated
        else {"OPENAI_COMPAT_ALLOW_UNAUTHENTICATED": "true"}
    )
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env=process_env,
    )
    persist_entity_accounts(config, runtime_paths)
    app = FastAPI()
    app.include_router(openai_compat.router)
    initialize_api_app(app, runtime_paths)
    with (
        patch("mindroom.api.openai_compat._load_config", return_value=(config, runtime_paths)),
        TestClient(app, base_url="http://localhost") as client,
    ):
        yield client


@pytest.mark.parametrize(("authenticated", "expected"), [(True, "luna"), (False, "default")])
def test_openai_compat_agent_completion_budgets_mapped_requesters(
    tmp_path: Path,
    authenticated: bool,
    expected: str,
) -> None:
    with (
        _openai_client(tmp_path, _openai_config(), authenticated=authenticated) as client,
        patch("mindroom.api.openai_compat.ai_response", new_callable=AsyncMock, return_value="Hi") as response,
    ):
        reply = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer alice-key"} if authenticated else {},
            json={"model": "general", "messages": [{"role": "user", "content": "Hello"}]},
        )

    assert reply.status_code == 200
    assert response.await_args.args[0].active_model_name == expected


@pytest.mark.parametrize("stream", [False, True])
def test_openai_compat_team_completion_budgets_priced_models(tmp_path: Path, stream: bool) -> None:
    team = AgnoTeam(name="Super Team", id="super-team", model=SyntheticModel(id="synthetic"), members=[], tools=[])

    async def stream_events() -> AsyncIterator[object]:
        yield TeamContentEvent(content="Team answer")

    async def run() -> TeamRunOutput:
        return TeamRunOutput(content="Team answer")

    team.arun = MagicMock(side_effect=lambda *_args, **kwargs: stream_events() if kwargs.get("stream") else run())
    with (
        _openai_client(tmp_path, _openai_config(), authenticated=True) as client,
        patch("mindroom.api.openai_compat._build_team", return_value=([], team, TeamMode.COORDINATE)) as build,
        patch(
            "mindroom.api.openai_compat._prepare_openai_team_prompt",
            new=AsyncMock(return_value=openai_compat._PreparedOpenAITeamPrompt("Build it", None)),
        ) as prepare,
    ):
        reply = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer alice-key"},
            json={"model": "team/super_team", "messages": [{"role": "user", "content": "Build it"}], "stream": stream},
        )

    assert reply.status_code == 200
    expected = TeamTurnModelSelection(team_model_name="luna", member_model_names={"general": "luna", "code": "local"})
    assert build.call_args.kwargs["models"] == expected
    assert prepare.await_args.kwargs["team_model_name"] == "luna"


def _budget_context_config(config: Config) -> None:
    config.models["default"].pricing = ModelPricing(input=3, output=15)
    config.models["luna"] = ModelConfig(
        provider="openai",
        id="gpt-6-luna",
        pricing=ModelPricing(input=0.2, output=1.25),
    )
    config.budgets = BudgetsConfig(fallback_model="luna", monthly_limit_usd=0)


@pytest.mark.asyncio
async def test_dynamic_workflow_room_agent_participant_uses_fallback(tmp_path: Path) -> None:
    context = _make_multi_agent_context(tmp_path, room_agents=["general", "specialist"])
    _budget_context_config(context.config)

    with patch("mindroom.agents.create_agent", return_value=_fake_stream_agent(content="done")) as create_agent:
        await dynamic_workflow_module._aexecute_room_agent_participant(
            context,
            {"id": "writer", "kind": "room_agent", "agent": "specialist"},
            "Write a report.",
        )

    assert create_agent.call_args.kwargs["active_model_name"] == "luna"


@pytest.mark.asyncio
async def test_dynamic_workflow_ephemeral_participant_uses_fallback(tmp_path: Path) -> None:
    context = _make_context(tmp_path)
    _budget_context_config(context.config)
    model = SyntheticModel(id="participant")

    with (
        patch.object(dynamic_workflow_module.model_loading, "get_model_instance", return_value=model) as get_model,
        patch.object(dynamic_workflow_module, "Agent", Mock(return_value=_fake_stream_agent(content="done"))),
    ):
        await dynamic_workflow_module._aexecute_ephemeral_agent_participant(
            context,
            {"id": "writer", "kind": "ephemeral_agent"},
            "Write a report.",
            run_scope="manual",
        )

    assert get_model.call_args.args[2] == "luna"
