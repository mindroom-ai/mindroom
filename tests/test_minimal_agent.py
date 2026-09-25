"""Minimal presentation must not prepare a hidden catalog during prompt sizing."""

# ruff: noqa: ANN001, ANN002, ANN003, ANN202, ARG001, ARG002, D103, PLR0915

import asyncio
from contextlib import ExitStack, asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Literal
from uuid import uuid4

import pytest
from agno.run import RunContext
from agno.run.agent import RunOutput
from agno.session import AgentSession
from agno.tools.function import FunctionCall
from agno.tools.toolkit import Toolkit

from mindroom import agents, ai, minimal_agent
from mindroom import minimal_agent as module
from mindroom.agent_cli.bash import MinimalBashTools
from mindroom.agent_cli.context import minimal_system_message
from mindroom.agent_cli.lifetime import response_cli_lifetime
from mindroom.agent_cli.protocol import ToolCallOperation, ToolListOperation
from mindroom.agent_cli.session import CliAuthenticationError, TurnToolRegistry
from mindroom.agent_knowledge_descriptions import KnowledgeToolDescribingAgent
from mindroom.agent_modes import resolve_agent_mode, set_agent_mode
from mindroom.agno_compat_prepared_tools import prepare_agent_tools
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.error_handling import MinimalModeUnavailableError, get_user_friendly_error_message
from mindroom.history.prompt_tokens import agent_tool_definition_payloads_for_logging, estimate_agent_static_tokens
from mindroom.history.session_context import close_agent_runtime_state_dbs
from mindroom.hooks import EnrichmentItem
from mindroom.memory.functions import MemoryPromptParts
from mindroom.minimal_agent import MinimalAgent
from mindroom.runtime_resolution import resolve_agent_storage
from mindroom.tool_system import sandbox_proxy
from mindroom.tool_system.events import CollectedStreamPresentation
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from mindroom.tool_system.skills import build_agent_skills
from mindroom.tool_system.tool_access import ToolKey
from mindroom.tool_system.worker_routing import agent_workspace_root_path
from tests.identity_helpers import persist_entity_accounts
from tests.minimal_agent_fixtures import ScriptedProvider
from tests.test_agent_cli_authority import _runtime_context, _turn_context


def test_explicit_mode_keeps_standard_factory_default(tmp_path: Path) -> None:
    config = Config(
        models={"default": ModelConfig(provider="openai", id="test-model")},
        agents={
            "helper": AgentConfig(
                display_name="Helper",
                role="Long authored role",
                instructions=["Long authored instruction"],
                minimal_instructions=["Required short rule"],
                tools=["shell"],
            ),
        },
    )
    runtime = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    persist_entity_accounts(config, runtime)
    standard = agents.create_agent("helper", config, runtime, None, persist_runtime_state=False)
    minimal = agents.create_agent("helper", config, runtime, None, persist_runtime_state=False, agent_mode="minimal")
    assert standard.system_message is None
    assert "Required short rule" in minimal.system_message
    assert "Long authored role" not in minimal.system_message
    assert "Long authored instruction" not in minimal.system_message
    assert minimal.context_documents["role"].endswith("Long authored role")
    assert "Long authored instruction" in minimal.context_documents["instructions"]
    tools = minimal.get_tools(
        RunOutput(run_id="budget"),
        RunContext(run_id="budget", session_id="budget", user_id="synthetic", session_state={}),
        AgentSession(session_id="budget"),
    )
    assert [name for tool in tools for name in tool.get_async_functions()] == ["bash"]


