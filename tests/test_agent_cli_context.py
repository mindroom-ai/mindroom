"""Full context remains available without bloating the actual provider prompt."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from mindroom import agents
from mindroom.agent_cli.minimal_agent import minimal_context_documents, minimal_turn, prepare_minimal_catalog
from mindroom.agent_loop.spec import RunContext
from mindroom.config.agent import AgentConfig
from mindroom.error_handling import MinimalModeUnavailableError
from mindroom.hooks import EnrichmentItem
from mindroom.knowledge.search import KnowledgeSourceDescription
from mindroom.run_storage import RunStorage
from mindroom.session_storage.types import ConversationSession
from mindroom.tool_system.agent_tool_calls import PreparedAgentToolCatalog
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from tests.test_agent_cli_authority import _runtime_context

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.agent_loop.build import AgentBuild
    from mindroom.tool_system.runtime_context import ToolRuntimeContext


def _minimal_build(runtime: ToolRuntimeContext, *, tools: tuple[str, ...] = ("shell",)) -> AgentBuild:
    runtime.config.agents["helper"] = AgentConfig(
        display_name="Helper",
        role="Keep the build machine healthy.",
        instructions=["Check the disk before cleaning."],
        tools=list(tools),
        memory_backend="file",
    )
    return agents.create_agent(
        "helper",
        runtime.config,
        runtime.runtime_paths,
        build_execution_identity_from_runtime_context(runtime),
        agent_mode="minimal",
        persist_runtime_state=False,
        knowledge=None,
        active_model_name=None,
    )


def test_only_the_bootstrap_and_required_context_reach_the_provider(tmp_path: Path) -> None:
    """The full prompt, memory, and optional enrichment are documents; required enrichment stays inline."""
    build = _minimal_build(_runtime_context(tmp_path))
    turn = minimal_turn(
        build,
        session_preamble="Earlier the disk was nearly full.",
        transient_memory="The user prefers short answers.",
        system_enrichment=(
            EnrichmentItem(key="required_system", text="Assigned task: rotate the logs.", minimal_required=True),
            EnrichmentItem(key="optional_system", text="The weather is sunny."),
        ),
        transient_enrichment=(
            EnrichmentItem(key="required_turn", text="Reply in the thread.", minimal_required=True),
            EnrichmentItem(key="optional_turn", text="A colleague mentioned lunch."),
        ),
    )

    system = turn.system_message.get_content_string()
    assert system.startswith("You are Helper (helper) in minimal mode.\nBash starts in your workspace.")
    assert "Assigned task: rotate the logs." in system
    # The build's turn context, such as the current date, stays inline.
    assert build.prompt.turn_context
    assert build.prompt.turn_context in system
    for hidden in ("Keep the build machine healthy.", "Check the disk", "nearly full", "sunny", "lunch"):
        assert hidden not in system
    assert "Reply in the thread." in turn.transient_context
    assert "lunch" not in turn.transient_context
    documents = turn.context_documents
    assert "Keep the build machine healthy." in documents["agent-context"]
    assert "Check the disk before cleaning." in documents["instructions"]
    assert "nearly full" in documents["memory"]
    assert "short answers" in documents["memory"]
    assert "sunny" in documents["enrichment"]
    assert "lunch" in documents["enrichment"]


def test_a_toolkit_s_instructions_are_a_context_document_not_part_of_the_bootstrap(tmp_path: Path) -> None:
    """Minimal mode leaves a toolkit's instructions out of the provider prompt and offers them as a document."""
    build = _minimal_build(_runtime_context(tmp_path), tools=("shell", "todo"))
    turn = minimal_turn(
        build,
        session_preamble="",
        transient_memory="",
        system_enrichment=(),
        transient_enrichment=(),
    )

    [guidance] = [toolkit.guidance for toolkit in build.spec.toolkits if toolkit.name == "todo"]
    assert guidance is not None
    assert "per-thread work plan" in guidance
    assert guidance in turn.context_documents.values()
    assert guidance not in turn.system_message.get_content_string()


def test_the_knowledge_document_lists_each_base_the_agent_s_search_reaches(tmp_path: Path) -> None:
    """`mindroom-agent context` describes each knowledge base the agent can search, one per line."""
    build = _minimal_build(_runtime_context(tmp_path))
    sources = (
        KnowledgeSourceDescription(base_id="handbook", description="The team handbook."),
        KnowledgeSourceDescription(base_id="runbooks", description="Operations runbooks."),
    )
    build = replace(build, surface=replace(build.surface, knowledge_sources=sources))

    documents = minimal_context_documents(build)

    assert documents["knowledge"] == "handbook: The team handbook.\nrunbooks: Operations runbooks."


@pytest.mark.asyncio
async def test_a_catalog_for_another_requester_names_the_recovery_command(tmp_path: Path) -> None:
    """Minimal Bash is refused for anyone but the turn's own requester, with the way back to standard mode."""
    runtime = _runtime_context(tmp_path)
    build = _minimal_build(runtime)
    session = ConversationSession(session_id="session", kind="agent", owner_id="helper")
    with tool_runtime_context(runtime), pytest.raises(MinimalModeUnavailableError, match="!mode helper standard"):
        await prepare_minimal_catalog(
            build,
            run=RunContext(run_id="run", user_id="@mallory:example.test", session=session),
            storage=RunStorage(None, session, None),
            delegation_depth=0,
        )
    with (
        tool_runtime_context(runtime),
        pytest.raises(MinimalModeUnavailableError, match="Start a new subagent without minimal"),
    ):
        await prepare_minimal_catalog(
            replace(build, spec=replace(build.spec, toolkits=())),
            run=RunContext(run_id="run", user_id=runtime.requester_id, session=session),
            storage=RunStorage(None, session, None),
            delegation_depth=1,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "text"),
    [(RuntimeError("toolkit unavailable"), "toolkit unavailable"), (TimeoutError(), "TimeoutError")],
    ids=["with_text", "without_text"],
)
async def test_a_catalog_whose_toolkits_fail_names_why_before_the_recovery_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    text: str,
) -> None:
    """A failure while preparing the tools names its exception's text, or its type name when it has none."""
    runtime = _runtime_context(tmp_path)
    build = _minimal_build(runtime)
    session = ConversationSession(session_id="session", kind="agent", owner_id="helper")

    async def fail(*_arguments: object, **_options: object) -> None:
        raise error

    monkeypatch.setattr(PreparedAgentToolCatalog, "prepare", fail)
    with tool_runtime_context(runtime), pytest.raises(MinimalModeUnavailableError) as raised:
        await prepare_minimal_catalog(
            build,
            run=RunContext(run_id="run", user_id=runtime.requester_id, session=session),
            storage=RunStorage(None, session, None),
            delegation_depth=0,
        )
    assert str(raised.value) == f"{text}. Return to standard mode with `!mode helper standard`."
