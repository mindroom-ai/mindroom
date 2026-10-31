"""``create_agent`` builds the owned agent: its spec, prompt, request template, tool owners, and surface."""

from __future__ import annotations

import json
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from agno.tools import Toolkit
from agno.tools.function import Function
from agno.tools.reasoning import ReasoningTools
from structlog.testing import capture_logs

import mindroom.agents as agents_module
from mindroom.agent_loop.build import (
    AgentBuild,
    AgentSurface,
    appended_context,
    prompt_for_turn,
    tool_owners_by_agent,
)
from mindroom.agent_loop.prompt import render_system_messages
from mindroom.agent_loop.tools import PreparedTools
from mindroom.agents import create_agent
from mindroom.config.approval import ApprovalRuleConfig, ToolApprovalConfig
from mindroom.config.main import Config
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.delegation.state import SubagentPersona
from mindroom.knowledge.search import AgentKnowledge, KnowledgeSource, KnowledgeSourceDescription
from mindroom.llm.provider import ModelRequest
from mindroom.matrix_rtc.call_tools import _function_available_during_call
from mindroom.mcp.toolkit import MindRoomMCPToolkit
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.skills import SKILL_FUNCTION_NAMES
from mindroom.tool_system.tool_function import ToolGate
from mindroom.tool_system.worker_routing import agent_workspace_root_path, build_tool_execution_identity
from tests.conftest import bind_runtime_paths, publish_test_plugins, test_runtime_paths
from tests.turn_model_helpers import turn_model

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.knowledge.read_protocol import SearchHit
    from mindroom.tool_system.tool_function import ToolFunction

_CLAUDE = {"provider": "anthropic", "id": "claude-sonnet-5-5", "extra_kwargs": {"api_key": "test-key"}}
_CALCULATOR_FUNCTIONS = {
    "add",
    "subtract",
    "multiply",
    "divide",
    "exponentiate",
    "factorial",
    "is_prime",
    "square_root",
}


class _Index:
    """A published index the agent is offered but these tests never search."""

    async def asearch(self, query: str, *, limit: int) -> list[SearchHit]:
        raise AssertionError((query, limit))


def _knowledge() -> AgentKnowledge:
    description = KnowledgeSourceDescription(base_id="handbook", description="The team handbook.")
    return AgentKnowledge(sources=(KnowledgeSource(description=description, index=_Index()),))


def _config(tmp_path: Path, **agent: object) -> tuple[Config, RuntimePaths]:
    authored = {
        "defaults": {"tools": []},
        "agents": {
            "assistant": {
                "display_name": "Assistant",
                "role": "Answer questions about the team handbook.",
                "tools": ["calculator"],
                "context_files": ["SOUL.md"],
                "memory_backend": "file",
                **agent,
            },
        },
        "memory": {"backend": "none"},
        "models": {"default": _CLAUDE},
        "timezone": "UTC",
    }
    runtime_paths = test_runtime_paths(tmp_path)
    workspace = agent_workspace_root_path(runtime_paths.storage_root, "assistant")
    (workspace / "skills" / "expense-report").mkdir(parents=True)
    (workspace / "skills" / "expense-report" / "SKILL.md").write_text(
        "---\nname: expense-report\ndescription: File an expense report.\n---\n\nCollect the receipts.\n",
        encoding="utf-8",
    )
    (workspace / "SOUL.md").write_text("You are calm and precise.\n", encoding="utf-8")
    return bind_runtime_paths(Config.model_validate(authored), runtime_paths), runtime_paths


