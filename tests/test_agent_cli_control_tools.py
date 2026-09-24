"""Hidden control outcomes stop real Bash without inventing provider calls."""

# ruff: noqa: ANN001, ANN003, ANN202, ARG001, ARG002, D103, PLR0915
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from uuid import uuid4

import pytest
from agno.agent import Agent
from agno.models.message import Message
from agno.models.response import ModelResponse
from agno.skills import LocalSkills, Skills
from agno.tools.function import Function, FunctionCall
from agno.tools.toolkit import Toolkit

from mindroom import agents, cli_approval_recovery
from mindroom.agent_cli.approval import CliApprovalCall
from mindroom.agent_cli.delegation import advance_cli_delegation, approval_calls_for_cli_pause
from mindroom.agent_cli.events import project_cli_execution, stream_cli_events
from mindroom.agent_cli.lifetime import response_cli_lifetime
from mindroom.agent_cli.protocol import ContextReadOperation, ToolCallOperation, ToolDescribeOperation
from mindroom.agent_cli.session import CliTurnOwner
from mindroom.agent_cli.turn import LiveTurnTools
from mindroom.agent_storage import create_session_storage, create_state_storage
from mindroom.agno_compat_prepared_tools import prepare_agent_tools
from mindroom.ai import collect_streamed_response_content
from mindroom.approval_tools import toolkit_owners_for_agents
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig
from mindroom.custom_tools.compact_context import CompactContextTools
from mindroom.custom_tools.memory import MemoryTools
from mindroom.delegation.lifecycle import child_execution_identity
from mindroom.delegation.state import DelegationState
from mindroom.dynamic_tool_continuation import continuation_decision_from_tools
from mindroom.event_journal import ApprovalContinuation
from mindroom.interactive import parse_and_format_interactive
from mindroom.mcp.manager import MCPServerManager
from mindroom.mcp.registry import sync_mcp_tool_registry
from mindroom.mcp.toolkit import bind_mcp_server_manager
from mindroom.memory import search_agent_memories
from mindroom.minimal_agent import MinimalAgent
from mindroom.prompts import INTERACTIVE_QUESTION_PROMPT
from mindroom.response_sources import ResponseSources
from mindroom.response_turn import (
    PausedAttempt,
    ResponsePausedForApproval,
    apply_exact_approval_decisions,
    paused_attempt_from_response,
)
from mindroom.tool_system.agent_tool_calls import DeferredAgentToolkit, execute_agent_tool_call
from mindroom.tool_system.events import CollectedStreamPresentation, serialize_tool_trace
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from mindroom.tool_system.tool_access import ToolKey
from mindroom.tools.shell import shell_tools
from tests.access_schema_support import with_responder_access
from tests.conftest import bind_runtime_paths
from tests.test_agent_tool_calls import _catalog
from tests.test_compact_context import _make_config
from tests.test_delegate_tools import _delegate_runtime_context, _runtime_paths
from tests.test_delegation_execution import DelegationModel, _call


