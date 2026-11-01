"""Over-budget requesters' replies use the configured fallback model."""
# ruff: noqa: D103

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import nio
import pytest
from fastapi.testclient import TestClient

from mindroom.ai import TurnResult
from mindroom.background_tasks import wait_for_background_tasks
from mindroom.budgets.monitor import BudgetMonitor
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.budgets import BudgetsConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, ModelPricing, RouterConfig
from mindroom.constants import AI_RUN_METADATA_KEY, resolve_runtime_paths
from mindroom.custom_tools import dynamic_workflow as dynamic_workflow_module
from mindroom.custom_tools.delegate import DelegateTools
from mindroom.custom_tools.dynamic_workflow import DynamicWorkflowTools
from mindroom.delegation.lifecycle import note_child_run_id, prepare_child_turn
from mindroom.dynamic_workflows.spec import RoomAgentParticipant
from mindroom.final_delivery import Cancelled, Completed, Failed, FailureCause, FinalDeliveryOutcome, Owed, TurnEnding
from mindroom.history.request_messages import TurnMessages
from mindroom.history.types import PreparedHistoryState
from mindroom.llm.messages import Message
from mindroom.memory import MemoryPromptParts
from mindroom.post_response_effects import PostResponseEffectsDeps, ResponseOutcome, apply_post_response_effects
from mindroom.providers.identity import model_identity
from mindroom.teams.envelope import PreparedTeamExecution
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, agent_workspace_root_path
from mindroom.turn_model import TeamTurnModelSelection
from tests.api.conftest import publish_api_config
from tests.approval_continuation_helpers import approval_continuation
from tests.budget_helpers import budget_monitor_with_spend, set_month_spend
from tests.conftest import (
    SETTLE_TIMEOUT_SECONDS,
    completed_ai_response_mock,
    install_model_providers,
    patch_response_runner_module,
    unwrap_extracted_collaborator,
)
from tests.fakes.scripted_model import ScriptedModel, answer, usage
from tests.identity_helpers import persist_entity_accounts
from tests.response_runner_helpers import _bot, _noop_typing, _plain_request, _target
from tests.runtime_fakes import bind_test_embedded_runtime, openai_compat_app
from tests.teams.squad import built_team, team_spec
from tests.test_delegate_tools import _delegate_runtime_context, _make_config, _runtime_paths
from tests.test_delegation_direct_audit import _identity
from tests.test_dynamic_workflow_subagents import _config as _subagent_workflow_config
from tests.test_dynamic_workflow_subagents import _spec as _subagent_workflow_spec
from tests.test_dynamic_workflow_subagents import _Workflow as _SubagentWorkflow
from tests.test_dynamic_workflows import _make_context, _make_multi_agent_context, _room_agent_model, _workflow_spec

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.response_runner import ResponseRunner
    from mindroom.teams.members import BuiltTeam

_ALICE = "@alice:localhost"


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

    runtime = await coordinator.prepare_response_runtime(_plain_request(_target()), show_tool_calls=True)

    assert runtime.active_model_name == "luna"


@pytest.mark.asyncio
async def test_agent_reply_keeps_its_model_within_budget(tmp_path: Path) -> None:
    coordinator = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    _budget(coordinator.deps.runtime.config, monthly_limit_usd=None)

    runtime = await coordinator.prepare_response_runtime(_plain_request(_target()), show_tool_calls=True)

    assert runtime.active_model_name == "default"


