"""Durable delegated runs retain exact parent waits across child approvals."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import pytest
from agno.agent import Agent
from agno.exceptions import ModelProviderError
from agno.metrics import RunMetrics
from agno.models.response import ModelResponse
from agno.run.agent import RunErrorEvent, RunOutput
from agno.run.base import RunStatus
from agno.run.team import TeamRunOutput
from agno.team import Team
from agno.tools import Toolkit
from agno.tools.function import Function

from mindroom.agent_storage import create_session_storage
from mindroom.agents import apply_tool_approval_capability
from mindroom.ai import run_delegated_child_response
from mindroom.approval_execution import _collect_agent_continuation
from mindroom.approval_response import require_ordered_pause_presentation
from mindroom.approval_tools import toolkit_owners_for_agents
from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig
from mindroom.custom_tools.delegate import DelegateTools
from mindroom.delegation.execution import (
    drive_delegation_stream,
    drive_delegations,
)
from mindroom.delegation.lifecycle import note_child_run_id
from mindroom.delegation.records import DelegationRecordLocator, DelegationRecordOwner
from mindroom.delegation.recovery import _cancel_delegations, cancel_approval_delegations
from mindroom.delegation.state import DelegationState
from mindroom.event_journal import ApprovalCall, ApprovalContinuation
from mindroom.response_sources import ResponseSources
from mindroom.response_turn import ResponsePausedForApproval, paused_attempt_from_response
from mindroom.teams import (
    _attach_team_pause_presentation,
    _collect_team_continuation,
    _continued_team_pause,
    _TeamStreamPresentation,
)
from mindroom.tool_system.events import CollectedStreamPresentation
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.access_schema_support import with_responder_access
from tests.history_helpers import RecordingModel
from tests.test_delegate_tools import _delegate_runtime_context, _runtime_paths

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from agno.db.base import BaseDb
    from agno.models.message import Message

    from mindroom.constants import RuntimePaths
    from mindroom.delegation.state import DelegationChild


@dataclass
class DelegationModel(RecordingModel):
    """Deterministic model runs real Agno tool and pause machinery."""

    responses: list[ModelResponse] = field(default_factory=list)

    async def ainvoke(self, *_args: object, **kwargs: object) -> ModelResponse:
        """Consume one planned provider response."""
        self.seen_messages = list(cast("list[Message]", kwargs.get("messages", [])))
        return self.responses.pop(0)

    async def ainvoke_stream(self, *_args: object, **kwargs: object) -> AsyncIterator[ModelResponse]:
        """Use planned responses through Agno's real streaming machinery."""
        yield await self.ainvoke(*_args, **kwargs)


def _call(name: str, call_id: str, **arguments: object) -> dict[str, object]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _saved_approval_calls(state: DelegationState) -> tuple[ApprovalCall, ...]:
    """Supply the saved journal calls when a test drives delegation without Matrix publication."""
    calls = []
    for tool in state.pending_tools:
        call_id = str(tool["tool_call_id"])
        if state.pending_child_id is None:
            assert tool["tool_name"] in {"run_subagent", "continue_subagent"}
            assert state.pending_agent_name is not None
            invoking_agent, toolkit_name = state.pending_agent_name, "delegate"
        else:
            source = state.pending_tool_sources[call_id]
            invoking_agent, toolkit_name = source.child.child_agent_name, source.toolkit_name
        calls.append(
            ApprovalCall(
                tool_call_id=call_id,
                tool_name=str(tool["tool_name"]),
                invoking_agent=invoking_agent,
                toolkit_name=toolkit_name,
                expires_at_ns=2**62,
            ),
        )
    return tuple(calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["approve", "deny", "cancel", "cancel_removed", "cancel_completed", "revoke"])