@pytest.mark.asyncio
@pytest.mark.parametrize("control", [True, False])
@pytest.mark.parametrize("raises", [False, True])
async def test_control_stops_batch_and_settles_admitted_work(tmp_path, control, raises) -> None:  # noqa: C901
    effects = []
    payload = json.dumps(
        {"tool": "thread_model", "action": "switch", "status": "ok", "model": "next", "when": "after-toolcall"},
    )

    async def switch_thread_model() -> str:
        if raises:
            msg = "switch rejected"
            raise ValueError(msg)
        return payload

    async def mutate() -> str:
        effects.append("mutated")
        return "done"

    async def run_shell_command(args: str) -> str:
        pytest.fail("ordinary shell")

    switch = Function.from_callable(switch_thread_model)
    switch.stop_after_tool_call = True
    catalog = await _catalog(
        tmp_path,
        [Toolkit(name="shell", tools=[run_shell_command]), Toolkit(name="control", tools=[switch, mutate])],
    )
    catalog.run_response.agent_id = "helper"
    catalog.agent.db = create_state_storage("helper", tmp_path, subdir="sessions", session_table="sessions")
    receipts = []

    async def authorize(key, arguments):
        return None

    class Worker:
        async def invoke_shell(self, name, arguments):
            effects.append(arguments["args"])
            if control:
                for function in ("switch_thread_model", "mutate"):
                    receipts.append(  # noqa: PERF401 - preserve sequential admission in this race
                        await owner.operation(
                            ToolCallOperation(
                                operation="tools.call",
                                call_id=uuid4(),
                                toolkit="control",
                                function=function,
                            ),
                        ),
                    )
            return payload

    owner = LiveTurnTools(
        CliTurnOwner(build_execution_identity_from_runtime_context(catalog.runtime_context), "turn", "run", "worker"),
        catalog=catalog,
        worker=Worker(),
        authorize=authorize,
    )

    async def bash(command: str, fc: FunctionCall) -> str:
        return await owner.execute_bash(ToolKey("shell", "run_shell_command"), {"args": command}, fc)

    function = Function.from_callable(bash)
    # Real Agno preparation injects the owning FunctionCall.

    function = prepare_agent_tools(
        catalog.agent,
        processed_tools=[function],
        run_response=catalog.run_response,
        run_context=catalog.run_context,
        session=catalog.session,
    )[0]
    message = Message(
        role="assistant",
        tool_calls=[
            {
                "id": f"bash-{index}",
                "type": "function",
                "function": {"name": "bash", "arguments": json.dumps({"command": f"outer-{index}"})},
            }
            for index in range(2)
        ],
    )
    messages = [message]
    results = []
    async with response_cli_lifetime() as lifetime:
        lifetime.bind_provider(owner.checkpoint, function)
        calls = catalog.agent.model.get_function_calls_to_run(message, messages, {"bash": function})
        async with asyncio.timeout(5):
            async for event in catalog.agent.model.arun_function_calls(calls, messages):
                results.extend(event.tool_executions or [])
    if control:
        assert effects == ["outer-0"]
        assert any(item.tool_name == "bash" and item.stop_after_tool_call for item in results)
        assert function.stop_after_tool_call is False
        first, second = [await owner.get_call(item["call_id"]) for item in receipts]
        assert first["status"] == ("failed" if raises else "completed")
        assert first["outcome"] == ("switch rejected" if raises else payload)
        assert second["status"] == "cancelled"
        assert "continuation" in second["outcome"].lower()
        assert second["parent_bash_call_id"] == "bash-0"
        decision = continuation_decision_from_tools(
            owner.control_executions,
            original_prompt="task",
            continuation_count=0,
        )
        assert decision.should_continue
        assert decision.model_switch_name == (None if raises else "next")
    else:
        assert effects == ["outer-0", "outer-1"]
        assert not owner.control_executions
    assert all(item.tool_name == "bash" for item in results)
    await owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [None, 1])
