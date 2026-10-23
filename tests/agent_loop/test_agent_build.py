"""``create_agent`` builds the owned agent: its spec, prompt, request template, store, tool owners, and surface."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest
from agno.tools import Toolkit

from mindroom.agent_loop.build import AgentBuild, AgentSurface, appended_context, prompt_for_turn
from mindroom.agent_loop.prompt import CLAUDE_TOOLS_NOTE, render_system_message
from mindroom.agent_loop.tools import prepare_run_tools
from mindroom.agents import _tool_owners, create_agent
from mindroom.config.approval import ApprovalRuleConfig, ToolApprovalConfig
from mindroom.config.main import Config
from mindroom.delegation.state import SubagentPersona
from mindroom.knowledge.search import AgentKnowledge, KnowledgeSource, KnowledgeSourceDescription
from mindroom.llm.provider import ModelRequest
from mindroom.session_storage.store import ConversationStore
from mindroom.tool_system.skills import SKILL_FUNCTION_NAMES
from mindroom.tool_system.worker_routing import agent_workspace_root_path, build_tool_execution_identity
from tests.conftest import bind_runtime_paths, test_runtime_paths

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
    return create_agent("assistant", config, runtime_paths, identity, **kwargs)


def _offered(build: AgentBuild) -> dict[str, ToolFunction]:
    return dict(prepare_run_tools(build.spec).functions)


def test_create_agent_builds_the_spec_prompt_store_and_owners(tmp_path: Path) -> None:
    """The build offers the configured toolkits, then the knowledge search, then skills, and renders their prompt."""
    config, runtime_paths = _config(tmp_path)
    knowledge = _knowledge()

    build = _build(config, runtime_paths, knowledge=knowledge)

    assert (build.spec.id, build.spec.name, build.spec.model.identity.id) == (
        "assistant",
        "Assistant",
        "claude-sonnet-5-5",
    )
    assert build.spec.system_message is None
    assert [toolkit.name for toolkit in build.spec.toolkits] == ["calculator", "search_knowledge_base", "skills"]
    assert set(_offered(build)) == _CALCULATOR_FUNCTIONS | {"search_knowledge_base"} | SKILL_FUNCTION_NAMES
    assert build.spec.tool_call_limit == config.resolve_entity("assistant").max_tool_calls_per_turn
    assert build.tool_owners == dict.fromkeys(_CALCULATOR_FUNCTIONS, "calculator")
    workspace = agent_workspace_root_path(runtime_paths.storage_root, "assistant")
    assert f"### {workspace / 'SOUL.md'}\nYou are calm and precise." in build.prompt.role
    assert build.prompt.role.endswith("Answer questions about the team handbook.")
    assert build.prompt.knowledge_block == knowledge.prompt_block()
    assert build.prompt.skills_block is not None
    assert "<name>expense-report</name>" in build.prompt.skills_block
    assert build.prompt.tool_instructions == ()
    assert build.prompt.model_note == CLAUDE_TOOLS_NOTE
    assert build.prompt.additional_context is not None
    assert build.prompt.additional_context.startswith("<mindroom_session_context>")
    assert build.request_template == ModelRequest(messages=())
    assert build.history_settings == config.resolve_entity("assistant").history_settings
    assert isinstance(build.store, ConversationStore)
    # Large CLI results and their media are saved in the agent's own workspace.
    assert build.surface.output_file_policy is not None
    assert build.surface.output_file_policy.workspace_root == workspace
    assert replace(build.surface, output_file_policy=None) == AgentSurface(
        mode="standard",
        cli_shell=False,
        context_documents=("You are calm and precise.",),
        context_files=("SOUL.md",),
        knowledge_sources=tuple(source.description for source in knowledge.sources),
    )


def test_a_build_without_runtime_state_has_no_store_tools_or_context(tmp_path: Path) -> None:
    """A restricted in-process run keeps the role and instructions but no tools, skills, knowledge, or store."""
    config, runtime_paths = _config(tmp_path)

    build = _build(
        config,
        runtime_paths,
        knowledge=_knowledge(),
        persist_runtime_state=False,
        disable_runtime_capabilities=True,
    )

    assert build.store is None
    assert build.spec.toolkits == ()
    assert (build.prompt.skills_block, build.prompt.knowledge_block, build.prompt.model_note) == (None, None, None)
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
    assert all(function.requires_confirmation is True for function in offered.values())
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


def test_prompt_for_turn_appends_the_turns_context_blocks(tmp_path: Path) -> None:
    """Each turn block follows the context so far after one blank line; empty blocks and trailing whitespace drop out."""
    config, runtime_paths = _config(tmp_path)
    build = _build(config, runtime_paths, persist_runtime_state=False)
    base = build.prompt.additional_context
    assert base is not None

    assert prompt_for_turn(build).content == render_system_message(build.prompt).content
    expected = replace(build.prompt, additional_context=f"{base.strip()}\n\nmemory\n\n<ctx>")
    assert prompt_for_turn(build, "memory\n", "", "<ctx>").content == render_system_message(expected).content


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

    build = _build(config, runtime_paths, persist_runtime_state=False, agent_mode=agent_mode)

    if agent_mode == "standard":
        assert "search_wikipedia" in _offered(build)
        assert build.request_template.deferred_tool_names == frozenset({"search_wikipedia"})
        assert build.surface.deferred == ()
        assert any("Deferred Tool Discovery" in instruction for instruction in build.prompt.instructions)
    else:
        assert "search_wikipedia" not in _offered(build)
        assert build.request_template.deferred_tool_names == frozenset()
        assert [deferred.name for deferred in build.surface.deferred] == ["wikipedia"]
        assert build.surface.mode == "minimal"


def test_tool_owners_keep_the_first_function_of_a_name_even_without_an_authored_toolkit() -> None:
    """The run offers the first function of a repeated name, so it owns the name; a later one never fills it in."""

    def add(a: int, b: int) -> str:
        """Add two integers.

        Args:
            a: First number.
            b: Second number.

        """
        return str(a + b)

    generated = Toolkit(name="generated", tools=[add])
    authored = Toolkit(name="calculator", tools=[add])
    authored.functions["add"].owning_toolkit = "calculator"

    assert _tool_owners([generated, authored]) == {"add": None}
    assert _tool_owners([authored, generated]) == {"add": "calculator"}


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
    assert build.tool_owners == dict.fromkeys(_CALCULATOR_FUNCTIONS, "calculator")
    assert build.prompt.description == "You are the reviewer."
    assert "Answer questions about the team handbook." not in (build.prompt.role or "")
    assert (build.prompt.knowledge_block, build.prompt.skills_block) == (None, None)
    assert build.surface.knowledge_sources == ()