@pytest.mark.parametrize(
    ("siblings", "nested", "retry"),
    [(1, False, False), (2, False, False), (1, True, False), (1, False, True)],
)
@pytest.mark.parametrize("team_parent", [False, True])
async def test_child_approval_survives_parent_reconstruction(  # noqa: C901, PLR0912, PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    siblings: int,
    nested: bool,
    retry: bool,
    team_parent: bool,
) -> None:
    """Approval pauses the child before its side effect and resumes the exact parent."""
    paths = _runtime_paths(tmp_path)
    config = with_responder_access(
        Config(
            agents={
                "leader": AgentConfig(display_name="Leader", delegate_to=["code"]),
                "code": AgentConfig(display_name="Code", tools=["file"], delegate_to=["code"]),
                "other": AgentConfig(display_name="Other", delegate_to=["code"]),
            },
            defaults=DefaultsConfig(tools=[]),
            memory={"backend": "none"},
        ),
        "code",
        users=["@alice:example.org"],
    )
    identity = ToolExecutionIdentity(
        "matrix",
        "leader",
        "@alice:example.org",
        "!room:example.org",
        None,
        None,
        "parent",
    )
    if outcome == "cancel_removed":
        config.agents["code"].private = AgentPrivateConfig(per="user", root="private_notes")
        config.agents["leader"].private = AgentPrivateConfig(per="user", root="private_notes")
    side_effects: list[str] = []

    async def write_report() -> str:
        side_effects.append("written")
        return "report written"

    async def run_subagent(agent_name: str, task: str) -> str:
        del agent_name, task
        pytest.fail("External delegate body must never be executed by Agno")

    parent_storage = create_session_storage("leader", config, paths, identity)
    external = Function.from_callable(run_subagent)
    external.external_execution = True
    external.owning_toolkit = "delegate"
    parent = Agent(
        name="leader",
        db=parent_storage,
        tools=[external],
        model=DelegationModel(
            id="test",
            responses=[
                ModelResponse(
                    tool_calls=[
                        _call("run_subagent", f"delegate-{index}", agent_name="code", task="Write report")
                        for index in range(1 if team_parent else siblings)
                    ],
                ),
            ],
        ),
    )
    if team_parent:
        members = [parent]
        if siblings == 2:
            members.append(
                Agent(
                    id="other",
                    name="Other",
                    tools=[external],
                    model=DelegationModel(
                        id="test",
                        responses=[
                            ModelResponse(
                                tool_calls=[
                                    _call("run_subagent", "delegate-0", agent_name="code", task="Write report"),
                                ],
                            ),
                        ],
                    ),
                ),
            )
        parent = Team(
            id="squad",
            name="Squad",
            db=parent_storage,
            members=members,
            delegate_to_all_members=siblings == 2,
            model=DelegationModel(
                id="test",
                responses=[
                    ModelResponse(
                        tool_calls=[
                            _call("delegate_task_to_members", "member-call", task="Delegate report")
                            if siblings == 2
                            else _call(
                                "delegate_task_to_member",
                                "member-call",
                                member_id="leader",
                                task="Delegate report",
                            ),
                        ],
                    ),
                ],
            ),
        )
    child_storages = []
    child_responses = []
    for _index in range(siblings):
        if nested:
            child_responses.append(
                ModelResponse(
                    tool_calls=[_call("run_subagent", "delegate-0", agent_name="code", task="Nested report")],
                ),
            )
        child_responses.extend(
            [ModelResponse(tool_calls=[_call("write_report", "same-call")]), ModelResponse(content="child result")],
        )
        if nested:
            child_responses.append(ModelResponse(content="child result"))

    def build_child(*args: object, **kwargs: object) -> Agent:
        child_identity = cast("ToolExecutionIdentity", args[3])
        storage = cast("BaseDb | None", kwargs.get("history_storage")) or create_session_storage(
            "code",
            config,
            paths,
            child_identity,
        )
        child_storages.append(storage)
        gated = Function.from_callable(write_report)
        gated.requires_confirmation = True
        gated.owning_toolkit = "file"
        child_delegate = Function.from_callable(run_subagent)
        child_delegate.external_execution = True
        child_delegate.owning_toolkit = "delegate"
        return Agent(
            id="code",
            name="code",
            db=storage,
            tools=[gated, child_delegate],
            model=DelegationModel(id="test", responses=child_responses),
        )

    async def start_child(
        prepared: DelegationChild,
        *,
        prompt: str,
        config: Config,
        runtime_paths: RuntimePaths,
        refresh_scheduler: object,
        supports_native_tool_approval: bool,
    ) -> str:
        assert runtime_paths == paths
        assert supports_native_tool_approval
        assert refresh_scheduler is None
        agent_name = prepared.child_agent_name
        task = prompt
        session_id = prepared.session_id
        run_id = prepared.run_id
        child_identity = replace(identity, agent_name=agent_name, session_id=session_id)
        child = build_child(agent_name, config, paths, child_identity)
        if retry:
            empty_attempt = Agent(
                name="code",
                db=child.db,
                model=DelegationModel(id="test", responses=[ModelResponse(content="")]),
            )
            await empty_attempt.arun(task, session_id=session_id, run_id=run_id, user_id=identity.requester_id)
            run_id = uuid4().hex
        note_child_run_id(prepared, run_id, paths)
        context = _delegate_runtime_context(config, paths, execution_identity=child_identity)
        with tool_runtime_context(replace(context, agent_name=agent_name)):
            response = await child.arun(
                task,
                session_id=session_id,
                run_id=run_id,
                user_id=identity.requester_id,
            )
            response = await drive_delegations(
                child,
                response,
                run_child=start_child,
                agent_name=agent_name,
                config=config,
                runtime_paths=paths,
                execution_identity=child_identity,
                delegation_depth=prepared.depth,
            )
            paused_child = paused_attempt_from_response(
                response,
                fallback_session_id=session_id,
                fallback_run_id=run_id,
                toolkit_owners=toolkit_owners_for_agents([child]),
            )
            if paused_child is not None:
                raise ResponsePausedForApproval(paused_child)
        return "ignored; persisted outcome owns success"

    monkeypatch.setattr("mindroom.agents.create_agent", build_child)
    team_presentation: _TeamStreamPresentation | None = None
    try:
        with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=identity)):
            response = await parent.arun("delegate", session_id="parent", user_id=identity.requester_id)
            paused = await drive_delegations(
                parent,
                response,
                run_child=start_child,
                agent_name="leader",
                config=config,
                runtime_paths=paths,
                execution_identity=identity,
                member_config_names={"leader": "leader", "other": "other"},
            )
            state = DelegationState.from_metadata(paused.metadata)
            assert paused.status == RunStatus.paused
            assert side_effects == []
            assert state.pending_agent_name == "code"
            assert state.pending_tools[0]["tool_call_id"].startswith(f"{state.children[0].delegation_id}:")
            assert state.pending_tools[0]["tool_call_id"].endswith(":same-call")
            assert parent_storage.get_run(response.run_id).status == RunStatus.paused

            if team_parent:
                team_pause = paused_attempt_from_response(
                    paused,
                    fallback_session_id="parent",
                    fallback_run_id=response.run_id,
                    toolkit_owners={},
                )
                assert team_pause is not None
                config_names = ["leader", "other"] if siblings == 2 else ["leader"]
                display_names = ["leader", "Other"] if siblings == 2 else ["leader"]
                blocking_pause = _attach_team_pause_presentation(
                    team_pause,
                    response=paused,
                    config_names=config_names,
                    display_names=display_names,
                    show_tool_calls=True,
                )
                team_presentation = _TeamStreamPresentation.new(config_names, display_names, show_tool_calls=True)
                streaming_pause = _continued_team_pause(team_presentation, team_pause)
                for presented in (blocking_pause, streaming_pause):
                    assert presented.approval_agent_name == "code"
                    require_ordered_pause_presentation(presented, show_tool_calls=True)

            if outcome in {"cancel", "cancel_removed", "cancel_completed"}:
                original_config = config.model_copy(update={"agents": dict(config.agents)})
                child = state.children[0]
                child_identity = replace(identity, agent_name="code", session_id=child.session_id)
                if outcome == "cancel_completed":
                    # Simulate a crash after the child committed completion but
                    # before its parent and audit record received that outcome.
                    storage = create_session_storage("code", config, paths, child_identity)
                    try:
                        completed = storage.get_run(child.run_id)
                        completed.status = RunStatus.completed
                        completed.content = "Saved result"
                        completed.tools = []
                        completed.requirements = []
                        storage.upsert_run(run=completed, session_id=child.session_id, user_id=identity.requester_id)
                    finally:
                        storage.close()
                if outcome == "cancel_removed":
                    config.agents.clear()
                initial_pause = paused_attempt_from_response(
                    paused,
                    fallback_session_id="parent",
                    fallback_run_id=response.run_id,
                    toolkit_owners={},
                )
                if team_parent:
                    await _cancel_delegations(paused, config=config, runtime_paths=paths)
                else:
                    await cancel_approval_delegations(
                        ApprovalContinuation(
                            approval_id="approval",
                            run_id=response.run_id,
                            session_id="parent",
                            entity_kind="agent",
                            entity_name="leader",
                            room_id=identity.room_id,
                            thread_id=None,
                            requester_id=identity.requester_id,
                            response_event_id="$response",
                            sources=ResponseSources(("$source",), ("$source",)),
                            calls=(),
                            state="failing",
                            execution_identity=state.children[0].execution_identity
                            | {"agent_name": "leader", "session_id": "parent"},
                            delegation_storage_bindings=initial_pause.delegation_storage_bindings,
                        ),
                        config=config,
                        runtime_paths=paths,
                        reason="Cancelled by requester",
                    )
                expected_status = "completed" if outcome == "cancel_completed" else "cancelled"
                storage = create_session_storage("code", original_config, paths, child_identity)
                try:
                    assert storage.get_run(child.run_id).status == (
                        RunStatus.completed if outcome == "cancel_completed" else RunStatus.cancelled
                    )
                finally:
                    storage.close()
                record = await DelegationRecordOwner(original_config, paths).reopen(
                    DelegationRecordLocator.from_dict(child.record_locator),
                )
                assert json.loads((record.record_dir / "run.json").read_text())["status"] == expected_status
                assert side_effects == []
                return
            if outcome == "revoke":
                config.agents["leader"].delegate_to = []
                config.agents["other"].delegate_to = []
            observed_ids = set()
            presentation = CollectedStreamPresentation(show_tool_calls=True, track_hidden_tools=True)
            initial_pause = paused_attempt_from_response(
                paused,
                fallback_session_id="parent",
                fallback_run_id=response.run_id,
                toolkit_owners={},
            )
            for tool in initial_pause.tools:
                presentation.start_tool(tool)
            for index in range(siblings):
                rebuilt = Agent(
                    name="leader",
                    db=parent_storage,
                    tools=[external],
                    model=DelegationModel(id="test", responses=[ModelResponse(content="parent result")]),
                )
                if team_parent:
                    rebuilt.model = DelegationModel(id="test", responses=[ModelResponse(content="member result")])
                    members = [rebuilt]
                    if siblings == 2:
                        members.append(
                            Agent(
                                id="other",
                                name="Other",
                                tools=[external],
                                model=DelegationModel(id="test", responses=[ModelResponse(content="member result")]),
                            ),
                        )
                    rebuilt = Team(
                        id="squad",
                        name="Squad",
                        db=parent_storage,
                        members=members,
                        delegate_to_all_members=siblings == 2,
                        model=DelegationModel(id="test", responses=[ModelResponse(content="parent result")]),
                    )
                persisted = await rebuilt.aget_run_output(response.run_id, session_id="parent")
                assert isinstance(persisted, (RunOutput, TeamRunOutput))
                state = DelegationState.from_metadata(persisted.metadata)
                call_id = state.pending_tools[0]["tool_call_id"]
                assert call_id not in observed_ids
                observed_ids.add(call_id)
                assert sum(child.status == "completed" for child in state.children) == index
                options = {
                    "agent_name": "leader",
                    "config": config,
                    "runtime_paths": paths,
                    "execution_identity": identity,
                    "member_config_names": {"leader": "leader", "other": "other"},
                    "decisions": {call_id: outcome in {"approve", "parent_provider_error", "child_provider_error"}},
                    "denial_reasons": {call_id: None},
                    "approval_calls": (
                        ApprovalCall(
                            tool_call_id=call_id,
                            tool_name="write_report",
                            invoking_agent="code",
                            toolkit_name="file",
                            expires_at_ns=2**62,
                        ),
                    ),
                }

                async def stored_run(
                    persisted_response: RunOutput | TeamRunOutput = persisted,
                ) -> AsyncIterator[RunOutput | TeamRunOutput]:
                    yield persisted_response

                events = drive_delegation_stream(rebuilt, stored_run(), run_child=start_child, **options)
                if team_parent:
                    assert team_presentation is not None
                    continuation = _collect_team_continuation(events, team_presentation, progress=None)
                else:
                    continuation = _collect_agent_continuation(events, presentation, progress=None)
                if outcome == "parent_provider_error":
                    with pytest.raises(RuntimeError, match="provider connection lost") as caught:
                        await continuation
                    assert "example-secret" not in str(caught.value)
                    assert side_effects == ["written"]
                    failed_run = await rebuilt.aget_run_output(response.run_id, session_id="parent")
                    assert failed_run.status == RunStatus.error
                    return
                collected = await continuation
                if team_parent:
                    resumed = collected
                else:
                    resumed = collected.response
                    presentation.append_text(collected.terminal_content)
                visible_trace = team_presentation.tool_trace if team_parent else presentation.tool_trace
                assert any(
                    entry.tool_call_id == call_id and entry.type == "tool_call_completed" for entry in visible_trace
                )
                if resumed.status == RunStatus.completed:
                    break
                assert resumed.status == RunStatus.paused
                next_pause = paused_attempt_from_response(
                    resumed,
                    toolkit_owners={},
                    fallback_session_id="parent",
                    fallback_run_id=response.run_id,
                )
                assert next_pause is not None
                if team_parent:
                    require_ordered_pause_presentation(
                        _continued_team_pause(team_presentation, next_pause),
                        show_tool_calls=True,
                    )
                else:
                    require_ordered_pause_presentation(
                        replace(
                            next_pause,
                            response_text=presentation.final_text(),
                            tool_trace=tuple(presentation.tool_trace),
                        ),
                        show_tool_calls=True,
                    )
                assert len(side_effects) == (index + 1 if outcome == "approve" else 0)
            assert resumed.status == RunStatus.completed
            assert resumed.content == "parent result"
            assert side_effects == (["written"] * siblings if outcome in {"approve", "child_provider_error"} else [])
            messages = resumed.member_responses[0].messages if team_parent else resumed.messages
            result_message = next(message.content for message in messages if message.tool_call_id == "delegate-0")
            if outcome == "child_provider_error":
                assert "provider connection lost" in result_message
                assert "example-secret" not in result_message
                return
            expected_result = (
                "Cannot delegate"
                if outcome == "revoke"
                else "resume failed"
                if outcome == "resume_error"
                else "child result"
            )
            assert expected_result in result_message
            if outcome == "resume_error":
                records = [
                    json.loads(path.read_text())
                    for path in tmp_path.glob("agents/*/workspace/.mindroom/delegations/*/*/run.json")
                ]
                assert sorted(record["status"] for record in records) == ["cancelled", "failed"]
                for storage in child_storages:
                    for session in storage.get_sessions():
                        assert all(run.status != RunStatus.paused and not run.requirements for run in session.runs)
    finally:
        parent_storage.close()
        for storage in child_storages:
            storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("team_parent", [False, True])
