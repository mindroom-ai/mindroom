"""Release comparisons through the supported response factory and CLI owner."""

# ruff: noqa: ANN001, ANN002, ANN003, ANN202, ARG001, ARG002, D103, PLR0915
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import AsyncIterator, Iterator  # noqa: TC003 - Agno evaluates tool annotations
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import yaml
from agno.agent import Agent
from agno.learn import LearningMachine
from agno.models.fallback import FallbackConfig
from agno.models.openai import OpenAIChat
from agno.models.response import ModelResponse
from agno.run import RunContext  # noqa: TC002 - injected tool argument
from agno.skills import LocalSkills, Skills
from agno.tools.toolkit import Toolkit
from pydantic import ValidationError

from mindroom import agents, ai, minimal_agent, provider_stream_retry
from mindroom.agent_cli.protocol import (
    ContextReadOperation,
    ToolCallOperation,
    ToolDescribeOperation,
    ToolListOperation,
    parse_operation,
)
from mindroom.agent_cli.session import CliAuthenticationError, TurnToolRegistry
from mindroom.agent_storage import create_session_storage
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.plugin import PluginEntryConfig
from mindroom.credentials import get_runtime_credentials_manager, save_scoped_credentials
from mindroom.delegation.lifecycle import child_execution_identity
from mindroom.interactive import parse_and_format_interactive
from mindroom.mcp.config import MCPServerConfig
from mindroom.mcp.manager import MCPServerManager
from mindroom.mcp.registry import sync_mcp_tool_registry
from mindroom.mcp.toolkit import bind_mcp_server_manager
from mindroom.memory import search_agent_memories
from mindroom.response_turn import apply_exact_approval_decisions
from mindroom.tool_system.dynamic_toolkits import get_loaded_tools_for_session
from mindroom.tool_system.events import CollectedStreamPresentation
from mindroom.tool_system.runtime_context import (
    build_execution_identity_from_runtime_context,
    get_tool_runtime_context,
    tool_runtime_context,
)
from mindroom.tool_system.worker_routing import get_tool_execution_identity
from tests.access_schema_support import with_responder_access
from tests.identity_helpers import persist_entity_accounts
from tests.minimal_agent_fixtures import PLUGIN, ScriptedProvider
from tests.test_agent_cli_authority import _runtime_context, _turn_context
from tests.test_delegation_execution import DelegationModel

pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")


class ProtocolWorker:
    """Use the real live protocol while replacing only the shell transport."""

    def __init__(self) -> None:
        self.handle = SimpleNamespace(worker_id=str(uuid4()))
        self.owner = None
        self.receipts = []
        self.grants = []
        self.catalog_snapshots = []

    async def install_grant(self, owner, grant, *, shell) -> None:
        """Retain the real response owner and its issued capability."""
        self.owner = owner
        self.grants.append(grant)

    async def invoke_shell(self, name, arguments) -> str:
        """Dispatch through the actual owner protocol inside the Bash window."""
        assert name == "run_shell_command"
        toolkit, function, values = json.loads(arguments["args"])
        listing = await self.owner.operation(ToolListOperation(operation="tools.list"))
        assert toolkit in str(listing)
        description = await self.owner.operation(
            ToolDescribeOperation(operation="tools.describe", toolkit=toolkit, function=function),
        )
        assert function in str(description)
        call_id = uuid4()
        await self.owner.operation(
            ToolCallOperation(
                operation="tools.call",
                toolkit=toolkit,
                function=function,
                arguments=values,
                call_id=call_id,
            ),
        )
        while (receipt := await self.owner.get_call(str(call_id)))["status"] in {"queued", "running", "waiting"}:  # noqa: ASYNC110
            await asyncio.sleep(0)
        self.receipts.append(receipt)
        self.catalog_snapshots.append(self.owner.catalog.metadata())
        assert receipt["status"] == "completed", receipt
        return json.dumps(receipt)


