"""Static prompt-token estimation: what the model sees before any history replay.

Estimates the non-history cost of one prepared agent or team request — system
message, tool schemas, tool instructions, and the current prompt text — by
reusing Agno's own prompt-preparation paths. Nothing here touches history
scopes, sessions, or summaries; compaction consumes these numbers through the
resolved execution plan.

The prompt-only tool surface (model-visible payloads plus tool instructions)
is prepared once per agent or team instance and reused by static token
budgeting and run-metadata assembly within that turn. Agent and team instances
are rebuilt per response, so the surface never outlives one turn's tool state.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from functools import partial
from threading import Lock
from typing import TYPE_CHECKING, TypeGuard, cast
from weakref import ref

from agno.run import RunContext
from agno.run.agent import RunOutput
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.team import TeamSession
from agno.team._tools import _determine_tools_for_model
from agno.tools import Toolkit
from agno.tools.function import Function

from mindroom.timing import timed_block
from mindroom.token_budget import estimate_text_tokens, stable_serialize
from mindroom.tool_schema_cache import cached_processed_schema

if TYPE_CHECKING:
    from agno.agent import Agent
    from agno.team import Team

type _ToolDefinition = dict[str, object]


@dataclass(frozen=True)
class _PromptToolSurface:
    """Prompt-only tool surface prepared once per agent or team instance.

    ``payloads`` are canonical copies owned by the cache; treat them as
    immutable and hand out deep copies at public boundaries.
    """

    payloads: tuple[_ToolDefinition, ...]
    tool_instructions: tuple[str, ...]
    definition_tokens: int


# Keyed by entity id because Agno Agent/Team instances are unhashable; each
# entry holds a weakref whose eviction callback removes the entry on GC.
_TOOL_SURFACE_CACHE: dict[int, tuple[ref[object], _PromptToolSurface]] = {}
_TOOL_SURFACE_CACHE_LOCK = Lock()


@dataclass(slots=True)
class _StaticTokenEstimator:
    """Request-local static-token estimator that caches the non-prompt cost once."""

    non_prompt_tokens_fn: Callable[[], int]
    _non_prompt_tokens: int | None = field(default=None, init=False)

    def estimate(self, full_prompt: str) -> int:
        """Estimate static prompt tokens while reusing Agno-prepared tools."""
        if self._non_prompt_tokens is None:
            self._non_prompt_tokens = self.non_prompt_tokens_fn()
        return estimate_text_tokens(full_prompt) + self._non_prompt_tokens


def agent_static_token_estimator(agent: Agent) -> _StaticTokenEstimator:
    """Return a request-local static-token estimator for one prepared agent response."""
    return _StaticTokenEstimator(partial(_estimate_agent_non_prompt_static_tokens, agent))


def team_static_token_estimator(team: Team) -> _StaticTokenEstimator:
    """Return a request-local static-token estimator for one prepared team response."""
    return _StaticTokenEstimator(partial(_estimate_team_non_prompt_static_tokens, team))


def estimate_agent_static_tokens(agent: Agent, full_prompt: str) -> int:
    """Estimate the non-history agent prompt using Agno's real system-message builder."""
    return agent_static_token_estimator(agent).estimate(full_prompt)


def _estimate_agent_non_prompt_static_tokens(agent: Agent) -> int:
    """Estimate system-message and tool tokens that do not depend on the prompt text."""
    surface = _agent_prompt_tool_surface(agent)
    session, _run_response, run_context = _agent_prompt_estimation_inputs(agent)
    previous_tool_instructions = agent._tool_instructions
    agent._tool_instructions = list(surface.tool_instructions)
    try:
        system_message = agent.get_system_message(
            session=session,
            run_context=run_context,
            tools=cast("list[Function | dict]", list(surface.payloads)) or None,
            add_session_state_to_context=False,
        )
    finally:
        agent._tool_instructions = previous_tool_instructions
    static_tokens = 0
    if system_message is not None and system_message.content is not None:
        static_tokens += estimate_text_tokens(str(system_message.content))
    return static_tokens + surface.definition_tokens


def estimate_team_static_tokens(team: Team, full_prompt: str) -> int:
    """Estimate the non-history team prompt using Agno's team system-message builder."""
    return team_static_token_estimator(team).estimate(full_prompt)


def _estimate_team_non_prompt_static_tokens(team: Team) -> int:
    """Estimate team system-message and tool tokens that do not depend on prompt text."""
    surface = _team_prompt_tool_surface(team)
    session, _run_response, _run_context = _team_prompt_estimation_inputs(team)
    previous_tool_instructions = team._tool_instructions
    team._tool_instructions = list(surface.tool_instructions)
    try:
        system_message = team.get_system_message(
            session=session,
            tools=cast("list[Function | dict]", list(surface.payloads)) or None,
            add_session_state_to_context=False,
        )
    finally:
        team._tool_instructions = previous_tool_instructions
    static_tokens = 0
    if system_message is not None and system_message.content is not None:
        static_tokens += estimate_text_tokens(str(system_message.content))
    return static_tokens + surface.definition_tokens