@pytest.mark.parametrize("failed_entity", ["parent", "child"])
@pytest.mark.parametrize("terminal_output", [False, True])
async def test_delegated_approval_preserves_redacted_provider_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    team_parent: bool,
    failed_entity: str,
    terminal_output: bool,
) -> None:
    """A provider failure after the approved write retains its cause without replaying the write."""
    original_stream = DelegationModel.ainvoke_stream

    async def failing_stream(self: DelegationModel, *args: object, **kwargs: object) -> AsyncIterator[ModelResponse]:
        if self.responses and self.responses[0].content == f"{failed_entity} result":
            yield ModelResponse(content="Earlier partial answer")
            raise ModelProviderError(message="provider connection lost: api_key=example-secret")
        async for event in original_stream(self, *args, **kwargs):
            yield event

    monkeypatch.setattr(DelegationModel, "ainvoke_stream", failing_stream)
    if terminal_output:
        original_continue = Agent.acontinue_run

        async def with_terminal_output(self: Agent, *args: object, **kwargs: object) -> AsyncIterator[object]:
            last_event = None
            async for event in original_continue(self, *args, **kwargs):
                last_event = event
                yield event
            if isinstance(last_event, RunErrorEvent):
                terminal = await self.aget_run_output(last_event.run_id, session_id=last_event.session_id)
                assert terminal is not None
                terminal.metrics = RunMetrics(input_tokens=17, output_tokens=3)
                yield terminal

        monkeypatch.setattr(Agent, "acontinue_run", with_terminal_output)
    await test_child_approval_survives_parent_reconstruction(
        tmp_path,
        monkeypatch,
        outcome=f"{failed_entity}_provider_error",
        siblings=1,
        nested=False,
        retry=False,
        team_parent=team_parent,
    )
    if terminal_output and failed_entity == "child":
        records = [
            json.loads(path.read_text())
            for path in tmp_path.glob("agents/*/workspace/.mindroom/delegations/*/*/run.json")
        ]
        assert len(records) == 1
        assert records[0]["usage"]["input_tokens"] == 17
        assert records[0]["usage"]["output_tokens"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("team_parent", [False, True])
async def test_failed_nested_resume_settles_descendants_and_visible_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    team_parent: bool,
) -> None:
    """A failed reconstruction leaves no descendant approval or visible tool pending."""

    async def failed_resume(*_args: object, **_kwargs: object) -> RunOutput:
        message = "resume failed"
        raise RuntimeError(message)

    monkeypatch.setattr("mindroom.delegation.execution._continue_child", failed_resume)
    await test_child_approval_survives_parent_reconstruction(
        tmp_path,
        monkeypatch,
        outcome="resume_error",
        siblings=1,
        nested=True,
        retry=False,
        team_parent=team_parent,
    )


