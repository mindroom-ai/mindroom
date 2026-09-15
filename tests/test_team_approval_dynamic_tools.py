"""Team approval reconstruction retains the frozen function owner for each call."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from agno.agent import Agent
from agno.models.message import Message
from agno.models.response import ToolExecution
from agno.run.agent import RunOutput
from agno.run.base import RunContext, RunStatus
from agno.run.requirement import RunRequirement
from agno.run.team import TeamRunOutput
from agno.session.team import TeamSession
from agno.team import Team
from agno.team._run import _aroute_requirements_to_members_stream
from agno.tools.calculator import CalculatorTools
from agno.tools.function import Function
from openai import AsyncOpenAI

from mindroom.approval_tools import approval_denial_context, toolkit_owners_for_agents
from mindroom.config.main import Config
from mindroom.event_journal import ApprovalCall, ApprovalContinuation
from mindroom.event_journal.approval_continuations import ApprovalDecision
from mindroom.history.session_context import close_team_runtime_state_dbs, open_bound_scope_session_context
from mindroom.history.types import HistoryScope
from mindroom.openai_models import MindRoomOpenAIResponses
from mindroom.response_sources import ResponseSources
from mindroom.response_turn import CompletedApprovalRun, paused_attempt_from_response
from mindroom.synthetic_model import SyntheticModel
from mindroom.teams import (
    TeamMode,
    _attach_team_pause_presentation,
    _member_approval_denials,
    build_materialized_team_instance,
    continue_paused_team_run,
    materialize_exact_team_members,
)
from mindroom.tool_system import dynamic_toolkits
from mindroom.tool_system.runtime_context import ToolDispatchContext
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, serialize_tool_execution_identity
from tests.conftest import bind_runtime_paths, test_runtime_paths, unwrap_extracted_collaborator
from tests.response_runner_helpers import _bot, _noop_typing, _plain_request, _target
from tests.test_openai_native_compaction import _ANSWER, _event, _response

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path


class _MemberAssemblyObservedError(Exception):
    """Stop after observing real rebuilt members, before unrelated team history reads."""


@pytest.mark.asyncio
async def test_team_approval_forwards_frozen_invoking_member_functions(tmp_path: Path) -> None:
    """The coordinator cannot replace frozen member ownership while rebuilding tools."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    target = _target(thread_id="$thread")
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="research",
        requester_id="@user:example.org",
        room_id=target.room_id,
        thread_id=target.resolved_thread_id,
        resolved_thread_id=target.resolved_thread_id,
        session_id="team-session",
    )
    calls = tuple(
        ApprovalCall(
            tool_call_id=call_id,
            tool_name=function_name,
            invoking_agent=member_name,
            toolkit_name="sleep" if member_name == "beta" else "calculator",
            expires_at_ns=2**62,
            decision=ApprovalDecision.APPROVED,
            human_approval_required=True,
        )
        for call_id, function_name, member_name in (
            ("call-one", "add", "alpha"),
            ("call-two", "subtract", "alpha"),
            ("call-three", "sleep", "beta"),
        )
    )
    continuation = ApprovalContinuation(
        approval_id="team-approval",
        run_id="paused-team-run",
        session_id="team-session",
        entity_kind="team",
        entity_name="research",
        room_id=target.room_id,
        thread_id=target.resolved_thread_id,
        requester_id=identity.requester_id,
        response_event_id="$waiting",
        sources=ResponseSources(("$source",), ("$source",)),
        calls=calls,
        state="claimed",
        execution_identity=serialize_tool_execution_identity(identity),
        runtime_model_name="default",
        team_member_names=("alpha", "beta"),
        team_mode="coordinate",
    )
    continued = AsyncMock(return_value=CompletedApprovalRun(response_text="done", metadata_content={}))
    with (
        patch.object(
            runner.deps.tool_runtime,
            "build_dispatch_context",
            return_value=ToolDispatchContext(execution_identity=identity),
        ),
        patch("mindroom.response_runner.continue_paused_team_run", new=continued),
        patch("mindroom.response_runner.typing_indicator", _noop_typing),
    ):
        result = await runner._continue_entity_call(
            continuation,
            request=_plain_request(target, source_event_id="$source"),
            target=target,
            tool_trace_collector=[],
        )
    assert isinstance(result, CompletedApprovalRun)
    assert continued.await_args.kwargs["approval_calls"] == calls
    assert continued.await_args.kwargs["member_names"] == ("alpha", "beta")
    assert continued.await_args.kwargs["decisions"] == dict.fromkeys((call.tool_call_id for call in calls), True)