def _build(config: Config, runtime_paths: RuntimePaths, **kwargs: Any) -> AgentBuild:  # noqa: ANN401
    """Build the agent for a Matrix requester outside any room, which resolves its workspace."""
    identity = build_tool_execution_identity(
        channel="matrix",
        agent_name="assistant",
        runtime_paths=runtime_paths,
        requester_id="@user:localhost",
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    return create_agent(
        "assistant",
        config,
        runtime_paths,
        identity,
        **(
            {
                "knowledge": None,
                "model": turn_model(config, runtime_paths, "assistant"),
                "plugins": config_plugins(config, runtime_paths),
            }
            | kwargs
        ),
    )


def _owners(build: AgentBuild) -> dict[str, str]:
    """Return the authored toolkit approvals name each function the build offers by, by function name."""
    return {name: toolkit for (_agent, name), toolkit in tool_owners_by_agent(build).items()}


def _offered(build: AgentBuild) -> dict[str, ToolFunction]:
    return dict(build.spec.functions)


def test_create_agent_builds_the_spec_prompt_and_owners(tmp_path: Path) -> None:
    """The build offers the configured toolkits, then the knowledge search, then skills, and renders their prompt."""
    config, runtime_paths = _config(tmp_path)
    knowledge = _knowledge()

    build = _build(config, runtime_paths, knowledge=knowledge)

    assert (build.spec.id, build.spec.name, build.spec.model.identity.id) == (
        "assistant",
        "Assistant",
        "claude-sonnet-5-5",
    )
    assert build.spec.system_messages == ()
    assert [toolkit.name for toolkit in build.spec.toolkits] == ["calculator", "search_knowledge_base", "skills"]
    assert set(_offered(build)) == _CALCULATOR_FUNCTIONS | {"search_knowledge_base"} | SKILL_FUNCTION_NAMES
    assert build.spec.max_tool_calls_per_turn == config.resolve_scope("assistant").max_tool_calls_per_turn
    assert _owners(build) == dict.fromkeys(_CALCULATOR_FUNCTIONS, "calculator")
    workspace = agent_workspace_root_path(runtime_paths.storage_root, "assistant")
    assert f"### {workspace / 'SOUL.md'}\nYou are calm and precise." in build.prompt.role
    assert build.prompt.role.endswith("Answer questions about the team handbook.")
    assert build.prompt.knowledge_block == knowledge.prompt_block()
    assert build.prompt.skills_block is not None
    assert "<name>expense-report</name>" in build.prompt.skills_block
    assert build.prompt.tool_instructions == ()
    assert build.prompt.turn_context is not None
    assert build.prompt.turn_context.startswith("<mindroom_session_context>")
    assert build.request_template == ModelRequest(messages=())
    assert build.history_settings == config.resolve_scope("assistant").history_settings
    # Large CLI results and their media are saved in the agent's own workspace.
    assert build.surface.output_file_policy is not None
    assert build.surface.output_file_policy.workspace_root == workspace
    assert replace(build.surface, output_file_policy=None) == AgentSurface(
        mode="standard",
        cli_shell=False,
        context_documents=("You are calm and precise.",),
        context_files=("SOUL.md",),
        knowledge_sources=tuple(source.description for source in knowledge.sources),
        deferred=(),
        output_file_policy=None,
    )


def test_a_build_without_runtime_capabilities_has_no_tools_or_context(tmp_path: Path) -> None:
    """A restricted in-process run keeps the role and instructions but no tools, skills, or knowledge."""
    config, runtime_paths = _config(tmp_path)

    build = _build(
        config,
        runtime_paths,
        knowledge=_knowledge(),
        disable_runtime_capabilities=True,
    )

    assert build.spec.toolkits == ()
    assert (build.prompt.skills_block, build.prompt.knowledge_block) == (None, None)
    assert "SOUL.md" not in build.prompt.role
    assert (build.surface.context_documents, build.surface.knowledge_sources) == ((), ())


def test_approval_rules_gate_configured_functions_and_hide_generated_ones(tmp_path: Path) -> None:
    """A rule that requires approval pauses configured functions but hides the knowledge and skill functions."""
    config, runtime_paths = _config(tmp_path)
    config.tool_approval = ToolApprovalConfig(default="require_approval")

    build = _build(
        config,
        runtime_paths,
        knowledge=_knowledge(),
        supports_native_tool_approval=True,
    )

    offered = _offered(build)
    assert set(offered) == _CALCULATOR_FUNCTIONS
    assert all(function.gate is ToolGate.POLICY for function in offered.values())
    assert (build.prompt.knowledge_block, build.prompt.skills_block) == (None, None)
    assert build.surface.knowledge_sources == ()
    assert config.get_prompt("SKILLS_TOOL_USAGE_PROMPT") not in build.prompt.instructions


def test_a_rule_hiding_one_skill_function_names_it_in_the_prompt(tmp_path: Path) -> None:
    """When approval hides some skill functions, the rest are offered and the prompt names the hidden ones."""
    config, runtime_paths = _config(tmp_path)
    config.tool_approval = ToolApprovalConfig(
        rules=[ApprovalRuleConfig(match="get_skill_script", action="require_approval")],
    )

    build = _build(config, runtime_paths, supports_native_tool_approval=True)

    assert set(_offered(build)) == _CALCULATOR_FUNCTIONS | {"get_skill_instructions", "get_skill_reference"}
    assert build.prompt.skills_block is not None
    assert "These skill functions are not available to you: get_skill_script." in build.prompt.instructions


def _write_plugin_skill(plugin_root: Path, name: str) -> None:
    (plugin_root / "skills" / name).mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": plugin_root.name, "tools_module": None, "skills": ["skills"]}),
        encoding="utf-8",
    )
    (plugin_root / "skills" / name / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: A skill {plugin_root.name} provides.\n---\n\nFollow it.\n",
        encoding="utf-8",
    )


