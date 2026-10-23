"""An authored persona replaces an agent's presentation while keeping its own tools and identity."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest
from agno.tools.function import Function
from agno.tools.toolkit import Toolkit

from mindroom import agents
from mindroom.agent_cli.minimal_agent import MinimalTurn, minimal_turn
from mindroom.agent_loop.build import prompt_for_turn
from mindroom.agent_loop.tools import prepare_run_tools
from mindroom.ai import ai_response
from mindroom.config.agent import AgentConfig
from mindroom.config.approval import ApprovalRuleConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.custom_tools.delegate import DelegateTools
from mindroom.delegation.personas import PersonaError, caller_toolkit_names, inline_persona, persona_tool_policy
from mindroom.delegation.state import SubagentPersona
from mindroom.history.summary_message import is_compaction_summary
from mindroom.history.types import HistoryScope
from mindroom.knowledge.search import AgentKnowledge, KnowledgeSource, KnowledgeSourceDescription
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.mcp.toolkit import MindRoomMCPToolkit
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.session_storage.types import ConversationSession
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from mindroom.tool_system.worker_routing import agent_workspace_root_path
from tests.conftest import seed_session
from tests.fakes.scripted_model import ScriptedModel, answer
from tests.history_helpers import compact_runs
from tests.identity_helpers import persist_entity_accounts
from tests.scripted_agent_helpers import install_agent_model
from tests.test_agent_cli_authority import _runtime_context, _turn_context

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.agent_loop.build import AgentBuild
    from mindroom.constants import RuntimePaths
    from mindroom.knowledge.read_protocol import SearchHit
    from mindroom.llm.provider import ModelRequest
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


def _code_runtime_paths(tmp_path: Path, **env: str) -> RuntimePaths:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\n", encoding="utf-8")
    return resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "mindroom_data",
        process_env={"MATRIX_HOMESERVER": "http://localhost:8008", "MINDROOM_NAMESPACE": "", **env},
    )


def _code_config(tmp_path: Path, tools: list[str], **env: str) -> Config:
    """Validate a one-agent ``code`` config whose agent lists ``tools``."""
    raw: dict[str, Any] = {
        "defaults": {"tools": []},
        "agents": {"code": {"display_name": "Code", "role": "Write code", "tools": tools}},
        "models": {"default": {"provider": "openai", "id": "gpt-4o-mini"}},
    }
    runtime_paths = _code_runtime_paths(tmp_path, **env)
    config = Config.validate_with_runtime(raw, runtime_paths)
    persist_entity_accounts(config, runtime_paths)
    return config


def _egress_broker_env(*, broker_on: bool) -> dict[str, str]:
    return {"MINDROOM_EGRESS_BROKER_PORT": "8768"} if broker_on else {}


class _Index:
    """A published index the persona is offered but these tests never search."""

    async def asearch(self, query: str, *, limit: int) -> list[SearchHit]:
        raise AssertionError((query, limit))


def _knowledge() -> AgentKnowledge:
    description = KnowledgeSourceDescription(base_id="docs", description="The docs.")
    return AgentKnowledge(sources=(KnowledgeSource(description=description, index=_Index()),))


def _function_names(build: AgentBuild) -> list[str]:
    """Return the functions a run of ``build`` offers the model."""
    return sorted(prepare_run_tools(build.spec).functions)


def _system_prompt(build: AgentBuild) -> str:
    content = prompt_for_turn(build).content
    assert isinstance(content, str)
    return content


def _child(runtime: ToolRuntimeContext, tools: list[str] | None, prompt: str = "P", **options: Any) -> AgentBuild:  # noqa: ANN401
    """Build the helper agent as an authored child that names ``tools``."""
    options = {"execution_identity": None, "persist_runtime_state": False, **options}
    return agents.create_agent(
        "helper",
        runtime.config,
        runtime.runtime_paths,
        persona=inline_persona(prompt, tools, None, None),
        **options,
    )


async def _persona_request(
    runtime: ToolRuntimeContext,
    monkeypatch: pytest.MonkeyPatch,
    persona: SubagentPersona | None,
    prompt: str,
) -> ModelRequest:
    """Run one reply of the helper agent presenting ``persona`` and return the request the model received."""
    model = ScriptedModel([answer("Done.")])
    install_agent_model(monkeypatch, model)
    with tool_runtime_context(runtime):
        await ai_response(
            replace(_turn_context(), persona=persona),
            prompt=prompt,
            runtime_paths=runtime.runtime_paths,
            config=runtime.config,
            execution_identity=build_execution_identity_from_runtime_context(runtime),
        )
    [request] = model.requests
    return request


def _minimal(build: AgentBuild) -> MinimalTurn:
    """Render one minimal turn of ``build`` without memory or enrichment."""
    return minimal_turn(build, session_preamble="", transient_memory="", system_enrichment=(), transient_enrichment=())


def _bootstrap(build: AgentBuild) -> str:
    """Return the bootstrap system message of one minimal turn of ``build``."""
    return _minimal(build).system_message.get_content_string()


def _roster(system_message: str) -> str:
    """Return the bootstrap line naming the toolkits a minimal agent calls through mindroom-agent."""
    rosters = [line for line in system_message.splitlines() if line.startswith("Toolkits callable")]
    assert len(rosters) == 1, system_message
    return rosters[0]


@pytest.mark.asyncio
async def test_persona_prompt_replaces_identity_and_keeps_runtime_sections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The authored prompt leads in place of the agent's identity, and runtime sections stay as for every agent."""
    runtime = _runtime(tmp_path, tools=["file"], memory_backend="none", context_files=["SOUL.md"])
    workspace = runtime.runtime_paths.storage_root / "agents" / "helper" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "SOUL.md").write_text("Configured personality")
    prompt = "Plain {not_a_var} text"
    configured = agents.create_agent("helper", runtime.config, runtime.runtime_paths, None, persist_runtime_state=False)
    assert "Configured personality" in str(configured.prompt.role)

    request = await _persona_request(runtime, monkeypatch, inline_persona(prompt, None, None, None), "task")

    content = str(request.messages[0].content)
    assert content.startswith(f"{prompt}\n")
    for identity in ("Configured role", "Configured rule", "Configured personality", "Helper"):
        assert identity not in content
    assert "## Tool Execution Environment" in content