@pytest.mark.parametrize(
    "failure",
    [ImportError("No module named 'praw'"), ValueError("Tool requires a separate service.")],
)
def test_toolkit_construction_failure_keeps_minimal_recovery_hint(tmp_path, monkeypatch, failure) -> None:
    runtime = _runtime_context(tmp_path)
    runtime.config.agents["helper"] = AgentConfig(
        display_name="Helper",
        tools=["shell", "calculator"],
        memory_backend="file",
        learning=False,
    )
    original = agents.get_tool_by_name

    def load(tool_name, *args, **kwargs):
        if tool_name == "calculator":
            raise failure
        return original(tool_name, *args, **kwargs)

    monkeypatch.setattr(agents, "get_tool_by_name", load)
    identity = build_execution_identity_from_runtime_context(runtime)
    with pytest.raises(MinimalModeUnavailableError) as raised:
        agents.create_agent(
            "helper",
            runtime.config,
            runtime.runtime_paths,
            identity,
            agent_mode="minimal",
            persist_runtime_state=False,
        )

    message = get_user_friendly_error_message(raised.value, "helper")
    assert str(failure) in message
    assert "!mode helper standard" in message
    assert raised.value.__cause__ is failure

    standard = agents.create_agent(
        "helper",
        runtime.config,
        runtime.runtime_paths,
        identity,
        persist_runtime_state=False,
    )
    functions = {name for tool in standard.tools if isinstance(tool, Toolkit) for name in tool.get_async_functions()}
    assert "run_shell_command" in functions
    assert "add" not in functions


@pytest.mark.parametrize("memory_backend", ["file", "mem0"])
def test_context_files_remain_complete_and_notes_untouched(
    tmp_path: Path,
    memory_backend: Literal["file", "mem0"],
) -> None:
    notes = tmp_path / "agents/helper/workspace/context/notes.md"
    notes.parent.mkdir(parents=True)
    notes.write_text("full-content-" * 2000)
    workspace = notes.parent.parent
    (workspace / "MEMORY.md").write_text("Remember the existing memory entrypoint.")
    (workspace / "memory").mkdir()
    (workspace / "memory/2026-09-22.md").write_text("Remember the existing daily memory.")
    config = Config(
        models={"default": ModelConfig(provider="openai", id="test-model")},
        agents={
            "helper": AgentConfig(
                display_name="Helper",
                role="role",
                memory_backend=memory_backend,
                context_files=["context/notes.md", "missing.md"],
                tools=["shell"],
            ),
        },
    )
    config.defaults.max_preload_chars = 1000
    runtime = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    persist_entity_accounts(config, runtime)
    minimal = agents.create_agent("helper", config, runtime, None, persist_runtime_state=False, agent_mode="minimal")
    if memory_backend == "file":
        assert "context/notes.md" in minimal.system_message
        assert minimal.output_file_policy.workspace_root / "context/notes.md" == notes
        memory_line = next(line for line in minimal.system_message.splitlines() if line.startswith("File memory: "))
        entrypoint, directory = [
            minimal.output_file_policy.workspace_root / path for path in memory_line.split("`")[1::2]
        ]
        assert entrypoint.read_text() == "Remember the existing memory entrypoint."
        assert (directory / "2026-09-22.md").read_text() == "Remember the existing daily memory."
    else:
        assert "context/notes.md" not in minimal.system_message
        assert "File memory:" not in minimal.system_message
    assert "missing.md" not in minimal.system_message
    assert str(tmp_path) not in minimal.system_message
    assert "full-content-" not in minimal.system_message
    assert notes.read_text() in minimal.context_documents.values()
    assert all(not key.startswith("/") and ".." not in key for key in minimal.context_documents)
    assert notes.read_text() == "full-content-" * 2000


@pytest.mark.parametrize("instructions", [[], ["Follow the concise deployment guide."]])
def test_minimal_prompt_keeps_context_with_custom_instructions(instructions: list[str]) -> None:
    message = minimal_system_message(
        agent_name="helper",
        display_name="Helper",
        toolkit_names=["shell", "file"],
        instructions=instructions,
        context_files=["SOUL.md", "context/notes.md"],
    )
    assert "minimal mode" in message
    assert "mindroom-agent --help" in message
    assert "SOUL.md" in message
    assert "context/notes.md" in message
    if instructions:
        assert message.endswith(instructions[0])


def test_minimal_context_roster_is_bounded() -> None:

    message = minimal_system_message(
        agent_name="helper",
        display_name="Helper",
        toolkit_names=["tool" + str(number) for number in range(1000)],
        instructions=[],
    )
    assert len(message) < 2600
    assert "1000 toolkits" in message