@pytest.mark.parametrize("model", [None, "alternate"])
@pytest.mark.parametrize(("target", "depth", "allowed"), [("code", 0, True), ("other", 0, False), ("code", 3, False)])
async def test_hidden_delegation_uses_native_child_owner(tmp_path, target, depth, allowed, limit, model) -> None:

    config = with_responder_access(
        Config(
            agents={
                "helper": AgentConfig(display_name="Helper", delegate_to=["code"]),
                "code": AgentConfig(display_name="Code"),
                "other": AgentConfig(display_name="Other"),
            },
            models={
                "default": ModelConfig(provider="openai", id="gpt-6-astra"),
                "alternate": ModelConfig(provider="anthropic", id="claude-sonnet-5"),
            },
            defaults=DefaultsConfig(tools=[]),
            memory={"backend": "none"},
        ),
        "code",
        users=["@alice:example.org"],
    )
    paths = _runtime_paths(tmp_path)

    async def run_subagent(agent_name: str, task: str, model: str | None = None) -> str:
        pytest.fail("external raw body")

    async def continue_subagent(subagent_id: str, message: str) -> str:
        pytest.fail("external raw body")

    followup = Function.from_callable(continue_subagent)
    followup.owning_toolkit = "delegate"
    followup.external_execution = True
    function = Function.from_callable(run_subagent)
    function.owning_toolkit = "delegate"
    function.external_execution = True
    catalog = await _catalog(tmp_path, [function, followup], tool_call_limit=limit)
    runtime = _delegate_runtime_context(config, paths)
    runtime = replace(runtime, agent_name="helper", target=replace(runtime.target, session_id="session"))
    catalog.runtime_context = runtime
    # Bind exact live runtime while preparing the actual external Function.
    catalog._bindings.clear()
    await catalog.prepare([function, followup])
    identity = build_execution_identity_from_runtime_context(runtime)
    catalog.agent.db = create_session_storage("helper", config, paths, identity)
    catalog.agent.db.upsert_session(catalog.session)
    catalog.run_response.agent_id = "helper"
    catalog.run_response.user_id = identity.requester_id
    children = []

    async def child_response(child, **kwargs):

        children.append(child)
        child_identity = child_execution_identity(child)
        child_agent = Agent(
            id="code",
            db=create_session_storage("code", config, paths, child_identity),
            model=DelegationModel(id="test", responses=[ModelResponse(content="child done")]),
        )
        await child_agent.arun(
            child.task,
            run_id=child.run_id,
            session_id=child.session_id,
            user_id=identity.requester_id,
        )
        return "ignored"

    async def authorize(key, arguments):
        return None

    owner = LiveTurnTools(
        CliTurnOwner(identity, "turn", "run", "worker"),
        catalog=catalog,
        worker=None,
        authorize=authorize,
        run_child=child_response,
        delegation_depth=depth,
    )
    call_id = uuid4()
    async with owner._window("bash-parent"):
        await owner.operation(
            ToolCallOperation(
                operation="tools.call",
                call_id=call_id,
                toolkit="delegate",
                function="run_subagent",
                arguments={"agent_name": target, "task": "work", "model": model},
            ),
        )
    receipt = await owner.get_call(str(call_id))
    assert receipt["status"] == "completed", receipt
    assert len(children) == int(allowed)
    if allowed:
        assert receipt["outcome"].startswith("child done\n\nDelegation")
        child = children[0]
        assert child.model_name == (model or "default")
        assert child.parent_tool_call_id == str(call_id)
        assert child.execution_identity["requester_id"] == "@alice:example.org"
        state = DelegationState.from_metadata(catalog.run_response.metadata)
        assert state.children[0].run_id == child.run_id
        assert catalog.agent.db.get_session("session").runs[0].requirements is None
        followup_id = uuid4()
        async with owner._window("bash-followup"):
            await owner.operation(
                ToolCallOperation(
                    operation="tools.call",
                    call_id=followup_id,
                    toolkit="delegate",
                    function="continue_subagent",
                    arguments={"subagent_id": child.subagent_id, "message": "continue work"},
                ),
            )
        continued = await owner.get_call(str(followup_id))
        if limit == 1:
            assert continued["status"] == "failed"
            assert "Tool call limit" in continued["outcome"]
            assert len(children) == 1
        else:
            assert continued["status"] == "completed"
            assert "child done" in continued["outcome"]
            assert len(children) == 2
            assert children[1].model_name == (model or "default")
            assert children[1].session_id == child.session_id
            assert children[1].run_id != child.run_id
            assert children[1].parent_tool_call_id == str(followup_id)
    else:
        assert "Cannot delegate" in receipt["outcome"]
    await owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["live", "recover", "recover_again", "deny", "cancel", "control"])