@pytest.mark.asyncio
async def test_scheduled_model_is_budgeted_too(tmp_path: Path) -> None:
    coordinator = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    config = coordinator.deps.runtime.config
    _budget(config)
    config.models["large"] = ModelConfig(provider="openai", id="gpt-6-astra", pricing=ModelPricing(input=5, output=30))

    runtime = await coordinator.prepare_response_runtime(
        replace(_plain_request(_target()), scheduled_model="large"),
        show_tool_calls=True,
    )

    assert runtime.active_model_name == "luna"


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_a_completed_reply_adds_its_cost_for_its_requester(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    streaming: bool,
) -> None:
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    config = coordinator.deps.runtime.config
    _budget(config, monthly_limit_usd=None)
    assert bot.client is not None
    bot.client.room_send.return_value = nio.RoomSendResponse(event_id="$response", room_id="!room:localhost")
    response_finished = MagicMock()
    monkeypatch.setattr(bot.orchestrator.budgets, "response_finished", response_finished)
    model = ScriptedModel([answer("Done.", usage(1_000, 100))], identity=model_identity(config.models["default"]))

    install_model_providers(monkeypatch, {"default": model})
    with patch_response_runner_module(
        typing_indicator=_noop_typing,
        should_use_streaming=AsyncMock(return_value=streaming),
    ):
        request = _plain_request(_target())
        await coordinator.generate_response(request)
        assert await wait_for_background_tasks(SETTLE_TIMEOUT_SECONDS, owner=coordinator.deps.runtime)

    # 1,000 input tokens at $5 and 100 output tokens at $30 per million, dated by when the turn was requested.
    response_finished.assert_called_once_with("@user:localhost", pytest.approx(0.008), request.requested_at)


def _budgets_with_spend(
    coordinator: ResponseRunner,
    monkeypatch: pytest.MonkeyPatch,
    spend_usd: float,
) -> BudgetMonitor:
    """Install an idle budget monitor whose latest scan of this month charged Alice ``spend_usd``."""
    config = coordinator.deps.runtime.config
    _budget(config, monthly_limit_usd=None)
    monitor = BudgetMonitor(runtime_paths=coordinator.deps.runtime_paths, config_provider=lambda: config)
    set_month_spend(monitor, {_ALICE: spend_usd})
    monkeypatch.setattr(coordinator.deps.runtime.orchestrator, "_budgets", monitor)
    return monitor


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ending", "extra_content", "spend_usd", "scan_requested"),
    [
        (Completed(), {AI_RUN_METADATA_KEY: {"run_id": "run-1", "cost": {"usd": "0.25"}}}, 23.25, False),
        (Completed(), {AI_RUN_METADATA_KEY: {"run_id": "run-1"}}, 23.0, False),
        (Cancelled("user_stop"), None, 23.0, True),
        (Failed(FailureCause.ERROR), None, 23.0, True),
        (Owed(), None, 23.0, True),
    ],
    ids=["priced", "unpriced", "stopped", "failed", "owed"],
)
async def test_a_finished_reply_adds_its_known_cost_or_requests_a_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ending: TurnEnding,
    extra_content: dict[str, object] | None,
    spend_usd: float,
    scan_requested: bool,
) -> None:
    """A stopped, failed, or owed reply reports no cost, though its run's usage is stored, so budgets recount."""
    coordinator = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    monitor = _budgets_with_spend(coordinator, monkeypatch, 23.0)

    await apply_post_response_effects(
        FinalDeliveryOutcome(ending=ending, extra_content=extra_content),
        ResponseOutcome(),
        PostResponseEffectsDeps(
            logger=MagicMock(),
            notify_response_finished=coordinator.locked.response_finished_notifier(
                ("$event",),
                _ALICE,
                datetime.now(UTC),
            ),
        ),
    )

    assert monitor._spend_usd(_ALICE) == spend_usd
    assert monitor._wake.is_set() is scan_requested


def test_a_finished_approval_continuation_recounts_instead_of_adding_its_cost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The latest scan may already hold the usage the run stored before its pause, so the turn's cost is not added."""
    coordinator = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    monitor = _budgets_with_spend(coordinator, monkeypatch, 23.0)

    deps = coordinator.approvals._approval_post_response_deps(approval_continuation(requester_id=_ALICE))
    assert deps.notify_response_finished is not None
    deps.notify_response_finished(4.0)

    assert monitor._spend_usd(_ALICE) == 23.0
    assert monitor._wake.is_set()


