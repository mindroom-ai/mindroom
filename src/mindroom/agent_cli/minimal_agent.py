"""Minimal mode: the agent's whole build behind one provider-facing Bash function.

A minimal turn sends the bootstrap message and offers only Bash; the build's own tools, its deferred toolkits, and
its full context stay reachable through `mindroom-agent` inside each Bash command, bound to the same live turn.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING, NoReturn

from mindroom.agent_cli.bash import MinimalBashTools
from mindroom.agent_cli.context import minimal_system_message
from mindroom.agent_cli.lifetime import current_cli_lifetime
from mindroom.agent_cli.response_owner import bind_response_owner
from mindroom.agent_cli.shell_contract import SHELL_OPERATION_NAMES
from mindroom.agent_loop.build import prompt_for_turn
from mindroom.error_handling import MinimalModeUnavailableError, minimal_mode_failure_message
from mindroom.hooks import render_enrichment_block, render_system_enrichment_block, render_transient_context
from mindroom.llm.messages import Message
from mindroom.tool_jobs.execution import agent_tool_middleware
from mindroom.tool_system.agent_tool_calls import DeferredAgentToolkit, PreparedAgentToolCatalog
from mindroom.tool_system.agno_toolkits import adapt_toolkit
from mindroom.tool_system.runtime_context import get_tool_runtime_context
from mindroom.tool_system.tool_access import ToolKey

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from mindroom.agent_cli.turn import LiveTurnTools
    from mindroom.agent_loop.build import AgentBuild
    from mindroom.agent_loop.model_loop import LoopHooks
    from mindroom.agent_loop.spec import AgentSpec, RunContext
    from mindroom.hooks import EnrichmentItem
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.llm.runs import RunRecord
    from mindroom.response_turn import ResponseTurnContext
    from mindroom.session_storage.store import ConversationStore
    from mindroom.session_storage.types import ConversationSession
    from mindroom.tool_system.tool_function import AdaptedToolkit, ToolFunction

__all__ = [
    "MinimalTurn",
    "bind_minimal_owner",
    "minimal_attempt",
    "minimal_context_documents",
    "minimal_turn",
    "prepare_minimal_catalog",
    "presented_build",
]


@dataclass(frozen=True)
class MinimalTurn:
    """One minimal turn's prompt: the system message, the transient context, and the documents Bash can read."""

    system_message: Message
    transient_context: str
    context_documents: dict[str, str]


def minimal_context_documents(build: AgentBuild) -> dict[str, str]:
    """Return the agent's context `mindroom-agent context` reads: what the standard prompt would hold, piece by piece.

    ``agent-context`` is the whole standard system message of the build, toolkit and skill guidance included.
    """
    surface = build.surface
    documents = {
        "role": build.prompt.role or "",
        "instructions": "\n\n".join(build.prompt.instructions),
        "interactive": surface.interactive_prompt,
        "knowledge": "\n".join(f"{source.base_id}: {source.description}" for source in surface.knowledge_sources),
    }
    for index, document in enumerate(surface.context_documents):
        documents[f"context-{index + 1}"] = document
    for index, skill in enumerate(surface.skills):
        documents[f"skill-{index + 1}"] = f"{skill.name}\n{skill.instructions}"
    for index, toolkit in enumerate(build.spec.toolkits):
        if toolkit.guidance:
            documents[f"toolkit-{index + 1}"] = toolkit.guidance
    documents["agent-context"] = prompt_for_turn(build).get_content_string()
    return documents


def minimal_turn(
    build: AgentBuild,
    *,
    session_preamble: str,
    transient_memory: str,
    system_enrichment: Sequence[EnrichmentItem],
    transient_enrichment: Sequence[EnrichmentItem],
) -> MinimalTurn:
    """Keep optional current context discoverable and trusted runtime assignments inline.

    Only enrichment marked ``minimal_required`` reaches the model directly: system items after the bootstrap
    message, transient ones in the transient context. Everything else, memory included, is a context document.
    """
    surface = build.surface
    system_message = minimal_system_message(
        agent_name=build.spec.id,
        display_name=build.spec.name,
        identity=surface.identity,
        toolkit_names=surface.toolkit_names,
        instructions=surface.minimal_instructions,
        context_files=surface.context_files,
        memory_root=surface.memory_root,
        runtime_context=(build.prompt.additional_context or "") + surface.runtime_context,
    )
    if required := [item for item in system_enrichment if item.minimal_required]:
        system_message += "\n" + render_system_enrichment_block(required)
    documents = minimal_context_documents(build)
    documents["memory"] = render_transient_context((session_preamble, transient_memory))
    documents["enrichment"] = render_enrichment_block([*system_enrichment, *transient_enrichment])
    return MinimalTurn(
        system_message=Message(role="system", content=system_message),
        transient_context=render_transient_context(
            (render_enrichment_block([item for item in transient_enrichment if item.minimal_required]),),
        ),
        context_documents=documents,
    )


def presented_build(build: AgentBuild) -> AgentBuild:
    """Return ``build`` as a minimal turn's requests present it: the Bash facade is its only tool."""
    return replace(build, spec=replace(build.spec, toolkits=(adapt_toolkit(MinimalBashTools()),)))


def _failure(build: AgentBuild, delegation_depth: int, reason: str) -> str:
    return minimal_mode_failure_message(reason, build.spec.id, subagent=delegation_depth > 0)


def _raise_failure(build: AgentBuild, delegation_depth: int, exc: BaseException) -> NoReturn:
    if not isinstance(exc, Exception):
        raise exc
    raise MinimalModeUnavailableError(_failure(build, delegation_depth, str(exc))) from exc