@pytest.mark.asyncio
@pytest.mark.parametrize("function", ["sync", "asynchronous", "generator", "async_generator"])
@pytest.mark.parametrize("streamed", [False, True])
async def test_response_direct_cli_parity(tmp_path, monkeypatch, function, streamed) -> None:  # noqa: C901 - paired native and CLI execution
    """Removing canonical context, hooks, generators, or hidden dispatch breaks parity."""
    runtime = _runtime_context(tmp_path)
    runtime.config.administrators = [runtime.requester_id]
    runtime.config.agents["helper"] = AgentConfig(
        display_name="Helper",
        tools=["shell", "calculator"],
        memory_backend="file",
        learning=False,
    )
    runtime = replace(runtime, orchestrator=SimpleNamespace(agent_cli_registry=TurnToolRegistry()))
    persist_entity_accounts(runtime.config, runtime.runtime_paths)
    effects = []
    hooks = []

    def effect(value, run_context):
        context = get_tool_runtime_context()
        identity = get_tool_execution_identity()
        effects.append((value, context.agent_name, context.requester_id, context.target, identity))
        run_context.session_state["parity"] = value

    def sync(value: int, run_context: RunContext) -> str:
        effect(value, run_context)
        return f"result:{value}"

    async def asynchronous(value: int, run_context: RunContext) -> str:
        return sync(value, run_context)

    def generator(value: int, run_context: RunContext) -> Iterator[str]:
        effect(value, run_context)
        yield "result:"
        yield str(value)

    async def async_generator(value: int, run_context: RunContext) -> AsyncIterator[str]:
        effect(value, run_context)
        yield "result:"
        yield str(value)

    async def hook(name, function_call, arguments):
        hooks.append((name, "before"))
        result = await function_call(**arguments)
        hooks.append((name, "after"))
        return result

    original = agents.get_tool_by_name

    def build(name, *args, **kwargs):
        if name != "calculator":
            return original(name, *args, **kwargs)
        toolkit = Toolkit(name="calculator", tools=[sync, asynchronous, generator, async_generator])
        for entry in toolkit.get_async_functions().values():
            entry.tool_hooks = [hook]
        return toolkit

    monkeypatch.setattr(agents, "get_tool_by_name", build)
    worker = ProtocolWorker()

    @asynccontextmanager
    async def open_worker(_runtime):
        yield worker

    monkeypatch.setattr(minimal_agent, "open_configured_cli_worker", open_worker)
    provider = ScriptedProvider()
    provider.install(monkeypatch)
    identity = build_execution_identity_from_runtime_context(runtime)
    results = []
    for mode in ("standard", "minimal"):
        provider.steps = [
            [(function, {"value": 7})]
            if mode == "standard"
            else [
                ("bash", {"command": json.dumps(["calculator", function, {"value": 7}])}),
            ],
            "done",
        ]
        ctx = replace(_turn_context(), agent_mode=mode, run_id=None)
        with tool_runtime_context(runtime):
            kwargs = {
                "prompt": "same parity task",
                "runtime_paths": runtime.runtime_paths,
                "config": runtime.config,
                "execution_identity": identity,
                "supports_native_tool_approval": True,
                "show_tool_calls": True,
            }
            result = (
                await ai.collect_streamed_response_content(
                    ai.stream_agent_response(ctx, **kwargs),
                    presentation=CollectedStreamPresentation(show_tool_calls=True),
                )
                if streamed
                else await ai.ai_response(ctx, **kwargs)
            )
        results.append(result)
    assert effects[0][:4] == effects[1][:4]
    assert effects[1][4] == identity
    assert len(effects) == 2
    assert hooks == [(function, "before"), (function, "after")] * 2
    assert worker.receipts[0]["outcome"] == "result:7"
    assert worker.owner.catalog.run_context.session_state["parity"] == 7
    assert all("done" in str(result) for result in results)
    assert len(provider.requests) == 4
    assert function in str(provider.requests[0]["tools"])
    assert all([tool["function"]["name"] for tool in request["tools"]] == ["bash"] for request in provider.requests[2:])
    assert "result:7" in str(provider.requests[1]["messages"])
    assert "result:7" in str(provider.requests[3]["messages"])
    assert worker.grants[0].raw_token not in str(provider.requests)


