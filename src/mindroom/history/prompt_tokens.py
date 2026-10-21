"""Static prompt-token estimation: what the model sees before any history replay.

Estimates the non-history cost of one prepared request — system message, tool schemas, tool instructions, and the
current prompt text. ``build_static_token_estimator`` sizes an owned agent build's rendered system message and tool
definitions; ``team_static_token_estimator`` sizes a live Agno team through Agno's own prompt-preparation paths.
Nothing here touches history scopes, sessions, or summaries; compaction consumes these numbers through the resolved
execution plan.

The prompt-only tool surface (model-visible payloads plus tool instructions) of an Agno team is prepared once per
team instance and reused by static token budgeting within that turn. Team instances are rebuilt per response, so the
surface never outlives one turn's tool state.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from functools import partial
from threading import RLock
from typing import TYPE_CHECKING, TypeGuard, cast
from weakref import ref

from agno.run import RunContext
from agno.run.team import TeamRunOutput
from agno.session.team import TeamSession
from agno.tools.function import Function

from mindroom.agno_compat_prepared_tools import prepare_team_prompt_tools, temporary_tool_instructions
from mindroom.timing import timed_block
from mindroom.token_budget import estimate_text_tokens, stable_serialize
from mindroom.tool_system.tool_schema_cache import default_function_parameters, live_function_definition

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from agno.team import Team

    from mindroom.llm.messages import Message
    from mindroom.llm.provider import ToolSpec

type _ToolDefinition = dict[str, object]


@dataclass(frozen=True)
class _PromptToolSurface:
    """Prompt-only tool surface prepared once per team instance.

    ``payloads`` are canonical copies owned by the cache; treat them as
    immutable and hand out deep copies at public boundaries.
    """

    payloads: tuple[_ToolDefinition, ...]
    tool_instructions: tuple[str, ...]
    definition_tokens: int


# Keyed by team id because Agno Team instances are unhashable; each entry
# holds a weakref whose eviction callback removes the entry on GC.
_TOOL_SURFACE_CACHE: dict[int, tuple[ref[object], _PromptToolSurface]] = {}
# Allocations under the lock can trigger GC and re-enter it through _evict.
_TOOL_SURFACE_CACHE_LOCK = RLock()


@dataclass(slots=True)
class _StaticTokenEstimator:
    """Request-local static-token estimator that caches the non-prompt cost once."""

    non_prompt_tokens_fn: Callable[[], int]
    _non_prompt_tokens: int | None = field(default=None, init=False)

    def non_prompt_tokens(self) -> int:
        """Estimate the system message and tool tokens, which do not depend on the prompt text."""
        if self._non_prompt_tokens is None:
            self._non_prompt_tokens = self.non_prompt_tokens_fn()
        return self._non_prompt_tokens

    def estimate(self, full_prompt: str) -> int:
        """Estimate the prompt text's tokens plus the non-prompt cost, which is computed once."""
        return estimate_text_tokens(full_prompt) + self.non_prompt_tokens()


def build_static_token_estimator(
    system_message: Message,
    tools: Sequence[ToolSpec],
    deferred_tool_names: frozenset[str],
) -> _StaticTokenEstimator:
    """Return a request-local static-token estimator for one owned agent build's rendered prompt and tools.

    Deferred tools stay off the request until tool search loads them, so they are left out of the estimate.
    """

    def non_prompt_tokens() -> int:
        payloads = [
            {key: value for key, value in asdict(tool).items() if value is not None}
            for tool in tools
            if tool.name not in deferred_tool_names
        ]
        return estimate_text_tokens(str(system_message.content or "")) + _definition_tokens(payloads)

    return _StaticTokenEstimator(non_prompt_tokens)


def team_static_token_estimator(team: Team) -> _StaticTokenEstimator:
    """Return a request-local static-token estimator for one prepared team response."""
    return _StaticTokenEstimator(partial(_estimate_team_non_prompt_static_tokens, team))


def _estimate_team_non_prompt_static_tokens(team: Team) -> int:
    """Estimate team system-message and tool tokens that do not depend on prompt text."""
    surface = _team_prompt_tool_surface(team)
    session, _run_response, _run_context = _team_prompt_estimation_inputs(team)
    with temporary_tool_instructions(team, surface.tool_instructions):
        system_message = team.get_system_message(
            session=session,
            tools=cast("list[Function | dict]", list(surface.payloads)) or None,
            add_session_state_to_context=False,
        )
    static_tokens = 0
    if system_message is not None and system_message.content is not None:
        static_tokens += estimate_text_tokens(str(system_message.content))
    return static_tokens + surface.definition_tokens


def _cached_tool_surface(entity: Team) -> _PromptToolSurface | None:
    with _TOOL_SURFACE_CACHE_LOCK:
        entry = _TOOL_SURFACE_CACHE.get(id(entity))
    if entry is None:
        return None
    entity_ref, surface = entry
    if entity_ref() is not entity:
        return None
    return surface


def _store_tool_surface(entity: Team, surface: _PromptToolSurface) -> None:
    entity_key = id(entity)

    def _evict(entity_ref: ref[object]) -> None:
        with _TOOL_SURFACE_CACHE_LOCK:
            entry = _TOOL_SURFACE_CACHE.get(entity_key)
            if entry is not None and entry[0] is entity_ref:
                del _TOOL_SURFACE_CACHE[entity_key]

    with _TOOL_SURFACE_CACHE_LOCK:
        _TOOL_SURFACE_CACHE[entity_key] = (ref(entity, _evict), surface)


def _team_prompt_tool_surface(team: Team) -> _PromptToolSurface:
    """Prepare (or reuse) the SDK's prompt-only tool surface for one team instance."""
    cached_surface = _cached_tool_surface(team)
    if cached_surface is not None:
        return cached_surface
    with timed_block("system_prompt_assembly.history_prepare.static_token_estimate.agno_determine_tools"):
        session, run_response, run_context = _team_prompt_estimation_inputs(team)
        prepared = prepare_team_prompt_tools(
            team,
            session=session,
            run_response=run_response,
            run_context=run_context,
        )
        payloads_by_name: dict[str, _ToolDefinition] = {}
        for tool in prepared.tools:
            if isinstance(tool, Function):
                payload = live_function_definition(tool)
            elif _is_tool_definition_dict(tool):
                payload = _dict_tool_payload(tool)
            else:
                continue
            tool_name = payload["name"]
            if isinstance(tool_name, str) and tool_name:
                payloads_by_name[tool_name] = payload
        surface = _build_prompt_tool_surface(
            list(payloads_by_name.values()),
            list(prepared.tool_instructions),
        )
    _store_tool_surface(team, surface)
    return surface


def _definition_tokens(payloads: Sequence[_ToolDefinition]) -> int:
    return len(stable_serialize(list(payloads))) // 4 if payloads else 0


def _build_prompt_tool_surface(
    payloads: list[_ToolDefinition],
    tool_instructions: list[str],
) -> _PromptToolSurface:
    return _PromptToolSurface(
        payloads=tuple(payloads),
        tool_instructions=tuple(tool_instructions),
        definition_tokens=_definition_tokens(payloads),
    )


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
        "parameters": deepcopy(parameters) if isinstance(parameters, dict) else default_function_parameters(),
    }


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