@pytest.mark.asyncio
async def test_persona_keeps_its_compacted_history_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A compacted session's summary replays as the first history message, outside an authored child's prompt."""
    runtime = _runtime(tmp_path, tools=["file"], memory_backend="none")
    identity = build_execution_identity_from_runtime_context(runtime)
    old_run = RunRecord(
        run_id="old-run",
        agent_id="helper",
        session_id="session-1",
        status=RunStatus.completed,
        messages=[Message(role="user", content="Earlier task"), Message(role="assistant", content="Earlier answer")],
    )
    storage = create_session_storage("helper", runtime.config, runtime.runtime_paths, identity)
    session = seed_session(
        storage,
        ConversationSession(session_id="session-1", kind="agent", owner_id="helper", runs=[old_run]),
    )
    compact_runs(storage, session, HistoryScope(kind="agent", scope_id="helper"), ["old-run"], "EARLIER-WORK")

    request = await _persona_request(runtime, monkeypatch, inline_persona("P", None, None, None), "task")

    system, *history = request.messages
    assert str(system.content).startswith("P\n")
    assert "EARLIER-WORK" not in str(system.content)
    summaries = [message for message in history if is_compaction_summary(message)]
    assert len(summaries) == 1
    assert "EARLIER-WORK" in str(summaries[0].content)
    assert history[0] is summaries[0]


def test_persona_with_tools_hides_generated_functions_from_the_agent() -> None:
    """An explicit tool list hides functions with no owning toolkit, which the configured agent offers."""
    generated = Function(name="generated")
    owned = Function(name="read_file")
    owned.owning_toolkit = "file"

    configured_filter, _ = persona_tool_policy(None, lambda: ["file"], None, frozenset())
    authored_filter, disabled = persona_tool_policy(("file",), lambda: ["file", "shell"], None, frozenset())

    assert configured_filter is None
    assert authored_filter is not None
    assert not authored_filter(generated)
    assert authored_filter(owned)
    assert disabled == {"shell", "dynamic_tools"}


def test_persona_tool_subset_hides_other_functions(tmp_path: Path) -> None:
    """Only the named toolkit's functions are offered; an unnamed caller toolkit offers none."""
    runtime = _runtime(tmp_path, tools=["file", "shell"])
    build = _child(runtime, ["file"])
    toolkit_functions = _function_names(build)
    assert "read_file" in toolkit_functions
    assert "run_shell_command" not in toolkit_functions