async def test_hidden_child_pause_reuses_native_resume(tmp_path, monkeypatch, mode) -> None:  # noqa: C901
    # Delegation records fsync every event; disk flush latency under parallel runs is not under test.
    monkeypatch.setattr(os, "fsync", lambda _descriptor: None)
    config = with_responder_access(
        Config(
            agents={
                "helper": AgentConfig(display_name="Helper", delegate_to=["code"]),
                "code": AgentConfig(display_name="Code", tools=["file"]),
            },
            defaults=DefaultsConfig(tools=[]),
            memory={"backend": "none"},
        ),
        "code",
        users=["@alice:example.org"],
    )
    paths = _runtime_paths(tmp_path)
    effects = []
    child_ids = []
    reached = asyncio.Event()

    async def run_subagent(agent_name: str, task: str) -> str:
        pytest.fail("external raw body")

    async def switch_thread_model() -> str:
        return '{"tool":"thread_model","action":"switch","status":"ok","model":"next","when":"after-toolcall"}'

    control = Function.from_callable(switch_thread_model)
    control.owning_toolkit = "control"
    control.stop_after_tool_call = True
    function = Function.from_callable(run_subagent)
    function.owning_toolkit = "delegate"
    function.external_execution = True
    catalog = await _catalog(tmp_path, [function, control])
    runtime = _delegate_runtime_context(config, paths)
    runtime = replace(runtime, agent_name="helper", target=replace(runtime.target, session_id="session"))
    catalog.runtime_context = runtime
    catalog._bindings.clear()
    await catalog.prepare([function, control])
    identity = build_execution_identity_from_runtime_context(runtime)
    catalog.agent.db = create_session_storage("helper", config, paths, identity)
    catalog.agent.db.upsert_session(catalog.session)
    catalog.run_response.agent_id = "helper"
    catalog.run_response.user_id = identity.requester_id

    async def write_report() -> str:
        effects.append("written")
        return "written"

    child_model = DelegationModel(
        id="test",
        responses=[
            ModelResponse(tool_calls=[_call("write_report", "child-write")]),
            ModelResponse(tool_calls=[_call("write_report", "child-write-again")])
            if mode == "recover_again"
            else ModelResponse(content="child done"),
        ],
    )

    def build_child(agent_name, current_config, runtime_paths, execution_identity, **kwargs):
        gate = Function.from_callable(write_report)
        gate.requires_confirmation = True
        gate.owning_toolkit = "file"
        return Agent(
            id="code",
            tools=[gate],
            db=kwargs.get("history_storage") or create_session_storage("code", config, paths, execution_identity),
            model=child_model,
        )

    async def child_response(child, **kwargs):
        child_ids.append(
            (child.run_id, child.session_id, child.parent_tool_call_id, child.execution_identity["requester_id"]),
        )
        reached.set()
        if mode == "cancel":
            await asyncio.Event().wait()
        child_agent = build_child("code", config, paths, child_execution_identity(child))
        response = await child_agent.arun(
            child.task,
            run_id=child.run_id,
            session_id=child.session_id,
            user_id=identity.requester_id,
        )
        paused = paused_attempt_from_response(
            response,
            fallback_session_id=child.session_id,
            fallback_run_id=child.run_id,
            toolkit_owners=toolkit_owners_for_agents([child_agent]),
        )
        assert paused is not None
        raise ResponsePausedForApproval(paused)

    monkeypatch.setattr(agents, "create_agent", build_child)
    approval_ids = []

    async def approve(paused):
        assert effects == []
        assert paused.approval_agent_name == "code"
        assert paused.toolkit_owners == {("code", "write_report"): "file"}
        approval_ids.extend(str(tool.tool_call_id) for tool in paused.tools)
        if mode == "control":
            switched = await owner.operation(
                ToolCallOperation(
                    operation="tools.call",
                    call_id=uuid4(),
                    toolkit="control",
                    function="switch_thread_model",
                ),
            )
            while (await owner.get_call(switched["call_id"]))["status"] in {"queued", "running"}:  # noqa: ASYNC110
                await asyncio.sleep(0)
        return tuple(
            apply_exact_approval_decisions(
                paused.requirements,
                decisions=dict.fromkeys(approval_ids, mode != "deny"),
                denial_reasons=dict.fromkeys(approval_ids, "denied"),
            ),
        )

    runtime = replace(runtime, cli_approval_handler=approve)
    catalog.runtime_context = runtime
    catalog._bindings.clear()
    await catalog.prepare([function, control])

    async def authorize(key, arguments):
        return None

    owner = LiveTurnTools(
        CliTurnOwner(identity, "turn", "run", "worker"),
        catalog=catalog,
        worker=None,
        authorize=authorize,
        run_child=child_response,
    )

    async def checkpoint(parent):
        assert parent == "bash-parent"

    monkeypatch.setattr(owner.checkpoint, "persist_approval", checkpoint)
    call_id = uuid4()
    arguments = {"agent_name": "code", "task": "work"}
    if mode in {"recover", "recover_again"}:
        shell_toolkit = shell_tools()(runtime_paths=runtime.runtime_paths)
        agents._set_toolkit_approval_origin(shell_toolkit, "shell")
        catalog.agent = MinimalAgent(
            id="helper",
            model=catalog.agent.model,
            db=catalog.agent.db,
            tools=[function, control, shell_toolkit],
        )
        binding = await catalog.bind(ToolKey("delegate", "run_subagent"))
        events = [event async for event in execute_agent_tool_call(binding, str(call_id), arguments)]
        requirement = next(event.requirement for event in events if event.kind == "waiting")
        paused = await advance_cli_delegation(
            binding,
            requirement,
            parent_bash_call_id="bash-parent",
            delegation_depth=0,
            run_child=child_response,
        )
        assert isinstance(paused, PausedAttempt)
        calls = approval_calls_for_cli_pause(paused, "helper")
        decisions = {call.tool_call_id: True for call in calls}

        presentation = CollectedStreamPresentation(show_tool_calls=True, track_hidden_tools=True)
        presentation.start_tool(
            project_cli_execution(requirement.tool_execution, parent="bash-parent", toolkit_name="delegate"),
        )
        for tool in paused.tools:
            presentation.start_tool(project_cli_execution(tool, parent="bash-parent", toolkit_name="file"))
        continuation = ApprovalContinuation(
            approval_id="approval",
            continuation_count=2,
            run_id="run",
            session_id="session",
            entity_kind="agent",
            entity_name="helper",
            room_id=runtime.room_id,
            thread_id=runtime.thread_id,
            requester_id=runtime.requester_id,
            response_event_id="$response",
            sources=ResponseSources(("$source",), ("$source",)),
            state="claimed",
            response_text=presentation.final_text(),
            response_tool_trace=serialize_tool_trace(presentation.tool_trace, include_internal=True),
            calls=calls,
            cli_call=paused.cli_call,
        )

        async def authorize_binding(binding):
            return None

        with tool_runtime_context(runtime):
            result = await cli_approval_recovery.continue_cli_approval(
                catalog.agent,
                continuation,
                CliApprovalCall.from_dict(continuation.cli_call),
                catalog.run_response,
                catalog.session,
                runtime_context=runtime,
                decisions=decisions,
                denial_reasons=dict.fromkeys(decisions),
                tool_trace_collector=[],
                refresh_scheduler=None,
                authorize=authorize_binding,
                progress=None,
            )
        if mode == "recover_again":
            assert isinstance(result, PausedAttempt)
            assert result.continuation_count == 2
            assert result.tools[0].tool_call_id.endswith(":child-write-again")
            assert [entry.type for entry in result.tool_trace] == [
                "tool_call_started",
                "tool_call_completed",
                "tool_call_started",
            ]
            assert [entry.toolkit_name for entry in result.tool_trace] == ["delegate", "file", "file"]
            assert all(entry.parent_bash_call_id == "bash-parent" for entry in result.tool_trace)
        else:
            assert isinstance(result, cli_approval_recovery.CliFollowUpTurn)
            trace = result.presentation.tool_trace
            assert len(trace) == 2
            assert all(entry.type == "tool_call_completed" for entry in trace)
            assert [entry.toolkit_name for entry in trace] == ["delegate", "file"]
            assert all(entry.parent_bash_call_id == "bash-parent" for entry in trace)
            assert trace[1].tool_call_id == calls[0].tool_call_id
            assert "written" in trace[1].result_preview
            assert "⏳" not in result.presentation.final_text()
            assert "child done" in result.ctx.transient_enrichment_items[0].text
    else:

        async def run_window():
            async with owner._window("bash-parent"):
                await owner.operation(
                    ToolCallOperation(
                        operation="tools.call",
                        call_id=call_id,
                        toolkit="delegate",
                        function="run_subagent",
                        arguments=arguments,
                    ),
                )

        async def source():
            await run_window()
            yield None

        running = asyncio.create_task(
            collect_streamed_response_content(
                stream_cli_events(source()),
                presentation=CollectedStreamPresentation(show_tool_calls=True),
            ),
        )
        if mode == "cancel":
            await reached.wait()
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
        else:
            async with asyncio.timeout(5):
                await running
            receipt = await owner.get_call(str(call_id))
            assert receipt["status"] == "completed", receipt
            assert "child done" in receipt["outcome"]
            assert len(approval_ids) == 1
            assert approval_ids[0].endswith(":child-write")
            text, trace = running.result()
            child_trace = [entry for entry in trace if entry.tool_name == "write_report"]
            assert len(child_trace) == 1
            assert child_trace[0].type == "tool_call_completed"
            assert child_trace[0].parent_bash_call_id == "bash-parent"
            assert child_trace[0].toolkit_name == "file"
            assert child_trace[0].tool_call_id == approval_ids[0]
            assert "⏳" not in text
    assert effects == (["written"] if mode in {"live", "recover", "recover_again", "control"} else [])
    assert len(child_ids) == 1
    state_metadata = catalog.run_response.metadata
    if mode in {"recover", "recover_again"}:
        saved_run = next(
            run for run in catalog.agent.db.get_session("session").runs if run.run_id == catalog.run_response.run_id
        )
        state_metadata = saved_run.metadata
    child = DelegationState.from_metadata(state_metadata).children[0]
    assert (
        child.run_id,
        child.session_id,
        child.parent_tool_call_id,
        child.execution_identity["requester_id"],
    ) == child_ids[0]
    assert child.parent_tool_call_id == str(call_id)
    assert child.status == ("cancelled" if mode == "cancel" else "paused" if mode == "recover_again" else "completed")
    await owner.close()