def test_a_build_offers_the_skills_of_the_plugins_its_config_loads(tmp_path: Path) -> None:
    """The first build after a start or a config change offers the configured plugins' skills, and no earlier set's."""
    config, runtime_paths = _config(tmp_path, skills=["fresh-skill", "stale-skill"])
    _write_plugin_skill(tmp_path / "plugins" / "fresh", "fresh-skill")
    stale = tmp_path / "stale"
    _write_plugin_skill(stale, "stale-skill")
    # An earlier config loaded another plugin.
    earlier = {**config.authored_model_dump(), "plugins": [str(stale)]}
    publish_test_plugins(bind_runtime_paths(Config.model_validate(earlier), runtime_paths), runtime_paths)
    authored = {**config.authored_model_dump(), "plugins": ["./plugins/fresh"]}
    config = bind_runtime_paths(Config.model_validate(authored), runtime_paths)
    publish_test_plugins(config, runtime_paths)

    build = _build(config, runtime_paths)

    assert build.prompt.skills_block is not None
    assert "<name>fresh-skill</name>" in build.prompt.skills_block
    assert "<name>stale-skill</name>" not in build.prompt.skills_block


def test_prompt_for_turn_appends_the_turns_context_blocks(tmp_path: Path) -> None:
    """Each turn block follows the context so far after one blank line; empty blocks and trailing whitespace drop out."""
    config, runtime_paths = _config(tmp_path)
    build = _build(config, runtime_paths)
    base = build.prompt.turn_context
    assert base is not None

    rendered = [message.content for message in render_system_messages(build.prompt)]
    assert [message.content for message in prompt_for_turn(build)] == rendered
    stable, turn = prompt_for_turn(build, "memory\n", "", "<ctx>")
    assert stable.content == rendered[0]
    assert turn.content == f"{base.strip()}\n\nmemory\n\n<ctx>"


@pytest.mark.parametrize(
    ("existing", "block", "expected"),
    [
        ("context \n", "block", "context\n\nblock"),
        ("context", "", "context"),
        ("  ", "block", "block"),
        (None, "block", "block"),
        (None, "", None),
    ],
)
def test_appended_context_puts_a_block_after_the_stripped_context(
    existing: str | None,
    block: str,
    expected: str | None,
) -> None:
    """A block follows the context after one blank line; it stands alone after no context, and an empty one adds none."""
    assert appended_context(existing, block) == expected


@pytest.mark.parametrize("agent_mode", ["standard", "minimal"])
def test_deferred_toolkits_fill_the_request_template_and_the_surface(tmp_path: Path, agent_mode: str) -> None:
    """A natively deferred toolkit is offered but named deferred; minimal mode materializes it through the CLI."""
    config, runtime_paths = _config(tmp_path, tools=["calculator", {"name": "wikipedia", "defer": True}])

    build = _build(config, runtime_paths, agent_mode=agent_mode)

    if agent_mode == "standard":
        assert "search_wikipedia" in _offered(build)
        assert build.request_template.deferred_tools.names == frozenset({"search_wikipedia"})
        assert build.surface.deferred == ()
        assert any("Deferred Tool Discovery" in instruction for instruction in build.prompt.instructions)
    else:
        assert "search_wikipedia" not in _offered(build)
        assert build.request_template.deferred_tools.names == frozenset()
        assert [deferred.name for deferred in build.surface.deferred] == ["wikipedia"]
        assert build.surface.mode == "minimal"


def ping(text: str) -> str:
    """Echo the text.

    Args:
        text: What to echo.

    """
    return text


def search_knowledge_base(query: str) -> str:
    """Search the configured index.

    Args:
        query: What to look for.

    """
    return query


