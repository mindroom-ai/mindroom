"""An authored persona replaces an agent's presentation while keeping its own tools and identity."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest
from agno.agent import _messages as agent_messages
from agno.knowledge.knowledge import Knowledge
from agno.models.message import Message
from agno.run import RunContext
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.session import AgentSession
from agno.tools.function import Function
from agno.tools.toolkit import Toolkit

from mindroom import agents, ai
from mindroom.agent_storage import create_session_storage
from mindroom.config.agent import AgentConfig
from mindroom.config.approval import ApprovalRuleConfig
from mindroom.config.models import ModelConfig
from mindroom.credentials import get_runtime_credentials_manager
from mindroom.delegation.personas import PersonaError, caller_toolkit_names, inline_persona
from mindroom.history.archive import archive_runs
from mindroom.history.replay import is_compaction_summary
from mindroom.history.session_context import open_resolved_scope_session_context
from mindroom.history.types import HistoryScope
from mindroom.mcp.toolkit import MindRoomMCPToolkit
from mindroom.minimal_agent import MinimalAgent
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context
from mindroom.tool_system.worker_routing import agent_workspace_root_path
from tests.conftest import seed_session
from tests.test_agent_cli_authority import _runtime_context, _turn_context
from tests.test_dynamic_toolkits import _base_config_data, _validated_config
from tests.test_dynamic_toolkits import _runtime_paths as _toolkit_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

    from agno.agent import Agent

    from mindroom.delegation.state import SubagentPersona
    from mindroom.tool_system.runtime_context import ToolRuntimeContext


def _runtime(tmp_path: Path, **agent_fields: object) -> ToolRuntimeContext:
    runtime = _runtime_context(tmp_path)
    runtime.config.agents["helper"] = AgentConfig(
        display_name="Helper",
        role="Configured role",
        instructions=["Configured rule"],
        **agent_fields,
    )
    return runtime


def _run_context() -> RunContext:
    return RunContext(
        run_id="run-1",
        session_id="session-1",
        user_id="@alice:example.test",
        session_state={"not_a_var": "SUBSTITUTED"},
    )


async def _function_names(agent: Agent) -> list[str]:
    tools = await agent.aget_tools(RunOutput(run_id="run-1"), _run_context(), AgentSession(session_id="session-1"))
    names: list[str] = []
    for tool in tools:
        if isinstance(tool, Toolkit):
            names.extend(tool.get_async_functions())
        elif isinstance(tool, Function):
            names.append(tool.name)
    return sorted(names)


def _child(runtime: ToolRuntimeContext, tools: list[str] | None, prompt: str = "P", **options: Any) -> Agent:  # noqa: ANN401
    """Build the helper agent as an authored child that names ``tools``."""
    options = {"execution_identity": None, "persist_runtime_state": False, **options}
    return agents.create_agent(
        "helper",
        runtime.config,
        runtime.runtime_paths,
        persona=inline_persona(prompt, tools),
        **options,
    )


async def _prepare(runtime: ToolRuntimeContext, persona: SubagentPersona, prompt: str) -> ai._PreparedAgentRun:
    """Prepare one turn of the helper agent presenting ``persona``."""
    return await ai._prepare_agent_and_prompt(
        replace(_turn_context(), persona=persona),
        prompt=prompt,
        runtime_paths=runtime.runtime_paths,
        config=runtime.config,
        execution_identity=build_execution_identity_from_runtime_context(runtime),
    )


@pytest.mark.asyncio
async def test_persona_prompt_replaces_identity_and_keeps_runtime_sections(tmp_path: Path) -> None:
    """The authored prompt leads in place of the agent's identity, and runtime sections stay as for every agent."""
    runtime = _runtime(tmp_path, tools=["file"], memory_backend="none", context_files=["SOUL.md"])
    workspace = runtime.runtime_paths.storage_root / "agents" / "helper" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "SOUL.md").write_text("Configured personality")
    prompt = "Plain {not_a_var} text"
    configured = agents.create_agent("helper", runtime.config, runtime.runtime_paths, None, persist_runtime_state=False)
    assert "Configured personality" in str(configured.role)
    prepared = await _prepare(runtime, inline_persona(prompt, None), "task")

    message = await prepared.agent.aget_system_message(AgentSession(session_id="session-1"), _run_context(), [])

    assert message is not None
    content = str(message.content)
    assert content.startswith(f"{prompt}\n")
    for identity in ("Configured role", "Configured rule", "Configured personality", "Helper"):
        assert identity not in content
    assert "## Tool Execution Environment" in content