@pytest.mark.asyncio
async def test_same_child_responder_direct_and_cli_provenance(tmp_path, monkeypatch) -> None:
    """The same child responder keeps native parent and follow-up relationships in both modes."""
    comparisons = []
    for mode in ("standard", "minimal"):
        runtime = _runtime_context(tmp_path / mode)
        runtime.config.administrators = [runtime.requester_id]
        runtime.config.agents["helper"] = AgentConfig(
            display_name="Helper",
            tools=["shell"],
            delegate_to=["code"],
            memory_backend="file",
            learning=False,
        )
        runtime.config.agents["code"] = AgentConfig(display_name="Code", learning=False)
        with_responder_access(runtime.config, "code", users=[runtime.requester_id])
        runtime = replace(runtime, orchestrator=SimpleNamespace(agent_cli_registry=TurnToolRegistry()))
        persist_entity_accounts(runtime.config, runtime.runtime_paths)
        children = []

        async def child_response(child, *, runtime=runtime, children=children, **kwargs):
            children.append(child)
            child_identity = child_execution_identity(child)
            child_agent = Agent(
                id="code",
                db=create_session_storage("code", runtime.config, runtime.runtime_paths, child_identity),
                model=DelegationModel(id="test", responses=[ModelResponse(content="child done")]),
            )
            try:
                await child_agent.arun(
                    child.task,
                    run_id=child.run_id,
                    session_id=child.session_id,
                    user_id=runtime.requester_id,
                )
            finally:
                child_agent.db.close()
            return "child done"

        monkeypatch.setattr(ai, "run_delegated_child_response", child_response)
        worker = ProtocolWorker()

        @asynccontextmanager
        async def open_worker(_runtime, worker=worker):
            yield worker

        monkeypatch.setattr(minimal_agent, "open_configured_cli_worker", open_worker)
        provider = ScriptedProvider()
        provider.install(monkeypatch)
        for function, initial_arguments in (
            ("run_subagent", {"agent_name": "code", "task": "work"}),
            ("continue_subagent", None),
        ):
            arguments = initial_arguments
            if arguments is None:
                assert len(children) == 1
                arguments = {"subagent_id": children[0].subagent_id, "message": "continue work"}
            provider.steps = [
                [(function, arguments)]
                if mode == "standard"
                else [
                    ("bash", {"command": json.dumps(["delegate", function, arguments])}),
                ],
                "done",
            ]
            with tool_runtime_context(runtime):
                await ai.ai_response(
                    replace(_turn_context(), agent_mode=mode, run_id=None),
                    prompt="delegate task",
                    runtime_paths=runtime.runtime_paths,
                    config=runtime.config,
                    execution_identity=build_execution_identity_from_runtime_context(runtime),
                    supports_native_tool_approval=True,
                )
        assert len(children) == 2
        first, second = children
        assert first.session_id == second.session_id
        assert first.run_id != second.run_id
        assert first.parent_tool_call_id != second.parent_tool_call_id
        assert first.child_agent_name == second.child_agent_name == "code"
        assert all(child.execution_identity["requester_id"] == runtime.requester_id for child in children)
        if mode == "minimal":
            assert [child.parent_tool_call_id for child in children] == [
                receipt["call_id"] for receipt in worker.receipts
            ]
            assert all(receipt["parent_bash_call_id"] != receipt["call_id"] for receipt in worker.receipts)
            assert all(
                [tool["function"]["name"] for tool in request["tools"]] == ["bash"] for request in provider.requests
            )
        else:
            assert all(child.parent_tool_call_id.startswith("provider-") for child in children)
        comparisons.append(
            [(child.child_agent_name, child.execution_identity["requester_id"], child.task) for child in children],
        )
    assert comparisons[0] == comparisons[1]


@pytest.fixture
def response_harness(tmp_path, monkeypatch) -> SimpleNamespace:
    runtime = _runtime_context(tmp_path)
    runtime.config.administrators = [runtime.requester_id]
    runtime.config.agents["helper"] = AgentConfig(
        display_name="Helper",
        tools=["shell"],
        learning=False,
        memory_backend="file",
    )
    runtime = replace(runtime, orchestrator=SimpleNamespace(agent_cli_registry=TurnToolRegistry()))
    worker = ProtocolWorker()

    @asynccontextmanager
    async def open_worker(_runtime):
        yield worker

    monkeypatch.setattr(minimal_agent, "open_configured_cli_worker", open_worker)
    provider = ScriptedProvider()
    provider.install(monkeypatch)

    h = SimpleNamespace(runtime=runtime, worker=worker, provider=provider)

    async def respond(steps, *, mode="minimal", prompt="release parity"):
        runtime = h.runtime
        persist_entity_accounts(runtime.config, runtime.runtime_paths)
        provider.steps = steps
        with tool_runtime_context(runtime):
            return await ai.ai_response(
                replace(_turn_context(), agent_mode=mode, run_id=None),
                prompt=prompt,
                runtime_paths=runtime.runtime_paths,
                config=runtime.config,
                execution_identity=build_execution_identity_from_runtime_context(runtime),
                supports_native_tool_approval=True,
                show_tool_calls=True,
            )

    h.respond = respond
    return h


def _bash_call(toolkit, function, arguments):
    return [("bash", {"command": json.dumps([toolkit, function, arguments])})]


