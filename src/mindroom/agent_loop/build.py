"""What ``create_agent`` builds for the owned loop: the agent's spec, prompt, request settings, store, and surface."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from mindroom.agent_loop.prompt import render_system_message

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from mindroom.agent_loop.prompt import PromptSpec
    from mindroom.agent_loop.spec import AgentSpec
    from mindroom.agent_modes import AgentMode
    from mindroom.history.types import ResolvedHistorySettings
    from mindroom.knowledge.search import KnowledgeSourceDescription
    from mindroom.llm.messages import Message
    from mindroom.llm.provider import ModelRequest
    from mindroom.session_storage.store import ConversationStore
    from mindroom.tool_jobs.execution import JobActor
    from mindroom.tool_system.agent_tool_calls import DeferredAgentToolkit
    from mindroom.tool_system.invocation import ToolMiddleware
    from mindroom.tool_system.output_files import ToolOutputFilePolicy
    from mindroom.tool_system.skills import Skill


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentSurface:
    """How the agent presents its tools to the model.

    Standard mode offers the spec's tools; ``cli_shell`` says the agent's shell commands can call its other tools
    through ``mindroom-agent``. Minimal mode offers one Bash function through which the agent reaches the spec's
    tools and the toolkits in ``deferred`` once it materializes them, and reads ``context_documents``, the complete
    preload files before the prompt cap, listed as ``context_files`` relative to its workspace, and
    ``knowledge_sources``, the knowledge bases its search reaches after the persona and approval gating.

    Minimal mode's bootstrap message names the agent by ``identity`` when an authored persona replaces it, lists
    ``toolkit_names`` as its roster, points at its file memory under ``memory_root``, and ends with the Mind agent's
    ``runtime_context`` and the ``minimal_instructions``; ``interactive_prompt`` and ``skills`` are context documents
    beside the role and instructions. ``output_file_policy`` saves large CLI results and their media in the agent's
    workspace.
    """

    mode: AgentMode
    cli_shell: bool
    deferred: tuple[DeferredAgentToolkit, ...] = ()
    context_documents: tuple[str, ...] = ()
    context_files: tuple[str, ...] = ()
    knowledge_sources: tuple[KnowledgeSourceDescription, ...] = ()
    identity: str | None = None
    toolkit_names: tuple[str, ...] = ()
    memory_root: Path | None = None
    runtime_context: str = ""
    minimal_instructions: tuple[str, ...] = ()
    interactive_prompt: str = ""
    skills: tuple[Skill, ...] = ()
    output_file_policy: ToolOutputFilePolicy | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentBuild:
    """One configured agent as ``create_agent`` built it.

    ``spec`` carries no system message: ``prompt_for_turn`` renders one per turn from ``prompt``, whose
    ``additional_context`` is the build's own context, the date and session context. ``request_template`` holds the
    settings of every request of a run, such as the deferred tool names; ``tool_owners``, which approvals check, maps
    each configured function to the authored toolkit it came from, None for one without an authored toolkit, and
    leaves out the functions generated for the agent, such as the knowledge search. ``tool_policy`` is the agent's
    tool-call policy, outermost first: the binding through which a scheduled call runs one of the build's functions
    (for a build that offers the scheduler), plugin before and after hooks, call recording, and the computer
    announcement. ``tool_jobs`` is the agent as tool jobs see it while the instance runs them, which the loop's hooks
    offer the wait budget of its managed calls and the notices of its ended jobs for. Every call runs through
    ``tool_policy`` behind tool-job admission when ``tool_jobs`` is set (``agent_tool_middleware``), and admission
    runs a managed job's call through ``tool_policy`` again in the job's own task, so a step that must reach every
    call joins ``tool_policy``.
    """

    spec: AgentSpec
    prompt: PromptSpec
    request_template: ModelRequest
    history_settings: ResolvedHistorySettings
    store: ConversationStore | None
    tool_owners: Mapping[str, str | None]
    surface: AgentSurface
    tool_policy: tuple[ToolMiddleware, ...] = ()
    tool_jobs: JobActor | None = None


def tool_owners_by_agent(*builds: AgentBuild) -> dict[tuple[str, str], str | None]:
    """Return the authored toolkit of each function the builds offer, keyed ``(agent id, function)`` as approvals look it up."""
    return {(build.spec.id, name): toolkit for build in builds for name, toolkit in build.tool_owners.items()}


def prompt_for_turn(build: AgentBuild, *context: str) -> Message:
    """Render the system message of one turn: the build's prompt with the turn's context blocks appended in order.

    Each block follows the context so far after one blank line, trailing whitespace of the earlier text dropped;
    an empty block adds nothing.
    """
    additional_context = build.prompt.additional_context
    for block in context:
        additional_context = _appended(additional_context, block)
    return render_system_message(replace(build.prompt, additional_context=additional_context))


def _appended(existing: str | None, block: str) -> str | None:
    if not block:
        return existing
    stripped = existing.strip() if existing else ""
    return f"{stripped}\n\n{block}" if stripped else block


__all__ = [
    "AgentBuild",
    "AgentSurface",
    "prompt_for_turn",
    "tool_owners_by_agent",
]