@pytest.mark.asyncio
async def test_persona_keeps_its_compacted_history_summary(tmp_path: Path) -> None:
    """A compacted session's summary replays as the first history message, outside an authored child's prompt."""
    runtime = _runtime(tmp_path, tools=["file"], memory_backend="none")
    identity = build_execution_identity_from_runtime_context(runtime)
    old_run = RunOutput(
        run_id="old-run",
        agent_id="helper",
        session_id="session-1",
        status=RunStatus.completed,
        messages=[Message(role="user", content="Earlier task"), Message(role="assistant", content="Earlier answer")],
    )
    storage = create_session_storage("helper", runtime.config, runtime.runtime_paths, identity)
    try:
        seed_session(storage, AgentSession(session_id="session-1", agent_id="helper", runs=[old_run]))
        archive_runs(
            storage,
            session_id="session-1",
            scope_key=HistoryScope(kind="agent", scope_id="helper").key,
            summary="EARLIER-WORK",
            summary_model="summary-model",
            runs=[old_run],
            event_ids={},
            seen_event_ids={},
        )
    finally:
        storage.close()

    with open_resolved_scope_session_context(
        agent_name="helper",
        scope=HistoryScope(kind="agent", scope_id="helper"),
        session_id="session-1",
        config=runtime.config,
        runtime_paths=runtime.runtime_paths,
        execution_identity=identity,
    ) as scope_context:
        assert scope_context is not None
        prepared = await ai._prepare_agent_and_prompt(
            replace(_turn_context(), persona=inline_persona("P", None)),
            prompt="task",
            runtime_paths=runtime.runtime_paths,
            config=runtime.config,
            execution_identity=identity,
            scope_context=scope_context,
        )
        message = await prepared.agent.aget_system_message(scope_context.session, _run_context(), [])
        run_messages = await agent_messages.aget_run_messages(
            prepared.agent,
            run_response=RunOutput(run_id="run-1", session_id="session-1"),
            run_context=_run_context(),
            input=[Message(role="user", content="task")],
            session=scope_context.session,
            add_history_to_context=prepared.agent.add_history_to_context,
        )

    assert message is not None
    assert str(message.content).startswith("P\n")
    assert "EARLIER-WORK" not in str(message.content)
    summaries = [message for message in run_messages.messages if is_compaction_summary(message)]
    assert len(summaries) == 1
    assert "EARLIER-WORK" in str(summaries[0].content)


def test_persona_tool_subset_hides_other_functions(tmp_path: Path) -> None:
    """Only the named toolkit's functions are offered; an unnamed caller toolkit offers none."""
    runtime = _runtime(tmp_path, tools=["file", "shell"])
    agent = _child(runtime, ["file"])
    toolkit_functions = {
        name
        for tool in agent.tools or []
        if isinstance(tool, Toolkit)
        for name in (*tool.functions, *tool.get_async_functions())
    }
    assert "read_file" in toolkit_functions
    assert "run_shell_command" not in toolkit_functions


@pytest.mark.asyncio
async def test_persona_function_entry_exposes_only_that_function(tmp_path: Path) -> None:
    """A toolkit.function entry keeps one function of its toolkit visible."""
    runtime = _runtime(tmp_path, tools=["file"])
    agent = _child(runtime, ["file.read_file"])
    assert await _function_names(agent) == ["read_file"]