@pytest.mark.asyncio
async def test_durable_memory_standard_minimal_standard(response_harness) -> None:

    h = response_harness
    h.runtime.config.agents["helper"].tools = ["shell", "memory"]
    h.runtime.config.memory.backend = "file"
    await h.respond(["standard history sentinel"], mode="standard", prompt="same conversation before switch")
    storage = create_session_storage(
        "helper",
        h.runtime.config,
        h.runtime.runtime_paths,
        build_execution_identity_from_runtime_context(h.runtime),
    )
    old_messages = [message.to_dict() for message in storage.get_session(h.runtime.session_id).runs[0].messages]
    await h.respond([_bash_call("memory", "add_memory", {"content": "Release parity durable lesson"}), "minimal saved"])
    assert h.worker.receipts[-1]["outcome"] == "Memorized: Release parity durable lesson"
    found = await search_agent_memories(
        "Release parity durable",
        "helper",
        h.runtime.runtime_paths.storage_root,
        h.runtime.config,
        h.runtime.runtime_paths,
        limit=5,
    )
    assert any(item["memory"] == "Release parity durable lesson" for item in found.results)
    await h.respond(["standard sees same history"], mode="standard", prompt="after switch")
    session = storage.get_session(h.runtime.session_id)
    storage.close()
    assert len(session.runs) == 3
    assert [message.to_dict() for message in session.runs[0].messages] == old_messages
    final = h.provider.requests[-1]
    assert "standard history sentinel" in str(final["messages"])
    assert "Release parity durable lesson" in str(final["messages"])
    assert [tool["function"]["name"] for tool in h.provider.requests[1]["tools"]] == ["bash"]
    assert "add_memory" in str(final["tools"])
    calls = {call["id"] for message in final["messages"] for call in message.get("tool_calls", [])}
    assert any(message.get("tool_call_id") in calls for message in final["messages"])


@pytest.mark.asyncio
@pytest.mark.parametrize("generated", ["skills", "knowledge", "learning"])
async def test_generated_tools_actual_minimal_response(response_harness, monkeypatch, tmp_path, generated) -> None:

    h = response_harness
    original = ai.create_agent
    writes = []

    async def retriever(agent, query, **kwargs):
        return [{"content": f"knowledge:{agent.id}:{query}"}]

    async def learning_tools(user_id, session_id, agent_id, **kwargs):
        async def remember(value: str) -> str:
            writes.append((agent_id, user_id, session_id, value))
            return "learning saved"

        return [remember]

    skill = tmp_path / "skills" / "release-guide"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: release-guide\ndescription: Release guide\n---\nUse the same canonical session.\n",
    )

    def build(*args, **kwargs):
        agent = original(*args, **kwargs)
        if generated == "skills":
            agent.skills = Skills(loaders=[LocalSkills(str(skill.parent))])
        elif generated == "knowledge":
            agent.knowledge_retriever = retriever
            agent.search_knowledge = True
        else:
            agent._learning = LearningMachine(custom_stores={"release": SimpleNamespace(aget_tools=learning_tools)})
        return agent

    monkeypatch.setattr(ai, "create_agent", build)
    function, arguments, expected = {
        "skills": ("get_skill_instructions", {"skill_name": "release-guide"}, "same canonical session"),
        "knowledge": ("search_knowledge_base", {"query": "release"}, "knowledge:helper:release"),
        "learning": ("remember", {"value": "lesson"}, "learning saved"),
    }[generated]
    result = await h.respond([_bash_call("agent", function, arguments), "done"])
    assert "done" in str(result)
    assert expected in str(h.worker.receipts[-1]["outcome"])
    assert expected in str(h.provider.requests[-1]["messages"])
    assert all([tool["function"]["name"] for tool in request["tools"]] == ["bash"] for request in h.provider.requests)
    if generated == "learning":
        assert writes == [("helper", h.runtime.requester_id, h.runtime.session_id, "lesson")]


@pytest.mark.asyncio
async def test_dynamic_load_unload_rebuild_keeps_one_bash(response_harness) -> None:

    h = response_harness
    h.runtime.config.agents["helper"] = AgentConfig(
        display_name="Helper",
        learning=False,
        memory_backend="file",
        tools=["shell", {"calculator": {"defer": True}}],
    )
    result = await h.respond(
        [
            _bash_call("dynamic_tools", "load_tool", {"tool_name": "calculator"}),
            _bash_call("calculator", "add", {"a": 2, "b": 3}),
            _bash_call("dynamic_tools", "unload_tool", {"tool_name": "calculator"}),
            "done",
        ],
    )
    assert "done" in str(result)
    assert len(h.worker.receipts) == 3
    assert '"result": 5' in h.worker.receipts[1]["outcome"]
    assert all(receipt["status"] == "completed" for receipt in h.worker.receipts)
    assert len(h.worker.grants) == 1
    assert len(h.provider.requests) == 4
    assert all([tool["function"]["name"] for tool in request["tools"]] == ["bash"] for request in h.provider.requests)
    assert not get_loaded_tools_for_session(
        agent_name="helper",
        config=h.runtime.config,
        session_id=h.runtime.session_id,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True])