def test_budgets_hear_of_a_finished_reply_when_automations_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    monitor = _budgets_with_spend(coordinator, monkeypatch, 23.0)
    automations = MagicMock()
    automations.response_finished.side_effect = RuntimeError("automation store unavailable")
    monkeypatch.setattr(coordinator.deps.runtime.orchestrator, "_automations", automations)

    with pytest.raises(RuntimeError, match="automation store unavailable"):
        coordinator.locked.response_finished_notifier(("$event",), _ALICE, datetime.now(UTC))(0.5)

    assert monitor._spend_usd(_ALICE) == 23.5


def _spent(runtime_paths: RuntimePaths, spend_usd: float) -> BudgetMonitor:
    """Return a monitor reporting ``spend_usd`` for the delegating owner this month."""
    return budget_monitor_with_spend(runtime_paths, {"@alice:example.org": spend_usd})


def _delegation_config() -> Config:
    config = _make_config(
        {
            "leader": AgentConfig(display_name="Leader", delegate_to=["child"]),
            "child": AgentConfig(display_name="Child"),
        },
    )
    _budget(config, monthly_limit_usd=10)
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
    runtime_paths = _runtime_paths(tmp_path)
    child = prepare_child_turn(
        "leader",
        "child",
        "Do the work",
        owner=_owner(),
        config=_delegation_config(),
        runtime_paths=runtime_paths,
        depth=0,
        budget_monitor=_spent(runtime_paths, spend),
        model=None,
        agent_mode="standard",
        previous=None,
        persona=None,
    )

    assert child.model_name == expected


def test_delegated_follow_up_returns_to_the_requested_model_under_budget(tmp_path: Path) -> None:
    """A subagent first run on the fallback uses its requested model again once spend is under the cap."""
    config = _delegation_config()
    runtime_paths = _runtime_paths(tmp_path)
    first = prepare_child_turn(
        "leader",
        "child",
        "Do the work",
        owner=_owner(),
        config=config,
        runtime_paths=runtime_paths,
        depth=0,
        budget_monitor=_spent(runtime_paths, 12.0),
        model=None,
        agent_mode="standard",
        previous=None,
        persona=None,
    )

    follow_up = prepare_child_turn(
        "leader",
        "child",
        "Continue",
        owner=_owner(),
        config=config,
        runtime_paths=runtime_paths,
        depth=0,
        budget_monitor=_spent(runtime_paths, 3.0),
        previous=first,
        model=None,
        agent_mode="standard",
        persona=None,
    )

    assert (first.model_name, first.requested_model_name) == ("luna", "default")
    assert (follow_up.model_name, follow_up.requested_model_name) == ("default", "default")


def test_delegated_follow_up_keeps_a_model_switched_to_during_the_run(tmp_path: Path) -> None:
    """A model the child switched to mid-run is what its follow-ups ask for, with or without budgets."""
    config = _delegation_config()
    config.models["alternate"] = ModelConfig(provider="openai", id="gpt-6-astra")
    runtime_paths = _runtime_paths(tmp_path)
    child = prepare_child_turn(
        "leader",
        "child",
        "Do the work",
        owner=_owner(),
        config=config,
        runtime_paths=runtime_paths,
        depth=0,
        budget_monitor=_spent(runtime_paths, 0.0),
        model=None,
        agent_mode="standard",
        previous=None,
        persona=None,
    )

    note_child_run_id(child, "after-switch", runtime_paths, model_name="alternate")
    follow_up = prepare_child_turn(
        "leader",
        "child",
        "Continue",
        owner=_owner(),
        config=config,
        runtime_paths=runtime_paths,
        depth=0,
        budget_monitor=_spent(runtime_paths, 0.0),
        previous=child,
        model=None,
        agent_mode="standard",
        persona=None,
    )

    assert (follow_up.model_name, follow_up.requested_model_name) == ("alternate", "alternate")