def test_persona_refuses_a_function_its_caller_configuration_removes(tmp_path: Path) -> None:
    """A named function the caller's configuration filters out stops construction."""
    runtime = _runtime(tmp_path, tools=[{"file": {"include_tools": ["read_file"]}}])

    with pytest.raises(PersonaError, match=r"'file\.save_file' is not available to you"):
        _child(runtime, ["file.read_file", "file.save_file"])


@pytest.mark.parametrize(
    ("tools", "missing"),
    [(["file.read_file", "file.save_file"], r"'file\.save_file'"), (["file", "calculator"], "'calculator'")],
)
def test_persona_refuses_tools_its_caller_filter_hides(tmp_path: Path, tools: list[str], missing: str) -> None:
    """A named function or toolkit the caller's own function filter hides, such as during a call, stops construction."""
    runtime = _runtime(tmp_path, tools=["file", "calculator"])

    def caller_filter(function: Function) -> bool:
        return function.name != "save_file" and function.owning_toolkit != "calculator"

    with pytest.raises(PersonaError, match=f"{missing} is not available to you"):
        _child(runtime, tools, tool_function_filter=caller_filter)


def test_minimal_persona_cli_lists_only_its_named_toolkits(tmp_path: Path) -> None:
    """A minimal persona's mindroom-agent catalog offers neither unnamed deferred toolkits nor the deferred-tool manager."""
    runtime = _runtime(tmp_path, tools=["shell", {"file": {"defer": True}}])

    agent = _child(runtime, ["shell"], session_id="session-1", agent_mode="minimal")

    assert isinstance(agent, MinimalAgent)
    assert [deferred.name for deferred in agent.deferred_toolkits] == []
    built = {
        name
        for tool in agent.tools or []
        if isinstance(tool, Toolkit)
        for name in (*tool.functions, *tool.get_async_functions())
    }
    assert "read_file" not in built


