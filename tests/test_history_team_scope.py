"""Tests for team-scope history preparation and the team build's history and call-limit settings."""
# ruff: noqa: D103, TC003

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom import execution_preparation
from mindroom.agent_loop.spec import RunContext
from mindroom.config.agent import AgentConfig, AgentPrivateConfig, TeamConfig
from mindroom.config.approval import ApprovalRuleConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig
from mindroom.history.prompt_tokens import build_static_token_estimator
from mindroom.history.runtime import (
    finalize_history_preparation,
    prepare_bound_scope_history,
)
from mindroom.history.session_context import (
    ScopeSessionContext,
    load_bound_scope_session,
    resolve_bound_team_scope,
)
from mindroom.history.types import (
    HistoryScope,
    PreparedHistoryState,
)
from mindroom.llm.messages import Message
from mindroom.response_turn import ResponsePausedForApproval
from mindroom.teams.envelope import prepare_materialized_team_execution
from mindroom.teams.members import BuiltTeam, build_team_spec, materialize_exact_team_members
from mindroom.teams.runtime import leader_tool_specs, run_team
from mindroom.teams.spec import TeamMode
from mindroom.token_budget import estimate_text_tokens, stable_serialize
from mindroom.tool_system.context_bound_streams import closing_async_stream
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.conftest import bind_runtime_paths, install_model_providers, make_turn_context, seed_session
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call, usage
from tests.history_helpers import _completed_team_run, _runtime_paths, _team_session
from tests.team_turn_helpers import SESSION_ID, delegate, member_approval, squad_turn

_PAIR = TeamConfig(
    display_name="Pair",
    role="Test team",
    agents=["alpha", "zeta"],
    num_history_messages=2,
    max_tool_calls_per_turn=3,
)


def _pair_config(tmp_path: Path, *, teams: dict[str, TeamConfig] | None = None) -> tuple[Config, object]:
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "alpha": AgentConfig(display_name="Alpha", num_history_messages=100),
                "zeta": AgentConfig(display_name="Zeta", num_history_messages=1),
            },
            teams=teams or {},
            defaults=DefaultsConfig(tools=[]),
            models={"default": ModelConfig(provider="openai", id="test-model", context_window=2_000)},
        ),
        runtime_paths,
    )
    return config, runtime_paths


def _build_pair(
    config: Config,
    runtime_paths: object,
    monkeypatch: pytest.MonkeyPatch,
    *,
    configured_team_name: str | None,
    scope_context: ScopeSessionContext | None = None,
    leader: ScriptedModel | None = None,
) -> BuiltTeam:
    """Build the alpha+zeta team on scripted providers, as a reply builds it; ``leader`` scripts the leader's turns."""
    install_model_providers(monkeypatch, {"default": leader or ScriptedModel([])})
    members = materialize_exact_team_members(
        ["zeta", "alpha"],
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=None,
    )
    assert members.failed_agent_names == []
    return build_team_spec(
        members=members,
        mode=TeamMode.COORDINATE,
        config=config,
        runtime_paths=runtime_paths,
        scope_context=scope_context,
        execution_identity=None,
        model_name="default",
        configured_team_name=configured_team_name,
    )


def _expected_static_tokens(built: BuiltTeam, prompt: str) -> int:
    """Return the estimate a request of ``prompt`` costs before its history: the prompt, the leader's prompt, its tools."""
    payloads = [
        {key: value for key, value in asdict(tool).items() if value is not None}
        for tool in leader_tool_specs(built.spec)
    ]
    system_content = built.spec.system_message.content
    assert isinstance(system_content, str)
    return estimate_text_tokens(prompt) + estimate_text_tokens(system_content) + len(stable_serialize(payloads)) // 4


