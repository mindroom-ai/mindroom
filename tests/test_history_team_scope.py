"""Tests for team-scope history preparation and the team build's history and call-limit settings."""
# ruff: noqa: D103, TC003

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom import agents
from mindroom.agent_loop.tools import prepare_run_tools
from mindroom.config.agent import AgentConfig, AgentPrivateConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig
from mindroom.history.prompt_tokens import build_static_token_estimator
from mindroom.history.runtime import (
    finalize_history_preparation,
    prepare_bound_scope_history,
)
from mindroom.history.session_context import (
    ScopeSessionContext,
    open_bound_scope_session_context,
    resolve_bound_team_scope_context,
)
from mindroom.history.types import (
    HistoryScope,
    PreparedHistoryState,
)
from mindroom.teams import envelope
from mindroom.teams.envelope import BuiltTeam, _leader_spec, build_team_spec
from mindroom.teams.members import materialize_exact_team_members
from mindroom.teams.spec import TeamMode
from mindroom.token_budget import estimate_text_tokens, stable_serialize
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.conftest import bind_runtime_paths
from tests.fakes.scripted_model import ScriptedModel
from tests.history_helpers import _runtime_paths

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
) -> BuiltTeam:
    """Build the alpha+zeta team on scripted providers, as a reply builds it."""
    monkeypatch.setattr(agents, "_build_agent_model_provider", lambda *_args, **_kwargs: ScriptedModel([]))
    monkeypatch.setattr(envelope, "build_model_provider", lambda *_args, **_kwargs: ScriptedModel([]))
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
        team_display_name="Team-zeta-alpha",
        scope_context=scope_context,
        execution_identity=None,
        model_name="default",
        configured_team_name=configured_team_name,
    )


def _expected_static_tokens(built: BuiltTeam, prompt: str) -> int:
    """Return the estimate a request of ``prompt`` costs before its history: the prompt, the leader's prompt, its tools."""
    payloads = [
        {key: value for key, value in asdict(tool).items() if value is not None}
        for tool in prepare_run_tools(_leader_spec(built.spec)).specs()
    ]
    system_content = built.spec.system_message.content
    assert isinstance(system_content, str)
    return estimate_text_tokens(prompt) + estimate_text_tokens(system_content) + len(stable_serialize(payloads)) // 4


@pytest.mark.asyncio
async def test_prepare_bound_scope_history_prepares_the_owner_s_team_scope_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ad hoc team's scope is prepared once, owned by the first member by name, sized by the leader's prompt."""
    config, runtime_paths = _pair_config(tmp_path)
    built = _build_pair(config, runtime_paths, monkeypatch, configured_team_name=None)
    estimator = build_static_token_estimator(
        built.spec.system_message,
        prepare_run_tools(_leader_spec(built.spec)).specs(),
        frozenset(),
    )
    expected_static_prompt_tokens = _expected_static_tokens(built, "Current prompt")

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
            member_names=["zeta", "alpha"],
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
        ) as scope_context,
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
    assert mock_prepare.await_args.kwargs["agent_name"] == "alpha"
    assert mock_prepare.await_args.kwargs["scope"] == HistoryScope(kind="team", scope_id="team_alpha+zeta")
    assert mock_prepare.await_args.kwargs["model"] is built.spec.model
    assert estimator.estimate("Current prompt") == expected_static_prompt_tokens
    assert (
        mock_prepare.await_args.kwargs["resolved_inputs"].execution_plan.static_prompt_tokens
        == expected_static_prompt_tokens
    )


def test_the_team_build_sizes_its_static_tokens_from_the_leader_prompt_and_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The static estimate is the leader's rendered prompt plus the definitions of the tools it offers, nothing else."""
    config, runtime_paths = _pair_config(tmp_path, teams={"pair": _PAIR})
    built = _build_pair(config, runtime_paths, monkeypatch, configured_team_name="pair")
    tools = prepare_run_tools(_leader_spec(built.spec)).specs()

    estimator = build_static_token_estimator(built.spec.system_message, tools, frozenset())

    assert estimator.estimate("Current prompt") == _expected_static_tokens(built, "Current prompt")
    assert estimator.non_prompt_tokens() == _expected_static_tokens(built, "")
    # The leader's prompt names each member; its own tools are only the job controls, when those are on.
    assert '<member id="alpha" name="Alpha">' in str(built.spec.system_message.content)
    assert [tool.name for tool in tools] == sorted(f.name for kit in built.spec.toolkits for f in kit.functions)


def test_the_team_build_takes_the_configured_team_s_call_limit_and_members(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, runtime_paths = _pair_config(tmp_path, teams={"pair": _PAIR})

    with open_bound_scope_session_context(
        member_names=["zeta", "alpha"],
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        team_name="pair",
    ) as scope_context:
        assert scope_context is not None
        configured = _build_pair(
            config,
            runtime_paths,
            monkeypatch,
            configured_team_name="pair",
            scope_context=scope_context,
        )
    ad_hoc = _build_pair(config, runtime_paths, monkeypatch, configured_team_name=None)

    assert (configured.spec.id, configured.spec.tool_call_limit) == ("pair", 3)
    assert [(member.spec.id, member.spec.name) for member in configured.spec.members] == [
        ("zeta", "Zeta"),
        ("alpha", "Alpha"),
    ]
    assert ad_hoc.spec.tool_call_limit == config.defaults.max_tool_calls_per_turn


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

    with (
        open_bound_scope_session_context(
            member_names=member_names,
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=identity_for("@alice:localhost"),
        ) as alice_scope,
        open_bound_scope_session_context(
            member_names=member_names,
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
    config, _runtime_paths = _private_pair_config(tmp_path)

    with pytest.raises(ValueError, match="Private ad hoc team history scope requires requester identity"):
        resolve_bound_team_scope_context(
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
    assert mock_prepare.await_args.kwargs["scope"] == opened_scope
    assert mock_prepare.await_args.kwargs["non_prompt_tokens"] == 2
