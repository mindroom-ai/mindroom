"""Release comparisons through the supported response factory and CLI owner."""

# ruff: noqa: ANN001, ANN002, ANN003, ANN202, ARG001, ARG002, D103, PLR0915
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
from collections.abc import AsyncIterator, Iterator  # noqa: TC003 - Agno evaluates tool annotations
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
import yaml
from agno.run import RunContext  # noqa: TC002 - injected tool argument
from agno.tools.toolkit import Toolkit
from pydantic import ValidationError

from mindroom import agents, ai
from mindroom.agent_cli.protocol import (
    ContextReadOperation,
    ToolCallOperation,
    ToolDescribeOperation,
    ToolListOperation,
    parse_operation,
)
from mindroom.agent_cli.session import CliAuthenticationError, TurnToolRegistry
from mindroom.agent_cli.shell_contract import current_agent_cli_shell_env
from mindroom.budgets.monitor import BudgetMonitor
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.plugin import PluginEntryConfig
from mindroom.credentials.manager import get_runtime_credentials_manager, save_scoped_credentials
from mindroom.interactive import parse_and_format_interactive
from mindroom.knowledge.read_protocol import SearchHit
from mindroom.knowledge.search import AgentKnowledge, KnowledgeSource, KnowledgeSourceDescription
from mindroom.mcp.config import MCPServerConfig
from mindroom.mcp.manager import MCPServerManager
from mindroom.mcp.registry import sync_mcp_tool_registry
from mindroom.mcp.toolkit import bind_mcp_server_manager
from mindroom.memory import search_agent_memories
from mindroom.response_turn import apply_exact_approval_decisions
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.tool_system.events import CollectedStreamPresentation
from mindroom.tool_system.runtime_context import (
    build_execution_identity_from_runtime_context,
    get_tool_runtime_context,
    tool_runtime_context,
)
from mindroom.tool_system.worker_routing import agent_workspace_root_path, get_tool_execution_identity
from tests.access_schema_support import with_responder_access
from tests.delegation_helpers import ScriptedAgents
from tests.fakes.scripted_model import SYNTHETIC, ScriptedModel, answer
from tests.identity_helpers import persist_entity_accounts
from tests.minimal_agent_fixtures import (  # noqa: F401 - agent_cli_api is a pytest fixture
    PLUGIN,
    ScriptedProvider,
    agent_cli_api,
    cli_window,
    install_scripted_shell,
    offered_tools,
    shell_cli_owner,
)
from tests.test_agent_cli_authority import _runtime_context, _turn_context

if TYPE_CHECKING:
    from mindroom.llm.provider import ModelRequest, StreamEvent
    from mindroom.tool_system.invocation import ToolCall, ToolNext

pytestmark = pytest.mark.usefixtures("enforce_turn_authorization", "agent_cli_api")