def test_minimal_context_file_list_is_bounded() -> None:
    message = minimal_system_message(
        agent_name="helper",
        display_name="Helper",
        toolkit_names=[],
        instructions=[],
        context_files=[f"context/document-{number}.md" for number in range(1000)],
    )
    assert len(message) < 2600
    assert "1000 context files" in message
    assert "mindroom-agent context list" in message


def test_minimal_skill_document_reads_count_as_workspace_skill_use(tmp_path: Path) -> None:
    """Minimal mode reads skills as context documents, which feeds the same usage telemetry as skill tools."""
    skills_root = agent_workspace_root_path(tmp_path, "helper") / "skills"
    (skills_root / "deploy").mkdir(parents=True)
    (skills_root / "deploy" / "SKILL.md").write_text("---\nname: deploy\ndescription: Deploy\n---\nSteps\n")
    config = Config(agents={"helper": AgentConfig(display_name="Helper")})
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    agent = MinimalAgent(
        id="helper",
        name="Helper",
        skills=build_agent_skills("helper", config, runtime_paths, env_vars={}, credential_keys=set()),
    )
    agent.configure_minimal(
        instructions=[],
        interactive_prompt="",
        context_documents=[],
        deferred_toolkits=(),
        toolkit_names=[],
        minimal_instructions=[],
        context_files=[],
        memory_root=None,
        runtime_context="",
        output_file_policy=None,
        delegation_depth=0,
        refresh_scheduler=None,
    )
    assert agent.context_documents["skill-1"] == "deploy\nSteps"
    agent._record_skill_document_read("instructions")
    agent._record_skill_document_read("skill-1")
    assert '"use_count":1' in (skills_root / ".usage.json").read_text()


@pytest.mark.asyncio
async def test_minimal_async_preparation_requires_managed_owner() -> None:
    from agno.models.openai import OpenAIChat  # noqa: PLC0415 - keep optional provider/server imports deferred

    agent = MinimalAgent(id="helper", model=OpenAIChat(id="test-model", api_key="test"))
    with pytest.raises(RuntimeError, match="response owner"):
        await agent.aget_tools(
            RunOutput(run_id="run"),
            RunContext(run_id="run", session_id="session", session_state={}),
            AgentSession(session_id="session"),
            user_id="alice",
        )


@pytest.mark.asyncio
async def test_live_preparation_keeps_hidden_catalog_and_budget_pure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    context = _runtime_context(tmp_path)
    context.config.agents["helper"] = AgentConfig(display_name="Helper", tools=["shell"], memory_backend="file")
    context = replace(context, orchestrator=SimpleNamespace(agent_cli_registry=TurnToolRegistry()))
    events = []

    class Worker:
        handle = SimpleNamespace(worker_id="worker-1")

        async def install_grant(self, owner, grant, *, shell):
            assert owner.authenticate(grant.raw_token, now_ns=module.time.time_ns()) is owner.owner
            assert 0 < grant.expires_at_ns - module.time.time_ns() <= module.MAX_CLI_GRANT_LIFETIME_NS
            events.append((owner, grant, shell))

        async def invoke_shell(self, name, arguments):
            return "done"

    @asynccontextmanager
    async def open_worker(runtime):
        events.append("opened")
        yield Worker()
        events.append("closed")

    monkeypatch.setattr(module, "open_configured_cli_worker", open_worker)
    agent = agents.create_agent(
        "helper",
        context.config,
        context.runtime_paths,
        build_execution_identity_from_runtime_context(context),
        agent_mode="minimal",
        session_id=context.session_id,
    )
    agent.response_context = _turn_context()
    run = RunOutput(run_id="generation-1")
    run_context = RunContext(
        run_id="generation-1",
        session_id=context.session_id,
        user_id=context.requester_id,
        session_state={},
    )
    session = AgentSession(session_id=context.session_id)
    with tool_runtime_context(context):
        async with response_cli_lifetime() as lifetime:
            tools = await agent.aget_tools(run, run_context, session, user_id=context.requester_id)
            assert [name for tool in tools for name in tool.get_async_functions()] == ["bash"]
            owner = lifetime.owner
            assert owner is not None
            assert any(item.get("function") == "run_shell_command" for item in owner.catalog.metadata())
            catalog = owner.catalog
            agent.get_tools(
                RunOutput(run_id="budget"),
                RunContext(run_id="budget", session_id="budget", session_state={}),
                AgentSession(session_id="budget"),
            )
            assert owner.catalog is catalog
            assert lifetime.owner is owner
            assert len(events) == 2
    assert events[-1] == "closed"