@pytest.mark.asyncio
async def test_direct_delegation_reads_the_orchestrators_budget_monitor(tmp_path: Path) -> None:
    config = _delegation_config()
    runtime_paths = _runtime_paths(tmp_path)
    tools = DelegateTools(
        "leader",
        runtime_paths,
        config,
        agent_workspace_root_path(runtime_paths.storage_root, "leader"),
        execution_identity=_owner(),
    )
    context = replace(
        _delegate_runtime_context(config, runtime_paths, execution_identity=_owner()),
        orchestrator=MagicMock(budgets=_spent(runtime_paths, 12.0)),
    )

    with (
        tool_runtime_context(context),
        patch(
            "mindroom.ai.ai_response",
            new_callable=AsyncMock,
            return_value=TurnResult("Child completed."),
        ) as response,
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
    app = openai_compat_app(runtime_paths)
    publish_api_config(app, config)
    with TestClient(app, base_url="http://localhost") as client:
        yield client


@pytest.mark.parametrize(("authenticated", "expected"), [(True, "luna"), (False, "default")])
def test_openai_compat_agent_completion_budgets_mapped_requesters(
    tmp_path: Path,
    authenticated: bool,
    expected: str,
) -> None:
    with (
        _openai_client(tmp_path, _openai_config(), authenticated=authenticated) as client,
        patch(
            "mindroom.api.openai_compat.ai_response",
            new_callable=completed_ai_response_mock,
            return_value="Hi",
        ) as response,
    ):
        reply = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer alice-key"} if authenticated else {},
            json={"model": "general", "messages": [{"role": "user", "content": "Hello"}]},
        )

    assert reply.status_code == 200
    assert response.await_args.args[0].active_model_name == expected


def _memberless_team(leader: ScriptedModel | None = None) -> BuiltTeam:
    """Return what ``_build_team`` builds for a team whose leader answers on its own: no members materialized."""
    model = leader or ScriptedModel([answer("Team answer")])
    return built_team(team_spec("super-team", "Super Team", model, "You lead the team.", ()))


def _prepared_team_turn() -> PreparedTeamExecution:
    """Return a prepared team turn of one prompt, with no history to replay."""
    return PreparedTeamExecution(
        turn=TurnMessages(prompt=Message(role="user", content="Build it"), context=()),
        history=(),
        summary=None,
        run_metadata=None,
        prepared_history=PreparedHistoryState(native_history=None),
        runtime_model_name="luna",
    )