@pytest.mark.asyncio
async def test_hidden_memory_write_standard_read_and_live_compaction(tmp_path) -> None:

    config, paths = _make_config(tmp_path)
    config.agents["helper"] = AgentConfig(display_name="Helper", memory_backend="file")
    config.memory.backend = "file"
    bind_runtime_paths(config, paths)
    memory = MemoryTools("helper", paths.storage_root, config, paths)
    catalog = await _catalog(tmp_path, [memory])
    identity = build_execution_identity_from_runtime_context(catalog.runtime_context)
    compact = CompactContextTools("helper", config, paths, identity)
    await catalog.prepare([compact])
    binding = await catalog.bind(ToolKey("memory", "add_memory"))
    events = [
        event
        async for event in execute_agent_tool_call(binding, "memory-write", {"content": "Canonical hidden memory"})
    ]
    assert events[-1].execution.result == "Memorized: Canonical hidden memory"
    memories = await search_agent_memories("Canonical hidden", "helper", paths.storage_root, config, paths, limit=5)
    assert any(item["memory"] == "Canonical hidden memory" for item in memories.results)
    catalog.run_context.session_state["sentinel"] = "kept"
    original = catalog.run_context.session_state
    compact_binding = await catalog.bind(ToolKey("compact_context", "compact_context"))
    events = [event async for event in execute_agent_tool_call(compact_binding, "compact", {})]
    assert events[-1].kind == "completed"
    assert "before the next reply" in events[-1].execution.result
    assert catalog.run_context.session_state is original
    assert catalog.run_context.session_state["sentinel"] == "kept"
    assert len(catalog.run_context.session_state) > 1
    await catalog.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["external_execution", "requires_user_input"])