def test_the_toolkit_whose_function_a_run_offers_owns_its_name_for_approvals_and_jobs(tmp_path: Path) -> None:
    """Two toolkits share a function name: the run offers the first one's, so approvals and tool jobs name it."""
    config, runtime_paths = _config(tmp_path, tools=["calculator", "wikipedia"])
    config.background_tool_jobs.enabled = True
    built: dict[str, Toolkit] = {}

    def build_toolkit(tool_name: str, **_kwargs: object) -> Toolkit:
        built[tool_name] = Toolkit(name=tool_name, tools=[ping])
        return built[tool_name]

    with patch.object(agents_module, "build_agent_toolkit", side_effect=build_toolkit):
        build = _build(config, runtime_paths)

    calculator, wikipedia = build.spec.toolkits[:2]
    assert (calculator.name, wikipedia.name) == ("calculator", "wikipedia")
    [offered] = calculator.functions
    assert _offered(build)["ping"] is offered
    assert _owners(build) == {"ping": "calculator"}
    assert build.tool_jobs is not None
    job_tool = build.tool_jobs.tools["ping"]
    assert (job_tool.function, job_tool.toolkit_name, job_tool.toolkit) == (offered, "calculator", built["calculator"])


def test_a_shadowed_function_is_reported_once_per_build_however_often_its_runs_read_the_tools(tmp_path: Path) -> None:
    """The build resolves the names its toolkits share once; the runs, resumes, and history budget only read the result."""
    config, runtime_paths = _config(tmp_path, tools=["calculator", "wikipedia"])

    def build_toolkit(tool_name: str, **_kwargs: object) -> Toolkit:
        return Toolkit(name=tool_name, tools=[ping])

    with patch.object(agents_module, "build_agent_toolkit", side_effect=build_toolkit), capture_logs() as logs:
        build = _build(config, runtime_paths)
        for _run in range(3):
            PreparedTools(functions=build.spec.functions).specs()

    calculator, wikipedia = build.spec.toolkits[:2]
    assert ([f.name for f in calculator.functions], wikipedia.functions) == (["ping"], ())
    assert [(log["toolkit"], log["tool_name"]) for log in logs if log["event"] == "duplicate_tool_name"] == [
        ("wikipedia", "ping"),
    ]


def test_a_configured_toolkit_the_run_offers_nothing_of_is_reported(tmp_path: Path) -> None:
    """A toolkit whose every function an earlier one shadows is reported; one the run offers any function of is not."""
    config, runtime_paths = _config(tmp_path, tools=["calculator", "wikipedia", "arxiv"])
    surfaces = {"calculator": [ping], "wikipedia": [ping], "arxiv": [ping, search_knowledge_base]}

    def build_toolkit(tool_name: str, **_kwargs: object) -> Toolkit:
        return Toolkit(name=tool_name, tools=surfaces[tool_name])

    with patch.object(agents_module, "build_agent_toolkit", side_effect=build_toolkit), capture_logs() as logs:
        _build(config, runtime_paths)

    shadowed = [log for log in logs if log["event"].startswith("Configured toolkit exposes no unique")]
    assert [(log["toolkit"], log["function_names"]) for log in shadowed] == [("wikipedia", ["ping"])]


def test_a_deferred_function_an_earlier_one_shadows_is_offered_nowhere_and_reported_once(tmp_path: Path) -> None:
    """The run's deferred toolkits hold only the functions it offers; the build reports each shadowed one once."""
    config, runtime_paths = _config(
        tmp_path,
        tools=["calculator", {"wikipedia": {"defer": True}}, {"arxiv": {"defer": True}}],
    )
    surfaces = {"calculator": [ping], "wikipedia": [ping, search_knowledge_base], "arxiv": [ping]}

    def build_toolkit(tool_name: str, **_kwargs: object) -> Toolkit:
        return Toolkit(name=tool_name, tools=surfaces[tool_name])

    with patch.object(agents_module, "build_agent_toolkit", side_effect=build_toolkit), capture_logs() as logs:
        build = _build(config, runtime_paths)
        offered = _offered(build)

    calculator, wikipedia, arxiv = build.spec.toolkits[:3]
    assert (calculator.name, wikipedia.name, arxiv.name) == ("calculator", "wikipedia", "arxiv")
    assert offered["ping"] is calculator.functions[0]
    assert [function.name for function in wikipedia.functions] == ["search_knowledge_base"]
    assert arxiv.functions == ()
    assert build.request_template.deferred_tools.names == frozenset({"search_knowledge_base"})
    assert [(log["toolkit"], log["tool_name"]) for log in logs if log["event"] == "duplicate_tool_name"] == [
        ("wikipedia", "ping"),
        ("arxiv", "ping"),
    ]
    shadowed = [log for log in logs if log["event"].startswith("Configured toolkit exposes no unique")]
    assert [(log["toolkit"], log["function_names"]) for log in shadowed] == [("arxiv", ["ping"])]


