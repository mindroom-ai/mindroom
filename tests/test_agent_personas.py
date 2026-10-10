"""An authored persona replaces an agent's presentation while keeping its own tools and identity."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from agno.run import RunContext
from agno.run.agent import RunOutput
from agno.session import AgentSession
from agno.tools.function import Function
from agno.tools.toolkit import Toolkit

from mindroom import agents, ai
from mindroom.config.agent import AgentConfig
from mindroom.delegation.personas import PersonaError, caller_toolkit_names, inline_persona
from mindroom.minimal_agent import MinimalAgent
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context
from tests.test_agent_cli_authority import _runtime_context, _turn_context

if TYPE_CHECKING:
    from pathlib import Path

    from agno.agent import Agent

    from mindroom.tool_system.runtime_context import ToolRuntimeContext

_BASH_HINT = "MindRoom tools are callable from Bash through mindroom-agent; run mindroom-agent --help to list them."


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


@pytest.mark.asyncio
async def test_persona_system_message_is_verbatim(tmp_path: Path) -> None:
    """The model receives the authored prompt byte for byte, with no MindRoom framing or state substitution."""
    runtime = _runtime(tmp_path, tools=["file"], memory_backend="none")
    prompt = "Plain {not_a_var} text\n"
    prepared = await ai._prepare_agent_and_prompt(
        replace(_turn_context(), persona=inline_persona(prompt, None)),
        prompt="task",
        runtime_paths=runtime.runtime_paths,
        config=runtime.config,
        execution_identity=build_execution_identity_from_runtime_context(runtime),
    )

    message = await prepared.agent.aget_system_message(AgentSession(session_id="session-1"), _run_context(), [])

    assert message is not None
    assert message.content == prompt


def test_persona_tool_subset_hides_other_functions(tmp_path: Path) -> None:
    """Only the named toolkit is built; unnamed caller toolkits are never constructed."""
    runtime = _runtime(tmp_path, tools=["file", "shell"])
    agent = agents.create_agent(
        "helper",
        runtime.config,
        runtime.runtime_paths,
        None,
        persist_runtime_state=False,
        persona=inline_persona("P", ["file"]),
    )
    toolkit_functions = {name for tool in agent.tools or [] if isinstance(tool, Toolkit) for name in tool.functions}
    assert "read_file" in toolkit_functions
    assert "run_shell_command" not in toolkit_functions


@pytest.mark.asyncio
async def test_persona_function_entry_exposes_only_that_function(tmp_path: Path) -> None:
    """A toolkit.function entry keeps one function of its toolkit visible."""
    runtime = _runtime(tmp_path, tools=["file"])
    agent = agents.create_agent(
        "helper",
        runtime.config,
        runtime.runtime_paths,
        None,
        persist_runtime_state=False,
        persona=inline_persona("P", ["file.read_file"]),
    )
    assert await _function_names(agent) == ["read_file"]


def test_persona_refuses_a_function_its_caller_configuration_removes(tmp_path: Path) -> None:
    """A named function the caller's configuration filters out stops construction."""
    runtime = _runtime(tmp_path, tools=[{"file": {"include_tools": ["read_file"]}}])

    with pytest.raises(PersonaError, match=r"'file\.save_file' is not available to you"):
        agents.create_agent(
            "helper",
            runtime.config,
            runtime.runtime_paths,
            None,
            persist_runtime_state=False,
            persona=inline_persona("P", ["file.read_file", "file.save_file"]),
        )


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
        agents.create_agent(
            "helper",
            runtime.config,
            runtime.runtime_paths,
            None,
            persist_runtime_state=False,
            tool_function_filter=caller_filter,
            persona=inline_persona("P", tools),
        )


def test_minimal_persona_cli_lists_only_its_named_toolkits(tmp_path: Path) -> None:
    """A minimal persona's mindroom-agent catalog offers neither unnamed deferred toolkits nor the deferred-tool manager."""
    runtime = _runtime(tmp_path, tools=["shell", {"file": {"defer": True}}])

    agent = agents.create_agent(
        "helper",
        runtime.config,
        runtime.runtime_paths,
        None,
        session_id="session-1",
        persist_runtime_state=False,
        agent_mode="minimal",
        persona=inline_persona("P", ["shell"]),
    )

    assert isinstance(agent, MinimalAgent)
    assert [deferred.name for deferred in agent.deferred_toolkits] == []


def test_persona_never_offers_the_deferred_tool_manager(tmp_path: Path) -> None:
    """An explicit tool list loads every toolkit it names, so the deferred-tool manager is not a caller tool to name."""
    runtime = _runtime(tmp_path, tools=[{"file": {"defer": True}}])

    names = caller_toolkit_names("helper", runtime.config, delegation_depth=0)

    assert "file" in names
    assert "dynamic_tools" not in names


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
        agents.create_agent(
            "helper",
            runtime.config,
            runtime.runtime_paths,
            None,
            persist_runtime_state=False,
            persona=inline_persona("P", ["file", "calculator"]),
        )


@pytest.mark.asyncio
async def test_empty_persona_tools_build_toolless_agent(tmp_path: Path) -> None:
    """An explicit empty tool list leaves the child with no provider-visible functions."""
    runtime = _runtime(tmp_path, tools=["file", "shell"])
    agent = agents.create_agent(
        "helper",
        runtime.config,
        runtime.runtime_paths,
        None,
        persist_runtime_state=False,
        persona=inline_persona("P", []),
    )
    assert await _function_names(agent) == []