async def test_hidden_unsupported_requirement_never_runs_body(tmp_path, flag) -> None:
    effects = []

    async def unsupported(answer: str) -> str:
        effects.append(answer)
        return answer

    function = Function.from_callable(unsupported)
    setattr(function, flag, True)
    catalog = await _catalog(tmp_path, [function])

    async def authorize(key, arguments):
        return None

    owner = LiveTurnTools(
        CliTurnOwner(build_execution_identity_from_runtime_context(catalog.runtime_context), "turn", "run", "worker"),
        catalog=catalog,
        worker=None,
        authorize=authorize,
    )
    with pytest.raises(ExceptionGroup) as error:
        async with owner._window("bash-parent"):
            await owner.operation(
                ToolCallOperation(
                    operation="tools.call",
                    call_id=uuid4(),
                    toolkit="tools",
                    function="unsupported",
                    arguments={"answer": "do not run"},
                ),
            )
    assert "unsupported non-confirmation requirement" in str(error.value.exceptions[0])
    assert not effects
    await owner.close()


@pytest.mark.asyncio
async def test_generated_skills_and_deferred_upstream_mcp_keep_live_catalog(tmp_path) -> None:

    skill = tmp_path / "skills" / "local-guide"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: local-guide\ndescription: Local guide\n---\nUse the original session.\n",
    )
    catalog = await _catalog(tmp_path, [], skills=Skills(loaders=[LocalSkills(str(skill.parent))]))
    binding = await catalog.bind(ToolKey("agent", "get_skill_instructions"))
    events = [event async for event in execute_agent_tool_call(binding, "skill-read", {"skill_name": "local-guide"})]
    assert "Use the original session." in events[-1].execution.result
    script = tmp_path / "server.py"
    script.write_text(
        'from mcp.server.fastmcp import FastMCP\nserver = FastMCP("echo")\n@server.tool()\ndef echo(text: str) -> str:\n    return "echo:" + text\nserver.run()\n',
    )
    paths = catalog.runtime_context.runtime_paths
    config = Config.validate_with_runtime(
        {"mcp_servers": {"echo": {"transport": "stdio", "command": "uv", "args": ["run", str(script)]}}},
        paths,
    )
    manager = MCPServerManager(paths)
    bind_mcp_server_manager(manager)
    materialized = []

    async def materialize():
        materialized.append(True)
        await manager.sync_servers(config)
        sync_mcp_tool_registry(config)
        return get_tool_by_name("mcp_echo", paths, worker_target=None)

    catalog.add_deferred(DeferredAgentToolkit("upstream", "Echo", materialize))
    assert not materialized
    assert catalog.metadata()[-1]["deferred"] is True
    binding = await catalog.bind(ToolKey("upstream", "echo_echo"))
    events = [event async for event in execute_agent_tool_call(binding, "mcp-call", {"text": "same-owner"})]
    assert events[-1].kind == "completed"
    assert "echo:same-owner" in events[-1].execution.result
    assert binding.catalog is catalog
    assert materialized == [True]
    await catalog.close()
    bind_mcp_server_manager(None)
    sync_mcp_tool_registry(None)
    await manager.shutdown()


