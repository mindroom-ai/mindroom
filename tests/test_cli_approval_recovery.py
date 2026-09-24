"""CLI approval recovery uses the existing claim and normal response driver."""

# ruff: noqa: ANN001, ANN003, ANN202, ARG002, PLR0915

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from functools import partial
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from agno.knowledge.knowledge import Knowledge
from agno.learn import LearningMachine
from agno.media import Audio, Image
from agno.models.message import Message
from agno.models.response import ToolExecution
from agno.run import RunContext
from agno.run.agent import RunOutput
from agno.run.requirement import RunRequirement
from agno.session.agent import AgentSession
from agno.tools.function import Function, ToolResult
from agno.tools.toolkit import Toolkit

from mindroom import agents, approval_execution, approval_tools, cli_approval_recovery, minimal_agent
from mindroom.agent_cli.approval import CliApprovalCall
from mindroom.agent_cli.events import emit_cli_suspension, project_cli_execution
from mindroom.agent_cli.lifetime import current_cli_lifetime, response_cli_lifetime
from mindroom.agent_cli.protocol import ToolCallOperation
from mindroom.agent_cli.session import TurnToolRegistry
from mindroom.agent_storage import create_session_storage, create_state_storage
from mindroom.agno_compat_cli_checkpoint import ProviderBatchCheckpoint
from mindroom.config.agent import AgentConfig
from mindroom.event_journal import ApprovalCall, ApprovalContinuation, ApprovalDecision
from mindroom.history.session_context import close_agent_runtime_state_dbs
from mindroom.media_inputs import MediaInputs
from mindroom.response_sources import ResponseSources
from mindroom.response_turn import CompletedApprovalRun, ResponsePausedForApproval, apply_exact_approval_decisions
from mindroom.tool_system.agent_tool_calls import PreparedAgentToolCatalog
from mindroom.tool_system.events import CollectedStreamPresentation, serialize_tool_trace
from mindroom.tool_system.runtime_context import (
    LiveToolDispatchContext,
    build_execution_identity_from_runtime_context,
    tool_runtime_context,
)
from mindroom.tools.shell import shell_tools
from tests.conftest import unwrap_extracted_collaborator
from tests.identity_helpers import persist_entity_accounts
from tests.response_runner_helpers import _bot
from tests.test_agent_cli_authority import _runtime_context
from tests.test_agent_tool_calls import _catalog
from tests.test_approval_dynamic_continuation import _ScriptedModel

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_approval_tool_restore_requires_current_dynamic_manager_assignment(tmp_path: Path) -> None:
    """Native and hidden approvals can restore a loader only while deferred tools remain assigned."""
    runtime = _runtime_context(tmp_path)
    runtime.config.agents["helper"] = AgentConfig(
        display_name="Helper",
        tools=["shell", {"sleep": {"defer": True}}],
    )
    call = ApprovalCall("loader", "load_tool", "helper", 100, toolkit_name="dynamic_tools")
    identity = build_execution_identity_from_runtime_context(runtime)
    required = await approval_tools.required_approval_tool_names(
        "helper",
        (call,),
        config=runtime.config,
        runtime_paths=runtime.runtime_paths,
        execution_identity=identity,
    )
    assert required == ("dynamic_tools",)
    runtime.config.agents["helper"].tools = ["shell"]
    with pytest.raises(RuntimeError, match="no longer permitted"):
        await approval_tools.required_approval_tool_names(
            "helper",
            (call,),
            config=runtime.config,
            runtime_paths=runtime.runtime_paths,
            execution_identity=identity,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("continuation_count", [2, 4])
async def test_recovered_dynamic_call_retains_response_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    continuation_count: int,
) -> None:
    """Recovery preserves knowledge, cancellation registration, typing and the remaining budget."""
    from agno.models.response import ModelResponse  # noqa: PLC0415

    runtime = _runtime_context(tmp_path)
    runtime.config.agents["helper"] = AgentConfig(
        display_name="Helper",
        tools=["shell", {"sleep": {"defer": True}}],
        memory_backend="file",
        learning=False,
    )
    runtime.config.administrators = [runtime.requester_id]
    runtime = replace(runtime, orchestrator=SimpleNamespace(agent_cli_registry=TurnToolRegistry()))
    persist_entity_accounts(runtime.config, runtime.runtime_paths)
    identity = build_execution_identity_from_runtime_context(runtime)
    knowledge = Knowledge()
    built_agents = []
    initialize = agents._initialize_agent_instance

    def build(**kwargs):
        agent = initialize(**kwargs)
        built_agents.append(agent)
        return agent

    requests = []
    responses = [ModelResponse(content="Recovered with knowledge.")]
    resumed_counts = []

    @asynccontextmanager
    async def worker(_runtime):
        resumed_counts.append(current_cli_lifetime().continuation_count)
        yield SimpleNamespace(handle=SimpleNamespace(worker_id="worker"), install_grant=AsyncMock())

    monkeypatch.setattr(minimal_agent, "open_configured_cli_worker", worker)
    monkeypatch.setattr(agents, "_initialize_agent_instance", build)
    monkeypatch.setattr(
        agents,
        "_load_agent_model_instance",
        lambda *_args, **_kwargs: _ScriptedModel(id="synthetic", responses=responses, requests=requests),
    )
    storage = create_session_storage("helper", runtime.config, runtime.runtime_paths, identity)
    persisted = RunOutput(
        run_id="saved-run",
        session_id=runtime.session_id,
        agent_id="helper",
        metadata={"agent_mode": "minimal"},
        messages=[Message(role="user", content="Load sleep and answer using knowledge")],
    )
    session = AgentSession(
        session_id=runtime.session_id,
        agent_id="helper",
        user_id=runtime.requester_id,
        runs=[persisted],
    )
    storage.upsert_session(session)
    storage.upsert_run(persisted, session_id=runtime.session_id)
    storage.close()
    requirement = RunRequirement(
        ToolExecution(
            tool_call_id="loader",
            tool_name="load_tool",
            tool_args={"tool_name": "sleep"},
            requires_confirmation=True,
        ),
    )
    continuation = ApprovalContinuation(
        approval_id="approval",
        correlation_id=runtime.correlation_id,
        run_id="saved-run",
        session_id=runtime.session_id,
        entity_kind="agent",
        entity_name="helper",
        room_id=runtime.room_id,
        thread_id=runtime.thread_id,
        requester_id=runtime.requester_id,
        response_event_id="$waiting",
        sources=ResponseSources((runtime.reply_to_event_id,), (runtime.reply_to_event_id,)),
        state="claimed",
        request_body="Load sleep and answer using knowledge",
        continuation_count=continuation_count,
        calls=(ApprovalCall("loader", "load_tool", "helper", 100, toolkit_name="dynamic_tools"),),
        cli_call={
            "kind": "agent_cli",
            "toolkit": "dynamic_tools",
            "function": "load_tool",
            "call_id": "loader",
            "parent_bash_call_id": "interrupted-bash",
            "arguments": {"tool_name": "sleep"},
            "requirements": [requirement.to_dict()],
            "delegation_depth": 2,
        },
    )
    runner = unwrap_extracted_collaborator(_bot(tmp_path / "runner")._response_runner)
    execution = replace(runner._approval_execution, config=lambda: runtime.config, runtime_paths=runtime.runtime_paths)
    monkeypatch.setattr(
        execution.knowledge_access,
        "resolve_for_agent_async",
        AsyncMock(return_value=SimpleNamespace(knowledge=knowledge)),
    )
    typing_events = []

    async def room_typing(_room, enabled, _timeout):
        typing_events.append(enabled)

    monkeypatch.setattr(execution.client(), "room_typing", room_typing)
    run_ids = []
    result = await execution.continue_run(
        continuation,
        execution_identity=identity,
        tool_dispatch=LiveToolDispatchContext.from_runtime_context(runtime),
        decisions={"loader": True},
        denial_reasons={"loader": None},
        tool_trace_collector=[],
        typing_log_context={},
        progress=None,
        run_id_callback=run_ids.append,
    )
    if continuation_count == 4:
        assert "Dynamic tool calls did not produce a final answer" in result.response_text
        assert requests == []
        assert run_ids == []
    else:
        assert "Recovered with knowledge." in result.response_text
        assert len(built_agents) == 2
        assert built_agents[-1].knowledge is knowledge
        assert resumed_counts == [3]
        assert len(run_ids) == 1
        assert run_ids[0] != "saved-run"
    assert typing_events == [True, False]


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [True, False])
@pytest.mark.parametrize("control", [None, "after-toolcall", "next-turn", "invalid", "dynamic"])
async def test_restart_resolves_hidden_call_and_never_replays_parent(
    tmp_path: Path,
    approved: bool,
    control: str | None,
) -> None:
    """Real prepared execution keeps arguments, while normal response gets an honest notice."""
    effects = []
    control_result = json.dumps(
        {"tool": "dynamic_tools", "status": "loaded", "tool_name": "file"}
        if control == "dynamic"
        else {
            "tool": "thread_model",
            "action": "switch",
            "status": "error" if control == "invalid" else "ok",
            "model": "new-model",
            "when": control,
        },
    )

    def action(value: str, run_context: RunContext) -> str:
        assert run_context.metadata == {"source": "original"}
        effects.append(value)
        return control_result if control else "action result"

    function = Function.from_callable(action)
    function.owning_toolkit = "actions"
    if control:
        function.name = "load_tool" if control == "dynamic" else "switch_thread_model"
        function.stop_after_tool_call = True
    function.requires_confirmation = True
    catalog = await _catalog(tmp_path, [function])
    shell_toolkit = shell_tools()(runtime_paths=catalog.runtime_context.runtime_paths)
    agents._set_toolkit_approval_origin(shell_toolkit, "shell")
    catalog.agent = minimal_agent.MinimalAgent(id="helper", model=catalog.agent.model, tools=[function, shell_toolkit])
    catalog.run_response.agent_id = "helper"
    catalog.run_response.metadata = {"source": "original"}
    catalog.agent.db = create_state_storage("helper", tmp_path, subdir="sessions", session_table="sessions")
    catalog.run_response.messages = [
        Message(
            role="assistant",
            tool_calls=[
                {
                    "id": "bash-parent",
                    "type": "function",
                    "function": {"name": "bash", "arguments": '{"command":"never replay"}'},
                },
                {
                    "id": "bash-sibling",
                    "type": "function",
                    "function": {"name": "bash", "arguments": '{"command":"also interrupted"}'},
                },
            ],
            provider_data={"signature": "kept"},
        ),
    ]
    requirement = RunRequirement(
        ToolExecution(
            tool_call_id="hidden",
            tool_name=function.name,
            tool_args={"value": "exact\nargument"},
            requires_confirmation=True,
        ),
    )
    continuation = ApprovalContinuation(
        approval_id="approval",
        run_id="run",
        session_id="session",
        entity_kind="agent",
        entity_name="helper",
        room_id="!room:test",
        thread_id="$thread",
        requester_id="@alice:test",
        response_event_id="$response",
        sources=ResponseSources(("$source",), ("$source",)),
        state="claimed",
        calls=(
            ApprovalCall(
                "hidden",
                function.name,
                "helper",
                100,
                decision=ApprovalDecision.APPROVED if approved else ApprovalDecision.DENIED,
                toolkit_name="actions",
            ),
        ),
        cli_call={
            "kind": "agent_cli",
            "delegation_depth": 0,
            "toolkit": "actions",
            "function": function.name,
            "arguments": {"value": "exact\nargument"},
            "call_id": "hidden",
            "parent_bash_call_id": "bash-parent",
            "requirements": [requirement.to_dict()],
        },
    )

    presentation = CollectedStreamPresentation(show_tool_calls=True, track_hidden_tools=True)
    presentation.start_tool(
        project_cli_execution(requirement.tool_execution, parent="bash-parent", toolkit_name="actions"),
    )

    continuation = replace(
        continuation,
        response_text=presentation.final_text(),
        response_tool_trace=serialize_tool_trace(presentation.tool_trace, include_internal=True),
    )
    published = []

    async def publish(chunk) -> None:
        published.append(chunk)

    async def authorize(binding) -> None:
        assert binding.key.function == function.name

    operation = cli_approval_recovery.continue_cli_approval(
        catalog.agent,
        continuation,
        CliApprovalCall.from_dict(continuation.cli_call),
        catalog.run_response,
        catalog.session,
        runtime_context=catalog.runtime_context,
        decisions={"hidden": approved},
        denial_reasons={"hidden": "Denied"},
        tool_trace_collector=[],
        refresh_scheduler=None,
        authorize=authorize,
        progress=publish,
    )
    with tool_runtime_context(catalog.runtime_context):
        follow_up = await operation
    assert isinstance(follow_up, cli_approval_recovery.CliFollowUpTurn)
    if control and approved:
        assert follow_up.reusable_agent is None
        assert follow_up.ctx.active_model_name == ("new-model" if control == "after-toolcall" else None)
        assert "SYSTEM NOTICE" in follow_up.prompt
    else:
        assert follow_up.reusable_agent is catalog.agent
    trace = follow_up.presentation.tool_trace
    assert len(trace) == 1
    assert trace[0].type == "tool_call_completed"
    assert trace[0].tool_call_id == "hidden"
    assert trace[0].parent_bash_call_id == "bash-parent"
    assert trace[0].toolkit_name == "actions"
    assert "⏳" not in follow_up.presentation.final_text()
    if not approved:
        assert trace[0].result_preview == "Denied"
    else:
        # The hidden call's own start and completion reach the continued reply live.
        assert published[-1].tool_trace[0].type == "tool_call_completed"
    assert effects == (["exact\nargument"] if approved else [])
    notice = follow_up.ctx.transient_enrichment_items[0].text
    assert "interrupted" in notice
    assert ((control_result if control else "action result") if approved else "Denied") in notice
    saved = catalog.agent.db.get_session("session").runs[0]
    assert saved.metadata["mindroom_replay_state"] == "interrupted"
    assert all(not message.tool_calls for message in saved.messages)
    assert "turn stopped before completion" in saved.messages[-1].content


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [True, False])
async def test_restart_settles_outer_bash_approval_without_running_it(
    tmp_path: Path,
    approved: bool,
) -> None:
    """A shell approval rule on the outer Bash recovers as an honest notice, never a replay."""
    catalog = await _catalog(tmp_path, [])
    shell_toolkit = shell_tools()(runtime_paths=catalog.runtime_context.runtime_paths)
    agents._set_toolkit_approval_origin(shell_toolkit, "shell")
    catalog.agent = minimal_agent.MinimalAgent(id="helper", model=catalog.agent.model, tools=[shell_toolkit])
    catalog.run_response.agent_id = "helper"
    catalog.agent.db = create_state_storage("helper", tmp_path, subdir="sessions", session_table="sessions")
    marker = tmp_path / "ran"
    arguments = {"command": f"touch {marker}"}
    requirement = RunRequirement(
        ToolExecution(
            tool_call_id="bash",
            tool_name="run_shell_command",
            tool_args=arguments,
            requires_confirmation=True,
        ),
    )
    presentation = CollectedStreamPresentation(show_tool_calls=True, track_hidden_tools=True)
    presentation.start_tool(project_cli_execution(requirement.tool_execution, parent="bash", toolkit_name="shell"))
    continuation = ApprovalContinuation(
        approval_id="approval",
        run_id="run",
        session_id="session",
        entity_kind="agent",
        entity_name="helper",
        room_id="!room:test",
        thread_id="$thread",
        requester_id="@alice:test",
        response_event_id="$response",
        sources=ResponseSources(("$source",), ("$source",)),
        state="claimed",
        calls=(
            ApprovalCall(
                "bash",
                "run_shell_command",
                "helper",
                100,
                decision=ApprovalDecision.APPROVED if approved else ApprovalDecision.DENIED,
                toolkit_name="shell",
            ),
        ),
        cli_call=CliApprovalCall(
            toolkit="shell",
            function="run_shell_command",
            arguments=arguments,
            call_id="bash",
            parent_bash_call_id="bash",
            requirements=(requirement,),
            delegation_depth=0,
        ).to_dict(),
        response_text=presentation.final_text(),
        response_tool_trace=serialize_tool_trace(presentation.tool_trace, include_internal=True),
    )
    with tool_runtime_context(catalog.runtime_context):
        follow_up = await cli_approval_recovery.continue_cli_approval(
            catalog.agent,
            continuation,
            CliApprovalCall.from_dict(continuation.cli_call),
            catalog.run_response,
            catalog.session,
            runtime_context=catalog.runtime_context,
            decisions={"bash": approved},
            denial_reasons={"bash": "Denied"},
            tool_trace_collector=[],
            refresh_scheduler=None,
            authorize=AsyncMock(),
            progress=None,
        )
    assert isinstance(follow_up, cli_approval_recovery.CliFollowUpTurn)
    trace = follow_up.presentation.tool_trace
    assert [(entry.tool_call_id, entry.type) for entry in trace] == [("bash", "tool_call_completed")]
    assert not marker.exists()
    assert ("saved command was not resumed" if approved else "Denied") in follow_up.ctx.transient_enrichment_items[
        0
    ].text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("shell", "missing_shell_function", "nested"),
    [
        (shell, missing, None)
        for shell in (False, True)
        for missing in (None, "check_shell_command", "kill_shell_command")
    ]
    + [(True, None, "media"), (True, None, "approval"), (True, None, "suspend")],
)
async def test_minimal_recovery_keeps_mode_media_and_uses_fresh_shell_worker(  # noqa: C901 - checkpoint through real resumed provider
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    shell: bool,
    missing_shell_function: str | None,
    nested: str | None,
) -> None:
    """An approved saved call keeps its exact owner, and its old Bash stays interrupted."""
    from agno.models.openai import OpenAIChat  # noqa: PLC0415 - keep optional provider/server imports deferred
    from openai.types.chat import ChatCompletionChunk  # noqa: PLC0415 - keep optional provider/server imports deferred

    runtime = _runtime_context(tmp_path)
    runtime.config.agents["helper"] = AgentConfig(
        display_name="Helper",
        tools=["shell"],
        memory_backend="file",
        learning=False,
    )
    runtime = replace(
        runtime,
        storage_path=tmp_path / "attachments",
        orchestrator=SimpleNamespace(agent_cli_registry=TurnToolRegistry()),
    )
    persist_entity_accounts(runtime.config, runtime.runtime_paths)
    agent = agents.create_agent(
        "helper",
        runtime.config,
        runtime.runtime_paths,
        build_execution_identity_from_runtime_context(runtime),
        session_id=runtime.session_id,
        agent_mode="minimal",
        supports_native_tool_approval=True,
    )
    request.addfinalizer(partial(close_agent_runtime_state_dbs, agent))
    requests = []
    sibling_image = Image(url="https://example.test/completed-sibling.png")
    effects = []
    image = Image(content=b"saved-image", mime_type="image/png")
    audio = Audio(content=b"saved-audio", mime_type="audio/wav")

    def media(value: str) -> ToolResult:
        effects.append(value)
        return ToolResult(content="media result", images=[image], audios=[audio])

    toolkit = Toolkit(name="media", tools=[media])
    toolkit.functions["media"].requires_confirmation = nested in {"approval", "suspend"}
    agents._set_toolkit_approval_origin(toolkit, "media")
    agent.add_tool(toolkit)
    name = "run_shell_command" if shell else "media"
    namespace = "shell" if shell else "media"
    arguments = {"args": "exact approved shell"} if shell else {"value": "exact approved media"}
    tool = ToolExecution(tool_call_id="hidden", tool_name=name, tool_args=arguments, requires_confirmation=True)
    requirement = RunRequirement(tool)
    presentation = CollectedStreamPresentation(show_tool_calls=True, track_hidden_tools=True)
    presentation.start_tool(project_cli_execution(tool, parent="old-bash", toolkit_name=namespace))
    persisted = RunOutput(
        run_id="saved-run",
        session_id=runtime.session_id,
        agent_id="helper",
        metadata={"agent_mode": "minimal"},
    )
    session = AgentSession(
        session_id=runtime.session_id,
        agent_id="helper",
        user_id=runtime.requester_id,
        runs=[persisted],
    )

    def bash(command: str) -> str:
        return command

    function = Function.from_callable(bash)
    function.process_entrypoint()
    checkpoint_catalog = PreparedAgentToolCatalog(
        agent,
        RunContext(run_id="saved-run", session_id=runtime.session_id, user_id=runtime.requester_id, session_state={}),
        persisted,
        session,
        runtime,
    )
    checkpoint = ProviderBatchCheckpoint(checkpoint_catalog)
    message = Message(
        role="assistant",
        tool_calls=[
            {
                "id": call_id,
                "type": "function",
                "function": {"name": "bash", "arguments": json.dumps({"command": command})},
            }
            for call_id, command in [("call_completed", "completed sibling"), ("old-bash", "never replay outer")]
        ],
    )
    async with response_cli_lifetime() as lifetime:
        lifetime.bind_provider(checkpoint, function)
        calls = agent.model.get_function_calls_to_run(message, [message], {"bash": function})
        checkpoint.complete(calls[0], ToolResult(content="completed sibling result", images=[sibling_image]))
        await checkpoint.persist_approval("old-bash")
    session = agent.db.get_session(runtime.session_id)
    persisted = session.runs[0]
    continuation = ApprovalContinuation(
        approval_id="approved",
        run_id="saved-run",
        session_id=runtime.session_id,
        entity_kind="agent",
        entity_name="helper",
        room_id=runtime.room_id,
        thread_id=runtime.resolved_thread_id,
        requester_id=runtime.requester_id,
        response_event_id="$response",
        correlation_id=runtime.correlation_id,
        sources=ResponseSources((runtime.reply_to_event_id,), (runtime.reply_to_event_id,)),
        state="claimed",
        show_tool_calls=True,
        request_body="recover actual request",
        response_text=presentation.response_text,
        response_tool_trace=serialize_tool_trace(presentation.tool_trace, include_internal=True),
        calls=(
            ApprovalCall("hidden", name, "helper", 100, decision=ApprovalDecision.APPROVED, toolkit_name=namespace),
        ),
        cli_call={
            "kind": "agent_cli",
            "toolkit": namespace,
            "function": name,
            "arguments": arguments,
            "call_id": "hidden",
            "parent_bash_call_id": "old-bash",
            "requirements": [requirement.to_dict()],
            "delegation_depth": 2,
        },
    )
    workers = []
    approvals = []
    pauses = []
    responses = []

    async def approve(paused):
        saved = agent.db.get_session(runtime.session_id)
        assert any(
            item.get("id") == paused.cli_call["parent_bash_call_id"]
            for message in saved.runs[-1].messages
            for item in message.tool_calls or ()
        )
        approvals.append(paused.cli_call["call_id"])
        if nested == "suspend":
            pauses.append(paused)
            error = ResponsePausedForApproval(paused)
            emit_cli_suspension(error)
            raise error
        return tuple(
            apply_exact_approval_decisions(
                list(paused.requirements),
                decisions={paused.cli_call["call_id"]: True},
                denial_reasons={paused.cli_call["call_id"]: None},
            ),
        )

    runtime = replace(runtime, cli_approval_handler=approve)

    if missing_shell_function:
        runtime.config.agents["helper"].tools = [{"shell": {"exclude_tools": [missing_shell_function]}}]
        agent = agents.create_agent(
            "helper",
            runtime.config,
            runtime.runtime_paths,
            build_execution_identity_from_runtime_context(runtime),
            session_id=runtime.session_id,
            agent_mode="minimal",
            supports_native_tool_approval=True,
        )
        request.addfinalizer(partial(close_agent_runtime_state_dbs, agent))
        agent.add_tool(toolkit)

    class Worker:
        handle = SimpleNamespace(worker_id="fresh-worker")

        async def install_grant(self, owner, grant, *, shell):
            self.owner = owner
            workers.append("installed")

        async def invoke_shell(self, function_name, values):
            assert function_name == name
            assert values["args"] in {"exact approved shell", "later model shell"}
            effects.append(values["args"])
            if nested:
                queued = await self.owner.operation(
                    ToolCallOperation(
                        operation="tools.call",
                        call_id=uuid4(),
                        toolkit="media",
                        function="media",
                        arguments={"value": "nested media"},
                    ),
                )
                async with asyncio.timeout(5):
                    while (receipt := await self.owner.get_call(queued["call_id"]))["status"] in {  # noqa: ASYNC110 - poll the public receipt API
                        "queued",
                        "running",
                        "waiting",
                    }:
                        await asyncio.sleep(0)
                if nested != "suspend":
                    assert receipt["status"] == "completed"
            return "fresh worker result"

    @asynccontextmanager
    async def worker(_runtime):
        workers.append("opened")
        try:
            yield Worker()
        finally:
            workers.append("closed")

    async def send(**request):
        requests.append(request)

        async def chunks():
            if nested == "approval" and len(requests) == 1:
                yield ChatCompletionChunk(
                    id="completion",
                    model="test",
                    created=0,
                    object="chat.completion.chunk",
                    choices=[
                        {
                            "index": 0,
                            "delta": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "fresh-bash",
                                        "type": "function",
                                        "function": {
                                            "name": "bash",
                                            "arguments": json.dumps({"command": "later model shell"}),
                                        },
                                    },
                                ],
                            },
                            "finish_reason": "tool_calls",
                        },
                    ],
                )
                return
            yield ChatCompletionChunk(
                id="completion",
                model="test",
                created=0,
                object="chat.completion.chunk",
                choices=[{"index": 0, "delta": {"role": "assistant", "content": "finished"}, "finish_reason": None}],
            )
            yield ChatCompletionChunk(
                id="completion",
                model="test",
                created=0,
                object="chat.completion.chunk",
                choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}],
            )

        return chunks()

    def check_follow_up(follow_up: cli_approval_recovery.CliFollowUpTurn) -> None:
        assert follow_up.ctx.agent_mode == "minimal"
        assert follow_up.delegation_depth == 2
        trace = follow_up.presentation.tool_trace
        assert len(trace) == (2 if nested else 1)
        assert trace[0].type == "tool_call_completed"
        assert trace[0].parent_bash_call_id == "old-bash"
        if nested:
            assert trace[1].tool_name == "media"
            assert trace[1].type == "tool_call_completed"
            assert trace[1].toolkit_name == "media"
            assert trace[1].parent_bash_call_id == "old-bash"
        if shell:
            assert workers == ["opened", "installed", "closed"]
        if not shell or nested:
            assert follow_up.media.images[-1].content == image.content
            assert follow_up.media.audio[0].content == b"saved-audio"
            assert runtime.runtime_attachment_ids

    async def authorize(binding, **_kwargs):
        assert binding.catalog.runtime_context.requester_id == runtime.requester_id

    monkeypatch.setattr(
        OpenAIChat,
        "get_async_client",
        lambda _self: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=send))),
    )
    monkeypatch.setattr(minimal_agent, "open_configured_cli_worker", worker)
    if nested:
        monkeypatch.setattr(approval_tools, "authorize_prepared_tool_call", authorize)

    published = []

    async def publish(chunk) -> None:
        published.append(chunk)

    async def recover():
        recovered = await cli_approval_recovery.continue_cli_approval(
            agent,
            continuation,
            CliApprovalCall.from_dict(continuation.cli_call),
            persisted,
            session,
            runtime_context=runtime,
            decisions={"hidden": True},
            denial_reasons={"hidden": None},
            tool_trace_collector=[],
            refresh_scheduler=None,
            authorize=authorize,
            progress=publish,
        )
        if not isinstance(recovered, cli_approval_recovery.CliFollowUpTurn):
            return recovered
        responses.append(recovered.ctx)
        check_follow_up(recovered)
        # The mocked provider accepts images only; the saved audio was checked above.
        return await approval_execution._stream_continuation_turn(
            recovered.ctx,
            recovered.presentation,
            prompt=recovered.prompt,
            show_tool_calls=continuation.show_tool_calls,
            reusable_agent=recovered.reusable_agent,
            initial_continuation_count=recovered.initial_continuation_count,
            delegation_depth=recovered.delegation_depth,
            media=MediaInputs(images=recovered.media.images),
            config=runtime.config,
            runtime_paths=runtime.runtime_paths,
            execution_identity=build_execution_identity_from_runtime_context(runtime),
            knowledge=None,
            refresh_scheduler=None,
            tool_trace_collector=[],
            run_id_callback=None,
            tool_dispatch=LiveToolDispatchContext.from_runtime_context(runtime),
            progress=publish,
        )

    with tool_runtime_context(runtime):
        if missing_shell_function:
            saved_before = agent.db.get_session(runtime.session_id).to_dict()
            with pytest.raises(RuntimeError, match="run, check, and kill shell permissions"):
                await recover()
            assert workers == []
            assert requests == []
            assert effects == []
            assert agent.db.get_session(runtime.session_id).to_dict() == saved_before
            assert continuation.state == "claimed"
            return
        result = await recover()
    if nested == "suspend":
        assert len(pauses) == 1
        assert result == pauses[0]
        assert result.cli_call["parent_bash_call_id"] == "old-bash"
        assert result.cli_call["toolkit"] == "media"
        assert result.cli_call["arguments"] == {"value": "nested media"}
        assert result.cli_call["call_id"] == approvals[0]
        assert responses == []
        assert effects == ["exact approved shell"]
        assert workers == ["opened", "installed", "closed"]
        saved = agent.db.get_session(runtime.session_id).runs[0]
        assert any(item.get("id") == "old-bash" for message in saved.messages for item in message.tool_calls or ())
        return
    assert "finished" in result.response_text
    # Like a standard recovery, the hidden call and the fresh model text stream into the continued reply.
    assert published[0].tool_trace
    assert any("finished" in chunk.content for chunk in published)
    assert len(requests) == (2 if nested == "approval" else 1)
    assert [item["function"]["name"] for item in requests[0]["tools"]] == ["bash"]
    assert sibling_image.url in str(requests[0]["messages"])
    assert "completed sibling result" in str(requests[0]["messages"])
    assert "interrupted" in str(requests[0]["messages"])
    assert effects == (
        ["exact approved shell", "nested media", "later model shell", "nested media"]
        if nested == "approval"
        else ["exact approved shell", "nested media"]
        if nested
        else ["exact approved shell" if shell else "exact approved media"]
    )
    assert len(approvals) == (2 if nested == "approval" else 0)
    saved = agent.db.get_session(runtime.session_id).runs[0]
    assert saved.metadata["mindroom_replay_state"] == "interrupted"
    assert all(not message.tool_calls for message in saved.messages)
    assert "turn stopped before completion" in saved.messages[-1].content