def test_persona_disables_learning(tmp_path: Path) -> None:
    """A persona child never runs Agno learning for the caller."""
    runtime = _runtime(tmp_path, tools=["file"], learning=True)
    configured = agents.create_agent("helper", runtime.config, runtime.runtime_paths, None)
    persona = agents.create_agent(
        "helper",
        runtime.config,
        runtime.runtime_paths,
        None,
        persona=inline_persona("P", None),
    )
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
    prepared = await ai._prepare_agent_and_prompt(
        replace(_turn_context(), persona=inline_persona("P", None)),
        prompt="the task",
        runtime_paths=runtime.runtime_paths,
        config=runtime.config,
        execution_identity=build_execution_identity_from_runtime_context(runtime),
    )
    assert "the task" in prepared.prompt_text


def test_minimal_persona_uses_authored_prompt_and_bash_hint(tmp_path: Path) -> None:
    """A minimal persona presents the authored prompt and tells the model where its tools are."""
    runtime = _runtime(tmp_path, tools=["shell"])
    agent = agents.create_agent(
        "helper",
        runtime.config,
        runtime.runtime_paths,
        None,
        persist_runtime_state=False,
        agent_mode="minimal",
        persona=inline_persona("Authored minimal prompt", None),
    )
    assert isinstance(agent, MinimalAgent)
    assert agent.bootstrap_message == "Authored minimal prompt"
    assert agent.system_message == "Authored minimal prompt"
    [bash] = agent.get_tools(RunOutput(run_id="run-1"), _run_context(), AgentSession(session_id="session-1"))
    assert bash.get_async_functions()["bash"].description.endswith(_BASH_HINT)


def test_agent_without_persona_is_unchanged(tmp_path: Path) -> None:
    """Configured agents keep their built prompt and the plain Bash description."""
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
    assert standard.resolve_in_context
    assert isinstance(minimal, MinimalAgent)
    assert minimal.system_message.startswith("You are Helper (helper) in minimal mode.")
    [bash] = minimal.get_tools(RunOutput(run_id="run-1"), _run_context(), AgentSession(session_id="session-1"))
    assert _BASH_HINT not in bash.get_async_functions()["bash"].description


@pytest.mark.asyncio
async def test_persona_selects_preset_member_toolkits_by_name(tmp_path: Path) -> None:
    """A preset's member toolkit keeps its functions when a persona names it."""
    runtime = _runtime(tmp_path, tools=["openclaw_compat"])
    names = caller_toolkit_names("helper", runtime.config, delegation_depth=0)
    agent = agents.create_agent(
        "helper",
        runtime.config,
        runtime.runtime_paths,
        None,
        persist_runtime_state=False,
        persona=inline_persona("P", ["shell", "coding.read_file"]),
    )

    functions = await _function_names(agent)

    assert "openclaw_compat" not in names
    assert {"shell", "coding"} <= set(names)
    assert {"run_shell_command", "read_file"} <= set(functions)
    assert "write_file" not in functions


@pytest.mark.asyncio
async def test_persona_loads_named_member_of_deferred_preset(tmp_path: Path) -> None:
    """A deferred preset member that a persona names is present from the child's first request."""
    runtime = _runtime(tmp_path, tools=[{"openclaw_compat": {"defer": True}}])
    agent = agents.create_agent(
        "helper",
        runtime.config,
        runtime.runtime_paths,
        None,
        persist_runtime_state=False,
        persona=inline_persona("P", ["shell"]),
    )
    assert "run_shell_command" in await _function_names(agent)


@pytest.mark.asyncio
async def test_persona_may_name_the_matrix_room_runtime_tool(tmp_path: Path) -> None:
    """A Matrix caller's injected room tool can be named, and the child refuses to start where it is absent."""
    runtime = _runtime(tmp_path, tools=["file"])
    persona = inline_persona("P", ["invite_router"])

    agent = agents.create_agent(
        "helper",
        runtime.config,
        runtime.runtime_paths,
        build_execution_identity_from_runtime_context(runtime),
        persist_runtime_state=False,
        persona=persona,
    )

    assert "invite_router" in caller_toolkit_names("helper", runtime.config, delegation_depth=0)
    assert await _function_names(agent) == ["invite_router"]
    with pytest.raises(PersonaError, match="'invite_router' is not available to you"):
        agents.create_agent(
            "helper",
            runtime.config,
            runtime.runtime_paths,
            None,
            persist_runtime_state=False,
            persona=persona,
        )


@pytest.mark.asyncio
async def test_persona_delegate_tool_keeps_its_cap_without_runtime_context(tmp_path: Path) -> None:
    """An authored child's delegate tool enforces its own tools even without a Matrix tool context."""
    runtime = _runtime(tmp_path, tools=["file", "calculator"], delegate_to=["helper"])
    agent = agents.create_agent(
        "helper",
        runtime.config,
        runtime.runtime_paths,
        None,
        persist_runtime_state=False,
        persona=inline_persona("P", ["delegate", "file"]),
    )
    [delegate] = [
        tool for tool in agent.tools or [] if isinstance(tool, Toolkit) and "run_subagent" in tool.async_functions
    ]
    run_subagent = delegate.async_functions["run_subagent"].entrypoint

    unauthored = await run_subagent(task="Plain copy.")
    widened = await run_subagent(task="Add.", system_prompt="Q", tools=["calculator"])

    assert "pass system_prompt or profile so the copy stays within your tools" in unauthored
    assert widened == "Cannot delegate: unknown tool 'calculator'. Your tools: delegate, file."
