"""Tests for team-scope history preparation and team instance history wiring."""
# ruff: noqa: D103, TC003

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agno.agent import Agent
from agno.run import RunContext
from agno.run.team import TeamRunOutput
from agno.session.team import TeamSession
from agno.team import Team
from agno.team._tools import _determine_tools_for_model
from agno.tools import Toolkit

from mindroom.config.agent import AgentConfig, AgentPrivateConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig
from mindroom.history import PreparedHistoryState
from mindroom.history.runtime import (
    ScopeSessionContext,
    _estimate_preparation_static_tokens_for_team,
    finalize_history_preparation,
    open_bound_scope_session_context,
    prepare_bound_scope_history,
    resolve_bound_team_scope_context,
)
from mindroom.history.types import (
    HistoryScope,
)
from mindroom.teams import TeamMode, _create_team_instance
from mindroom.token_budget import estimate_text_tokens, stable_serialize
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.conftest import (
    FakeModel,
    bind_runtime_paths,
)
from tests.history_helpers import (  # noqa: F401
    _agent,
    _close_test_storages,
    _make_config,
    _runtime_paths,
)


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

    prepared_tools = _determine_tools_for_model(
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
            "tests.test_history_team_scope.finalize_history_preparation",
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
        _estimate_preparation_static_tokens_for_team(team, full_prompt="Current prompt")
        == expected_static_prompt_tokens
    )
    assert mock_prepare.await_args.kwargs["resolved_inputs"].static_prompt_tokens == expected_static_prompt_tokens


def test_private_ad_hoc_bound_team_scope_is_requester_partitioned(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "private_worker": AgentConfig(
                    display_name="Private Worker",
                    private=AgentPrivateConfig(per="user", root="private_worker_data"),
                ),
                "shared": AgentConfig(display_name="Shared"),
            },
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
        runtime_paths,
    )
    agents = [
        Agent(id="private_worker", name="Private Worker"),
        Agent(id="shared", name="Shared"),
    ]

    def identity_for(requester_id: str) -> ToolExecutionIdentity:
        return ToolExecutionIdentity(
            channel="matrix",
            agent_name="router",
            requester_id=requester_id,
            room_id="!room:localhost",
            thread_id="$thread",
            resolved_thread_id="$thread",
            session_id="session-1",
        )

    with (
        open_bound_scope_session_context(
            agents=agents,
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=identity_for("@alice:localhost"),
        ) as alice_scope,
        open_bound_scope_session_context(
            agents=agents,
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=identity_for("@bob:localhost"),
        ) as bob_scope,
    ):
        assert alice_scope is not None
        assert bob_scope is not None
        assert alice_scope.scope.scope_id.startswith("team_private_worker+shared_requester_")
        assert bob_scope.scope.scope_id.startswith("team_private_worker+shared_requester_")
        assert alice_scope.scope != bob_scope.scope


def test_private_ad_hoc_bound_team_scope_requires_requester_identity(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "private_worker": AgentConfig(
                    display_name="Private Worker",
                    private=AgentPrivateConfig(per="user", root="private_worker_data"),
                ),
                "shared": AgentConfig(display_name="Shared"),
            },
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
        runtime_paths,
    )

    with pytest.raises(ValueError, match="Private ad hoc team history scope requires requester identity"):
        resolve_bound_team_scope_context(
            agents=[
                Agent(id="private_worker", name="Private Worker"),
                Agent(id="shared", name="Shared"),
            ],
            config=config,
            execution_identity=None,
        )


@pytest.mark.asyncio
async def test_prepare_bound_scope_history_uses_opened_private_ad_hoc_scope(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "private_worker": AgentConfig(
                    display_name="Private Worker",
                    private=AgentPrivateConfig(per="user", root="private_worker_data"),
                ),
                "shared": AgentConfig(display_name="Shared"),
            },
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
        runtime_paths,
    )
    owner_agent = Agent(id="private_worker", name="Private Worker")
    peer_agent = Agent(id="shared", name="Shared")
    opened_scope = HistoryScope(kind="team", scope_id="team_private_worker+shared_requester_alice")
    scope_context = ScopeSessionContext(
        scope=opened_scope,
        storage=MagicMock(),
        session=None,
        session_id="session-1",
    )
    team = Team(name="Ad hoc team", members=[owner_agent, peer_agent])

    with patch(
        "mindroom.history.runtime.prepare_scope_history",
        new=AsyncMock(return_value=MagicMock()),
    ) as mock_prepare:
        await prepare_bound_scope_history(
            agents=[owner_agent, peer_agent],
            team=team,
            full_prompt="Current prompt",
            runtime_paths=runtime_paths,
            config=config,
            scope_context=scope_context,
            static_prompt_tokens=1,
        )

    assert mock_prepare.await_count == 1
    assert mock_prepare.await_args.kwargs["scope"] == opened_scope


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
    prepared_tools = _determine_tools_for_model(
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
        _estimate_preparation_static_tokens_for_team(team, full_prompt="Current prompt")
        == expected_static_prompt_tokens
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

    _estimate_preparation_static_tokens_for_team(team, full_prompt="Current prompt")

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

    with (
        open_bound_scope_session_context(
            agents=[alpha, zeta],
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            team_name="pair",
        ) as scope_context,
        patch("mindroom.model_loading.get_model_instance", return_value=FakeModel(id="fake-model", provider="fake")),
    ):
        assert scope_context is not None
        team = _create_team_instance(
            agents=[alpha, zeta],
            mode=TeamMode.COORDINATE,
            config=config,
            runtime_paths=runtime_paths,
            team_display_name="Team-alpha-zeta",
            scope_context=scope_context,
            execution_identity=None,
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

    with (
        open_bound_scope_session_context(
            agents=[alpha, zeta],
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
            team_name="pair",
        ) as scope_context,
        patch("mindroom.model_loading.get_model_instance", return_value=FakeModel(id="fake-model", provider="fake")),
    ):
        assert scope_context is not None
        team = _create_team_instance(
            agents=[alpha, zeta],
            mode=TeamMode.COORDINATE,
            config=config,
            runtime_paths=runtime_paths,
            team_display_name="Team-alpha-zeta",
            scope_context=scope_context,
            execution_identity=None,
            configured_team_name="pair",
        )

    assert team.num_history_runs is None
    assert team.num_history_messages is None