@pytest.mark.asyncio
@pytest.mark.parametrize("rebuild", [False, True])
@pytest.mark.parametrize("deferred", [False, True])
@pytest.mark.parametrize(
    "include_tools",
    [
        None,
        ["run_shell_command"],
        ["run_shell_command", "check_shell_command"],
        ["run_shell_command", "kill_shell_command"],
        ["check_shell_command", "kill_shell_command"],
    ],
)
async def test_saved_minimal_mode_checks_shell_before_initial_or_rebuilt_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rebuild: bool,
    deferred: bool,
    include_tools: list[str] | None,
) -> None:
    """A saved choice cannot publish a Bash facade after losing shell operations."""
    runtime = _runtime_context(tmp_path)
    runtime.config.agents["helper"] = AgentConfig(
        display_name="Helper",
        tools=["shell"],
        memory_backend="file",
        learning=False,
    )
    registry = TurnToolRegistry()
    runtime = replace(runtime, orchestrator=SimpleNamespace(agent_cli_registry=registry))
    identity = build_execution_identity_from_runtime_context(runtime)
    root = resolve_agent_storage("helper", runtime.config, runtime.runtime_paths, identity).state_root
    set_agent_mode(root, "helper", runtime.session_id, "minimal", runtime.requester_id)
    saved_choice = (root / "agent_modes.json").read_bytes()
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    notes = workspace / "notes.md"
    notes.write_text("Keep this workspace note.")
    provider = ScriptedProvider()
    provider.install(monkeypatch)
    events = []
    grants = []

    class Worker:
        handle = SimpleNamespace(worker_id="eligibility-worker")

        async def install_grant(self, owner, grant, *, shell):
            events.append("installed")
            grants.append(grant)

    @asynccontextmanager
    async def worker(_runtime):
        events.append("opened")
        try:
            yield Worker()
        finally:
            events.append("closed")

    monkeypatch.setattr(minimal_agent, "open_configured_cli_worker", worker)

    def create():
        agent = agents.create_agent(
            "helper",
            runtime.config,
            runtime.runtime_paths,
            identity,
            agent_mode=resolve_agent_mode(root, "helper", runtime.session_id),
            session_id=runtime.session_id,
        )
        resources.callback(close_agent_runtime_state_dbs, agent)
        agent.response_context = _turn_context()
        return agent

    with ExitStack() as resources, tool_runtime_context(runtime):
        async with response_cli_lifetime() as lifetime:
            if rebuild:
                initial = create()
                result = await initial.arun(
                    "First request",
                    user_id=runtime.requester_id,
                    session_id=runtime.session_id,
                )
                assert result.content == "done"
                history = [run.to_dict() for run in initial.db.get_session(runtime.session_id).runs]
                await lifetime.retire_attempt()
            previous_owner = lifetime.owner
            prior_events = list(events)
            prior_requests = list(provider.requests)
            settings = {"defer": deferred}
            if include_tools is not None:
                settings["include_tools"] = include_tools
            runtime.config.agents["helper"].tools = [{"shell": settings}]
            agent = create()
            if include_tools is not None:
                result = await agent.arun(
                    "Restricted request",
                    user_id=runtime.requester_id,
                    session_id=runtime.session_id,
                )
                assert result.status == "ERROR"
                assert "run, check, and kill shell permissions" in result.content
                assert "!mode helper standard" in result.content
                assert events == prior_events
                assert provider.requests == prior_requests
                assert lifetime.owner is previous_owner
                if rebuild:
                    assert [
                        run.to_dict() for run in agent.db.get_session(runtime.session_id).runs[: len(history)]
                    ] == history
            else:
                result = await agent.arun(
                    "Supported request",
                    user_id=runtime.requester_id,
                    session_id=runtime.session_id,
                )
                assert result.content == "done"
                assert [tool["function"]["name"] for tool in provider.requests[-1]["tools"]] == ["bash"]
                assert len(provider.requests) == len(prior_requests) + 1
                assert events == ["opened", "installed"]
                if rebuild:
                    assert lifetime.owner is previous_owner
                    assert lifetime.owner.catalog.agent is agent
    assert events == (["opened", "installed", "closed"] if rebuild or include_tools is None else [])
    for grant in grants:
        with pytest.raises(CliAuthenticationError):
            registry.resolve("Bearer " + grant.raw_token, now_ns=0)
    assert (root / "agent_modes.json").read_bytes() == saved_choice
    assert notes.read_text() == "Keep this workspace note."


