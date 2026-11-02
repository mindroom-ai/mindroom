"""Tests for team-scope history preparation and the team build's history and call-limit settings."""
# ruff: noqa: D103, TC003

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from mindroom import execution_preparation
from mindroom.agent_loop.spec import RunContext, UnstoredSession
from mindroom.config.agent import AgentConfig, AgentPrivateConfig, TeamConfig
from mindroom.config.approval import ApprovalRuleConfig, ToolApprovalConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig
from mindroom.dispatch_source import ScheduledHistoryBudget
from mindroom.execution_preparation import prepare_bound_team_run_context, render_prepared_messages_text
from mindroom.history.session_context import (
    ScopeSessionContext,
    load_bound_scope_session,
    resolve_bound_team_scope,
)
from mindroom.history.types import (
    PreparedHistoryState,
)
from mindroom.llm.messages import Message, stable_serialize, system_text
from mindroom.response_turn import CompletedApprovalRun, ResponsePausedForApproval
from mindroom.teams.envelope import prepare_materialized_team_execution
from mindroom.teams.members import (
    BuiltTeam,
    build_team_spec,
    resolve_exact_team_members,
)
from mindroom.teams.runtime import leader_tool_specs
from mindroom.teams.spec import TeamMode
from mindroom.timing import DispatchPipelineTiming
from mindroom.token_counting.counts import count
from mindroom.tool_system.context_bound_streams import closing_async_stream
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.conftest import (
    bind_runtime_paths,
    install_model_providers,
    make_turn_context,
    seed_session,
    unstored_team_scope_context,
)
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call, usage
from tests.history_helpers import RecordingCompactionLifecycle, _completed_team_run, _runtime_paths, _team_session
from tests.team_turn_helpers import (
    PROBE_TOOL,
    SESSION_ID,
    TeamTurn,
    authored_team_config,
    build_team_turn,
    delegate,
    member_approval,
    squad_turn,
)
from tests.teams.squad import start_team
from tests.turn_model_helpers import turn_model

if TYPE_CHECKING:
    from mindroom.llm.provider import ModelRequest, TokenCounter

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
    members = resolve_exact_team_members(
        ["zeta", "alpha"],
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=None,
        refresh_scheduler=None,
        active_model_names={name: turn_model(config, runtime_paths, name).model_name for name in ("zeta", "alpha")},
    )
    return build_team_spec(
        members=members,
        mode=TeamMode.COORDINATE,
        config=config,
        runtime_paths=runtime_paths,
        scope_context=scope_context
        or unstored_team_scope_context(config, ["zeta", "alpha"], team_name=configured_team_name),
        execution_identity=None,
        model_name="default",
        configured_team_name=configured_team_name,
        timezone="UTC",
    )


def _leader_non_prompt_tokens(built: BuiltTeam) -> int:
    """Return the estimate a leader request costs besides its prompt text and history: the leader's prompt, its tools."""
    payloads = [
        {key: value for key, value in asdict(tool).items() if value is not None}
        for tool in leader_tool_specs(built.spec)
    ]
    counter = built.spec.leader.model.token_counter
    return count(counter, system_text(built.spec.leader.system_messages)) + count(counter, stable_serialize(payloads))