def test_persona_function_entry_exposes_only_that_function(tmp_path: Path) -> None:
    """A toolkit.function entry keeps one function of its toolkit visible."""
    runtime = _runtime(tmp_path, tools=["file"])
    build = _child(runtime, ["file.read_file"])
    assert _function_names(build) == ["read_file"]


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

    build = _child(runtime, ["shell"], agent_mode="minimal")

    assert build.surface.mode == "minimal"
    assert build.surface.deferred == ()
    # The catalog behind Bash holds every function the build owns, whichever ones its spec offers the model.
    assert "run_shell_command" in build.tool_owners
    assert "read_file" not in build.tool_owners


def test_persona_tools_are_never_wire_deferred(tmp_path: Path) -> None:
    """On a model with native tool search, a persona's named toolkits are present from its first request."""
    runtime = _runtime(tmp_path, tools=[{"file": {"defer": True}}])
    runtime.config.models["default"] = ModelConfig(
        provider="anthropic",
        id="claude-sonnet-5-5",
        extra_kwargs={"api_key": "test-key"},
    )

    configured = agents.create_agent("helper", runtime.config, runtime.runtime_paths, execution_identity=None)
    persona = _child(runtime, ["file"])

    # Only the configured agent defers its toolkit; the persona's requests defer nothing.
    assert "read_file" in configured.request_template.deferred_tool_names
    assert persona.request_template.deferred_tool_names == frozenset()
    assert "read_file" in _function_names(persona)


def test_minimal_persona_context_never_holds_its_configured_role(tmp_path: Path) -> None:
    """A minimal copy that keeps delegate sees itself listed as a target without its configured role."""
    runtime = _runtime(tmp_path, tools=["shell", "delegate"], delegate_to=["helper"])

    build = _child(runtime, ["shell", "delegate"], agent_mode="minimal")

    assert build.surface.mode == "minimal"
    documents = "\n".join(_minimal(build).context_documents.values())
    assert "Configured role" not in documents
    assert "Yourself, run as a fresh copy." in documents


def test_delegate_lists_its_caller_as_a_fresh_copy(tmp_path: Path) -> None:
    """Every caller's own delegate entry reads as a fresh copy and never repeats its configured role."""
    runtime = _runtime(tmp_path)

    toolkit = DelegateTools("helper", ["helper"], runtime.runtime_paths, runtime.config)

    assert "helper\n  - Yourself, run as a fresh copy." in str(toolkit.instructions)
    assert "Configured role" not in str(toolkit.instructions)


def test_minimal_persona_inheriting_tools_lists_only_callable_toolkits(tmp_path: Path) -> None:
    """A persona that inherits its caller's tools lists only the toolkits that survived approval filtering."""
    runtime = _runtime(tmp_path, tools=["shell", {"file": {"include_tools": ["save_file"]}}])
    runtime.config.tool_approval.rules.append(ApprovalRuleConfig(match="save_file", action="require_approval"))

    build = _child(runtime, None, agent_mode="minimal")

    assert "file" not in _roster(_bootstrap(build))


def test_persona_with_tools_never_advertises_knowledge_search(tmp_path: Path) -> None:
    """A tool list hides knowledge search, so the prompt never points at it."""
    runtime = _runtime(tmp_path, tools=["file"])
    build = _child(runtime, ["file"], knowledge=_knowledge())

    assert "search_knowledge_base" not in _function_names(build)
    assert "search_knowledge_base" not in _system_prompt(build)


def test_persona_with_tools_searches_the_knowledge_it_names(tmp_path: Path) -> None:
    """A persona listing tools keeps knowledge search when it names knowledge bases."""
    runtime = _runtime(tmp_path, tools=["file"])
    build = agents.create_agent(
        "helper",
        runtime.config,
        runtime.runtime_paths,
        None,
        persist_runtime_state=False,
        persona=inline_persona("P", ["file"], None, ["docs"]),
        knowledge=_knowledge(),
    )

    assert "search_knowledge_base" in _function_names(build)


def _write_skills(runtime: ToolRuntimeContext, *names: str) -> None:
    for name in names:
        skill = agent_workspace_root_path(runtime.runtime_paths.storage_root, "helper") / "skills" / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"---\nname: {name}\ndescription: The {name} skill\n---\n\n{name} body\n")