async def test_deferred_upstream_mcp_response_filters_and_requester(
    response_harness,
    monkeypatch,
    tmp_path,
    private,
) -> None:

    h = response_harness
    script = tmp_path / "echo_server.py"
    script.write_text(
        'from mcp.server.fastmcp import FastMCP\nserver = FastMCP("echo")\n@server.tool()\ndef echo(text: str) -> str:\n return "echo:" + text\n@server.tool()\ndef secret() -> str:\n raise AssertionError("filtered tool ran")\nserver.run()\n',
    )
    h.runtime.config.mcp_servers = {"echo": MCPServerConfig(transport="stdio", command="uv", args=["run", str(script)])}
    h.runtime.config.agents["helper"] = AgentConfig(
        display_name="Helper",
        learning=False,
        memory_backend="file",
        private=AgentPrivateConfig(per="user_agent") if private else None,
        tools=[
            "shell",
            {"mcp_echo": {"defer": True, "include_tools": ["echo"], "exclude_tools": ["secret"]}},
            {"duckduckgo": {"defer": True}},
        ],
    )
    manager = MCPServerManager(h.runtime.runtime_paths)
    bind_mcp_server_manager(manager)
    calls = []
    original = manager.call_tool

    async def call(server_id, remote_tool_name, arguments, **kwargs):
        calls.append((server_id, remote_tool_name, arguments, kwargs))
        return await original(server_id, remote_tool_name, arguments, **kwargs)

    monkeypatch.setattr(manager, "call_tool", call)
    try:
        await manager.sync_servers(h.runtime.config)
        sync_mcp_tool_registry(h.runtime.config)
        result = await h.respond([_bash_call("mcp_echo", "echo_echo", {"text": "same requester"}), "done"])
        assert "done" in str(result)
        assert "echo:same requester" in h.worker.receipts[-1]["outcome"]
        assert len(calls) == 1
        server, function, arguments, kwargs = calls[0]
        assert (server, function, arguments) == ("echo", "echo", {"text": "same requester"})
        assert kwargs["worker_target"].execution_identity.requester_id == h.runtime.requester_id
        assert kwargs["include_tools"] == ["echo"]
        assert kwargs["exclude_tools"] == ["secret"]
        metadata = h.worker.catalog_snapshots[-1]
        assert next(item for item in metadata if item["toolkit"] == "duckduckgo")["deferred"] is True
        assert not any(item.get("function") == "echo_secret" for item in metadata)
        assert all(
            [tool["function"]["name"] for tool in request["tools"]] == ["bash"] for request in h.provider.requests
        )
    finally:
        bind_mcp_server_manager(None)
        sync_mcp_tool_registry(None)
        await manager.shutdown()


@pytest.mark.asyncio
async def test_actual_fallback_request_keeps_only_bash(response_harness, monkeypatch) -> None:
    from openai import APIStatusError  # noqa: PLC0415 - keep optional provider/server imports deferred

    h = response_harness
    original = ai.create_agent
    failed_requests = []
    send = h.provider.send
    monkeypatch.setattr(provider_stream_retry, "_RETRY_BASE_DELAY_SECONDS", 0.0)

    async def failing_primary(**request):
        if request["model"] == "test-model":
            failed_requests.append(request)
            message = "fake provider unavailable"
            raise APIStatusError(
                message,
                response=httpx.Response(503, request=httpx.Request("POST", "https://fake.test")),
                body=None,
            )
        return await send(**request)

    monkeypatch.setattr(
        OpenAIChat,
        "get_async_client",
        lambda _self: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=failing_primary))),
    )

    def build(*args, **kwargs):
        agent = original(*args, **kwargs)
        agent.fallback_config = FallbackConfig(on_error=[OpenAIChat(id="fallback", api_key="fake")])
        return agent

    monkeypatch.setattr(ai, "create_agent", build)
    result = await h.respond(["fallback done"])
    assert "fallback done" in str(result)
    assert failed_requests
    assert h.provider.requests[-1]["model"] == "fallback"
    assert all(
        [tool["function"]["name"] for tool in request["tools"]] == ["bash"]
        for request in failed_requests + h.provider.requests
    )


@pytest.mark.asyncio
async def test_compaction_cli_marks_real_session_before_next_reply(response_harness) -> None:
    h = response_harness
    h.runtime.config.agents["helper"].tools = ["shell", "compact_context"]
    h.runtime.config.models["default"].context_window = 128000
    result = await h.respond([_bash_call("compact_context", "compact_context", {}), "done"])
    assert "done" in str(result)
    assert "before the next reply" in h.worker.receipts[-1]["outcome"]
    state = h.worker.owner.catalog.run_context.session_state
    assert state["mindroom_pending_compaction_scope_keys"]
    assert len(h.provider.requests) == 2