def test_a_function_hidden_across_toolkits_leaves_the_run_and_its_owners(tmp_path: Path) -> None:
    """An MCP function that collides with a later local one is hidden, so the run offers and owns the local one."""
    config, runtime_paths = _config(tmp_path, tools=["wikipedia", "calculator"])

    def build_toolkit(tool_name: str, **_kwargs: object) -> Toolkit:
        if tool_name == "calculator":
            return Toolkit(name="calculator", tools=[ping])
        server = MindRoomMCPToolkit(server_id="demo", manager=None, catalog=None)
        server.functions["ping"] = ping
        return server

    with patch.object(agents_module, "build_agent_toolkit", side_effect=build_toolkit):
        build = _build(config, runtime_paths)

    server, calculator = build.spec.toolkits[:2]
    assert (server.name, server.functions) == ("demo", ())
    assert _offered(build)["ping"] is calculator.functions[0]
    assert _owners(build) == {"ping": "calculator"}


def test_an_mcp_function_yields_to_a_local_toolkit_that_shares_its_toolkit_name(tmp_path: Path) -> None:
    """A local toolkit whose name matches an MCP toolkit's still counts as another owner of the function name."""
    config, runtime_paths = _config(tmp_path, tools=["wikipedia", "calculator"])

    def build_toolkit(tool_name: str, **_kwargs: object) -> Toolkit:
        if tool_name == "calculator":
            return Toolkit(name="demo", tools=[ping])
        server = MindRoomMCPToolkit(server_id="demo", manager=None, catalog=None)
        server.functions["ping"] = ping
        return server

    with patch.object(agents_module, "build_agent_toolkit", side_effect=build_toolkit):
        build = _build(config, runtime_paths)

    server, local = build.spec.toolkits[:2]
    assert (server.name, server.functions) == ("demo", ())
    assert (local.name, [function.name for function in local.functions]) == ("demo", ["ping"])
    assert _offered(build)["ping"] is local.functions[0]
    assert _owners(build) == {"ping": "calculator"}


def test_an_mcp_function_yields_to_a_generated_one_and_mcp_copies_of_one_name_all_leave(tmp_path: Path) -> None:
    """A remote catalog never takes the knowledge search's name, and two servers offering one name both lose it."""
    config, runtime_paths = _config(tmp_path, tools=["wikipedia", "arxiv"])

    def build_toolkit(tool_name: str, **_kwargs: object) -> Toolkit:
        server = MindRoomMCPToolkit(server_id=tool_name, manager=None, catalog=None)
        names = ("search_knowledge_base", "ping") if tool_name == "wikipedia" else ("ping", "pong")
        for name in names:
            server.functions[name] = ping
        return server

    with patch.object(agents_module, "build_agent_toolkit", side_effect=build_toolkit), capture_logs() as logs:
        build = _build(config, runtime_paths, knowledge=_knowledge())

    wikipedia, arxiv, generated = build.spec.toolkits[:3]
    assert wikipedia.functions == ()
    assert [function.name for function in arxiv.functions] == ["pong"]
    assert (generated.name, [function.name for function in generated.functions]) == (
        "search_knowledge_base",
        ["search_knowledge_base"],
    )
    hidden = [log for log in logs if log["event"] == "Hiding an MCP function whose name another toolkit offers"]
    assert [(log["toolkit"], log["function_name"], log["colliding_toolkits"]) for log in hidden] == [
        ("wikipedia", "search_knowledge_base", ["search_knowledge_base"]),
        ("wikipedia", "ping", ["arxiv"]),
        ("arxiv", "ping", ["wikipedia"]),
    ]