@pytest.mark.asyncio
async def test_prepare_bound_scope_history_prepares_the_team_scope_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ad hoc team's scope is prepared once, sized by the leader's prompt."""
    config, runtime_paths = _pair_config(tmp_path)
    built = _build_pair(config, runtime_paths, monkeypatch, configured_team_name=None)
    estimator = build_static_token_estimator(built.spec.system_message, leader_tool_specs(built.spec), frozenset())
    expected_static_prompt_tokens = _expected_static_tokens(built, "Current prompt")

    scope_context = load_bound_scope_session(
        member_names=["zeta", "alpha"],
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
    )
    with (
        patch(
            "mindroom.history.runtime.prepare_scope_history",
            new=AsyncMock(return_value=MagicMock()),
        ) as mock_prepare,
        patch(
            "tests.test_history_team_scope.finalize_history_preparation",
            return_value=PreparedHistoryState(replays_persisted_history=True),
        ) as mock_finalize,
    ):
        prepared_scope_history = await prepare_bound_scope_history(
            member_names=["zeta", "alpha"],
            model=built.spec.model,
            runtime_paths=runtime_paths,
            config=config,
            static_prompt_tokens=estimator.estimate("Current prompt"),
            non_prompt_tokens=estimator.non_prompt_tokens(),
            scope_context=scope_context,
        )
        prepared = finalize_history_preparation(
            prepared_scope_history=prepared_scope_history,
            config=config,
            static_prompt_tokens=expected_static_prompt_tokens,
        )

    assert prepared.replays_persisted_history is True
    assert mock_finalize.call_count == 1
    assert mock_prepare.await_count == 1
    assert mock_prepare.await_args.kwargs["scope_context"].scope == HistoryScope("team", "team_alpha+zeta")
    assert mock_prepare.await_args.kwargs["model"] is built.spec.model
    assert estimator.estimate("Current prompt") == expected_static_prompt_tokens
    assert (
        mock_prepare.await_args.kwargs["resolved_inputs"].execution_plan.static_prompt_tokens
        == expected_static_prompt_tokens
    )


@pytest.mark.asyncio
async def test_the_team_turn_sizes_its_static_tokens_from_the_leader_prompt_and_every_tool_it_offers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The static estimate counts the leader's rendered prompt and the tools its requests offer, the delegation tool among them."""
    config, runtime_paths = _pair_config(tmp_path, teams={"pair": _PAIR})
    leader = ScriptedModel([answer("ok")])
    built = _build_pair(config, runtime_paths, monkeypatch, configured_team_name="pair", leader=leader)
    sized: dict[str, Any] = {}
    real_estimator = execution_preparation.build_static_token_estimator

    def capture_estimator(system_message: Message, tools: Any, deferred_tool_names: frozenset[str]) -> Any:  # noqa: ANN401
        sized["system_message"], sized["tools"] = system_message, list(tools)
        return real_estimator(system_message, tools, deferred_tool_names)

    monkeypatch.setattr(execution_preparation, "build_static_token_estimator", capture_estimator)

    scope_context = load_bound_scope_session(
        member_names=["zeta", "alpha"],
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        team_name="pair",
    )
    await prepare_materialized_team_execution(
        make_turn_context("pair", session_id="session-1"),
        scope_context=scope_context,
        built=built,
        message="Current prompt",
        thread_history=None,
        config=config,
        runtime_paths=runtime_paths,
        runtime_model=config.resolve_runtime_model(entity_name="pair", active_model_name="default"),
        response_sender_id=None,
        current_sender_id=None,
        configured_team_name="pair",
    )
    events = run_team(
        built.spec,
        [Message(role="user", content="Current prompt")],
        ctx=RunContext(run_id="run-1", session_id="session-1"),
    )
    async with closing_async_stream(events):
        async for _event in events:
            pass

    assert sized["system_message"] is built.spec.system_message
    offered = [tool.name for tool in leader.requests[0].tools]
    assert [tool.name for tool in sized["tools"]] == offered
    assert "delegate_task_to_member" in offered
    assert set(offered) > {function.name for kit in built.spec.toolkits for function in kit.functions}