class ProtocolShell:
    """Use the real live protocol while replacing only the shell's subprocess."""

    def __init__(self, registry: TurnToolRegistry) -> None:
        self.registry = registry
        self.owner = None
        self.receipts = []
        self.tokens = []
        self.catalog_snapshots = []

    def bind(self) -> None:
        """Reach the response owner only through the grant this Bash call exports."""
        self.owner = shell_cli_owner(self.registry)
        token = current_agent_cli_shell_env().token
        if token not in self.tokens:
            self.tokens.append(token)

    async def run_shell_command(self, args: str, timeout: int = 120, tail: int = 100) -> str:  # noqa: ASYNC109
        """Dispatch through the actual owner protocol inside the Bash window."""
        self.bind()
        toolkit, function, values = json.loads(args)
        listing = await self.owner.operation(window=cli_window(), operation=ToolListOperation(operation="tools.list"))
        assert toolkit in str(listing)
        description = await self.owner.operation(
            window=cli_window(),
            operation=ToolDescribeOperation(operation="tools.describe", toolkit=toolkit, function=function),
        )
        assert function in str(description)
        call_id = uuid4()
        await self.owner.operation(
            window=cli_window(),
            operation=ToolCallOperation(
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
async def test_response_direct_cli_parity(tmp_path, monkeypatch, function, streamed) -> None:
    """Removing canonical context, hooks, generators, or hidden dispatch breaks parity."""
    runtime = _runtime_context(tmp_path)
    runtime.config.administrators = [runtime.requester_id]
    runtime.config.agents["helper"] = AgentConfig(
        display_name="Helper",
        tools=["shell", "calculator"],
        memory_backend="file",
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

    async def hook(call: ToolCall, call_next: ToolNext) -> object:
        hooks.append((call.function.name, "before"))
        result = await call_next(call.arguments)
        hooks.append((call.function.name, "after"))
        return result

    original = agents.get_tool_by_name

    def build(name, *args, **kwargs):
        if name != "calculator":
            return original(name, *args, **kwargs)
        return Toolkit(name="calculator", tools=[sync, asynchronous, generator, async_generator])

    monkeypatch.setattr(agents, "get_tool_by_name", build)
    # The build's tool policy, which runs around every call the reply makes, natively or through Bash.
    monkeypatch.setattr(agents, "_build_agent_tool_middleware", lambda **_kwargs: hook)
    shell = ProtocolShell(runtime.orchestrator.agent_cli_registry)
    install_scripted_shell(monkeypatch, shell.run_shell_command)
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
    assert [hook for hook in hooks if hook[0] == function] == [(function, "before"), (function, "after")] * 2
    # Minimal Bash runs the agent's own shell call under the same policy; the Bash facade itself has none.
    assert ("bash", "before") not in hooks
    assert ("run_shell_command", "before") in hooks
    assert shell.receipts[0]["outcome"] == "result:7"
    assert shell.owner.catalog.run.session_state["parity"] == 7
    assert all("done" in str(result) for result in results)
    assert len(provider.requests) == 4
    assert function in offered_tools(provider.requests[0])
    assert all(offered_tools(request) == ["bash"] for request in provider.requests[2:])
    assert "result:7" in str(provider.requests[1].messages)
    assert "result:7" in str(provider.requests[3].messages)
    assert shell.tokens[0] not in str(provider.requests)


@pytest.mark.asyncio
async def test_same_child_responder_direct_and_cli_provenance(tmp_path, monkeypatch) -> None:
    """The same child responder keeps native parent and follow-up relationships in both modes."""
    comparisons = []
    respond_as_child = ai.run_delegated_child_response
    for mode in ("standard", "minimal"):
        runtime = _runtime_context(tmp_path / mode)
        runtime.config.administrators = [runtime.requester_id]
        runtime.config.agents["helper"] = AgentConfig(
            display_name="Helper",
            tools=["shell"],
            delegate_to=["code"],
            memory_backend="file",
        )
        runtime.config.agents["code"] = AgentConfig(display_name="Code")
        with_responder_access(runtime.config, "code", users=[runtime.requester_id])
        runtime = replace(
            runtime,
            orchestrator=SimpleNamespace(
                agent_cli_registry=TurnToolRegistry(),
                budgets=BudgetMonitor(
                    runtime_paths=runtime.runtime_paths,
                    config_provider=lambda config=runtime.config: config,
                ),
            ),
        )
        persist_entity_accounts(runtime.config, runtime.runtime_paths)
        children = []

        async def child_response(child, *, children=children, **kwargs):
            children.append(child)
            return await respond_as_child(child, **kwargs)

        monkeypatch.setattr(ai, "run_delegated_child_response", child_response)
        shell = ProtocolShell(runtime.orchestrator.agent_cli_registry)
        install_scripted_shell(monkeypatch, shell.run_shell_command)
        provider = ScriptedProvider()
        ScriptedAgents({"helper": provider, "code": ScriptedModel([answer("child done")] * 2)}).install(monkeypatch)
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
                receipt["call_id"] for receipt in shell.receipts
            ]
            assert all(receipt["parent_bash_call_id"] != receipt["call_id"] for receipt in shell.receipts)
            assert all(offered_tools(request) == ["bash"] for request in provider.requests)
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
        memory_backend="file",
    )
    runtime = replace(
        runtime,
        orchestrator=SimpleNamespace(
            agent_cli_registry=TurnToolRegistry(),
            budgets=BudgetMonitor(runtime_paths=runtime.runtime_paths, config_provider=lambda: runtime.config),
        ),
    )
    shell = ProtocolShell(runtime.orchestrator.agent_cli_registry)
    install_scripted_shell(monkeypatch, shell.run_shell_command)
    provider = ScriptedProvider()
    provider.install(monkeypatch)

    h = SimpleNamespace(runtime=runtime, shell=shell, provider=provider)

    async def respond(steps, *, mode="minimal", prompt="release parity", knowledge=None):
        runtime = h.runtime
        persist_entity_accounts(runtime.config, runtime.runtime_paths)
        provider.steps = steps
        with tool_runtime_context(runtime):
            return await ai.ai_response(
                replace(_turn_context(), agent_mode=mode, run_id=None),
                prompt=prompt,
                runtime_paths=runtime.runtime_paths,
                config=runtime.config,
                knowledge=knowledge,
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
    old_messages = [
        message.to_dict() for message in storage.load_session(h.runtime.session_id, "agent").runs[0].messages
    ]
    await h.respond([_bash_call("memory", "add_memory", {"content": "Release parity durable lesson"}), "minimal saved"])
    assert h.shell.receipts[-1]["outcome"] == "Memorized: Release parity durable lesson"
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
    session = storage.load_session(h.runtime.session_id, "agent")
    assert len(session.runs) == 3
    assert [message.to_dict() for message in session.runs[0].messages] == old_messages
    final = h.provider.requests[-1]
    assert "standard history sentinel" in str(final.messages)
    assert "Release parity durable lesson" in str(final.messages)
    assert offered_tools(h.provider.requests[1]) == ["bash"]
    assert "add_memory" in offered_tools(final)
    calls = {call["id"] for message in final.messages for call in message.tool_calls or ()}
    assert any(message.tool_call_id in calls for message in final.messages)


class _ReleaseIndex:
    """A published index that answers every search with one chunk naming the query."""

    def search(self, query: str, *, limit: int) -> list[SearchHit]:
        return [SearchHit(content=f"knowledge:{query}")]

    async def asearch(self, query: str, *, limit: int) -> list[SearchHit]:
        return self.search(query, limit=limit)


@pytest.mark.asyncio
@pytest.mark.parametrize("generated", ["skills", "knowledge"])
async def test_generated_tools_actual_minimal_response(response_harness, generated) -> None:
    """The agent's generated functions are reached through Bash as `agent`, with Bash still the only tool offered."""
    h = response_harness
    skill = agent_workspace_root_path(h.runtime.runtime_paths.storage_root, "helper") / "skills" / "release-guide"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: release-guide\ndescription: Release guide\n---\nUse the same canonical session.\n",
    )
    knowledge = AgentKnowledge(
        sources=(
            KnowledgeSource(description=KnowledgeSourceDescription("release", "Releases"), index=_ReleaseIndex()),
        ),
    )
    function, arguments, expected = {
        "skills": ("get_skill_instructions", {"skill_name": "release-guide"}, "same canonical session"),
        "knowledge": ("search_knowledge_base", {"query": "release"}, "knowledge:release"),
    }[generated]
    result = await h.respond(
        [_bash_call("agent", function, arguments), "done"],
        knowledge=knowledge if generated == "knowledge" else None,
    )
    assert "done" in str(result)
    assert expected in str(h.shell.receipts[-1]["outcome"])
    assert expected in str(h.provider.requests[-1].messages)
    assert all(offered_tools(request) == ["bash"] for request in h.provider.requests)


@pytest.mark.asyncio
async def test_deferred_toolkit_is_callable_through_bash_without_loading(response_harness) -> None:

    h = response_harness
    h.runtime.config.agents["helper"] = AgentConfig(
        display_name="Helper",
        memory_backend="file",
        tools=["shell", {"calculator": {"defer": True}}],
    )
    result = await h.respond([_bash_call("calculator", "add", {"a": 2, "b": 3}), "done"])
    assert "done" in str(result)
    assert len(h.shell.receipts) == 1
    assert '"result": 5' in h.shell.receipts[0]["outcome"]
    assert len(h.provider.requests) == 2
    assert all(offered_tools(request) == ["bash"] for request in h.provider.requests)


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
        'from mcp.server.mcpserver import MCPServer\nserver = MCPServer("echo")\n@server.tool()\ndef echo(text: str) -> str:\n return "echo:" + text\n@server.tool()\ndef secret() -> str:\n raise AssertionError("filtered tool ran")\nserver.run()\n',
    )
    h.runtime.config.mcp_servers = {
        "echo": MCPServerConfig(transport="stdio", command=sys.executable, args=[str(script)]),
    }
    h.runtime.config.agents["helper"] = AgentConfig(
        display_name="Helper",
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
        assert "echo:same requester" in h.shell.receipts[-1]["outcome"]
        assert len(calls) == 1
        server, function, arguments, kwargs = calls[0]
        assert (server, function, arguments) == ("echo", "echo", {"text": "same requester"})
        assert kwargs["worker_target"].execution_identity.requester_id == h.runtime.requester_id
        assert kwargs["include_tools"] == ["echo"]
        assert kwargs["exclude_tools"] == ["secret"]
        metadata = h.shell.catalog_snapshots[-1]
        assert next(item for item in metadata if item["toolkit"] == "duckduckgo")["deferred"] is True
        assert not any(item.get("function") == "echo_secret" for item in metadata)
        assert all(offered_tools(request) == ["bash"] for request in h.provider.requests)
    finally:
        sync_mcp_tool_registry(None)
        await manager.shutdown()


@pytest.mark.asyncio
async def test_compaction_cli_marks_real_session_before_next_reply(response_harness) -> None:
    h = response_harness
    h.runtime.config.agents["helper"].tools = ["shell", "compact_context"]
    h.runtime.config.models["default"].context_window = 128000
    result = await h.respond([_bash_call("compact_context", "compact_context", {}), "done"])
    assert "done" in str(result)
    assert "before the next reply" in h.shell.receipts[-1]["outcome"]
    state = h.shell.owner.catalog.run.session_state
    assert state["mindroom_pending_compaction_scope_keys"]
    assert len(h.provider.requests) == 2


@pytest.mark.asyncio
async def test_interactive_context_actual_response_and_selection(response_harness, monkeypatch) -> None:

    h = response_harness
    guidance = []

    async def run_shell_command(args: str, timeout: int = 120, tail: int = 100) -> str:  # noqa: ASYNC109
        h.shell.bind()
        document = await h.shell.owner.operation(
            window=cli_window(),
            operation=ContextReadOperation(operation="context.read", name="interactive"),
        )
        guidance.append(document["text"])
        return document["text"]

    install_scripted_shell(monkeypatch, run_shell_command)
    question = '```interactive\n{"question":"Which path?","options":[{"id":"fast","label":"Fast"},{"id":"careful","label":"Careful"}]}\n```'
    result = await h.respond([[("bash", {"command": "read interactive context"})], question], prompt="ask me")
    assert guidance
    assert "interactive" in guidance[0]
    rendered = parse_and_format_interactive(result, extract_mapping=True)
    assert rendered.interactive_metadata is not None
    chosen = rendered.interactive_metadata.option_map["1"]
    result = await h.respond(["same-session selection accepted"], prompt=f"The user selected: {chosen}")
    assert "same-session selection accepted" in result
    assert "Which path?" in str(h.provider.requests[-1].messages)
    assert h.shell.owner.catalog.run.session_id == h.runtime.session_id


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
    assert h.shell.receipts[-1]["status"] == ("completed" if decision == "allow" else "failed")
    assert all(offered_tools(request) == ["bash"] for request in h.provider.requests)


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
    receipt = h.shell.receipts[-1]
    assert receipt["status"] == "completed"
    saved = yaml.safe_load(h.runtime.runtime_paths.config_path.read_text())
    assert saved["agents"]["helper"]["instructions"] == ["new persisted instruction"]
    assert "new persisted instruction" not in h.provider.requests[0].messages[0].get_content_string()


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
            memory_backend="file",
            worker_scope=scope,
        )
    requesters = ["@alice:example.test", "@bob:example.test"]
    config.administrators = requesters
    registry = TurnToolRegistry()
    all_entered = asyncio.Event()
    all_completed = asyncio.Event()
    shells = {}
    providers = {}
    runtimes = {}
    secrets = {}
    completed = []

    async def run_shell_command(args: str, timeout: int = 120, tail: int = 100) -> str:  # noqa: ASYNC109
        shell = ProtocolShell(registry)
        shell.bind()
        shells[shell.owner.owner.execution_identity.session_id] = shell
        if len(shells) == 4:
            all_entered.set()
        await all_entered.wait()
        result = await shell.run_shell_command(args)
        completed.append(shell)
        if len(completed) == 4:
            all_completed.set()
        await all_completed.wait()
        for peer in shells.values():
            if peer is shell:
                continue
            with pytest.raises(CliAuthenticationError):
                shell.owner.authenticate(peer.tokens[-1], now_ns=time.time_ns())
            with pytest.raises(CliAuthenticationError):
                await shell.owner.get_call(peer.receipts[-1]["call_id"])
        for forged in ("requester_id", "agent_name", "session_id", "generation"):
            with pytest.raises(ValidationError):
                parse_operation({"operation": "tools.list", forged: "forged"})
        return result

    install_scripted_shell(monkeypatch, run_shell_command)
    # Each conversation gets its own scripted model, chosen by the session its reply runs in.
    ScriptedAgents({}, by_session=providers).install(monkeypatch)
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
                primary_built_tool=True,
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
        assert "primary credential accepted" in str(provider.requests[-1].messages)
        provider.steps = minimal_steps[session]
    async with asyncio.timeout(20):
        results = await asyncio.gather(*(respond(runtime) for runtime in runtimes.values()))
    assert all("done" in result for result in results)
    assert len(shells) == 4
    assert len(secrets) == (4 if scope == "user_agent" else 2)
    for session, shell in shells.items():
        assert shell.receipts[-1]["outcome"] == "primary credential accepted"
        with pytest.raises(CliAuthenticationError):
            registry.resolve("Bearer " + shell.tokens[-1], now_ns=time.time_ns())
        with pytest.raises(CliAuthenticationError):
            await shell.owner.get_call(shell.receipts[-1]["call_id"])
        assert not any(secret in str(providers[session].requests) for secret in secrets.values())
        assert all(offered_tools(request) == ["bash"] for request in providers[session].requests[2:])