def _check_handlers(functions: Iterable[tuple[str, ToolFunction]]) -> None:
    """Refuse a function whose pause no CLI caller can continue: only delegations run outside their call."""
    for toolkit, function in functions:
        if function.external_execution and toolkit != "delegate":
            msg = f"Configured function {function.name!r} has no supported response continuation handler"
            raise RuntimeError(msg)


def _checked(deferred: DeferredAgentToolkit) -> DeferredAgentToolkit:
    async def materialize() -> AdaptedToolkit:
        toolkit = await deferred.materialize()
        _check_handlers((deferred.name, function) for function in toolkit.functions)
        return toolkit

    return replace(deferred, materialize=materialize)


async def prepare_minimal_catalog(
    build: AgentBuild,
    *,
    run: RunContext,
    session: ConversationSession,
    store: ConversationStore | None,
    delegation_depth: int,
    record: RunRecord | None = None,
) -> PreparedAgentToolCatalog:
    """Prepare the build's tools behind Bash, without starting a shell worker.

    ``record`` is the stored run a recovery continues; a live turn's catalog learns its run from the checkpoint.
    The effective surface is checked before a catalog is published or gains authority: every handler must be
    supported and the agent's own shell must be offered.
    """
    runtime = get_tool_runtime_context()
    if runtime is None or run.user_id != runtime.requester_id:
        raise MinimalModeUnavailableError(
            _failure(build, delegation_depth, "Minimal catalog requires its authenticated requester"),
        )
    catalog = PreparedAgentToolCatalog(
        agent_name=build.spec.id,
        run=run,
        runtime_context=runtime,
        session=session,
        store=store,
        middleware=agent_tool_middleware(build),
        tool_call_limit=build.spec.tool_call_limit,
        record=record,
    )
    try:
        await catalog.prepare(build.spec.toolkits, build.tool_owners)
        await catalog.connect(build.spec.toolkits)
        _check_handlers((binding.key.toolkit, binding.function) for binding in catalog.bindings())
        loaded = {item["toolkit"] for item in catalog.metadata()}
        for deferred in build.surface.deferred:
            if deferred.name not in loaded:
                catalog.add_deferred(_checked(deferred))
        try:
            for name in SHELL_OPERATION_NAMES:
                await catalog.bind(ToolKey("shell", name))
        except ValueError as exc:
            msg = "Minimal mode requires the agent's `run_shell_command` shell permission."
            raise RuntimeError(msg) from exc
    except BaseException as exc:
        await catalog.close()
        _raise_failure(build, delegation_depth, exc)
    return catalog


def bind_minimal_owner(
    build: AgentBuild,
    catalog: PreparedAgentToolCatalog,
    *,
    response_context: ResponseTurnContext,
    context_documents: Mapping[str, str],
    delegation_depth: int,
    refresh_scheduler: KnowledgeRefreshScheduler | None,
) -> LiveTurnTools:
    """Bind the real managed turn to ``catalog``: its owner and grant on the first attempt, its catalog afterwards."""
    lifetime = current_cli_lifetime()
    runtime = catalog.runtime_context
    if lifetime is None or runtime.orchestrator is None:
        msg = "Minimal Bash requires an active managed response owner"
        raise RuntimeError(msg)
    if build.surface.output_file_policy is None:
        msg = "Minimal Bash requires a canonical agent workspace"
        raise RuntimeError(msg)
    return bind_response_owner(
        catalog,
        runtime=runtime,
        lifetime=lifetime,
        response_context=response_context,
        run_id=catalog.run.run_id,
        context_documents=context_documents,
        output_file_policy=build.surface.output_file_policy,
        delegation_depth=delegation_depth,
        refresh_scheduler=refresh_scheduler,
        failure_message=partial(_failure, build, delegation_depth),
    )


async def minimal_attempt(
    build: AgentBuild,
    spec: AgentSpec,
    hooks: LoopHooks,
    *,
    run: RunContext,
    session: ConversationSession,
    store: ConversationStore | None,
    response_context: ResponseTurnContext,
    context_documents: Mapping[str, str],
    delegation_depth: int,
    refresh_scheduler: KnowledgeRefreshScheduler | None,
) -> tuple[AgentSpec, LoopHooks]:
    """Bind the attempt's CLI owner, then return the spec and hooks of a run that offers only its Bash facade.

    Each provider batch is checkpointed before its Bash calls run. The build's tool policy runs around the calls
    Bash makes through the CLI, not around Bash itself.
    """
    lifetime = current_cli_lifetime()
    runtime = get_tool_runtime_context()
    if lifetime is None or runtime is None or runtime.orchestrator is None:
        raise MinimalModeUnavailableError(
            _failure(build, delegation_depth, "Minimal Bash requires an active managed response owner"),
        )
    catalog = await prepare_minimal_catalog(
        build,
        run=run,
        session=session,
        store=store,
        delegation_depth=delegation_depth,
    )
    try:
        owner = bind_minimal_owner(
            build,
            catalog,
            response_context=response_context,
            context_documents=context_documents,
            delegation_depth=delegation_depth,
            refresh_scheduler=refresh_scheduler,
        )
    except BaseException as exc:
        await catalog.close()
        _raise_failure(build, delegation_depth, exc)
    facade = adapt_toolkit(MinimalBashTools(execute=owner.execute_bash))
    return (
        replace(spec, toolkits=(facade,)),
        replace(hooks, tool_middleware=(), before_dispatch=owner.checkpoint.capture),
    )