@pytest.mark.asyncio
@pytest.mark.parametrize("scheduled", [False, True])
async def test_a_team_turn_prepares_its_scope_once_on_the_leader_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scheduled: bool,
) -> None:
    """The scope is prepared once on the leader's model and the turn's window, sized by the leader's prompt and tools.

    A scheduled turn's history budget rules out native compaction.
    """
    config, runtime_paths = _pair_config(tmp_path)
    built = _build_pair(config, runtime_paths, monkeypatch, configured_team_name=None)
    scope_context = unstored_team_scope_context(config, ["zeta", "alpha"], team_name=None)
    prepare = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(execution_preparation, "prepare_scope_history", prepare)
    monkeypatch.setattr(
        execution_preparation,
        "finalize_history_preparation",
        lambda **_kwargs: PreparedHistoryState(replays_persisted_history=True, native_history=None),
    )
    lifecycle = RecordingCompactionLifecycle()
    timing = DispatchPipelineTiming(source_event_id="$current", room_id="!room:localhost")
    budget = ScheduledHistoryBudget(limit=5, source_event_id="$current") if scheduled else None

    prepared = await prepare_bound_team_run_context(
        make_turn_context("team_alpha+zeta", session_id="session-1", scheduled_history_budget=budget),
        scope_context=scope_context,
        leader_system_messages=built.spec.leader.system_messages,
        leader_tools=leader_tool_specs(built.spec),
        model=built.spec.leader.model,
        prompt="Current prompt",
        thread_history=None,
        runtime_paths=runtime_paths,
        config=config,
        timezone="UTC",
        history_settings=built.history_settings,
        active_model_name="default",
        active_context_window=1_500,
        compaction_lifecycle=lifecycle,
        pipeline_timing=timing,
    )

    assert prepare.await_count == 1
    kwargs = prepare.await_args.kwargs
    assert kwargs["scope_context"] is scope_context
    assert kwargs["model"] is built.spec.leader.model
    assert (kwargs["compaction_lifecycle"], kwargs["pipeline_timing"]) == (lifecycle, timing)
    assert kwargs["allow_native_compaction"] is not scheduled
    resolved_inputs = kwargs["resolved_inputs"]
    assert resolved_inputs.history_settings is built.history_settings
    assert (resolved_inputs.active_model_name, resolved_inputs.active_context_window) == ("default", 1_500)
    non_prompt_tokens = _leader_non_prompt_tokens(built)
    assert kwargs["non_prompt_tokens"] == non_prompt_tokens
    sent_prompt = render_prepared_messages_text(prepared.turn.in_request_order())
    sent_tokens = count(built.spec.leader.model.token_counter, sent_prompt)
    assert resolved_inputs.execution_plan.static_prompt_tokens == sent_tokens + non_prompt_tokens


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

    def capture_estimator(
        counter: TokenCounter,
        system_messages: tuple[Message, ...],
        tools: Any,  # noqa: ANN401
        deferred_tool_names: frozenset[str],
    ) -> Any:  # noqa: ANN401
        sized["system_messages"], sized["tools"] = system_messages, list(tools)
        return real_estimator(counter, system_messages, tools, deferred_tool_names)

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
        runtime_model=turn_model(config, runtime_paths, "pair", "default"),
        response_sender_id=None,
        current_sender_id=None,
    )
    events = start_team(
        built.spec,
        [Message(role="user", content="Current prompt")],
        ctx=RunContext(run_id="run-1", session=UnstoredSession("session-1")),
    )
    async with closing_async_stream(events):
        async for _event in events:
            pass

    assert sized["system_messages"] is built.spec.leader.system_messages
    offered = [tool.name for tool in leader.requests[0].tools]
    assert [tool.name for tool in sized["tools"]] == offered
    assert "delegate_task_to_member" in offered
    assert set(offered) > {function.name for kit in built.spec.leader.toolkits for function in kit.functions}


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
    configured = _build_pair(
        config,
        runtime_paths,
        monkeypatch,
        configured_team_name="pair",
        scope_context=scope_context,
    )
    ad_hoc = _build_pair(config, runtime_paths, monkeypatch, configured_team_name=None)

    assert (configured.spec.leader.id, configured.spec.leader.max_tool_calls_per_turn) == ("pair", 3)
    assert [(member.id, member.name) for member in configured.spec.members] == [
        ("zeta", "Zeta"),
        ("alpha", "Alpha"),
    ]
    assert ad_hoc.spec.leader.max_tool_calls_per_turn == config.defaults.max_tool_calls_per_turn