@pytest.mark.asyncio
async def test_interactive_context_renders_native_question_and_keeps_session(tmp_path) -> None:

    catalog = await _catalog(tmp_path, [])
    catalog.agent.db = create_state_storage("helper", tmp_path, subdir="sessions", session_table="sessions")

    async def authorize(key, arguments):
        return None

    owner = LiveTurnTools(
        CliTurnOwner(build_execution_identity_from_runtime_context(catalog.runtime_context), "turn", "run", "worker"),
        catalog=catalog,
        worker=None,
        authorize=authorize,
        context={"interactive": INTERACTIVE_QUESTION_PROMPT},
    )
    guidance = await owner.operation(ContextReadOperation(operation="context.read", name="interactive"))
    question = "```interactive" + guidance["text"].split("```interactive", 1)[1].split("```", 1)[0] + "```"
    model = DelegationModel(
        id="test",
        responses=[ModelResponse(content=question), ModelResponse(content="Continue fast")],
    )
    catalog.agent.model = model
    first = await catalog.agent.arun("Ask a question", session_id="session", user_id="@alice:test")
    rendered = parse_and_format_interactive(first.content, extract_mapping=True)
    assert "Fast and automated" in rendered.formatted_text
    selection = rendered.interactive_metadata.option_map["1"]
    second = await catalog.agent.arun(
        f"The user selected: {selection}",
        session_id=first.session_id,
        user_id="@alice:test",
    )
    assert first.agent_id == second.agent_id == "helper"
    assert first.session_id == second.session_id == "session"
    assert second.content == "Continue fast"
    assert any(
        "Ask a question" in str(message.content) for message in catalog.agent.db.get_session("session").runs[0].messages
    )
    await owner.close()


@pytest.mark.asyncio
async def test_control_fences_deferred_materialization_waiting_for_catalog(tmp_path) -> None:

    entered = asyncio.Event()
    release = asyncio.Event()
    materialized = []

    async def switch_thread_model() -> str:
        entered.set()
        await release.wait()
        return '{"tool":"thread_model","action":"switch","status":"ok","model":"next","when":"after-toolcall"}'

    switch = Function.from_callable(switch_thread_model)
    switch.stop_after_tool_call = True
    catalog = await _catalog(tmp_path, [Toolkit(name="control", tools=[switch])])

    async def late() -> str:
        return "must not run"

    async def materialize():
        materialized.append(True)
        return Toolkit(name="late", tools=[late])

    catalog.add_deferred(DeferredAgentToolkit("late", "Deferred", materialize))

    async def authorize(key, arguments):
        return None

    owner = LiveTurnTools(
        CliTurnOwner(build_execution_identity_from_runtime_context(catalog.runtime_context), "turn", "run", "worker"),
        catalog=catalog,
        worker=None,
        authorize=authorize,
    )
    async with owner._window("bash-parent"):
        await owner.operation(
            ToolCallOperation(
                operation="tools.call",
                call_id=uuid4(),
                toolkit="control",
                function="switch_thread_model",
            ),
        )
        await entered.wait()
        describe = asyncio.create_task(
            owner.operation(ToolDescribeOperation(operation="tools.describe", toolkit="late", function="late")),
        )
        await owner.operation(
            ToolCallOperation(operation="tools.call", call_id=uuid4(), toolkit="late", function="late"),
        )
        await asyncio.sleep(0)
        release.set()
    with pytest.raises(ValueError, match="continuation"):
        await describe
    assert materialized == []
    await owner.close()