@pytest.mark.asyncio
async def test_team_approval_restores_tools_only_for_the_frozen_member(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real team member assembly cannot give a sibling the paused owner's required tools."""
    await _exercise_team_member_assembly(tmp_path, monkeypatch)


@pytest.mark.asyncio
async def test_team_approval_discovers_independent_member_tools_concurrently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One member's catalog request cannot prevent another member's discovery from starting."""
    barrier = asyncio.Barrier(2)
    beta_completed = asyncio.Event()

    async def resolve_member_tools(
        name: str,
        calls: tuple[ApprovalCall, ...],
        **_kwargs: object,
    ) -> tuple[str, ...]:
        await barrier.wait()
        if name == "alpha":
            await beta_completed.wait()
            assert [(call.tool_name, call.toolkit_name) for call in calls] == [("add", "calculator")]
            return ("calculator",)
        assert name == "beta"
        assert calls == ()
        beta_completed.set()
        return ()

    async with asyncio.timeout(10):
        await _exercise_team_member_assembly(tmp_path, monkeypatch, required_resolver=resolve_member_tools)


@pytest.mark.asyncio
async def test_team_approval_failed_discovery_cancels_and_drains_siblings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed approval reconstruction must settle other catalog requests before returning."""
    barrier = asyncio.Barrier(2)
    sibling_settled = asyncio.Event()

    async def resolve_member_tools(name: str, *_args: object, **_kwargs: object) -> tuple[str, ...]:
        if name == "alpha":
            await barrier.wait()
            msg = "Synthetic catalog unavailable"
            raise RuntimeError(msg)
        try:
            await barrier.wait()
            await asyncio.Event().wait()
        finally:
            sibling_settled.set()
        msg = "Sibling discovery unexpectedly completed"
        raise AssertionError(msg)

    async with asyncio.timeout(10):
        with pytest.raises(ExceptionGroup) as failure:
            await _exercise_team_member_assembly(tmp_path, monkeypatch, required_resolver=resolve_member_tools)
    assert len(failure.value.exceptions) == 1
    assert isinstance(failure.value.exceptions[0], RuntimeError)
    assert str(failure.value.exceptions[0]) == "Synthetic catalog unavailable"
    assert sibling_settled.is_set()


async def _exercise_team_member_assembly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    required_resolver: Callable[..., Awaitable[tuple[str, ...]]] | None = None,
) -> None:
    """Inspect real member construction after the optional catalog transport boundary."""
    paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config.model_validate(
            {
                "defaults": {"tools": [], "learning": False},
                "agents": {
                    name: {"display_name": name.title(), "tools": [{"calculator": {"defer": True}}]}
                    for name in ("alpha", "beta")
                },
                "teams": {
                    "research": {
                        "display_name": "Research",
                        "role": "Coordinate synthetic calculations",
                        "agents": ["alpha", "beta"],
                    },
                },
                "models": {"default": {"provider": "openai", "id": "test-model"}},
                "tool_approval": {
                    "default": "auto_approve",
                    "rules": [{"match": "add", "action": "require_approval"}],
                },
            },
        ),
        paths,
    )
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="research",
        requester_id="@user:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="team-session",
    )
    monkeypatch.setattr(
        "mindroom.agents._load_agent_model_instance",
        lambda *_args, **_kwargs: SyntheticModel(id="synthetic", tool_call_probability=0),
    )
    if required_resolver is not None:
        monkeypatch.setattr("mindroom.teams.required_approval_tool_names", required_resolver)

    def inspect_members(*, agents: list[Agent], **_kwargs: object) -> None:
        functions = {
            agent.id: {name for toolkit in agent.tools or [] for name in (*toolkit.functions, *toolkit.async_functions)}
            for agent in agents
        }
        assert set(functions) == {"alpha", "beta"}
        assert "add" in functions["alpha"]
        assert "add" not in functions["beta"]
        assert dynamic_toolkits._loaded_tools == {}
        raise _MemberAssemblyObservedError

    dynamic_toolkits._loaded_tools.clear()
    monkeypatch.setattr("mindroom.teams.open_bound_scope_session_context", inspect_members)
    try:
        with pytest.raises(_MemberAssemblyObservedError):
            await continue_paused_team_run(
                member_names=("alpha", "beta"),
                mode=TeamMode.COORDINATE,
                config=config,
                runtime_paths=paths,
                execution_identity=identity,
                session_id="team-session",
                run_id="paused-team-run",
                user_id=identity.requester_id,
                configured_team_name="research",
                model_name="default",
                decisions={"call-one": True},
                denial_reasons={"call-one": None},
                refresh_scheduler=None,
                approval_calls=(
                    ApprovalCall(
                        tool_call_id="call-one",
                        tool_name="add",
                        invoking_agent="alpha",
                        toolkit_name="calculator",
                        expires_at_ns=2**62,
                    ),
                ),
            )
    finally:
        dynamic_toolkits._loaded_tools.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["approved", "denied", "removed"])