@pytest.mark.asyncio
async def test_interactive_context_actual_response_and_selection(response_harness, monkeypatch) -> None:

    h = response_harness
    guidance = []

    async def invoke(name, arguments):
        document = await h.worker.owner.operation(ContextReadOperation(operation="context.read", name="interactive"))
        guidance.append(document["text"])
        return document["text"]

    monkeypatch.setattr(h.worker, "invoke_shell", invoke)
    question = '```interactive\n{"question":"Which path?","options":[{"id":"fast","label":"Fast"},{"id":"careful","label":"Careful"}]}\n```'
    result = await h.respond([[("bash", {"command": "read interactive context"})], question], prompt="ask me")
    assert guidance
    assert "interactive" in guidance[0]
    rendered = parse_and_format_interactive(result, extract_mapping=True)
    assert rendered.interactive_metadata is not None
    chosen = rendered.interactive_metadata.option_map["1"]
    result = await h.respond(["same-session selection accepted"], prompt=f"The user selected: {chosen}")
    assert "same-session selection accepted" in result
    assert "Which path?" in str(h.provider.requests[-1]["messages"])
    assert h.worker.owner.catalog.run_context.session_id == h.runtime.session_id


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["allow", "deny", "revoke", "filter", "membership"])
async def test_authored_confirmation_actual_owner_rechecks_effect_boundary(
    response_harness,
    monkeypatch,
    decision,
) -> None:

    h = response_harness
    effects = []
    decisions = []
    original = agents.get_tool_by_name
    h.runtime.config.agents["helper"].tools = ["shell", "calculator"]
    current_config = h.runtime.config
    function_allowed = True

    def action(value: str) -> str:
        effects.append(value)
        return "effect:" + value

    def build(name, *args, **kwargs):
        if name != "calculator":
            return original(name, *args, **kwargs)
        toolkit = Toolkit(name="calculator", tools=[action])
        toolkit.get_async_functions()["action"].requires_confirmation = True
        return toolkit

    async def approve(paused):
        nonlocal current_config, function_allowed
        assert not effects
        assert paused.cli_call["arguments"] == {"value": "exact value"}
        assert paused.cli_call["parent_bash_call_id"] != paused.cli_call["call_id"]
        decisions.append(paused.cli_call)
        if decision == "revoke":
            # Hot reload publishes a replacement config; the prepared catalog keeps its snapshot.
            current_config = current_config.model_copy(deep=True)
            current_config.agents["helper"].tools = ["shell"]
        elif decision == "filter":
            function_allowed = False
        elif decision == "membership":
            h.runtime.config.administrators = []
            h.runtime.config.agents["helper"].access = ResponderAccessConfig(users=[])
        ids = [str(tool.tool_call_id) for tool in paused.tools]
        return tuple(
            apply_exact_approval_decisions(
                paused.requirements,
                decisions=dict.fromkeys(ids, decision != "deny"),
                denial_reasons=dict.fromkeys(ids, "No"),
            ),
        )

    monkeypatch.setattr(agents, "get_tool_by_name", build)
    h.runtime = replace(
        h.runtime,
        config_provider=lambda: current_config,
        cli_approval_handler=approve,
        tool_function_filter=lambda function: function_allowed or function.name != "action",
    )
    await h.respond([_bash_call("calculator", "action", {"value": "exact value"}), "done"])
    assert len(decisions) == 1
    assert effects == (["exact value"] if decision == "allow" else [])
    assert h.worker.receipts[-1]["status"] == ("completed" if decision == "allow" else "failed")
    assert all([tool["function"]["name"] for tool in request["tools"]] == ["bash"] for request in h.provider.requests)


@pytest.mark.asyncio
async def test_self_config_cli_preserves_native_next_build_change(response_harness) -> None:

    h = response_harness
    h.runtime.config.agents["helper"].tools = ["shell", "self_config"]
    h.runtime.runtime_paths.config_path.write_text(yaml.safe_dump(h.runtime.config.model_dump(mode="json")))
    approved = []

    async def approve(paused):
        # Self-config writes always require approval; the requester approves this one.
        ids = [str(tool.tool_call_id) for tool in paused.tools]
        approved.extend(ids)
        return tuple(
            apply_exact_approval_decisions(
                paused.requirements,
                decisions=dict.fromkeys(ids, True),
                denial_reasons=dict.fromkeys(ids),
            ),
        )

    h.runtime = replace(h.runtime, cli_approval_handler=approve)
    await h.respond(
        [_bash_call("self_config", "update_own_config", {"instructions": ["new persisted instruction"]}), "done"],
    )
    assert len(approved) == 1
    receipt = h.worker.receipts[-1]
    assert receipt["status"] == "completed"
    saved = yaml.safe_load(h.runtime.runtime_paths.config_path.read_text())
    assert saved["agents"]["helper"]["instructions"] == ["new persisted instruction"]
    assert "new persisted instruction" not in h.provider.requests[0]["messages"][0]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["shared", "user", "user_agent"])