def test_the_team_build_names_its_leader_by_the_members_label_and_identifies_it_by_its_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The leader carries the team's label as its name and the team's history scope as its id."""
    config, runtime_paths = _pair_config(tmp_path)

    ad_hoc = _build_pair(config, runtime_paths, monkeypatch, configured_team_name=None)

    assert (ad_hoc.spec.leader.id, ad_hoc.spec.leader.name) == ("team_alpha+zeta", "Team (Zeta, Alpha)")


def test_an_ad_hoc_team_that_names_no_member_has_no_history_scope(tmp_path: Path) -> None:
    """Every team run names a member, so an ad hoc team that names none is refused rather than run without history."""
    config, _runtime_paths = _pair_config(tmp_path)

    with pytest.raises(ValueError, match="names no member"):
        resolve_bound_team_scope(member_names=[], config=config, team_name=None)


_PAST_2_ONLY = ["past-2 team question", "past-2 team answer"]


def _store_two_past_runs_under_a_one_run_limit(team: TeamTurn) -> None:
    """Author ``num_history_runs = 1`` on the squad and store two runs, where the defaults would replay both."""
    team.config.teams["squad"].num_history_runs = 1
    assert team.config.resolve_scope(None).history_settings.policy.mode == "all"
    past = [_completed_team_run("past-1", team_id="squad"), _completed_team_run("past-2", team_id="squad")]
    seed_session(team.storage, _team_session(SESSION_ID, team_id="squad", runs=past))


def _replayed_history(request: ModelRequest) -> list[str]:
    return [str(message.content) for message in request.messages if message.from_history]


@pytest.mark.asyncio
@pytest.mark.usefixtures("squad_probe")
async def test_a_team_turn_replays_the_history_its_configured_team_authors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The leader's request replays the one run the team authors, where the defaults would replay every run."""
    team = squad_turn(tmp_path, monkeypatch, leader=[answer("Done.")])
    _store_two_past_runs_under_a_one_run_limit(team)

    await team.stream(show_tool_calls=False)

    [request] = team.models["default"].requests
    assert _replayed_history(request) == _PAST_2_ONLY


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
    _store_two_past_runs_under_a_one_run_limit(team)
    with pytest.raises(ResponsePausedForApproval) as raised:
        await team.stream(show_tool_calls=False)
    team.models["default"] = ScriptedModel([answer("The checker touched it.", usage(50, 5))])
    team.models["checker-model"] = ScriptedModel([answer("Touched.")])

    await team.resume(raised.value.paused, approved=True, approvals=(member_approval("call-touch", "dangerous"),))

    [resumed] = team.models["default"].requests
    assert _replayed_history(resumed) == _PAST_2_ONLY


@pytest.mark.asyncio
@pytest.mark.usefixtures("squad_probe")
async def test_a_private_ad_hoc_team_approval_that_retained_no_scope_resumes_in_its_requesters_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A saved approval without its scope finds the paused run where its requester's private ad hoc team stored it."""
    authored = authored_team_config(
        {
            "writer": {
                "display_name": "Writer",
                "role": "Writes.",
                "model": "writer-model",
                "private": {"per": "user", "root": "writer_data"},
            },
            "checker": {"display_name": "Checker", "role": "Checks.", "model": "checker-model", "tools": [PROBE_TOOL]},
        },
        models=("default", "writer-model", "checker-model"),
    )
    team = build_team_turn(
        tmp_path,
        monkeypatch,
        authored,
        members=("writer", "checker"),
        scripts={
            "default": [delegate("call-team", "checker", "Touch it")],
            "checker-model": [calls(tool_call("touch", "call-touch", label="dangerous"))],
        },
    )
    team.config.tool_approval = ToolApprovalConfig(
        rules=[ApprovalRuleConfig(match="touch", action="require_approval")],
    )
    assert team.scope.scope_id.startswith("team_checker+writer_requester_")
    with pytest.raises(ResponsePausedForApproval) as raised:
        await team.stream(show_tool_calls=False)
    team.models["default"] = ScriptedModel([answer("The checker touched it.")])
    team.models["checker-model"] = ScriptedModel([answer("Touched.")])

    resumed = await team.resume(
        raised.value.paused,
        approved=True,
        approvals=(member_approval("call-touch", "dangerous"),),
        history_scope=None,
    )

    assert isinstance(resumed, CompletedApprovalRun)
    assert resumed.response_text.endswith("The checker touched it.")


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
        team_name=None,
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=identity_for("@alice:localhost"),
    )
    bob_scope = load_bound_scope_session(
        member_names=member_names,
        team_name=None,
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=identity_for("@bob:localhost"),
    )
    assert alice_scope.scope.scope_id.startswith("team_private_worker+shared_requester_")
    assert bob_scope.scope.scope_id.startswith("team_private_worker+shared_requester_")
    assert alice_scope.scope != bob_scope.scope


def test_private_ad_hoc_bound_team_scope_requires_requester_identity(tmp_path: Path) -> None:
    config, _runtime_paths = _private_pair_config(tmp_path)

    with pytest.raises(ValueError, match="Private ad hoc team history scope requires requester identity"):
        resolve_bound_team_scope(
            member_names=["private_worker", "shared"],
            config=config,
            team_name=None,
            requester_user_id=None,
        )