def test_persona_inherits_its_callers_skills_without_the_authoring_note(tmp_path: Path) -> None:
    """A persona without skills lists its caller's skills, but only a configured agent gets the authoring note."""
    runtime = _runtime(tmp_path, tools=["file"], memory_backend="file")
    identity = build_execution_identity_from_runtime_context(runtime)
    _write_skills(runtime, "scripted")
    options = {"persist_runtime_state": False}

    configured = _system_prompt(
        agents.create_agent("helper", runtime.config, runtime.runtime_paths, identity, **options),
    )
    authored = _system_prompt(_child(runtime, None, execution_identity=identity))

    assert "skills/<skill-name>/" in configured
    assert "skills/<skill-name>/" not in authored
    assert "scripted" in configured
    assert "scripted" in authored


def test_only_the_configured_agent_learns_which_workspace_skills_did_not_load(tmp_path: Path) -> None:
    """A workspace skill that does not load is named to the agent that owns it, not to a persona borrowing its skills."""
    runtime = _runtime(tmp_path, tools=["file"], memory_backend="file")
    identity = build_execution_identity_from_runtime_context(runtime)
    _write_skills(runtime, "scripted")
    broken = agent_workspace_root_path(runtime.runtime_paths.storage_root, "helper") / "skills" / "broken"
    broken.mkdir()
    (broken / "SKILL.md").write_text("---\nname: broken\ndescription: Use when: it breaks\n---\n\nbody\n")
    options = {"persist_runtime_state": False}

    configured = _system_prompt(
        agents.create_agent("helper", runtime.config, runtime.runtime_paths, identity, **options),
    )
    authored = _system_prompt(_child(runtime, None, execution_identity=identity))

    assert "skills/broken/SKILL.md" in configured
    assert "skills/broken/SKILL.md" not in authored
    assert "scripted" in authored


@pytest.mark.parametrize(
    ("skills", "listed"),
    [(["scripted"], {"scripted"}), ([], set()), (["scripted", "removed"], {"scripted"})],
)
def test_persona_lists_only_the_skills_it_names(tmp_path: Path, skills: list[str], listed: set[str]) -> None:
    """A skill list narrows the caller's skills, an empty list leaves none, and a skill the caller lost is skipped."""
    runtime = _runtime(tmp_path, tools=["file"], memory_backend="file")
    identity = build_execution_identity_from_runtime_context(runtime)
    _write_skills(runtime, "scripted", "other")

    build = agents.create_agent(
        "helper",
        runtime.config,
        runtime.runtime_paths,
        identity,
        persist_runtime_state=False,
        persona=inline_persona("P", None, skills, None),
    )
    prompt = _system_prompt(build)

    skills_block = build.prompt.skills_block or ""
    assert {name for name in ("scripted", "other") if f"<name>{name}</name>" in skills_block} == listed
    assert {name for name in ("scripted", "other") if f"The {name} skill" in prompt} == listed


def test_persona_with_tools_and_no_skill_list_builds_without_skills(tmp_path: Path) -> None:
    """A persona listing tools but no skills gets none however it was built, so a narrow child stays narrow."""
    runtime = _runtime(tmp_path, tools=["file"], memory_backend="file")
    identity = build_execution_identity_from_runtime_context(runtime)
    _write_skills(runtime, "scripted")
    persona = SubagentPersona(source_kind="inline", source_name="", system_prompt="P", tools=("file",))

    build = agents.create_agent(
        "helper",
        runtime.config,
        runtime.runtime_paths,
        identity,
        persist_runtime_state=False,
        persona=persona,
    )

    assert build.prompt.skills_block is None
    assert not {"get_skill_instructions", "get_skill_reference", "get_skill_script"} & set(_function_names(build))


def test_persona_whose_named_skills_are_gone_gets_no_skill_functions(tmp_path: Path) -> None:
    """A persona keeping only skills its caller has since lost is offered no skill functions, even beside configured skills."""
    runtime = _runtime(tmp_path, tools=["file"], memory_backend="file", skills=["configured"])
    identity = build_execution_identity_from_runtime_context(runtime)

    build = agents.create_agent(
        "helper",
        runtime.config,
        runtime.runtime_paths,
        identity,
        persist_runtime_state=False,
        persona=inline_persona("P", ["file"], ["removed"], None),
    )

    assert build.prompt.skills_block is None
    assert not {"get_skill_instructions", "get_skill_reference", "get_skill_script"} & set(_function_names(build))