def test_persona_tools_are_never_wire_deferred(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """On a model with native tool search, a persona's named toolkits are present from its first request."""
    runtime = _runtime(tmp_path, tools=[{"file": {"defer": True}}])
    runtime.config.models["default"] = ModelConfig(provider="anthropic", id="claude-sonnet-5-5")
    deferred: list[frozenset[str]] = []

    def record(_model: object, *, deferred_tool_names: frozenset[str]) -> None:
        deferred.append(deferred_tool_names)

    monkeypatch.setattr(agents, "install_claude_deferred_tool_search", record)

    agents.create_agent("helper", runtime.config, runtime.runtime_paths, execution_identity=None)
    _child(runtime, ["file"])

    # Only the configured agent defers its toolkit; the persona's build installs no tool search.
    [configured] = deferred
    assert "read_file" in configured


def test_minimal_persona_context_never_holds_its_configured_role(tmp_path: Path) -> None:
    """A minimal copy that keeps delegate sees itself listed as a target without its configured role."""
    runtime = _runtime(tmp_path, tools=["shell", "delegate"], delegate_to=["helper"])

    agent = _child(runtime, ["shell", "delegate"], agent_mode="minimal")

    assert isinstance(agent, MinimalAgent)
    documents = "\n".join(agent.context_documents.values())
    assert "Configured role" not in documents
    assert "Yourself, run as a fresh copy." in documents


def test_configured_minimal_agent_keeps_its_own_delegate_description(tmp_path: Path) -> None:
    """Only an authored copy hides its configured role; a configured agent's delegate guidance is unchanged."""
    runtime = _runtime(tmp_path, tools=["shell", "delegate"], delegate_to=["helper"])

    agent = agents.create_agent(
        "helper",
        runtime.config,
        runtime.runtime_paths,
        None,
        persist_runtime_state=False,
        agent_mode="minimal",
    )

    assert isinstance(agent, MinimalAgent)
    documents = "\n".join(agent.context_documents.values())
    assert "Yourself, run as a fresh copy." not in documents
    assert "- Configured role" in documents


def test_minimal_persona_inheriting_tools_lists_only_callable_toolkits(tmp_path: Path) -> None:
    """A persona that inherits its caller's tools lists only the toolkits that survived approval filtering."""
    runtime = _runtime(tmp_path, tools=["shell", {"file": {"include_tools": ["save_file"]}}])
    runtime.config.tool_approval.rules.append(ApprovalRuleConfig(match="save_file", action="require_approval"))

    agent = _child(runtime, None, agent_mode="minimal")

    assert isinstance(agent, MinimalAgent)
    [roster] = [line for line in agent.system_message.splitlines() if line.startswith("Toolkits callable")]
    assert "file" not in roster


@pytest.mark.asyncio
async def test_persona_with_tools_never_advertises_knowledge_search(tmp_path: Path) -> None:
    """A tool list hides knowledge search, so the prompt never points at it."""
    runtime = _runtime(tmp_path, tools=["file"])
    agent = _child(runtime, ["file"], knowledge=Knowledge(name="docs"))
    session = AgentSession(session_id="session-1")
    tools = await agent.aget_tools(RunOutput(run_id="run-1"), _run_context(), session)

    message = await agent.aget_system_message(session, _run_context(), [t for t in tools if isinstance(t, Function)])

    assert message is not None
    content = str(message.content)
    assert "search_knowledge_base" not in content


async def _system_prompt(agent: Agent) -> str:
    session = AgentSession(session_id="session-1")
    tools = await agent.aget_tools(RunOutput(run_id="run-1"), _run_context(), session)
    message = await agent.aget_system_message(session, _run_context(), [t for t in tools if isinstance(t, Function)])
    assert message is not None
    return str(message.content)


@pytest.mark.asyncio
async def test_persona_gets_no_skills_or_skill_authoring_note(tmp_path: Path) -> None:
    """A configured agent lists its workspace skills and the authoring note; an authored subagent gets neither."""
    runtime = _runtime(tmp_path, tools=["file"], memory_backend="file")
    identity = build_execution_identity_from_runtime_context(runtime)
    skill = agent_workspace_root_path(runtime.runtime_paths.storage_root, "helper") / "skills" / "scripted"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: scripted\ndescription: Scripted skill\n---\n\nSKILL BODY\n")
    options = {"persist_runtime_state": False}

    configured = await _system_prompt(
        agents.create_agent("helper", runtime.config, runtime.runtime_paths, identity, **options),
    )
    authored = await _system_prompt(_child(runtime, None, execution_identity=identity))

    for marker in ("skills/<skill-name>/", "scripted"):
        assert marker in configured
        assert marker not in authored


def test_persona_with_tools_gets_no_deferred_loading_guidance(tmp_path: Path) -> None:
    """An explicit tool list loads its toolkits up front, so the prompt never points at load_tool."""
    runtime = _runtime(tmp_path, tools=["file", {"calculator": {"defer": True}}])

    agent = _child(runtime, ["file"], session_id="session-1")

    instructions = "\n".join(str(item) for item in agent.instructions or [])
    assert "load_tool" not in instructions
    assert "calculator" not in instructions


def test_minimal_persona_roster_lists_the_toolkits_it_built(tmp_path: Path) -> None:
    """A minimal persona's bootstrap lists the toolkits it can call, deferred ones it named included."""
    runtime = _runtime(tmp_path, tools=["shell", "file", {"calculator": {"defer": True}}])

    agent = _child(runtime, ["shell", "calculator"], session_id="session-1", agent_mode="minimal")

    assert isinstance(agent, MinimalAgent)
    [roster] = [line for line in agent.system_message.splitlines() if line.startswith("Toolkits callable")]
    assert "calculator" in roster
    assert "file" not in roster


def test_persona_never_offers_the_deferred_tool_manager(tmp_path: Path) -> None:
    """An explicit tool list loads every toolkit it names, so the deferred-tool manager is not a caller tool to name."""
    runtime = _runtime(tmp_path, tools=[{"file": {"defer": True}}])

    names = caller_toolkit_names("helper", runtime.config, delegation_depth=0)

    assert "file" in names
    assert "dynamic_tools" not in names


def test_persona_refuses_an_mcp_function_a_collision_hides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A named MCP function that the session's collision projection hides stops construction."""
    raw = _base_config_data()
    raw["agents"]["code"]["tools"] = ["sleep", "mcp_demo"]  # type: ignore[index]
    raw["mcp_servers"] = {
        "demo": {
            "transport": "streamable-http",
            "url": "https://mcp.example.test/mcp",
            "auth": {
                "type": "oauth",
                "discovery": "manual",
                "authorization_url": "https://auth.example.test/authorize",
                "token_url": "https://auth.example.test/token",
            },
        },
    }
    config = _validated_config(tmp_path, raw)
    runtime_paths = _toolkit_runtime_paths(tmp_path)

    def build(tool_name: str, **_kwargs: object) -> Toolkit:
        if tool_name == "mcp_demo":
            return MindRoomMCPToolkit(
                server_id="demo",
                manager=None,
                catalog=None,
                server_config=config.mcp_servers["demo"],
                runtime_paths=runtime_paths,
                credentials_manager=get_runtime_credentials_manager(runtime_paths),
            )
        local = Toolkit(name="local", auto_register=False)
        local.functions["demo_list_tools"] = Function(name="demo_list_tools", entrypoint=lambda: "local")
        return local

    monkeypatch.setattr(agents, "build_agent_toolkit", build)

    with pytest.raises(PersonaError, match=r"'mcp_demo\.demo_list_tools' is not available to you"):
        agents.create_agent(
            "code",
            config,
            runtime_paths,
            None,
            persist_runtime_state=False,
            persona=inline_persona("P", ["mcp_demo.demo_list_tools", "sleep"]),
        )


def test_persona_refuses_a_toolkit_that_fails_to_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A named toolkit that cannot be built stops construction instead of being skipped."""
    runtime = _runtime(tmp_path, tools=["file", "calculator"])
    build = agents.build_agent_toolkit

    def failing_build(tool_name: str, **kwargs: object) -> Toolkit | None:
        if tool_name == "calculator":
            msg = "calculator is unavailable"
            raise ValueError(msg)
        return build(tool_name, **kwargs)

    monkeypatch.setattr(agents, "build_agent_toolkit", failing_build)

    with pytest.raises(PersonaError, match="'calculator' is not available to you"):
        _child(runtime, ["file", "calculator"])


@pytest.mark.asyncio
async def test_empty_persona_tools_build_toolless_agent(tmp_path: Path) -> None:
    """An explicit empty tool list leaves the child with no provider-visible functions."""
    runtime = _runtime(tmp_path, tools=["file", "shell"])
    agent = _child(runtime, [])
    assert await _function_names(agent) == []


def test_persona_disables_learning(tmp_path: Path) -> None:
    """A persona child never runs Agno learning for the caller."""
    runtime = _runtime(tmp_path, tools=["file"], learning=True)
    configured = agents.create_agent("helper", runtime.config, runtime.runtime_paths, None)
    persona = _child(runtime, None, persist_runtime_state=True)
    assert configured.learning
    assert not persona.learning


@pytest.mark.asyncio
async def test_persona_skips_memory_recall(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No recalled memories enter a persona child's prompt."""
    runtime = _runtime(tmp_path, tools=["file"])

    async def recall(*_args: object, **_kwargs: object) -> object:
        msg = "persona children must not recall memories"
        raise AssertionError(msg)

    monkeypatch.setattr(ai, "build_memory_prompt_parts", recall)
    prepared = await _prepare(runtime, inline_persona("P", None), "the task")
    assert "the task" in prepared.prompt_text


def test_minimal_persona_prompt_replaces_only_the_bootstrap_identity(tmp_path: Path) -> None:
    """A minimal persona's authored prompt replaces the bootstrap's identity line and keeps its runtime lines."""
    runtime = _runtime(tmp_path, tools=["shell"])
    agent = _child(runtime, None, prompt="Authored minimal prompt", agent_mode="minimal")
    assert isinstance(agent, MinimalAgent)
    assert agent.system_message == agent.bootstrap_message
    assert agent.system_message.startswith("Authored minimal prompt\n")
    assert "You are Helper" not in agent.system_message
    assert "mindroom-agent --help" in agent.system_message
    assert "Configured role" not in "\n".join(agent.context_documents.values())
    assert "Configured rule" not in "\n".join(agent.context_documents.values())


def test_agent_without_persona_is_unchanged(tmp_path: Path) -> None:
    """Configured agents keep their built prompt and their own minimal identity line."""
    runtime = _runtime(tmp_path, tools=["shell"])
    standard = agents.create_agent(
        "helper",
        runtime.config,
        runtime.runtime_paths,
        None,
        persist_runtime_state=False,
        persona=None,
    )
    minimal = agents.create_agent(
        "helper",
        runtime.config,
        runtime.runtime_paths,
        None,
        persist_runtime_state=False,
        agent_mode="minimal",
        persona=None,
    )
    assert standard.system_message is None
    assert standard.description is None
    assert standard.resolve_in_context
    assert isinstance(minimal, MinimalAgent)
    assert minimal.system_message.startswith("You are Helper (helper) in minimal mode.")


@pytest.mark.asyncio
async def test_persona_selects_preset_member_toolkits_by_name(tmp_path: Path) -> None:
    """A preset's member toolkit keeps its functions when a persona names it."""
    runtime = _runtime(tmp_path, tools=["openclaw_compat"])
    names = caller_toolkit_names("helper", runtime.config, delegation_depth=0)
    agent = _child(runtime, ["shell", "coding.read_file"])

    functions = await _function_names(agent)

    assert "openclaw_compat" not in names
    assert {"shell", "coding"} <= set(names)
    assert {"run_shell_command", "read_file"} <= set(functions)
    assert "write_file" not in functions


@pytest.mark.asyncio
async def test_persona_loads_named_member_of_deferred_preset(tmp_path: Path) -> None:
    """A deferred preset member that a persona names is present from the child's first request."""
    runtime = _runtime(tmp_path, tools=[{"openclaw_compat": {"defer": True}}])
    agent = _child(runtime, ["shell"])
    assert "run_shell_command" in await _function_names(agent)


@pytest.mark.asyncio
async def test_persona_may_name_the_matrix_room_runtime_tool(tmp_path: Path) -> None:
    """A Matrix caller's injected room tool can be named, and the child refuses to start where it is absent."""
    runtime = _runtime(tmp_path, tools=["file"])

    agent = _child(
        runtime,
        ["invite_router"],
        execution_identity=build_execution_identity_from_runtime_context(runtime),
    )

    assert "invite_router" in caller_toolkit_names("helper", runtime.config, delegation_depth=0)
    assert await _function_names(agent) == ["invite_router"]
    with pytest.raises(PersonaError, match="'invite_router' is not available to you"):
        _child(runtime, ["invite_router"])


@pytest.mark.asyncio
async def test_persona_delegate_tool_keeps_its_cap_without_runtime_context(tmp_path: Path) -> None:
    """An authored child's delegate tool enforces its own tools even without a Matrix tool context."""
    runtime = _runtime(tmp_path, tools=["file", "calculator"], delegate_to=["helper"])
    agent = _child(runtime, ["delegate", "file"])
    [delegate] = [
        tool for tool in agent.tools or [] if isinstance(tool, Toolkit) and "run_subagent" in tool.async_functions
    ]
    run_subagent = delegate.async_functions["run_subagent"].entrypoint

    unauthored = await run_subagent(task="Plain copy.")
    widened = await run_subagent(task="Add.", system_prompt="Q", tools=["calculator"])

    assert "pass system_prompt or profile so the copy stays within your tools" in unauthored
    assert widened == "Cannot delegate: unknown tool 'calculator'. Your tools: delegate, file."