async def test_concurrent_factory_owners_scoped_credentials_and_grants(tmp_path, monkeypatch, scope) -> None:  # noqa: C901

    base = _runtime_context(tmp_path)

    config = base.config
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "mindroom.plugin.json").write_text(
        json.dumps({"name": "parity", "tools_module": "tools.py", "skills": []}),
    )
    (plugin / "tools.py").write_text(PLUGIN)
    config.plugins = [PluginEntryConfig(path=str(plugin))]
    for name in ("helper", "other"):
        config.agents[name] = AgentConfig(
            display_name=name,
            tools=["shell", "parity"],
            learning=False,
            memory_backend="file",
            worker_scope=scope,
        )
    requesters = ["@alice:example.test", "@bob:example.test"]
    config.administrators = requesters
    registry = TurnToolRegistry()
    all_entered = asyncio.Event()
    all_completed = asyncio.Event()
    workers = {}
    providers = {}
    runtimes = {}
    secrets = {}
    completed = []

    class ConcurrentWorker(ProtocolWorker):
        async def invoke_shell(self, name, arguments):
            workers[self.owner.owner.execution_identity.session_id] = self
            if len(workers) == 4:
                all_entered.set()
            await all_entered.wait()
            result = await super().invoke_shell(name, arguments)
            completed.append(self)
            if len(completed) == 4:
                all_completed.set()
            await all_completed.wait()
            for peer in workers.values():
                if peer is self:
                    continue
                with pytest.raises(CliAuthenticationError):
                    self.owner.authenticate(
                        peer.grants[-1].raw_token,
                        now_ns=time.time_ns(),
                    )
                with pytest.raises(CliAuthenticationError):
                    await self.owner.get_call(peer.receipts[-1]["call_id"])
            for field in ("requester_id", "agent_name", "session_id", "worker_id", "generation"):
                with pytest.raises(ValidationError):
                    parse_operation({"operation": "tools.list", field: "forged"})
            return result

    @asynccontextmanager
    async def open_worker(runtime):
        yield ConcurrentWorker()

    monkeypatch.setattr(minimal_agent, "open_configured_cli_worker", open_worker)

    async def send(**request):
        runtime = get_tool_runtime_context()
        return await providers[runtime.session_id].send(**request)

    monkeypatch.setattr(
        OpenAIChat,
        "get_async_client",
        lambda _self: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=send))),
    )
    for agent in config.agents:
        for requester in requesters:
            session = f"{agent}-{requester}"
            runtime = replace(
                base,
                agent_name=agent,
                requester_id=requester,
                target=replace(base.target, session_id=session),
                orchestrator=SimpleNamespace(agent_cli_registry=registry),
            )
            runtimes[session] = runtime
            target = runtime.resolve_worker_target()
            key = target.worker_key
            secret = secrets.setdefault(key, "fake-scoped-" + str(uuid4()))
            save_scoped_credentials(
                "parity",
                {"api_key": secret},
                credentials_manager=get_runtime_credentials_manager(base.runtime_paths),
                worker_target=target,
            )
            provider = ScriptedProvider()
            provider.steps = [
                _bash_call("parity", "integration", {"digest": hashlib.sha256(secret.encode()).hexdigest()}),
                "done",
            ]
            providers[session] = provider
    persist_entity_accounts(config, base.runtime_paths)

    async def respond(runtime, mode="minimal"):
        turn = replace(
            _turn_context(),
            entity_label=runtime.agent_name,
            session_id=runtime.session_id,
            requester_id=runtime.requester_id,
            agent_mode=mode,
            run_id=None,
        )
        with tool_runtime_context(runtime):
            return await ai.ai_response(
                turn,
                prompt="concurrent scoped integration",
                runtime_paths=runtime.runtime_paths,
                config=config,
                execution_identity=build_execution_identity_from_runtime_context(runtime),
                supports_native_tool_approval=True,
            )

    minimal_steps = {session: provider.steps for session, provider in providers.items()}
    for session, runtime in runtimes.items():
        secret = secrets[runtime.resolve_worker_target().worker_key]
        providers[session].steps = [[("integration", {"digest": hashlib.sha256(secret.encode()).hexdigest()})], "done"]
    async with asyncio.timeout(20):
        standard_results = await asyncio.gather(*(respond(runtime, "standard") for runtime in runtimes.values()))
    assert all("done" in result for result in standard_results)
    for session, provider in providers.items():
        assert "primary credential accepted" in str(provider.requests[-1]["messages"])
        provider.steps = minimal_steps[session]
    async with asyncio.timeout(20):
        results = await asyncio.gather(*(respond(runtime) for runtime in runtimes.values()))
    assert all("done" in result for result in results)
    assert len(workers) == 4
    assert len(secrets) == (4 if scope == "user_agent" else 2)
    for session, worker in workers.items():
        assert worker.receipts[-1]["outcome"] == "primary credential accepted"
        with pytest.raises(CliAuthenticationError):
            registry.resolve("Bearer " + worker.grants[-1].raw_token, now_ns=time.time_ns())
        with pytest.raises(CliAuthenticationError):
            await worker.owner.get_call(worker.receipts[-1]["call_id"])
        assert not any(secret in str(providers[session].requests) for secret in secrets.values())
        assert all(
            [tool["function"]["name"] for tool in request["tools"]] == ["bash"]
            for request in providers[session].requests[2:]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_name", ["anthropic", "google"])
async def test_provider_sdk_initial_and_continued_minimal_requests(
    response_harness,
    monkeypatch,
    provider_name,
) -> None:
    from agno.models.anthropic import Claude  # noqa: PLC0415 - keep optional provider/server imports deferred
    from agno.models.google import Gemini  # noqa: PLC0415 - keep optional provider/server imports deferred
    from anthropic.lib.streaming import ContentBlockStopEvent  # noqa: PLC0415 - optional provider SDK
    from anthropic.types import (  # noqa: PLC0415 - keep optional provider/server imports deferred
        ContentBlockDeltaEvent,
        TextDelta,
        ToolUseBlock,
    )
    from google.genai.types import (  # noqa: PLC0415 - keep optional provider/server imports deferred
        Candidate,
        Content,
        FunctionCall,
        GenerateContentResponse,
        Part,
    )

    h = response_harness
    h.runtime.config.models["default"].provider = provider_name
    h.runtime.config.models["default"].id = "test-provider"
    h.runtime.config.agents["helper"].tools = ["shell", "calculator"]
    requests = []
    command = json.dumps(["calculator", "add", {"a": 2, "b": 3}])

    @asynccontextmanager
    async def anthropic_stream(**request):
        requests.append(request)
        first = len(requests) == 1

        async def events():
            if first:
                yield ContentBlockStopEvent(
                    type="content_block_stop",
                    index=0,
                    content_block=ToolUseBlock(
                        type="tool_use",
                        id="anthropic-bash",
                        name="bash",
                        input={"command": command},
                    ),
                )
            else:
                yield ContentBlockDeltaEvent(
                    type="content_block_delta",
                    index=0,
                    delta=TextDelta(type="text_delta", text="done"),
                )

        yield events()

    async def google_stream(**request):
        requests.append(request)
        first = len(requests) == 1

        async def events():
            part = (
                Part(function_call=FunctionCall(id="google-bash", name="bash", args={"command": command}))
                if first
                else Part(text="done")
            )
            yield GenerateContentResponse(candidates=[Candidate(content=Content(role="model", parts=[part]))])

        return events()

    messages = SimpleNamespace(stream=anthropic_stream)
    monkeypatch.setattr(
        Claude,
        "get_async_client",
        lambda _self: SimpleNamespace(messages=messages, beta=SimpleNamespace(messages=messages)),
    )
    monkeypatch.setattr(
        Gemini,
        "get_client",
        lambda _self: SimpleNamespace(
            aio=SimpleNamespace(models=SimpleNamespace(generate_content_stream=google_stream)),
        ),
    )
    result = await h.respond([])
    assert "done" in str(result)
    assert len(requests) == 2
    assert '"result": 5' in h.worker.receipts[-1]["outcome"]
    for request in requests:
        if provider_name == "anthropic":
            assert [tool["name"] for tool in request["tools"]] == ["bash"]
        else:
            assert [function.name for tool in request["config"].tools for function in tool.function_declarations] == [
                "bash",
            ]
    assert "result" in str(requests[1])


@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["after-toolcall", "next-turn"])
async def test_model_switch_timing_actual_minimal_requests(response_harness, when) -> None:
    h = response_harness
    h.runtime.config.agents["helper"].tools = ["shell", "thread_model"]
    h.runtime.config.models["other"] = h.runtime.config.models["default"].model_copy(update={"id": "other-model"})
    await h.respond([_bash_call("thread_model", "switch_thread_model", {"model_name": "other", "when": when}), "done"])
    assert [request["model"] for request in h.provider.requests] == [
        "test-model",
        "other-model" if when == "after-toolcall" else "test-model",
    ]
    assert len(h.worker.grants) == 1
    await h.respond(["next turn done"])
    assert h.provider.requests[-1]["model"] == "other-model"
    assert all([tool["function"]["name"] for tool in request["tools"]] == ["bash"] for request in h.provider.requests)