def test_persona_with_tools_keeps_its_skill_functions(tmp_path: Path) -> None:
    """An explicit tool list hides generated functions but still offers the functions that load its skills."""
    runtime = _runtime(tmp_path, tools=["file"], memory_backend="file")
    identity = build_execution_identity_from_runtime_context(runtime)
    _write_skills(runtime, "scripted")

    names = _function_names(
        agents.create_agent(
            "helper",
            runtime.config,
            runtime.runtime_paths,
            identity,
            persist_runtime_state=False,
            persona=inline_persona("P", ["file"], ["scripted"], None),
        ),
    )

    assert "get_skill_instructions" in names
    assert "read_file" in names


def test_minimal_persona_reads_its_skills_as_context_documents(tmp_path: Path) -> None:
    """A minimal persona reads the skills it names on demand, like any minimal agent."""
    runtime = _runtime(tmp_path, tools=["shell"], memory_backend="file")
    identity = build_execution_identity_from_runtime_context(runtime)
    _write_skills(runtime, "scripted", "other")

    build = agents.create_agent(
        "helper",
        runtime.config,
        runtime.runtime_paths,
        identity,
        persist_runtime_state=False,
        agent_mode="minimal",
        persona=inline_persona("P", ["shell"], ["scripted"], None),
    )

    assert build.surface.mode == "minimal"
    documents = list(_minimal(build).context_documents.values())
    assert "scripted\nscripted body" in documents
    assert not any(document.startswith("other\n") for document in documents)


def test_persona_with_tools_gets_no_tool_search_guidance(tmp_path: Path) -> None:
    """An explicit tool list loads its toolkits up front, so the prompt never points at tool_search."""
    runtime = _runtime(tmp_path, tools=["file", {"calculator": {"defer": True}}])

    build = _child(runtime, ["file"])

    instructions = "\n".join((*build.prompt.instructions, *build.prompt.tool_instructions))
    assert "tool_search" not in instructions
    assert "calculator" not in instructions


def test_minimal_persona_roster_lists_the_toolkits_it_built(tmp_path: Path) -> None:
    """A minimal persona's bootstrap lists the toolkits it can call, deferred ones it named included."""
    runtime = _runtime(tmp_path, tools=["shell", "file", {"calculator": {"defer": True}}])

    build = _child(runtime, ["shell", "calculator"], agent_mode="minimal")

    roster = _roster(_bootstrap(build))
    assert "calculator" in roster
    assert "file" not in roster


def test_persona_never_offers_the_deferred_tool_manager(tmp_path: Path) -> None:
    """An explicit tool list loads every toolkit it names, so the deferred-tool manager is not a caller tool to name."""
    runtime = _runtime(tmp_path, tools=[{"file": {"defer": True}}])

    names = caller_toolkit_names("helper", runtime.config, delegation_depth=0)

    assert "file" in names
    assert "dynamic_tools" not in names


@pytest.mark.parametrize("broker_on", [False, True])
def test_caller_toolkits_include_egress_credentials_only_while_the_broker_is_on(
    tmp_path: Path,
    *,
    broker_on: bool,
) -> None:
    """With the broker off a subagent cannot name the egress credentials tool its caller lists."""
    config = _code_config(tmp_path, ["sleep", "egress_credentials"], **_egress_broker_env(broker_on=broker_on))

    names = caller_toolkit_names("code", config, delegation_depth=0)

    assert "sleep" in names
    assert ("egress_credentials" in names) is broker_on


def test_persona_refuses_an_mcp_function_a_collision_hides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A named MCP function that the session's collision projection hides stops construction."""
    raw: dict[str, Any] = {
        "defaults": {"tools": []},
        "agents": {"code": {"display_name": "Code", "role": "Write code", "tools": ["sleep", "mcp_demo"]}},
        "models": {"default": {"provider": "openai", "id": "gpt-4o-mini"}},
    }
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
    runtime_paths = _code_runtime_paths(tmp_path)
    config = Config.validate_with_runtime(raw, runtime_paths)
    persist_entity_accounts(config, runtime_paths)

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
            persona=inline_persona("P", ["mcp_demo.demo_list_tools", "sleep"], None, None),
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


def test_empty_persona_tools_build_toolless_agent(tmp_path: Path) -> None:
    """An explicit empty tool list leaves the child with no provider-visible functions."""
    runtime = _runtime(tmp_path, tools=["file", "shell"])
    build = _child(runtime, [])
    assert _function_names(build) == []