def test_a_configured_function_named_like_a_generated_one_is_what_the_run_offers_owns_and_runs_as_a_job(
    tmp_path: Path,
) -> None:
    """A configured function takes the knowledge search's name, so the run, approvals, and tool jobs all name it."""
    config, runtime_paths = _config(tmp_path)
    config.background_tool_jobs.enabled = True
    built: dict[str, Toolkit] = {}

    def build_toolkit(tool_name: str, **_kwargs: object) -> Toolkit:
        built[tool_name] = Toolkit(name=tool_name, tools=[search_knowledge_base])
        return built[tool_name]

    with patch.object(agents_module, "build_agent_toolkit", side_effect=build_toolkit), capture_logs() as logs:
        build = _build(config, runtime_paths, knowledge=_knowledge())

    configured, generated = build.spec.toolkits[:2]
    assert (configured.name, generated.name) == ("calculator", "search_knowledge_base")
    [offered] = configured.functions
    assert _offered(build)["search_knowledge_base"] is offered
    assert _owners(build) == {"search_knowledge_base": "calculator"}
    assert build.tool_jobs is not None
    job_tool = build.tool_jobs.tools["search_knowledge_base"]
    assert (job_tool.function, job_tool.toolkit_name, job_tool.toolkit) == (offered, "calculator", built["calculator"])
    assert [log["toolkit"] for log in logs if log["event"] == "duplicate_tool_name"] == ["search_knowledge_base"]
    assert not [log for log in logs if log["event"].startswith("Configured toolkit exposes no unique")]


def test_a_function_asking_for_prompt_instructions_loads_only_in_a_deferred_toolkit(tmp_path: Path) -> None:
    """Tool search shows a deferred function's instructions; an eager toolkit offering one is refused, not the agent."""
    config, runtime_paths = _config(tmp_path, tools=[{"wikipedia": {"defer": True}}, "arxiv"])

    def build_toolkit(tool_name: str, **_kwargs: object) -> Toolkit:
        function = Function(name=f"{tool_name}_lookup", entrypoint=ping, instructions="Look up named pages only.")
        return Toolkit(name=tool_name, tools=[function])

    with patch.object(agents_module, "build_agent_toolkit", side_effect=build_toolkit), capture_logs() as logs:
        build = _build(config, runtime_paths)

    assert [toolkit.name for toolkit in build.spec.toolkits] == ["wikipedia", "skills"]
    assert build.request_template.deferred_tools.names == frozenset({"wikipedia_lookup"})
    [warning] = [log for log in logs if log["event"] == "Could not load tool for agent construction"]
    assert (warning["tool"], warning["error"]) == (
        "arxiv",
        (
            "Tool function 'arxiv_lookup' sets instructions for Agno to add to the system prompt, which MindRoom adds"
            " only per toolkit; set them on the toolkit instead"
        ),
    )


def _no_hook(*_args: object, **_kwargs: object) -> None:
    return None


_REFUSED_FIELDS = [
    {"pre_hook": _no_hook},
    {"requires_user_input": True},
    {"external_execution": True},
    {"instructions": "Look up named pages."},
]


@pytest.mark.parametrize(
    ("agent_mode", "narrowed_by"),
    [("standard", "caller"), ("standard", "persona"), ("minimal", "caller")],
)
@pytest.mark.parametrize("refused", _REFUSED_FIELDS, ids=["hook", "user_input", "external_execution", "instructions"])
def test_a_refused_function_the_agent_never_offers_costs_its_toolkit_nothing(
    tmp_path: Path,
    agent_mode: str,
    narrowed_by: str,
    refused: dict[str, object],
) -> None:
    """A function setting a field MindRoom refuses fails its toolkit only when the agent offers it."""
    config, runtime_paths = _config(tmp_path, tools=["wikipedia"])

    def build_toolkit(tool_name: str, **_kwargs: object) -> Toolkit:
        return Toolkit(name=tool_name, tools=[Function(name="lookup", entrypoint=ping, **refused), ping])

    narrowing: dict[str, Any] = (
        {"tool_function_filter": lambda function: function.name != "lookup"}
        if narrowed_by == "caller"
        else {
            "persona": SubagentPersona(
                source_kind="inline",
                source_name="",
                system_prompt="You ping.",
                tools=("wikipedia.ping",),
            ),
        }
    )
    with patch.object(agents_module, "build_agent_toolkit", side_effect=build_toolkit), capture_logs() as logs:
        build = _build(config, runtime_paths, agent_mode=agent_mode, **narrowing)

    assert not [log for log in logs if log["event"] == "Could not load tool for agent construction"]
    assert [function.name for function in build.spec.toolkits[0].functions] == ["ping"]