def test_the_team_build_takes_the_configured_team_s_call_limit_and_members(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, runtime_paths = _pair_config(tmp_path, teams={"pair": _PAIR})

    scope_context = load_bound_scope_session(
        member_names=["zeta", "alpha"],
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        team_name="pair",
    )
    assert scope_context is not None
    configured = _build_pair(
        config,
        runtime_paths,
        monkeypatch,
        configured_team_name="pair",
        scope_context=scope_context,
    )
    ad_hoc = _build_pair(config, runtime_paths, monkeypatch, configured_team_name=None)

    assert (configured.spec.id, configured.spec.max_tool_calls_per_turn) == ("pair", 3)
    assert [(member.spec.id, member.spec.name) for member in configured.spec.members] == [
        ("zeta", "Zeta"),
        ("alpha", "Alpha"),
    ]
    assert ad_hoc.spec.max_tool_calls_per_turn == config.defaults.max_tool_calls_per_turn


@pytest.mark.asyncio
@pytest.mark.usefixtures("squad_probe")
async def test_a_saved_team_approval_resumes_under_the_history_settings_its_configured_team_authors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The leader's resumed request replays the one run the team authors, where the defaults would replay every run."""
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "checker", "Touch it")],
        checker=[calls(tool_call("touch", "call-touch", label="dangerous"), metrics=usage(10, 1))],
        approval_rules=[ApprovalRuleConfig(match="touch", action="require_approval")],
    )
    team.config.teams["squad"].num_history_runs = 1
    assert team.config.resolve_entity(None).history_settings.policy.mode == "all"
    past = [_completed_team_run("past-1", team_id="squad"), _completed_team_run("past-2", team_id="squad")]
    seed_session(team.storage, _team_session(SESSION_ID, team_id="squad", runs=past))
    with pytest.raises(ResponsePausedForApproval) as raised:
        await team.stream(show_tool_calls=False)
    team.models["default"] = ScriptedModel([answer("The checker touched it.", usage(50, 5))])
    team.models["checker-model"] = ScriptedModel([answer("Touched.")])

    await team.resume(raised.value.paused, approved=True, approvals=(member_approval("call-touch", "dangerous"),))

    [resumed] = team.models["default"].requests
    replayed = [str(message.content) for message in resumed.messages if message.from_history]
    assert replayed == ["past-2 team question", "past-2 team answer"]


def _private_pair_config(tmp_path: Path) -> tuple[Config, object]:
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
    return config, runtime_paths


def test_private_ad_hoc_bound_team_scope_is_requester_partitioned(tmp_path: Path) -> None:
    config, runtime_paths = _private_pair_config(tmp_path)
    member_names = ["private_worker", "shared"]

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

    alice_scope = load_bound_scope_session(
        member_names=member_names,
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=identity_for("@alice:localhost"),
    )
    bob_scope = load_bound_scope_session(
        member_names=member_names,
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=identity_for("@bob:localhost"),
    )
    assert alice_scope is not None
    assert bob_scope is not None
    assert alice_scope.scope.scope_id.startswith("team_private_worker+shared_requester_")
    assert bob_scope.scope.scope_id.startswith("team_private_worker+shared_requester_")
    assert alice_scope.scope != bob_scope.scope


def test_private_ad_hoc_bound_team_scope_requires_requester_identity(tmp_path: Path) -> None:
    config, _runtime_paths = _private_pair_config(tmp_path)

    with pytest.raises(ValueError, match="Private ad hoc team history scope requires requester identity"):
        resolve_bound_team_scope(
            member_names=["private_worker", "shared"],
            config=config,
            execution_identity=None,
        )


@pytest.mark.asyncio
async def test_prepare_bound_scope_history_uses_opened_private_ad_hoc_scope(tmp_path: Path) -> None:
    config, runtime_paths = _private_pair_config(tmp_path)
    opened_scope = HistoryScope(kind="team", scope_id="team_private_worker+shared_requester_alice")
    scope_context = ScopeSessionContext(
        scope=opened_scope,
        storage=MagicMock(),
        session=None,
        session_id="session-1",
    )

    with patch(
        "mindroom.history.runtime.prepare_scope_history",
        new=AsyncMock(return_value=MagicMock()),
    ) as mock_prepare:
        await prepare_bound_scope_history(
            member_names=["private_worker", "shared"],
            model=None,
            runtime_paths=runtime_paths,
            config=config,
            scope_context=scope_context,
            static_prompt_tokens=5,
            non_prompt_tokens=2,
        )

    assert mock_prepare.await_count == 1
    assert mock_prepare.await_args.kwargs["scope_context"].scope == opened_scope
    assert mock_prepare.await_args.kwargs["non_prompt_tokens"] == 2