@pytest.mark.parametrize("stream", [False, True])
def test_openai_compat_team_completion_budgets_priced_models(tmp_path: Path, stream: bool) -> None:
    with (
        _openai_client(tmp_path, _openai_config(), authenticated=True) as client,
        patch("mindroom.api.openai_compat._build_team", return_value=_memberless_team()) as build,
        patch(
            "mindroom.api.openai_compat._prepare_openai_team_execution",
            new=AsyncMock(return_value=_prepared_team_turn()),
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
async def test_dynamic_workflow_room_agent_participant_uses_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _make_multi_agent_context(tmp_path, room_agents=["general", "specialist"])
    _budget_context_config(context.config)
    _model, loaded = _room_agent_model(monkeypatch, answer("done"))

    await dynamic_workflow_module._execute_room_agent_participant(
        context,
        RoomAgentParticipant.model_validate({"id": "writer", "kind": "room_agent", "agent": "specialist"}),
        "Write a report.",
        run_key="report:run_1",
    )

    assert loaded == ["luna"]


@pytest.mark.asyncio
async def test_dynamic_workflow_subagent_participant_uses_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _subagent_workflow_config()
    _budget(config)
    workflow = _SubagentWorkflow(tmp_path, monkeypatch, config)

    run = await workflow.run(
        _subagent_workflow_spec([{"id": "writer", "system_prompt": "Write.", "tools": [], "model": "default"}]),
    )

    assert run["status"] == "completed", run
    assert workflow.models_loaded == ["luna"]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("model", ["general", "team/super_team"])
def test_openai_compat_completion_adds_its_cost_for_its_requester(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    stream: bool,
) -> None:
    config = _openai_config()
    luna = ScriptedModel([answer("Hi", usage(1_000, 100))], identity=model_identity(config.models["luna"]))
    install_model_providers(monkeypatch, {"luna": luna})
    monitor = budget_monitor_with_spend(_runtime_paths(tmp_path), {})
    with (
        _openai_client(tmp_path, config, authenticated=True) as client,
        patch.object(monitor, "response_finished") as response_finished,
        patch("mindroom.ai.build_memory_prompt_parts", new=AsyncMock(return_value=MemoryPromptParts())),
        patch("mindroom.api.openai_compat._build_team", return_value=_memberless_team(luna)),
        patch(
            "mindroom.api.openai_compat._prepare_openai_team_execution",
            new=AsyncMock(return_value=_prepared_team_turn()),
        ),
    ):
        bind_test_embedded_runtime(client.app, budget_monitor=monitor)
        reply = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer alice-key"},
            json={"model": model, "messages": [{"role": "user", "content": "Hello"}], "stream": stream},
        )

    assert reply.status_code == 200
    # Alice is over her zero cap, so the reply runs on luna: 1,000 input tokens at $0.20 and 100 at $1.25 per million.
    response_finished.assert_called_once_with("@alice:localhost", pytest.approx(0.000325), ANY)
    assert response_finished.call_args.args[2] <= datetime.now(UTC)


async def _run_workflow_as(workflow: _SubagentWorkflow, active_model_name: str) -> dict[str, object]:
    """Create and run the workflow from a reply that runs on ``active_model_name``."""
    spec = _subagent_workflow_spec([{"id": "writer", "system_prompt": "Write.", "tools": []}], models=["default-model"])
    context = replace(
        _delegate_runtime_context(workflow.config, workflow.paths, execution_identity=_identity()),
        active_model_name=active_model_name,
    )
    with tool_runtime_context(context):
        created = json.loads(await workflow.tools.create_workflow(spec))
        assert created["status"] == "ok", created
        return json.loads(await workflow.tools.run_workflow(workflow_id="review", input={}))


@pytest.mark.asyncio
async def test_over_budget_caller_runs_saved_workflows_on_the_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A workflow that names the caller's priced model keeps working once the caller is over budget."""
    config = _subagent_workflow_config()
    _budget(config)
    workflow = _SubagentWorkflow(tmp_path, monkeypatch, config)

    # The response runner hands tools the budgeted model of the reply.
    run = await _run_workflow_as(workflow, "luna")

    assert run["status"] == "completed", run
    assert workflow.models_loaded == ["luna"]


@pytest.mark.asyncio
async def test_workflow_permissions_still_bound_over_budget_callers(tmp_path: Path) -> None:
    context = _make_context(tmp_path)
    _budget_context_config(context.config)
    context.config.models["opus"] = ModelConfig(
        provider="anthropic",
        id="claude-opus-5",
        pricing=ModelPricing(input=5, output=25),
    )
    context = replace(context, active_model_name="luna")
    spec = _workflow_spec()
    spec["participants"][0]["model"] = "claude-opus-5"  # type: ignore[index]

    with tool_runtime_context(context):
        payload = json.loads(await DynamicWorkflowTools().create_workflow(spec))

    assert payload["status"] == "error"
    assert "not allowed by permissions.models" in payload["message"]


@pytest.mark.asyncio
async def test_a_reply_that_crosses_the_cap_still_runs_its_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller's reply started on the priced model; its workflow then runs on the fallback."""
    config = _subagent_workflow_config()
    _budget(config)
    workflow = _SubagentWorkflow(tmp_path, monkeypatch, config)

    run = await _run_workflow_as(workflow, "default")

    assert run["status"] == "completed", run
    assert workflow.models_loaded == ["luna"]