@pytest.mark.asyncio
async def test_persona_skips_memory_recall(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No recalled memories enter a persona child's prompt."""
    runtime = _runtime(tmp_path, tools=["file"])

    async def recall(*_args: object, **_kwargs: object) -> object:
        msg = "persona children must not recall memories"
        raise AssertionError(msg)

    monkeypatch.setattr("mindroom.ai.build_memory_prompt_parts", recall)
    request = await _persona_request(runtime, monkeypatch, inline_persona("P", None, None, None), "the task")
    assert "the task" in str(request.messages[-1].content)


def test_minimal_persona_prompt_replaces_only_the_bootstrap_identity(tmp_path: Path) -> None:
    """A minimal persona's authored prompt replaces the bootstrap's identity line and keeps its runtime lines."""
    runtime = _runtime(tmp_path, tools=["shell"])
    prompt = "Authored minimal prompt"

    build = _child(runtime, None, prompt=prompt, agent_mode="minimal")

    bootstrap = _bootstrap(build)
    assert bootstrap.startswith(f"{prompt}\n")
    assert "You are Helper" not in bootstrap
    assert "Bash starts in your workspace." in bootstrap
    assert "mindroom-agent --help" in bootstrap
    documents = "\n".join(_minimal(build).context_documents.values())
    assert "Configured role" not in documents
    assert "Configured rule" not in documents


def test_agent_without_persona_is_unchanged(tmp_path: Path) -> None:
    """Configured agents keep their built prompt, in either mode, with no authored identity in front of it."""
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
    assert standard.spec.system_message is None
    assert standard.prompt.description is None
    assert "Configured role" in (standard.prompt.role or "")
    assert minimal.surface.mode == "minimal"
    assert minimal.prompt.description is None
    assert _bootstrap(minimal).startswith("You are Helper (helper) in minimal mode.")


def test_persona_selects_preset_member_toolkits_by_name(tmp_path: Path) -> None:
    """A preset's member toolkit keeps its functions when a persona names it."""
    runtime = _runtime(tmp_path, tools=["openclaw_compat"])
    names = caller_toolkit_names("helper", runtime.config, delegation_depth=0)
    build = _child(runtime, ["shell", "coding.read_file"])

    functions = _function_names(build)

    assert "openclaw_compat" not in names
    assert {"shell", "coding"} <= set(names)
    assert {"run_shell_command", "read_file"} <= set(functions)
    assert "write_file" not in functions


def test_persona_loads_named_member_of_deferred_preset(tmp_path: Path) -> None:
    """A deferred preset member that a persona names is present from the child's first request."""
    runtime = _runtime(tmp_path, tools=[{"openclaw_compat": {"defer": True}}])
    build = _child(runtime, ["shell"])
    assert "run_shell_command" in _function_names(build)


def test_persona_may_name_the_matrix_room_runtime_tool(tmp_path: Path) -> None:
    """A Matrix caller's injected room tool can be named, and the child refuses to start where it is absent."""
    runtime = _runtime(tmp_path, tools=["file"])

    build = _child(
        runtime,
        ["invite_router"],
        execution_identity=build_execution_identity_from_runtime_context(runtime),
    )

    assert "invite_router" in caller_toolkit_names("helper", runtime.config, delegation_depth=0)
    assert _function_names(build) == ["invite_router"]
    with pytest.raises(PersonaError, match="'invite_router' is not available to you"):
        _child(runtime, ["invite_router"])


@pytest.mark.asyncio
async def test_persona_delegate_tool_keeps_its_cap_without_runtime_context(tmp_path: Path) -> None:
    """An authored child's delegate tool enforces its own tools even without a Matrix tool context."""
    runtime = _runtime(tmp_path, tools=["file", "calculator"], delegate_to=["helper"])
    build = _child(runtime, ["delegate", "file"])
    run_subagent = prepare_run_tools(build.spec).functions["run_subagent"].entrypoint

    unauthored = await run_subagent(task="Plain copy.")
    widened = await run_subagent(task="Add.", system_prompt="Q", tools=["calculator"])

    assert "pass system_prompt or profile so the copy stays within your tools" in unauthored
    assert widened == "Cannot delegate: unknown tool 'calculator'. Your tools: delegate, file."