@dataclass
class _NamedModel(ScriptedProvider):
    """A scripted model that logs its own name for every request it answers."""

    log: list[str] = field(default_factory=list)

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        """Log this model's id, then answer as scripted."""
        self.log.append(self.identity.id)
        async for event in super().stream(request):
            yield event


@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["after-toolcall", "next-turn"])
async def test_model_switch_timing_actual_minimal_requests(response_harness, monkeypatch, when) -> None:
    h = response_harness
    h.runtime.config.agents["helper"].tools = ["shell", "thread_model"]
    h.runtime.config.models["other"] = h.runtime.config.models["default"].model_copy(update={"id": "other-model"})
    steps: list = []
    log: list[str] = []
    requests: list[ModelRequest] = []
    models = {
        name: _NamedModel(steps=steps, requests=requests, log=log, identity=replace(SYNTHETIC, id=model_id))
        for name, model_id in (("default", "test-model"), ("other", "other-model"))
    }
    monkeypatch.setattr(agents, "_build_agent_model_provider", lambda _config, _paths, name, _identity: models[name])
    steps.extend([_bash_call("thread_model", "switch_thread_model", {"model_name": "other", "when": when}), "done"])
    await h.respond([])
    assert log == ["test-model", "other-model" if when == "after-toolcall" else "test-model"]
    assert len(h.shell.tokens) == 1
    steps.append("next turn done")
    assert "next turn done" in await h.respond([])
    assert log[-1] == "other-model"
    assert all(offered_tools(request) == ["bash"] for request in requests)
