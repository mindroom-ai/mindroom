"""Hidden control outcomes stop real Bash without inventing provider calls."""

# ruff: noqa: ANN001, ANN003, ANN202, ARG001, D103, PLR0915
from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from agno.tools.function import Function
from agno.tools.toolkit import Toolkit

from mindroom import agents
from mindroom.agent_cli import cli_approval_recovery
from mindroom.agent_cli.approval import CliApprovalCall
from mindroom.agent_cli.delegation import advance_cli_delegation, approval_calls_for_cli_pause
from mindroom.agent_cli.events import project_cli_execution, stream_cli_events
from mindroom.agent_cli.protocol import ContextReadOperation, ToolCallOperation, ToolDescribeOperation
from mindroom.agent_cli.session import CliBashWindowRequiredError, CliTurnOwner
from mindroom.agent_cli.shell_contract import AgentCliShellEnv
from mindroom.agent_cli.turn import LiveTurnTools
from mindroom.ai import collect_streamed_response_content, run_delegated_child_response
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig
from mindroom.custom_tools.compact_context import CompactContextTools
from mindroom.custom_tools.memory import MemoryTools
from mindroom.delegation.state import DelegationState
from mindroom.interactive import parse_and_format_interactive
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.mcp.manager import MCPServerManager
from mindroom.mcp.registry import sync_mcp_tool_registry
from mindroom.mcp.toolkit import bind_mcp_server_manager
from mindroom.memory import search_agent_memories
from mindroom.prompts import INTERACTIVE_QUESTION_PROMPT
from mindroom.response_turn import PausedAnswer, PausedAttempt, apply_exact_approval_decisions
from mindroom.session_storage.agent_storage import create_session_storage, create_state_storage
from mindroom.tool_system.agent_tool_calls import DeferredAgentToolkit, execute_agent_tool_call
from mindroom.tool_system.agno_toolkits import adapt_function, adapt_toolkit
from mindroom.tool_system.events import CollectedStreamPresentation
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from mindroom.tool_system.skills import Skill, SkillsToolkit
from mindroom.tool_system.tool_access import ToolKey
from mindroom.tool_system.tool_function import AdaptedToolkit
from mindroom.turn_continuation import continuation_decision_from_tools
from tests.access_schema_support import with_responder_access
from tests.approval_continuation_helpers import approval_continuation
from tests.conftest import bind_runtime_paths
from tests.delegation_helpers import ScriptedAgents, _delegate_runtime_context, _runtime_paths, registered_report_tool
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call
from tests.identity_helpers import entity_ids
from tests.minimal_agent_fixtures import cli_window, run_bash_batch, settled_receipt
from tests.test_agent_tool_calls import _catalog, _toolkit
from tests.test_compact_context import _make_config

if TYPE_CHECKING:
    from collections.abc import Callable


def _owner(catalog, **options) -> LiveTurnTools:
    async def authorize(key, arguments):
        return None

    return LiveTurnTools(
        CliTurnOwner(build_execution_identity_from_runtime_context(catalog.runtime_context), "turn", "run"),
        catalog=catalog,
        authorize=authorize,
        **options,
    )


def _flagged(name: str, *entrypoints: Callable[..., object], **flags: object) -> AdaptedToolkit:
    """Return a toolkit named ``name`` whose functions all carry ``flags``."""
    functions = []
    for entrypoint in entrypoints:
        function = Function.from_callable(entrypoint)
        for flag, value in flags.items():
            setattr(function, flag, value)
        functions.append(adapt_function(function))
    return AdaptedToolkit(name=name, functions=tuple(functions), instructions=None)