def test_a_toolkit_s_instructions_show_whenever_it_offers_a_function(tmp_path: Path) -> None:
    """Agno's own toolkits show their instructions too, though their ``add_instructions`` switch is off."""
    config, runtime_paths = _config(tmp_path, tools=["reasoning"])

    build = _build(config, runtime_paths)

    assert ReasoningTools().add_instructions is False
    assert build.prompt.tool_instructions == (ReasoningTools().instructions,)


@pytest.mark.parametrize(
    ("overrides", "saved", "expected"),
    [
        ({"enable_think": False}, {}, ()),
        ({"enable_analyze": False}, {}, ()),
        (
            {"enable_think": False, "instructions": "Call think before each reply."},
            {},
            ("Call think before each reply.",),
        ),
        (
            {},
            {"enable_analyze": False, "instructions": "Use think to analyze complex requests."},
            ("Use think to analyze complex requests.",),
        ),
    ],
    ids=["without_think", "without_analyze", "authored_without_think", "saved_authored_without_analyze"],
)
def test_reasoning_s_default_instructions_show_only_while_both_its_functions_are_offered(
    tmp_path: Path,
    overrides: dict[str, object],
    saved: dict[str, object],
    expected: tuple[str, ...],
) -> None:
    """The default instructions require both ``think`` and ``analyze``; authored instructions show with either one.

    Settings saved for the tool, as the dashboard saves them, are authored like those in ``config.yaml``.
    """
    config, runtime_paths = _config(tmp_path, tools=[{"reasoning": overrides}])
    get_runtime_credentials_manager(runtime_paths).save_credentials("reasoning", saved)

    build = _build(config, runtime_paths)

    [reasoning] = [toolkit for toolkit in build.spec.toolkits if toolkit.name == "reasoning_tools"]
    assert len(reasoning.functions) == 1
    assert build.prompt.tool_instructions == expected