def agent_tool_definition_payloads_for_logging(agent: Agent) -> list[dict[str, object]]:
    """Return model-visible agent tool schemas using Agno's prompt-preparation path."""
    return [deepcopy(payload) for payload in _agent_prompt_tool_surface(agent).payloads]


def team_tool_definition_payloads_for_logging(team: Team) -> list[dict[str, object]]:
    """Return model-visible team tool schemas using Agno's prompt-preparation path."""
    return [deepcopy(payload) for payload in _team_prompt_tool_surface(team).payloads]


def _cached_tool_surface(entity: Agent | Team) -> _PromptToolSurface | None:
    with _TOOL_SURFACE_CACHE_LOCK:
        entry = _TOOL_SURFACE_CACHE.get(id(entity))
    if entry is None:
        return None
    entity_ref, surface = entry
    if entity_ref() is not entity:
        return None
    return surface


def _store_tool_surface(entity: Agent | Team, surface: _PromptToolSurface) -> None:
    entity_key = id(entity)

    def _evict(entity_ref: ref[object]) -> None:
        with _TOOL_SURFACE_CACHE_LOCK:
            entry = _TOOL_SURFACE_CACHE.get(entity_key)
            if entry is not None and entry[0] is entity_ref:
                del _TOOL_SURFACE_CACHE[entity_key]

    with _TOOL_SURFACE_CACHE_LOCK:
        _TOOL_SURFACE_CACHE[entity_key] = (ref(entity, _evict), surface)


def _agent_prompt_tool_surface(agent: Agent) -> _PromptToolSurface:
    """Prepare (or reuse) the prompt-only tool surface for one agent instance.

    The estimator and run-metadata assembly only need model-visible schemas and
    tool instructions, not executable validate-call wrappers. Preparing those
    schemas here reuses cached Function schema metadata across fresh Agent
    instances.
    """
    cached_surface = _cached_tool_surface(agent)
    if cached_surface is not None:
        return cached_surface
    session, run_response, run_context = _agent_prompt_estimation_inputs(agent)
    with timed_block("system_prompt_assembly.history_prepare.static_token_estimate.agno_get_tools"):
        processed_tools = agent.get_tools(
            run_response=run_response,
            run_context=run_context,
            session=session,
            user_id=run_context.user_id,
        )
    with timed_block("system_prompt_assembly.history_prepare.static_token_estimate.tool_schema_prepare"):
        surface = _prompt_tool_surface_for_tools(processed_tools)
    _store_tool_surface(agent, surface)
    return surface


def _team_prompt_tool_surface(team: Team) -> _PromptToolSurface:
    """Prepare (or reuse) the prompt-only tool surface for one team instance.

    Agno exposes `Team.get_system_message()` publicly, but the exact prepared tool
    payload and `_tool_instructions` state that feed that prompt are only built by
    the internal `_determine_tools_for_model()` path. Using that single internal
    entrypoint is less brittle than re-implementing several private team helpers in
    MindRoom. This logic is verified against `agno==2.6.12`; if Agno changes those
    internals, update this estimator to match the new team prompt builder.
    """
    cached_surface = _cached_tool_surface(team)
    if cached_surface is not None:
        return cached_surface
    previous_tool_instructions = team._tool_instructions
    try:
        with timed_block("system_prompt_assembly.history_prepare.static_token_estimate.agno_determine_tools"):
            session, run_response, run_context = _team_prompt_estimation_inputs(team)
            model = team.model
            assert model is not None
            prepared_tools = _determine_tools_for_model(
                team=team,
                model=model,
                run_response=run_response,
                run_context=run_context,
                team_run_context={},
                session=session,
                check_mcp_tools=False,
            )
            payloads_by_name: dict[str, _ToolDefinition] = {}
            for tool in prepared_tools:
                if isinstance(tool, Function):
                    payload = _live_function_payload(tool)
                elif _is_tool_definition_dict(tool):
                    payload = _dict_tool_payload(tool)
                else:
                    continue
                tool_name = payload["name"]
                if isinstance(tool_name, str) and tool_name:
                    payloads_by_name[tool_name] = payload
            surface = _build_prompt_tool_surface(
                list(payloads_by_name.values()),
                list(team._tool_instructions or ()),
            )
    finally:
        team._tool_instructions = previous_tool_instructions
    _store_tool_surface(team, surface)
    return surface


def _build_prompt_tool_surface(
    payloads: list[_ToolDefinition],
    tool_instructions: list[str],
) -> _PromptToolSurface:
    definition_tokens = len(stable_serialize(payloads)) // 4 if payloads else 0
    return _PromptToolSurface(
        payloads=tuple(payloads),
        tool_instructions=tuple(tool_instructions),
        definition_tokens=definition_tokens,
    )