async def _delegation_catalog(tmp_path, config, paths, toolkits, *, tool_call_limit=None, approval_handler=None):
    """Return a catalog whose turn may delegate: a stored session, the live parent run, and its requester context."""
    entity_ids(config, paths)
    catalog = await _catalog(tmp_path, toolkits, tool_call_limit=tool_call_limit)
    runtime = _delegate_runtime_context(config, paths)
    runtime = replace(
        runtime,
        agent_name="helper",
        target=replace(runtime.target, session_id="session"),
        cli_approval_handler=approval_handler,
    )
    catalog.runtime_context = runtime
    identity = build_execution_identity_from_runtime_context(runtime)
    catalog.store = create_session_storage("helper", config, paths, identity)
    catalog.store.ensure_session_row(catalog.session)
    catalog.record = RunRecord(
        run_id="run",
        session_id="session",
        agent_id="helper",
        user_id=identity.requester_id,
        status=RunStatus.running,
    )
    return catalog, runtime, identity


@pytest.mark.asyncio
@pytest.mark.parametrize("control", [True, False])
@pytest.mark.parametrize("raises", [False, True])
async def test_control_stops_batch_and_settles_admitted_work(tmp_path, control, raises) -> None:
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

    async def run_shell_command(args: str, tail: int = 100) -> str:
        effects.append(args)
        if control:
            for function in ("switch_thread_model", "mutate"):
                receipts.append(  # noqa: PERF401 - preserve sequential admission in this race
                    await owner.operation(
                        window=cli_window(),
                        operation=ToolCallOperation(
                            operation="tools.call",
                            call_id=uuid4(),
                            toolkit="control",
                            function=function,
                        ),
                    ),
                )
        return payload

    switch = Function.from_callable(switch_thread_model)
    switch.stop_after_tool_call = True
    catalog = await _catalog(
        tmp_path,
        [_toolkit("shell", run_shell_command), adapt_toolkit(Toolkit(name="control", tools=[switch, mutate]))],
    )
    catalog.store = create_state_storage("helper", tmp_path, session_table="sessions")
    receipts = []
    owner = _owner(catalog)
    owner.shell_env = AgentCliShellEnv("http://127.0.0.1:9", "test-grant")
    async with asyncio.timeout(5):
        record = await run_bash_batch(owner, "outer-0", "outer-1")
    results = record.tools or []
    if control:
        assert effects == ["outer-0"]
        # The control ends the run after its batch, so no further request is sent.
        assert record.content is None
        assert [item.stop_after_tool_call for item in results] == [True, True]
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
        assert record.content == "done"
    assert all(item.tool_name == "bash" for item in results)
    await owner.close()