@pytest.mark.parametrize("realtime_call", [False, True], ids=["chat", "realtime_call"])
def test_matrix_message_guidance_reaches_the_prompt_only_with_the_matrix_message_function(
    tmp_path: Path,
    *,
    realtime_call: bool,
) -> None:
    """Delegation guidance never names matrix_message; its own description, offered only with it, says how to use it.

    A realtime call leaves out matrix_message when it needs approval, and nothing in the prompt still names it.
    """
    config, runtime_paths = _config(tmp_path, tools=["matrix_message"], delegate_to=["assistant"])
    config.tool_approval = ToolApprovalConfig(
        rules=[ApprovalRuleConfig(match="matrix_message", action="require_approval")],
    )
    identity = build_tool_execution_identity(
        channel="matrix",
        agent_name="assistant",
        runtime_paths=runtime_paths,
        requester_id="@user:localhost",
        room_id="!room:localhost",
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    call_filter = partial(_function_available_during_call, config=config, agent_name="assistant")

    build = create_agent(
        "assistant",
        config,
        runtime_paths,
        identity,
        knowledge=None,
        model=turn_model(config, runtime_paths, "assistant"),
        tool_function_filter=call_filter if realtime_call else None,
        # Chat asks for the approval; the realtime call builds as build_call_tools does.
        supports_native_tool_approval=not realtime_call,
        plugins=config_plugins(config, runtime_paths),
    )

    offered = {function.name for toolkit in build.spec.toolkits for function in toolkit.functions}
    assert {"run_subagent", "continue_subagent"} <= offered
    assert ("matrix_message" in offered) is not realtime_call
    assert not [text for text in build.prompt.tool_instructions if "matrix_message" in text]


def test_a_toolkit_whose_functions_are_all_left_out_shows_no_instructions(tmp_path: Path) -> None:
    """A toolkit shows its instructions only while it offers a function, not after a filter leaves them all out."""
    config, runtime_paths = _config(tmp_path, tools=["reasoning", "calculator"])

    build = _build(
        config,
        runtime_paths,
        tool_function_filter=lambda function: function.name not in {"think", "analyze"},
    )

    assert "reasoning_tools" not in [toolkit.name for toolkit in build.spec.toolkits]
    assert build.prompt.tool_instructions == ()


def test_a_toolkit_whose_every_function_an_earlier_one_took_shows_no_instructions(tmp_path: Path) -> None:
    """A run offers the first function of each name, so the later toolkit offers none and shows no instructions."""
    config, runtime_paths = _config(tmp_path, tools=["calculator", "wikipedia"])

    def build_toolkit(tool_name: str, **_kwargs: object) -> Toolkit:
        return Toolkit(name="pinger", tools=[ping], instructions=f"Ping for {tool_name}.")

    with patch.object(agents_module, "build_agent_toolkit", side_effect=build_toolkit):
        build = _build(config, runtime_paths)

    assert [toolkit.functions != () for toolkit in build.spec.toolkits[:2]] == [True, False]
    assert build.prompt.tool_instructions == ("Ping for calculator.",)


@pytest.mark.parametrize(
    ("claimed_async", "beside"),
    [
        (False, None),
        (True, None),
        (False, Function(name="lookup", entrypoint=ping, pre_hook=_no_hook)),
        (False, Function(name="lookup")),
    ],
    ids=["alone", "async_only", "beside_a_refused_function", "beside_a_function_without_entrypoint"],
)
def test_a_toolkit_claiming_a_room_recovery_function_stops_the_build(
    tmp_path: Path,
    *,
    claimed_async: bool,
    beside: Function | None,
) -> None:
    """Only the registered room recovery tool may offer its function, whatever else the toolkit or the filters hold."""
    config, runtime_paths = _config(tmp_path)

    def build_toolkit(tool_name: str, **_kwargs: object) -> Toolkit:
        toolkit = Toolkit(name=tool_name)
        claimed = toolkit.async_functions if claimed_async else toolkit.functions
        claimed["invite_router"] = Function(name="invite_router", entrypoint=ping)
        if beside is not None:
            toolkit.functions[beside.name] = beside
        return toolkit

    with (
        patch.object(agents_module, "build_agent_toolkit", side_effect=build_toolkit),
        pytest.raises(ValueError, match="reserved for Matrix room recovery tools"),
    ):
        _build(config, runtime_paths, tool_function_filter=lambda _function: False)


def test_a_toolkit_that_cannot_be_adapted_drops_only_itself_and_its_deferral(tmp_path: Path) -> None:
    """A function adaptation refuses costs its own toolkit, deferred or not, and never the agent."""
    config, runtime_paths = _config(tmp_path, tools=["calculator", {"name": "wikipedia", "defer": True}])
    build_agent_toolkit = agents_module.build_agent_toolkit

    def build_toolkit(tool_name: str, **kwargs: object) -> Toolkit | None:
        if tool_name != "wikipedia":
            return build_agent_toolkit(tool_name, **kwargs)
        toolkit = Toolkit(name="wikipedia", tools=[ping])
        toolkit.functions["search_wikipedia"] = Function(name="search_wikipedia")
        return toolkit

    with patch.object(agents_module, "build_agent_toolkit", side_effect=build_toolkit), capture_logs() as logs:
        build = _build(config, runtime_paths)

    assert [toolkit.name for toolkit in build.spec.toolkits] == ["calculator", "skills"]
    assert build.request_template.deferred_tools.names == frozenset()
    assert not any("Deferred Tool Discovery" in instruction for instruction in build.prompt.instructions)
    [warning] = [log for log in logs if log["event"] == "Could not load tool for agent construction"]
    assert (warning["tool"], warning["error"]) == ("wikipedia", "Tool function 'search_wikipedia' has no entrypoint")


def test_a_persona_build_offers_only_its_tools_and_speaks_with_its_prompt(tmp_path: Path) -> None:
    """An authored subagent persona narrows the child to its tool list and replaces the agent's prompt with its own."""
    config, runtime_paths = _config(tmp_path)
    persona = SubagentPersona(
        source_kind="inline",
        source_name="",
        system_prompt="You are the reviewer.",
        tools=("calculator",),
    )

    build = _build(config, runtime_paths, knowledge=_knowledge(), persona=persona)

    assert set(_offered(build)) == _CALCULATOR_FUNCTIONS
    assert _owners(build) == dict.fromkeys(_CALCULATOR_FUNCTIONS, "calculator")
    assert build.prompt.persona == "You are the reviewer."
    assert "Answer questions about the team handbook." not in (build.prompt.role or "")
    assert (build.prompt.knowledge_block, build.prompt.skills_block) == (None, None)
    assert build.surface.knowledge_sources == ()