async def test_real_team_member_pause_reopens_with_exact_toolkit_owner(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    """Persist a delegated member pause, then restore only its owner after losing selection."""
    paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config.model_validate(
            {
                "defaults": {"tools": [], "learning": False},
                "agents": {
                    name: {"display_name": name.title(), "tools": [{"calculator": {"defer": True}}]}
                    for name in ("alpha", "beta")
                },
                "teams": {
                    "research": {
                        "display_name": "Research",
                        "agents": ["alpha", "beta"],
                        "role": "Coordinate calculations",
                    },
                },
                "models": {"default": {"provider": "openai", "id": "test-model", "api": "responses"}},
                "tool_approval": {"default": "auto_approve", "rules": [{"match": "add", "action": "require_approval"}]},
            },
        ),
        paths,
    )
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="research",
        requester_id="@user:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="team-session",
    )
    requests: list[dict] = []
    executed: list[tuple[float, float]] = []
    original_add = CalculatorTools.add

    def add(self: CalculatorTools, a: float, b: float) -> str:
        executed.append((a, b))
        return original_add(self, a, b)

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) <= 2:
            item = {
                "type": "function_call",
                "id": "fc_delegate" if len(requests) == 1 else "fc_add",
                "call_id": "call_delegate" if len(requests) == 1 else "call_add",
                "status": "completed",
                "name": "delegate_task_to_member" if len(requests) == 1 else "add",
                "arguments": json.dumps(
                    {"member_id": "alpha", "task": "Add 2 and 3"} if len(requests) == 1 else {"a": 2, "b": 3},
                ),
            }
            return httpx.Response(200, json=_response([item]))
        events = _event("response.output_text.delta", delta="Ready", output_index=0, content_index=0)
        events += _event("response.completed", response=_response([_ANSWER]))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=events)

    dynamic_toolkits._loaded_tools.clear()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        client = AsyncOpenAI(api_key="test-key", http_client=http_client)
        monkeypatch.setattr(
            "mindroom.model_loading.get_model_instance",
            lambda *_args, **_kwargs: MindRoomOpenAIResponses(
                id="test-model",
                async_client=client,
                store=False,
            ),
        )
        monkeypatch.setattr(CalculatorTools, "add", add)
        dynamic_toolkits.save_loaded_tools_for_session(
            agent_name="alpha",
            session_id="team-session",
            loaded_tools=["calculator"],
        )
        members = materialize_exact_team_members(
            ["alpha", "beta"],
            config=config,
            runtime_paths=paths,
            execution_identity=identity,
            session_id="team-session",
            supports_native_tool_approval=True,
        )
        history_scope = HistoryScope(kind="team", scope_id="research")
        with open_bound_scope_session_context(
            agents=members.agents,
            session_id="team-session",
            runtime_paths=paths,
            config=config,
            execution_identity=identity,
            team_name="research",
            scope=history_scope,
            create_session_if_missing=True,
        ) as scope:
            assert scope is not None
            team = build_materialized_team_instance(
                requested_agent_names=["alpha", "beta"],
                agents=members.agents,
                mode=TeamMode.COORDINATE,
                config=config,
                runtime_paths=paths,
                scope_context=scope,
                model_name="default",
                configured_team_name="research",
                execution_identity=identity,
            )
            output = await team.arun("Ask alpha to calculate", session_id="team-session", user_id=identity.requester_id)
            assert output.status == RunStatus.paused
            pause = paused_attempt_from_response(
                output,
                fallback_session_id="team-session",
                fallback_run_id=output.run_id,
                toolkit_owners=toolkit_owners_for_agents(members.agents),
            )
            assert pause is not None
            assert pause.requirements[0].member_agent_id == "alpha"
            assert pause.toolkit_owners[("alpha", "add")] == "calculator"
            assert ("beta", "add") not in pause.toolkit_owners
            pause = _attach_team_pause_presentation(
                pause,
                response=output,
                config_names=["alpha", "beta"],
                display_names=["Alpha", "Beta"],
                show_tool_calls=False,
            )
            close_team_runtime_state_dbs(agents=members.agents, team_db=team.db, shared_scope_storage=scope.storage)
        assert executed == []
        dynamic_toolkits._loaded_tools.clear()
        if scenario != "approved":
            config.agents["alpha"].tools = []
        calls = tuple(
            ApprovalCall(
                tool_call_id=tool.tool_call_id,
                tool_name=tool.tool_name,
                invoking_agent="alpha",
                toolkit_name=pause.toolkit_owners[("alpha", "add")],
                expires_at_ns=2**62,
            )
            for tool in pause.tools
        )
        arguments = {
            "member_names": ("alpha", "beta"),
            "mode": TeamMode.COORDINATE,
            "config": config,
            "runtime_paths": paths,
            "execution_identity": identity,
            "session_id": "team-session",
            "run_id": output.run_id,
            "user_id": identity.requester_id,
            "configured_team_name": "research",
            "model_name": "default",
            "refresh_scheduler": None,
            "decisions": {call.tool_call_id: scenario != "denied" for call in calls},
            "denial_reasons": {call.tool_call_id: "Declined by requester" for call in calls},
            "approval_calls": calls,
            "history_scope": history_scope,
            "show_tool_calls": False,
            "prior_presentation_state": pause.response_presentation_state,
            "prior_response_text": pause.response_text,
        }
        if scenario == "removed":
            with pytest.raises(ExceptionGroup):
                await continue_paused_team_run(**arguments)
            assert len(requests) == 2
        else:
            result = await continue_paused_team_run(**arguments)
            assert isinstance(result, CompletedApprovalRun)
            assert len(requests) == 4
            results = [item for item in requests[2]["input"] if item.get("type") == "function_call_output"]
            assert len(results) == 1
            if scenario == "denied":
                assert "Declined by requester" in results[0]["output"]
                assert all(tool.get("name") != "add" for tool in requests[2].get("tools", []))
        assert executed == ([(2, 3)] if scenario == "approved" else [])
        assert dynamic_toolkits._loaded_tools == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("earlier_run", ["none", "ordinary", "approved", "reused_call_id"])