def test_native_delegate_uses_external_execution() -> None:
    """A native parent must pause before entering the child call stack."""

    async def run_subagent(agent_name: str, task: str) -> str:
        return f"{agent_name}: {task}"

    toolkit = Toolkit(name="delegate", tools=[run_subagent])
    apply_tool_approval_capability(
        toolkit,
        Config(),
        supports_native_tool_approval=True,
        registered_tool_name="delegate",
    )
    assert toolkit.async_functions["run_subagent"].external_execution is True


def test_detached_delegate_retains_inline_execution() -> None:
    """Detached callers cannot take ownership of Matrix approvals."""

    async def run_subagent(agent_name: str, task: str) -> str:
        return f"{agent_name}: {task}"

    toolkit = Toolkit(name="delegate", tools=[run_subagent])
    apply_tool_approval_capability(
        toolkit,
        Config(),
        supports_native_tool_approval=False,
        registered_tool_name="delegate",
    )
    assert not toolkit.async_functions["run_subagent"].external_execution


@pytest.mark.asyncio
async def test_delegate_policy_denial_never_starts_child(tmp_path: Path) -> None:
    """External execution must retain the policy gate on run_subagent itself."""
    paths = _runtime_paths(tmp_path)
    config = with_responder_access(
        Config(
            agents={
                "leader": AgentConfig(display_name="Leader", delegate_to=["code"]),
                "code": AgentConfig(display_name="Code"),
            },
            defaults=DefaultsConfig(tools=[]),
            memory={"backend": "none"},
            tool_approval={"default": "require_approval"},
        ),
        "code",
        users=["@alice:example.org"],
    )
    identity = ToolExecutionIdentity(
        "matrix",
        "leader",
        "@alice:example.org",
        "!room:example.org",
        None,
        None,
        "parent",
    )
    toolkit = DelegateTools("leader", ["code"], paths, config, execution_identity=identity)
    apply_tool_approval_capability(toolkit, config, supports_native_tool_approval=True, registered_tool_name="delegate")
    storage = create_session_storage("leader", config, paths, identity)
    parent = Agent(
        name="leader",
        db=storage,
        tools=[toolkit],
        model=DelegationModel(
            id="test",
            responses=[
                ModelResponse(
                    tool_calls=[_call("run_subagent", "policy-gated", agent_name="code", task="Write report")],
                ),
                ModelResponse(content="Denied safely"),
            ],
        ),
    )
    try:
        with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=identity)):
            response = await parent.arun("Delegate", session_id="parent", user_id=identity.requester_id)
            response = await drive_delegations(
                parent,
                response,
                run_child=run_delegated_child_response,
                agent_name="leader",
                config=config,
                runtime_paths=paths,
                execution_identity=identity,
            )
            state = DelegationState.from_metadata(response.metadata)
            assert state.children == []
            assert state.pending_tools[0]["tool_call_id"] == "policy-gated"
            response = await drive_delegations(
                parent,
                response,
                run_child=run_delegated_child_response,
                agent_name="leader",
                config=config,
                runtime_paths=paths,
                execution_identity=identity,
                decisions={"policy-gated": False},
                denial_reasons={"policy-gated": "No"},
            )
            assert response.status == RunStatus.completed
            assert DelegationState.from_metadata(response.metadata).children == []
            assert "child was not executed" in next(
                message.content for message in response.messages if message.tool_call_id == "policy-gated"
            )
    finally:
        storage.close()