@pytest.mark.asyncio
@pytest.mark.parametrize("available", [True, False])
@pytest.mark.parametrize("permitted", [True, False])
async def test_generated_cli_approval_rebuilds_and_authorizes_exact_function(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    available: bool,
    permitted: bool,
) -> None:
    """Generated approvals bypass toolkit restoration only, never rebuilt binding authorization."""
    runtime = _runtime_context(tmp_path)
    runtime.config.agents["helper"] = AgentConfig(
        display_name="Helper",
        tools=["shell"],
        memory_backend="file",
        learning=False,
    )
    runtime.config.administrators = [runtime.requester_id]
    runtime = replace(runtime, tool_function_filter=lambda function: permitted or function.name != "remember")
    persist_entity_accounts(runtime.config, runtime.runtime_paths)
    identity = build_execution_identity_from_runtime_context(runtime)
    effects = []

    async def generated_tools(**_kwargs):
        def remember(value: str) -> str:
            effects.append(value)
            return "saved generated memory"

        return [Function.from_callable(remember)] if available else []

    real_create = agents.create_agent

    def build(*args: object, **kwargs):
        agent = real_create(*args, **kwargs)
        agent._learning = LearningMachine(custom_stores={"notes": SimpleNamespace(aget_tools=generated_tools)})
        return agent

    storage = create_session_storage("helper", runtime.config, runtime.runtime_paths, identity)
    persisted = RunOutput(
        run_id="generated-run",
        session_id=runtime.session_id,
        agent_id="helper",
        metadata={"agent_mode": "minimal"},
        messages=[Message(role="user", content="Remember this")],
    )
    session = AgentSession(
        session_id=runtime.session_id,
        agent_id="helper",
        user_id=runtime.requester_id,
        runs=[persisted],
    )
    storage.upsert_session(session)
    storage.upsert_run(persisted, session_id=runtime.session_id)
    storage.close()
    requirement = RunRequirement(
        ToolExecution(
            tool_call_id="remember-id",
            tool_name="remember",
            tool_args={"value": "exact saved note"},
            requires_confirmation=True,
        ),
    )
    continuation = ApprovalContinuation(
        approval_id="generated-approval",
        run_id=persisted.run_id,
        session_id=runtime.session_id,
        entity_kind="agent",
        entity_name="helper",
        room_id=runtime.room_id,
        thread_id=runtime.thread_id,
        requester_id=runtime.requester_id,
        response_event_id="$waiting",
        sources=ResponseSources(("$source",), ("$source",)),
        state="claimed",
        calls=(ApprovalCall("remember-id", "remember", "helper", 100, toolkit_name="agent"),),
        cli_call={
            "kind": "agent_cli",
            "delegation_depth": 0,
            "toolkit": "agent",
            "function": "remember",
            "call_id": "remember-id",
            "parent_bash_call_id": "interrupted-bash",
            "arguments": {"value": "exact saved note"},
            "requirements": [requirement.to_dict()],
        },
    )
    runner = unwrap_extracted_collaborator(_bot(tmp_path / "runner")._response_runner)
    execution = replace(runner._approval_execution, config=lambda: runtime.config, runtime_paths=runtime.runtime_paths)
    monkeypatch.setattr(
        execution.knowledge_access,
        "resolve_for_agent_async",
        AsyncMock(return_value=SimpleNamespace(knowledge=None)),
    )
    monkeypatch.setattr(approval_execution, "create_agent", build)
    response = AsyncMock(
        return_value=CompletedApprovalRun(response_text="Recovered generated call", metadata_content={}),
    )
    monkeypatch.setattr(approval_execution, "_stream_continuation_turn", response)

    async def recover():
        return await execution.continue_run(
            continuation,
            execution_identity=identity,
            tool_dispatch=LiveToolDispatchContext.from_runtime_context(runtime),
            decisions={"remember-id": True},
            denial_reasons={"remember-id": None},
            tool_trace_collector=[],
            typing_log_context={},
            progress=None,
        )

    if available and permitted:
        result = await recover()
        assert result.response_text == "Recovered generated call"
        assert effects == ["exact saved note"]
        response.assert_awaited_once()
    else:
        with pytest.raises((ValueError, PermissionError)):
            await recover()
        assert effects == []
        response.assert_not_awaited()