async def test_member_denials_survive_other_runs_on_the_same_actor(earlier_run: str) -> None:
    """One rebuilt member must reject each saved run after its toolkit is removed."""
    executed: list[str] = []

    def harmless() -> str:
        executed.append("harmless")
        return "Harmless result"

    model = SyntheticModel(
        id="synthetic",
        min_response_chars=2,
        max_response_chars=2,
        chars_per_second=0,
        tool_call_probability=0,
    )
    member = Agent(
        id="alpha",
        name="Alpha",
        model=model,
        tools=[Function(name="harmless", entrypoint=harmless)],
        telemetry=False,
    )
    team = Team(id="research", members=[member], model=model, telemetry=False)
    requirements: list[RunRequirement] = []
    runs: list[RunOutput] = []
    calls: list[ApprovalCall] = []
    for number in (0, 1, 2) if earlier_run == "approved" else (1, 2):
        call_id = f"call-{number}"
        tool_name = "harmless" if number == 0 else "removed_tool"
        tool = ToolExecution(
            tool_call_id=call_id,
            tool_name=tool_name,
            tool_args={},
            requires_confirmation=True,
        )
        requirement = RunRequirement(tool)
        requirement.member_agent_id = "alpha"
        requirement.member_run_id = f"member-run-{number}"
        if number == 0:
            requirement.confirm()
        else:
            requirement.reject("Declined by requester")
            calls.append(
                ApprovalCall(
                    tool_call_id=call_id,
                    tool_name=tool_name,
                    invoking_agent="alpha",
                    toolkit_name="removed",
                    expires_at_ns=2**62,
                ),
            )
        requirements.append(requirement)
        runs.append(
            RunOutput(
                run_id=requirement.member_run_id,
                agent_id="alpha",
                session_id="team-session",
                status=RunStatus.paused,
                tools=[tool],
                requirements=[requirement],
                messages=[
                    Message(role="user", content="Do task"),
                    Message(
                        role="assistant",
                        tool_calls=[
                            {"id": call_id, "type": "function", "function": {"name": tool_name, "arguments": "{}"}},
                        ],
                    ),
                ],
            ),
        )
    parent = TeamRunOutput(
        run_id="team-run",
        team_id="research",
        session_id="team-session",
        status=RunStatus.paused,
        requirements=requirements,
    )
    session = TeamSession(session_id="team-session", team_id="research", runs=[parent, *runs])
    context = RunContext(run_id="team-run", session_id="team-session", session_state={})
    denied_calls = {call.tool_call_id: call for call in calls}
    with approval_denial_context(member, _member_approval_denials(member.id, denied_calls, requirements)):
        if earlier_run == "ordinary":
            unrelated = await member.arun("Another task", session_id="other-session")
            assert unrelated.status == RunStatus.completed
            assert not any(message.role == "tool" for message in unrelated.messages or [])

        if earlier_run == "reused_call_id":
            unrelated = deepcopy(runs[0])
            unrelated.run_id = "unrelated-run"
            for requirement in unrelated.requirements or []:
                requirement.member_run_id = unrelated.run_id
            with pytest.raises(ValueError, match="Function call not found"):
                await member.acontinue_run(run_response=unrelated)
            assert not any(message.role == "tool" for message in unrelated.messages or [])

        async for _ in _aroute_requirements_to_members_stream(team, parent, session, [], context):
            pass

    for run in runs:
        assert run.status == RunStatus.completed
        results = [message for message in run.messages or [] if message.role == "tool"]
        assert len(results) == 1
        assert results[0].tool_call_id == (run.tools or [])[0].tool_call_id
        if run.run_id == "member-run-0":
            assert results[0].content == "Harmless result"
        else:
            assert "Declined by requester" in results[0].get_content_string()
            assert (run.tools or [])[0].tool_call_error is True
    assert executed == (["harmless"] if earlier_run == "approved" else [])


def test_member_denial_requires_persisted_run_identity() -> None:
    """A saved denial cannot be rebound to a guessed member run."""
    requirement = RunRequirement(ToolExecution(tool_call_id="call-1", tool_name="removed_tool"))
    requirement.member_agent_id = "alpha"
    call = ApprovalCall(
        tool_call_id="call-1",
        tool_name="removed_tool",
        invoking_agent="alpha",
        expires_at_ns=2**62,
    )
    with pytest.raises(RuntimeError, match="no paused run identity"):
        _member_approval_denials("alpha", {"call-1": call}, [requirement])