def _delegation_config(**helper) -> Config:
    return with_responder_access(
        Config(
            agents={
                "helper": AgentConfig(display_name="Helper", delegate_to=["code"], **helper),
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


async def run_subagent(agent_name: str, task: str, model: str | None = None) -> str:
    pytest.fail("external raw body")


async def continue_subagent(subagent_id: str, message: str) -> str:
    pytest.fail("external raw body")


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [None, 1])
@pytest.mark.parametrize("model", [None, "alternate"])
@pytest.mark.parametrize(("target", "depth", "allowed"), [("code", 0, True), ("other", 0, False), ("code", 3, False)])
async def test_hidden_delegation_uses_native_child_owner(
    tmp_path,
    monkeypatch,
    target,
    depth,
    allowed,
    limit,
    model,
) -> None:
    config = _delegation_config()
    paths = _runtime_paths(tmp_path)
    ScriptedAgents({"code": ScriptedModel([answer("child done"), answer("child done")])}).install(monkeypatch)
    catalog, _runtime, identity = await _delegation_catalog(
        tmp_path,
        config,
        paths,
        [_flagged("delegate", run_subagent, continue_subagent, external_execution=True)],
        tool_call_limit=limit,
    )
    children = []

    async def child_response(child, **kwargs):
        children.append(child)
        return await run_delegated_child_response(child, **kwargs)

    owner = LiveTurnTools(
        CliTurnOwner(identity, "turn", "run"),
        catalog=catalog,
        authorize=_allow,
        run_child=child_response,
        delegation_depth=depth,
    )
    call_id = uuid4()
    async with owner._window("bash-parent"):
        await owner.operation(
            window="bash-parent",
            operation=ToolCallOperation(
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
        assert catalog.record is not None
        state = DelegationState.from_metadata(catalog.record.metadata)
        assert state.children[0].run_id == child.run_id
        assert catalog.store is not None
        saved = catalog.store.get_run("run")
        assert saved is not None
        assert saved.requirements is None
        followup_id = uuid4()
        async with owner._window("bash-followup"):
            await owner.operation(
                window="bash-followup",
                operation=ToolCallOperation(
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


async def _allow(key, arguments) -> None:
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [True, False])
async def test_hidden_delegation_never_inherits_an_approval_under_a_reused_call_id(
    tmp_path,
    monkeypatch,
    approved,
) -> None:
    """The CLI caller chooses call IDs, so an approval saved under one in the run never admits a new call."""
    config = _delegation_config()
    config.tool_approval.default = "require_approval"
    paths = _runtime_paths(tmp_path)
    ScriptedAgents({"code": ScriptedModel([answer("child done")])}).install(monkeypatch)
    asked = []

    async def decide(paused):
        asked.extend(str(tool.tool_call_id) for tool in paused.tools)
        return tuple(
            apply_exact_approval_decisions(
                paused.requirements,
                decisions=dict.fromkeys(asked, approved),
                denial_reasons=dict.fromkeys(asked, "denied"),
            ),
        )

    catalog, _runtime, identity = await _delegation_catalog(
        tmp_path,
        config,
        paths,
        [_flagged("delegate", run_subagent, external_execution=True)],
        approval_handler=decide,
    )
    call_id = uuid4()
    # A recovered follow-up turn inherits the run metadata that approved an earlier call under this ID.
    assert catalog.record is not None
    catalog.record.metadata = {"mindroom_delegation": DelegationState(gates={str(call_id): True}).to_dict()}
    children = []

    async def child_response(child, **kwargs):
        children.append(child)
        return await run_delegated_child_response(child, **kwargs)

    owner = LiveTurnTools(
        CliTurnOwner(identity, "turn", "run"),
        catalog=catalog,
        authorize=_allow,
        run_child=child_response,
    )

    async def checkpoint(parent):
        assert parent == "bash-parent"

    monkeypatch.setattr(owner.checkpoint, "persist_approval", checkpoint)
    async with owner._window("bash-parent"):
        await owner.operation(
            window="bash-parent",
            operation=ToolCallOperation(
                operation="tools.call",
                call_id=call_id,
                toolkit="delegate",
                function="run_subagent",
                arguments={"agent_name": "code", "task": "a task nobody approved"},
            ),
        )
    receipt = await owner.get_call(str(call_id))

    assert asked == [str(call_id)]
    assert [child.task for child in children] == (["a task nobody approved"] if approved else [])
    assert receipt["status"] == "completed", receipt
    await owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["live", "recover", "recover_again", "deny", "cancel", "control"])
async def test_hidden_child_pause_reuses_native_resume(tmp_path, monkeypatch, mode) -> None:  # noqa: C901
    # Delegation records fsync every event; disk flush latency under parallel runs is not under test.
    monkeypatch.setattr(os, "fsync", lambda _descriptor: None)
    paths = _runtime_paths(tmp_path)
    child_ids = []
    reached = asyncio.Event()
    control_payload = '{"tool":"thread_model","action":"switch","status":"ok","model":"next","when":"after-toolcall"}'

    async def switch_thread_model() -> str:
        return control_payload

    with registered_report_tool() as report:
        config = with_responder_access(
            Config(
                agents={
                    "helper": AgentConfig(display_name="Helper", tools=["shell"], delegate_to=["code"]),
                    "code": AgentConfig(display_name="Code", tools=["report"]),
                },
                defaults=DefaultsConfig(tools=[]),
                memory={"backend": "none"},
                tool_approval={"rules": [{"match": "write_report", "action": "require_approval"}]},
            ),
            "code",
            users=["@alice:example.org"],
        )
        ScriptedAgents(
            {
                "helper": ScriptedModel([]),
                "code": ScriptedModel(
                    [
                        calls(tool_call("write_report", "child-write")),
                        calls(tool_call("write_report", "child-write-again"))
                        if mode == "recover_again"
                        else answer("child done"),
                    ],
                ),
            },
        ).install(monkeypatch)
        approval_ids = []

        async def approve(paused):
            assert report.written == []
            assert paused.approval_agent_name == "code"
            assert paused.toolkit_owners == {("code", "write_report"): "report"}
            approval_ids.extend(str(tool.tool_call_id) for tool in paused.tools)
            if mode == "control":
                switched = await owner.operation(
                    window="bash-parent",
                    operation=ToolCallOperation(
                        operation="tools.call",
                        call_id=uuid4(),
                        toolkit="control",
                        function="switch_thread_model",
                    ),
                )
                await settled_receipt(owner, switched["call_id"])
            return tuple(
                apply_exact_approval_decisions(
                    paused.requirements,
                    decisions=dict.fromkeys(approval_ids, mode != "deny"),
                    denial_reasons=dict.fromkeys(approval_ids, "denied"),
                ),
            )

        catalog, runtime, identity = await _delegation_catalog(
            tmp_path,
            config,
            paths,
            [
                _flagged("delegate", run_subagent, external_execution=True),
                _flagged("control", switch_thread_model, stop_after_tool_call=True),
            ],
            approval_handler=approve,
        )

        async def child_response(child, **kwargs):
            child_ids.append(
                (child.run_id, child.session_id, child.parent_tool_call_id, child.execution_identity["requester_id"]),
            )
            reached.set()
            if mode == "cancel":
                await asyncio.Event().wait()
            return await run_delegated_child_response(child, **kwargs)

        owner = LiveTurnTools(
            CliTurnOwner(identity, "turn", "run"),
            catalog=catalog,
            authorize=_allow,
            run_child=child_response,
        )

        async def checkpoint(parent):
            assert parent == "bash-parent"

        monkeypatch.setattr(owner.checkpoint, "persist_approval", checkpoint)
        call_id = uuid4()
        arguments = {"agent_name": "code", "task": "work"}
        if mode in {"recover", "recover_again"}:
            binding = await catalog.bind(ToolKey("delegate", "run_subagent"))
            events = [event async for event in execute_agent_tool_call(binding, str(call_id), arguments)]
            requirement = next(event.requirement for event in events if event.kind == "waiting")
            assert requirement is not None
            with catalog.execution_context():
                paused = await advance_cli_delegation(
                    binding,
                    requirement,
                    parent_bash_call_id="bash-parent",
                    delegation_depth=0,
                    run_child=child_response,
                )
            assert isinstance(paused, PausedAttempt)
            assert paused.cli_call is not None
            approval_calls = approval_calls_for_cli_pause(paused, "helper")
            decisions = {call.tool_call_id: True for call in approval_calls}

            presentation = CollectedStreamPresentation(show_tool_calls=True, track_hidden_tools=True)
            presentation.start_tool(
                project_cli_execution(requirement.tool_execution, parent="bash-parent", toolkit_name="delegate"),
            )
            for tool in paused.tools:
                presentation.start_tool(project_cli_execution(tool, parent="bash-parent", toolkit_name="report"))
            continuation = approval_continuation(
                continuation_count=2,
                run_id="run",
                session_id="session",
                entity_name="helper",
                room_id=runtime.room_id,
                thread_id=runtime.thread_id,
                requester_id=runtime.requester_id,
                response_event_id="$response",
                state="claimed",
                calls=approval_calls,
                cli_call=paused.cli_call,
            )
            assert catalog.store is not None
            persisted = catalog.store.get_run("run")
            session = catalog.store.load_session("session", "agent")
            assert isinstance(persisted, RunRecord)
            assert session is not None
            build = agents.create_agent(
                "helper",
                config,
                paths,
                identity,
                agent_mode="minimal",
                history_storage=catalog.store,
                supports_native_tool_approval=True,
            )

            async def authorize_binding(binding):
                return None

            with tool_runtime_context(runtime):
                result = await cli_approval_recovery.continue_cli_approval(
                    build,
                    continuation,
                    CliApprovalCall.from_dict(continuation.cli_call),
                    persisted,
                    session,
                    catalog.store,
                    paused_answer=PausedAnswer(
                        text=presentation.final_text(),
                        tool_trace=tuple(presentation.tool_trace),
                    ),
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
                assert str(result.tools[0].tool_call_id).endswith(":child-write-again")
                assert [entry.type for entry in result.tool_trace] == [
                    "tool_call_started",
                    "tool_call_completed",
                    "tool_call_started",
                ]
                assert [entry.toolkit_name for entry in result.tool_trace] == ["delegate", "report", "report"]
                assert all(entry.parent_bash_call_id == "bash-parent" for entry in result.tool_trace)
            else:
                assert isinstance(result, cli_approval_recovery.CliFollowUpTurn)
                trace = result.presentation.tool_trace
                assert len(trace) == 2
                assert all(entry.type == "tool_call_completed" for entry in trace)
                assert [entry.toolkit_name for entry in trace] == ["delegate", "report"]
                assert all(entry.parent_bash_call_id == "bash-parent" for entry in trace)
                assert trace[1].tool_call_id == approval_calls[0].tool_call_id
                assert trace[1].result_preview is not None
                assert "report written" in trace[1].result_preview
                assert "⏳" not in result.presentation.final_text()
                assert "child done" in result.ctx.transient_enrichment_items[0].text
        else:

            async def run_window():
                async with owner._window("bash-parent"):
                    await owner.operation(
                        window="bash-parent",
                        operation=ToolCallOperation(
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
                async with asyncio.timeout(10):
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
                assert child_trace[0].toolkit_name == "report"
                assert child_trace[0].tool_call_id == approval_ids[0]
                assert "⏳" not in text
        assert report.written == (["written"] if mode in {"live", "recover", "recover_again", "control"} else [])
    assert len(child_ids) == 1
    assert catalog.record is not None
    state_metadata = catalog.record.metadata
    if mode in {"recover", "recover_again"}:
        assert catalog.store is not None
        saved_run = catalog.store.get_run("run")
        assert saved_run is not None
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
    memory = adapt_toolkit(MemoryTools("helper", paths.storage_root, config, paths))
    catalog = await _catalog(tmp_path, [memory])
    identity = build_execution_identity_from_runtime_context(catalog.runtime_context)
    compact = adapt_toolkit(CompactContextTools("helper", config, paths, identity))
    await catalog.prepare([compact], {"compact_context": "compact_context"})
    binding = await catalog.bind(ToolKey("memory", "add_memory"))
    events = [
        event
        async for event in execute_agent_tool_call(binding, "memory-write", {"content": "Canonical hidden memory"})
    ]
    assert events[-1].execution.result == "Memorized: Canonical hidden memory"
    memories = await search_agent_memories("Canonical hidden", "helper", paths.storage_root, config, paths, limit=5)
    assert any(item["memory"] == "Canonical hidden memory" for item in memories.results)
    catalog.run.session_state["sentinel"] = "kept"
    original = catalog.run.session_state
    compact_binding = await catalog.bind(ToolKey("compact_context", "compact_context"))
    events = [event async for event in execute_agent_tool_call(compact_binding, "compact", {})]
    assert events[-1].kind == "completed"
    assert "before the next reply" in events[-1].execution.result
    assert catalog.run.session_state is original
    assert catalog.run.session_state["sentinel"] == "kept"
    await catalog.close()


@pytest.mark.asyncio
async def test_hidden_unsupported_requirement_never_runs_body(tmp_path) -> None:
    """A call only an outside party could finish is refused, never run, unless it is a delegation."""
    effects = []

    async def unsupported(answer: str) -> str:
        effects.append(answer)
        return answer

    catalog = await _catalog(tmp_path, [_flagged("tools", unsupported, external_execution=True)])
    owner = _owner(catalog)
    with pytest.raises(ExceptionGroup) as error:
        async with owner._window("bash-parent"):
            await owner.operation(
                window="bash-parent",
                operation=ToolCallOperation(
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

    skill_dir = tmp_path / "skills" / "local-guide"
    skill_dir.mkdir(parents=True)
    skill = Skill(
        name="local-guide",
        description="Local guide",
        instructions="Use the original session.",
        source_path=skill_dir / "SKILL.md",
    )
    skills = adapt_toolkit(SkillsToolkit({skill.name: skill}, workspace_roots={}))
    catalog = await _catalog(tmp_path, [skills], owners={})
    binding = await catalog.bind(ToolKey("agent", "get_skill_instructions"))
    events = [event async for event in execute_agent_tool_call(binding, "skill-read", {"skill_name": "local-guide"})]
    assert "Use the original session." in events[-1].execution.result
    script = tmp_path / "server.py"
    script.write_text(
        'from mcp.server.mcpserver import MCPServer\nserver = MCPServer("echo")\n@server.tool()\ndef echo(text: str) -> str:\n    return "echo:" + text\nserver.run()\n',
    )
    paths = catalog.runtime_context.runtime_paths
    config = Config.validate_with_runtime(
        {"mcp_servers": {"echo": {"transport": "stdio", "command": sys.executable, "args": [str(script)]}}},
        paths,
    )
    manager = MCPServerManager(paths)
    bind_mcp_server_manager(manager)
    materialized = []

    async def materialize():
        materialized.append(True)
        await manager.sync_servers(config)
        sync_mcp_tool_registry(config)
        return adapt_toolkit(get_tool_by_name("mcp_echo", paths, worker_target=None))

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
    sync_mcp_tool_registry(None)
    await manager.shutdown()


@pytest.mark.asyncio
async def test_interactive_context_renders_native_question(tmp_path) -> None:
    """The interactive guidance minimal mode reads through the CLI produces questions the reply renders natively."""
    catalog = await _catalog(tmp_path, [])
    owner = _owner(catalog, context={"interactive": INTERACTIVE_QUESTION_PROMPT})
    guidance = await owner.operation(
        window=None,
        operation=ContextReadOperation(operation="context.read", name="interactive"),
    )
    question = "```interactive" + guidance["text"].split("```interactive", 1)[1].split("```", 1)[0] + "```"
    rendered = parse_and_format_interactive(question, extract_mapping=True)
    assert "Fast and automated" in rendered.formatted_text
    assert rendered.interactive_metadata is not None
    assert rendered.interactive_metadata.option_map["1"]
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

    catalog = await _catalog(tmp_path, [_flagged("control", switch_thread_model, stop_after_tool_call=True)])

    async def late() -> str:
        return "must not run"

    async def materialize():
        materialized.append(True)
        return _toolkit("late", late)

    catalog.add_deferred(DeferredAgentToolkit("late", "Deferred", materialize))
    owner = _owner(catalog)
    async with owner._window("bash-parent"):
        switch_call = await owner.operation(
            window="bash-parent",
            operation=ToolCallOperation(
                operation="tools.call",
                call_id=uuid4(),
                toolkit="control",
                function="switch_thread_model",
            ),
        )
        await entered.wait()
        describe = asyncio.create_task(
            owner.operation(
                window="bash-parent",
                operation=ToolDescribeOperation(operation="tools.describe", toolkit="late", function="late"),
            ),
        )
        await owner.operation(
            window="bash-parent",
            operation=ToolCallOperation(operation="tools.call", call_id=uuid4(), toolkit="late", function="late"),
        )
        await asyncio.sleep(0)
        release.set()
        await settled_receipt(owner, switch_call["call_id"])
        # The fence rejects calls from every window, even one whose command is still running.
        with pytest.raises(CliBashWindowRequiredError, match="continuation requires a rebuilt tool catalog"):
            await owner.operation(
                window="bash-parent",
                operation=ToolCallOperation(operation="tools.call", call_id=uuid4(), toolkit="late", function="late"),
            )
    with pytest.raises(ValueError, match="continuation"):
        await describe
    assert materialized == []
    await owner.close()