@pytest.mark.asyncio
async def test_minimal_worker_failure_offers_standard_mode_without_downgrading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken deployment leaves the saved mode intact and shows the recovery command."""
    runtime = _runtime_context(tmp_path)
    runtime.config.agents["helper"] = AgentConfig(display_name="Helper", tools=["shell"], memory_backend="file")
    runtime = replace(runtime, orchestrator=SimpleNamespace(agent_cli_registry=TurnToolRegistry()))
    identity = build_execution_identity_from_runtime_context(runtime)
    root = resolve_agent_storage("helper", runtime.config, runtime.runtime_paths, identity).state_root
    set_agent_mode(root, "helper", runtime.session_id, "minimal", runtime.requester_id)
    provider = ScriptedProvider()
    provider.install(monkeypatch)

    def unavailable_worker(_runtime):
        message = "Docker worker is unavailable"
        raise RuntimeError(message)

    monkeypatch.setattr(minimal_agent, "open_configured_cli_worker", unavailable_worker)
    agent = agents.create_agent(
        "helper",
        runtime.config,
        runtime.runtime_paths,
        identity,
        agent_mode=resolve_agent_mode(root, "helper", runtime.session_id),
        session_id=runtime.session_id,
    )
    agent.response_context = _turn_context()
    try:
        with tool_runtime_context(runtime):
            async with response_cli_lifetime() as lifetime:
                result = await agent.arun("Hello", user_id=runtime.requester_id, session_id=runtime.session_id)
                assert result.status == "ERROR"
                assert "Docker worker is unavailable" in result.content
                assert "!mode helper standard" in result.content
                assert lifetime.owner is None
        assert provider.requests == []
        assert resolve_agent_mode(root, "helper", runtime.session_id) == "minimal"
    finally:
        close_agent_runtime_state_dbs(agent)


@pytest.mark.asyncio
async def test_prepared_bash_facade_reports_its_per_run_copy(tmp_path: Path) -> None:
    from agno.models.openai import OpenAIChat  # noqa: PLC0415 - keep optional provider/server imports deferred

    prepared = []

    async def execute(key, arguments, fc):
        return "canonical owner"

    facade = MinimalBashTools(execute=execute, on_prepare=prepared.append)
    agent = MinimalAgent(id="helper", model=OpenAIChat(id="test-model", api_key="test"))
    functions = prepare_agent_tools(
        agent,
        processed_tools=[facade],
        run_response=RunOutput(run_id="run"),
        run_context=RunContext(run_id="run", session_id="session", session_state={}),
        session=AgentSession(session_id="session"),
    )
    function = functions[0]
    assert prepared[-1] is function
    call = FunctionCall(function=function, call_id="outer", arguments={"command": "true"})
    await call.aexecute()
    assert call.result == "canonical owner"


@pytest.mark.asyncio
async def test_prepared_minimal_prompt_moves_optional_enrichment_to_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    runtime = _runtime_context(tmp_path)
    runtime.config.agents["helper"] = AgentConfig(display_name="Helper", tools=["shell"], memory_backend="none")

    async def memory(*args, **kwargs):
        return MemoryPromptParts(
            session_preamble="optional preamble " * 100,
            transient_turn_context="optional memory " * 100,
        )

    monkeypatch.setattr(ai, "build_memory_prompt_parts", memory)
    ctx = replace(
        _turn_context(),
        agent_mode="minimal",
        transient_enrichment_items=(
            EnrichmentItem(key="plugin", text="optional plugin " * 100),
            EnrichmentItem(key="required", text="required current target", minimal_required=True),
        ),
        system_enrichment_items=(EnrichmentItem(key="system_plugin", text="optional system " * 100),),
    )
    prepared = await ai._prepare_agent_and_prompt(
        ctx,
        prompt="actual current user request",
        runtime_paths=runtime.runtime_paths,
        config=runtime.config,
        execution_identity=build_execution_identity_from_runtime_context(runtime),
    )
    assert "actual current user request" in prepared.prompt_text
    assert "required current target" in prepared.prompt_text
    assert "optional memory" not in prepared.prompt_text
    assert "optional plugin" not in prepared.prompt_text
    assert "optional system" not in prepared.agent.system_message
    documents = "\n".join(prepared.agent.context_documents.values())
    assert "optional memory" in documents
    assert "optional plugin" in documents
    assert "optional system" in documents


@pytest.mark.asyncio
async def test_deferred_catalog_uses_normal_builder_without_opening_unrelated_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    runtime = _runtime_context(tmp_path)
    runtime.config.agents["helper"] = AgentConfig(
        display_name="Helper",
        tools=["shell", {"calculator": {"defer": True}}, {"duckduckgo": {"defer": True}}],
    )
    original = agents.build_agent_toolkit

    def build(name, **kwargs):
        if name == "duckduckgo":
            msg = "unrelated integration opened"
            raise AssertionError(msg)
        return original(name, **kwargs)

    monkeypatch.setattr(agents, "build_agent_toolkit", build)
    agent = agents.create_agent(
        "helper",
        runtime.config,
        runtime.runtime_paths,
        build_execution_identity_from_runtime_context(runtime),
        agent_mode="minimal",
        session_id=runtime.session_id,
    )
    with tool_runtime_context(runtime):
        catalog = await agent.prepare_execution_catalog(
            RunOutput(run_id="run"),
            RunContext(run_id="run", session_id=runtime.session_id, session_state={}),
            AgentSession(session_id=runtime.session_id),
            user_id=runtime.requester_id,
        )
        try:
            metadata = catalog.metadata()
            assert any(item["toolkit"] == "calculator" and item.get("deferred") for item in metadata)
            binding = await catalog.bind(ToolKey("calculator", "add"))
            assert binding.catalog.agent is agent
            assert binding.function.name == "add"
            assert any(item["toolkit"] == "duckduckgo" and item.get("deferred") for item in catalog.metadata())
        finally:
            await catalog.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("switch_model", [False, True])
async def test_real_response_requests_only_bash_after_deferred_call_and_history(  # noqa: C901 - real response across continuations
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    streamed: bool,
    switch_model: bool,
) -> None:
    """The supported response entrypoints format actual SDK requests with one tool."""
    from agno.models.openai import OpenAIChat  # noqa: PLC0415 - keep optional provider/server imports deferred
    from openai.types.chat import ChatCompletionChunk  # noqa: PLC0415 - keep optional provider/server imports deferred

    runtime = _runtime_context(tmp_path)
    runtime.config.agents["helper"] = AgentConfig(
        display_name="Helper",
        role="optional long role " * 500,
        tools=["shell", "thread_model", {"calculator": {"defer": True}}],
        memory_backend="file",
        learning=False,
    )
    runtime = replace(runtime, orchestrator=SimpleNamespace(agent_cli_registry=TurnToolRegistry()))
    persist_entity_accounts(runtime.config, runtime.runtime_paths)
    runtime.config.models["other"] = runtime.config.models["default"].model_copy(update={"id": "other-model"})
    requests = []
    workers = []

    class Worker:
        handle = SimpleNamespace(worker_id="worker")
        owner = None

        async def install_grant(self, owner, grant, *, shell):
            self.owner = owner
            workers.append(self)

        async def invoke_shell(self, name, arguments):
            assert name == "run_shell_command"
            assert arguments["args"] == "discover"
            listing = await self.owner.operation(ToolListOperation(operation="tools.list"))
            assert "calculator" in str(listing)
            call_id = uuid4()
            await self.owner.operation(
                ToolCallOperation(
                    operation="tools.call",
                    toolkit="calculator",
                    function="add",
                    arguments={"a": 2, "b": 3},
                    call_id=call_id,
                ),
            )
            while (receipt := await self.owner.get_call(str(call_id)))["status"] in {"queued", "running"}:  # noqa: ASYNC110 - poll actual CLI receipt protocol
                await asyncio.sleep(0)
            assert receipt["status"] == "completed", receipt
            if switch_model:
                switch_id = uuid4()
                await self.owner.operation(
                    ToolCallOperation(
                        operation="tools.call",
                        toolkit="thread_model",
                        function="switch_thread_model",
                        arguments={"model_name": "other", "when": "after-toolcall"},
                        call_id=switch_id,
                    ),
                )
                while (switch_receipt := await self.owner.get_call(str(switch_id)))["status"] in {"queued", "running"}:  # noqa: ASYNC110 - actual receipt protocol
                    await asyncio.sleep(0)
                assert switch_receipt["status"] == "completed", switch_receipt
            return "discovered result: " + str(receipt["outcome"])

    @asynccontextmanager
    async def worker(_runtime):
        yield Worker()

    async def send(**request):
        requests.append(request)
        assert [tool["function"]["name"] for tool in request["tools"]] == ["bash"]
        assert "optional long role" not in str(request["messages"])
        assert "actual current request" in str(request["messages"])
        first = len(requests) == 1

        async def chunks():
            if first:
                yield ChatCompletionChunk(
                    id="completion",
                    model="test-model",
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
                                        "id": "bash-1",
                                        "type": "function",
                                        "function": {"name": "bash", "arguments": '{"command":"discover"}'},
                                    },
                                ],
                            },
                            "finish_reason": None,
                        },
                    ],
                )
            else:
                yield ChatCompletionChunk(
                    id="completion",
                    model="test-model",
                    created=0,
                    object="chat.completion.chunk",
                    choices=[{"index": 0, "delta": {"role": "assistant", "content": "done"}, "finish_reason": None}],
                )
            yield ChatCompletionChunk(
                id="completion",
                model="test-model",
                created=0,
                object="chat.completion.chunk",
                choices=[{"index": 0, "delta": {}, "finish_reason": "tool_calls" if first else "stop"}],
            )

        return chunks()

    monkeypatch.setattr(minimal_agent, "open_configured_cli_worker", worker)
    monkeypatch.setattr(
        OpenAIChat,
        "get_async_client",
        lambda _self: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=send))),
    )
    ctx = replace(_turn_context(), agent_mode="minimal", run_id=None)
    identity = build_execution_identity_from_runtime_context(runtime)

    async def respond():
        kwargs = {
            "prompt": "actual current request",
            "runtime_paths": runtime.runtime_paths,
            "config": runtime.config,
            "execution_identity": identity,
            "supports_native_tool_approval": True,
            "show_tool_calls": True,
        }
        if streamed:
            return await ai.collect_streamed_response_content(
                ai.stream_agent_response(ctx, **kwargs),
                presentation=CollectedStreamPresentation(show_tool_calls=True),
            )
        return await ai.ai_response(ctx, **kwargs)

    with tool_runtime_context(runtime):
        result = await respond()
        assert "done" in str(result)
        assert len(requests) == 2
        assert requests[1]["model"] == ("other-model" if switch_model else "test-model")
        saved = workers[0].owner.catalog.agent.db.get_session(ctx.session_id)
        assert saved is not None
        assert saved.runs
        assert saved.runs[-1].status == "COMPLETED", [(run.status, len(run.messages or [])) for run in saved.runs or []]
        set_agent_mode(
            runtime.runtime_paths.storage_root / "agents/helper",
            "helper",
            ctx.session_id,
            "standard",
            runtime.requester_id,
        )
        ctx = replace(ctx, reply_to_event_id="$next", correlation_id="next")
        runtime = replace(runtime, target=replace(runtime.target, reply_to_event_id="$next"), correlation_id="next")
        identity = build_execution_identity_from_runtime_context(runtime)
        with tool_runtime_context(runtime):
            result = await respond()
        assert "done" in str(result)
    assert len(requests) == 3
    assert len(workers) == 2
    replay = requests[-1]["messages"]
    replayed_ids = {call["id"] for message in replay for call in message.get("tool_calls", [])}
    assert any(message.get("tool_call_id") in replayed_ids for message in replay), replay
    assert all("add" not in str(message.get("tool_calls", [])) for message in replay)


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [False, True])
async def test_routed_shell_reuses_effective_global_and_agent_path_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    override: bool,
) -> None:

    runtime = _runtime_context(tmp_path)
    runtime.config.defaults.tools = [{"shell": {"shell_path_prepend": "/global/bin"}}]
    runtime.config.agents["helper"] = AgentConfig(
        display_name="Helper",
        memory_backend="file",
        include_default_tools=True,
        tools=[{"shell": {"shell_path_prepend": "/agent/bin"}}] if override else [],
        worker_tools=["shell"],
    )
    runtime = replace(runtime, orchestrator=SimpleNamespace(agent_cli_registry=TurnToolRegistry()))
    monkeypatch.setattr(sandbox_proxy, "sandbox_proxy_enabled_for_tool", lambda *_args, **_kwargs: True)
    captured = []

    class Worker:
        handle = SimpleNamespace(worker_id="worker")

        async def install_grant(self, owner, grant, *, shell):
            captured.append(shell)

        async def invoke_shell(self, name, arguments):
            pytest.fail("sizing must not execute")

    @asynccontextmanager
    async def worker(_runtime):
        yield Worker()

    monkeypatch.setattr(minimal_agent, "open_configured_cli_worker", worker)
    agent = agents.create_agent(
        "helper",
        runtime.config,
        runtime.runtime_paths,
        build_execution_identity_from_runtime_context(runtime),
        agent_mode="minimal",
        session_id=runtime.session_id,
    )
    agent.response_context = _turn_context()
    assert estimate_agent_static_tokens(agent, "budget input") > 0
    assert not captured
    assert [item["name"] for item in agent_tool_definition_payloads_for_logging(agent)] == ["bash"]
    with tool_runtime_context(runtime):
        async with response_cli_lifetime() as lifetime:
            await agent.aget_tools(
                RunOutput(run_id="generation-1"),
                RunContext(
                    run_id="generation-1",
                    session_id=runtime.session_id,
                    user_id=runtime.requester_id,
                    session_state={},
                ),
                AgentSession(session_id=runtime.session_id),
                user_id=runtime.requester_id,
            )
            catalog = lifetime.owner.catalog
            assert estimate_agent_static_tokens(agent, "budget input") > 0
            assert lifetime.owner.catalog is catalog
            assert len(captured) == 1
    assert captured[0].shell_path_prepend == ("/agent/bin" if override else "/global/bin")


@pytest.mark.asyncio
async def test_minimal_rejects_loaded_toolkit_omitted_by_native_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    agent = MinimalAgent(id="helper", tools=[Toolkit(name="unavailable")])

    async def unavailable(*_args, **_kwargs):
        return []

    monkeypatch.setattr(KnowledgeToolDescribingAgent, "aget_tools", unavailable)
    with pytest.raises(RuntimeError, match=r"unavailable.*failed connection"):
        await agent.aget_execution_tools(
            RunOutput(run_id="run"),
            RunContext(run_id="run", session_id="session"),
            AgentSession(session_id="session"),
        )