def _prompt_tool_surface_for_tools(tools: object) -> _PromptToolSurface:
    """Build the prompt-only tool surface for one agent's Agno tool list."""
    if not isinstance(tools, Sequence):
        return _build_prompt_tool_surface([], [])

    payloads: list[_ToolDefinition] = []
    tool_instructions: list[str] = []
    seen_names: set[str] = set()
    for tool in tools:
        for function, payload in _prompt_payload_candidates(tool):
            tool_name = payload["name"]
            if not isinstance(tool_name, str) or not tool_name or tool_name in seen_names:
                continue
            seen_names.add(tool_name)
            payloads.append(payload)
            if function is not None and function.add_instructions and function.instructions is not None:
                tool_instructions.append(function.instructions)

        if isinstance(tool, Toolkit) and tool.add_instructions and tool.instructions is not None:
            tool_instructions.append(tool.instructions)
    return _build_prompt_tool_surface(payloads, tool_instructions)


def _prompt_payload_candidates(tool: object) -> list[tuple[Function | None, _ToolDefinition]]:
    if isinstance(tool, Function):
        return [(tool, _processed_function_payload(tool))]
    if isinstance(tool, Toolkit):
        return [(function, _processed_function_payload(function)) for function in _toolkit_functions(tool).values()]
    if _is_tool_definition_dict(tool):
        return [(None, _dict_tool_payload(tool))]
    if callable(tool):
        function = Function.from_callable(tool)
        return [(function, _live_function_payload(function))]
    return []


def _toolkit_functions(toolkit: Toolkit) -> dict[str, Function]:
    functions = dict(toolkit.functions)
    if not functions:
        for raw_tool in toolkit.tools:
            if isinstance(raw_tool, Function):
                functions[raw_tool.name] = raw_tool
    for name, function in toolkit.async_functions.items():
        functions.setdefault(name, function)
    return functions


def _processed_function_payload(function: Function) -> _ToolDefinition:
    """Build one prompt-only payload with a processed schema, never mutating the input."""
    if function.skip_entrypoint_processing or function.entrypoint is None:
        return _live_function_payload(function)
    effective_strict = False if function.strict is None else function.strict
    snapshot = cached_processed_schema(function, strict=effective_strict)
    if snapshot is None:
        prepared_function = function.model_copy(deep=True)
        prepared_function.process_entrypoint(strict=effective_strict)
        return {
            "name": prepared_function.name,
            "description": prepared_function.description or "",
            "parameters": prepared_function.parameters or _default_function_parameters(),
        }
    return {
        "name": function.name,
        "description": snapshot.description or "",
        "parameters": snapshot.parameters or _default_function_parameters(),
    }


def _live_function_payload(function: Function) -> _ToolDefinition:
    """Build one prompt-only payload from a Function's current schema fields."""
    return {
        "name": function.name,
        "description": function.description or "",
        "parameters": deepcopy(function.parameters) if function.parameters else _default_function_parameters(),
    }


def _is_tool_definition_dict(tool: object) -> TypeGuard[_ToolDefinition]:
    if not isinstance(tool, dict):
        return False
    candidate_tool = cast("_ToolDefinition", tool)
    tool_name = candidate_tool.get("name")
    return isinstance(tool_name, str) and bool(tool_name)


def _dict_tool_payload(tool: _ToolDefinition) -> _ToolDefinition:
    parameters = tool.get("parameters")
    return {
        "name": str(tool["name"]),
        "description": str(tool.get("description", "")),
        "parameters": deepcopy(parameters) if isinstance(parameters, dict) else _default_function_parameters(),
    }


def _default_function_parameters() -> dict[str, object]:
    return {"type": "object", "properties": {}, "required": []}


def _team_prompt_estimation_inputs(team: Team) -> tuple[TeamSession, TeamRunOutput, RunContext]:
    budget_session_id = "history-budget"
    session = TeamSession(session_id=budget_session_id, team_id=team.id)
    run_response = TeamRunOutput(
        run_id=budget_session_id,
        team_id=team.id,
        session_id=budget_session_id,
        session_state={},
    )
    run_context = RunContext(
        run_id=budget_session_id,
        session_id=budget_session_id,
        session_state={},
    )
    return session, run_response, run_context


def _agent_prompt_estimation_inputs(agent: Agent) -> tuple[AgentSession, RunOutput, RunContext]:
    budget_session_id = "history-budget"
    budget_user_id = "history-budget-user"
    session = AgentSession(
        session_id=budget_session_id,
        agent_id=agent.id,
        user_id=budget_user_id,
    )
    run_response = RunOutput(
        run_id=budget_session_id,
        agent_id=agent.id,
        agent_name=agent.name,
        session_id=budget_session_id,
        user_id=budget_user_id,
        session_state={},
    )
    run_context = RunContext(
        run_id=budget_session_id,
        session_id=budget_session_id,
        user_id=budget_user_id,
        session_state={},
    )
    return session, run_response, run_context
