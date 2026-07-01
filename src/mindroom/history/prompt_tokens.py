"""Static prompt-token estimation: what the model sees before any history replay.

Estimates the non-history cost of one prepared agent or team request — system
message, tool schemas, tool instructions, and the current prompt text — by
reusing Agno's own prompt-preparation paths. Nothing here touches history
scopes, sessions, or summaries; compaction consumes these numbers through the
resolved execution plan.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, TypeGuard, cast

from agno.run import RunContext
from agno.run.agent import RunOutput
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.team import TeamSession
from agno.team._tools import _determine_tools_for_model
from agno.tools import Toolkit
from agno.tools.function import Function

from mindroom.timing import timed, timed_block
from mindroom.token_budget import estimate_text_tokens, stable_serialize
from mindroom.tool_schema_cache import process_function_schema_for_prompt

if TYPE_CHECKING:
    from agno.agent import Agent
    from agno.team import Team

type _ToolDefinition = dict[str, object]


@dataclass(slots=True)
class StaticTokenEstimator:
    """Request-local static-token estimator that caches the non-prompt cost once."""

    non_prompt_tokens_fn: Callable[[], int]
    _non_prompt_tokens: int | None = field(default=None, init=False)

    def estimate(self, full_prompt: str) -> int:
        """Estimate static prompt tokens while reusing Agno-prepared tools."""
        if self._non_prompt_tokens is None:
            self._non_prompt_tokens = self.non_prompt_tokens_fn()
        return estimate_text_tokens(full_prompt) + self._non_prompt_tokens


def agent_static_token_estimator(agent: Agent) -> StaticTokenEstimator:
    """Return a request-local static-token estimator for one prepared agent response."""
    return StaticTokenEstimator(partial(_estimate_agent_non_prompt_static_tokens, agent))


def team_static_token_estimator(team: Team) -> StaticTokenEstimator:
    """Return a request-local static-token estimator for one prepared team response."""
    return StaticTokenEstimator(partial(_estimate_team_non_prompt_static_tokens, team))


def estimate_agent_static_tokens(agent: Agent, full_prompt: str) -> int:
    """Estimate the non-history agent prompt using Agno's real system-message builder."""
    return agent_static_token_estimator(agent).estimate(full_prompt)


def _estimate_agent_non_prompt_static_tokens(agent: Agent) -> int:
    """Estimate system-message and tool tokens that do not depend on the prompt text."""
    static_tokens = 0
    previous_tool_instructions = agent._tool_instructions
    try:
        session, run_context, prepared_tools = _prepare_agent_prompt_inputs_for_estimation(agent)
        system_message = agent.get_system_message(
            session=session,
            run_context=run_context,
            tools=prepared_tools or None,
            add_session_state_to_context=False,
        )
    finally:
        agent._tool_instructions = previous_tool_instructions
    if system_message is not None and system_message.content is not None:
        static_tokens += estimate_text_tokens(str(system_message.content))
    return static_tokens + _estimate_prepared_tool_definition_tokens(prepared_tools)


def _estimate_tool_definition_tokens(agent: Agent) -> int:
    """Estimate the model-visible tool schema and tool instructions for one agent."""
    prepared_tools, tool_instructions = _prepare_tools_for_estimation(agent.tools)
    return _estimate_prepared_tool_definition_tokens(
        prepared_tools,
        tool_instructions=tool_instructions,
    )


def estimate_team_static_tokens(team: Team, full_prompt: str) -> int:
    """Estimate the non-history team prompt using Agno's team system-message builder."""
    return team_static_token_estimator(team).estimate(full_prompt)


def _estimate_team_non_prompt_static_tokens(team: Team) -> int:
    """Estimate team system-message and tool tokens that do not depend on prompt text."""
    static_tokens = 0
    previous_tool_instructions = team._tool_instructions
    try:
        session, prepared_tools = _prepare_team_prompt_inputs_for_estimation(team)
        system_message = team.get_system_message(
            session=session,
            tools=prepared_tools or None,
            add_session_state_to_context=False,
        )
    finally:
        team._tool_instructions = previous_tool_instructions
    if system_message is not None and system_message.content is not None:
        static_tokens += estimate_text_tokens(str(system_message.content))
    return static_tokens + _estimate_prepared_tool_definition_tokens(prepared_tools)


def agent_tool_definition_payloads_for_logging(agent: Agent) -> list[dict[str, object]]:
    """Return model-visible agent tool schemas using Agno's prompt-preparation path."""
    previous_tool_instructions = agent._tool_instructions
    try:
        _session, _run_context, prepared_tools = _prepare_agent_prompt_inputs_for_estimation(agent)
    finally:
        agent._tool_instructions = previous_tool_instructions
    return _prepared_tool_definition_payloads(prepared_tools)


def team_tool_definition_payloads_for_logging(team: Team) -> list[dict[str, object]]:
    """Return model-visible team tool schemas using Agno's prompt-preparation path."""
    previous_tool_instructions = team._tool_instructions
    try:
        _session, prepared_tools = _prepare_team_prompt_inputs_for_estimation(team)
    finally:
        team._tool_instructions = previous_tool_instructions
    return _prepared_tool_definition_payloads(prepared_tools)


def _estimate_prepared_tool_definition_tokens(
    prepared_tools: Sequence[Function | dict[str, object]],
    *,
    tool_instructions: Sequence[str] = (),
) -> int:
    tool_definitions = _prepared_tool_definition_payloads(prepared_tools)
    tool_definition_tokens = len(stable_serialize(tool_definitions)) // 4 if tool_definitions else 0
    instruction_tokens = sum(estimate_text_tokens(instruction) for instruction in tool_instructions)
    return tool_definition_tokens + instruction_tokens


def _prepare_tools_for_estimation(tools: object) -> tuple[list[Function | _ToolDefinition], list[str]]:
    if not isinstance(tools, Sequence):
        return [], []

    prepared_tools: list[Function | _ToolDefinition] = []
    tool_instructions: list[str] = []
    seen_names: set[str] = set()
    for tool in tools:
        for prepared_tool in _prepare_tool_for_estimation(tool):
            tool_name = _prepared_tool_name(prepared_tool)
            if tool_name is None or tool_name in seen_names:
                continue
            seen_names.add(tool_name)
            prepared_tools.append(prepared_tool)
            if (
                isinstance(prepared_tool, Function)
                and prepared_tool.add_instructions
                and prepared_tool.instructions is not None
            ):
                tool_instructions.append(prepared_tool.instructions)

        if isinstance(tool, Toolkit) and tool.add_instructions and tool.instructions is not None:
            tool_instructions.append(tool.instructions)
    return prepared_tools, tool_instructions


def _prepare_tool_for_estimation(tool: object) -> list[Function | _ToolDefinition]:
    if isinstance(tool, Function):
        return [_prepare_function_for_estimation(tool)]
    if isinstance(tool, Toolkit):
        return [_prepare_function_for_estimation(function) for function in _toolkit_functions(tool).values()]
    if _is_tool_definition_dict(tool):
        return [tool]
    if callable(tool):
        return [Function.from_callable(tool)]
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


def _prepare_function_for_estimation(function: Function) -> Function:
    prepared_function = function.model_copy(deep=True)
    if not prepared_function.skip_entrypoint_processing and prepared_function.entrypoint is not None:
        effective_strict = False if prepared_function.strict is None else prepared_function.strict
        process_function_schema_for_prompt(prepared_function, strict=effective_strict)
    return prepared_function


def _prepared_tool_definition_payloads(
    prepared_tools: Sequence[Function | _ToolDefinition],
) -> list[dict[str, object]]:
    payloads_by_name: dict[str, dict[str, object]] = {}
    for tool in prepared_tools:
        payload = _function_payload(tool) if isinstance(tool, Function) else _dict_tool_payload(tool)
        tool_name = payload.get("name")
        if isinstance(tool_name, str) and tool_name:
            payloads_by_name[tool_name] = payload
    return list(payloads_by_name.values())


def _prepared_tool_name(tool: Function | _ToolDefinition) -> str | None:
    if isinstance(tool, Function):
        return tool.name
    tool_name = tool.get("name")
    if isinstance(tool_name, str) and tool_name:
        return tool_name
    return None


def _function_payload(function: Function) -> dict[str, object]:
    return {
        "name": function.name,
        "description": function.description or "",
        "parameters": function.parameters or _default_function_parameters(),
    }


def _is_tool_definition_dict(tool: object) -> TypeGuard[_ToolDefinition]:
    if not isinstance(tool, dict):
        return False
    candidate_tool = cast("_ToolDefinition", tool)
    tool_name = candidate_tool.get("name")
    return isinstance(tool_name, str) and bool(tool_name)


def _dict_tool_payload(tool: _ToolDefinition) -> dict[str, object]:
    parameters = tool.get("parameters")
    return {
        "name": str(tool["name"]),
        "description": str(tool.get("description", "")),
        "parameters": parameters if isinstance(parameters, dict) else _default_function_parameters(),
    }


def _default_function_parameters() -> dict[str, object]:
    return {"type": "object", "properties": {}, "required": []}


@timed("system_prompt_assembly.history_prepare.static_token_estimate.agno_determine_tools")
def _prepare_team_prompt_inputs_for_estimation(
    team: Team,
) -> tuple[TeamSession, list[Function | _ToolDefinition]]:
    """Reuse Agno's own team tool-preparation path for prompt budgeting.

    Agno exposes `Team.get_system_message()` publicly, but the exact prepared tool
    payload and `_tool_instructions` state that feed that prompt are only built by
    the internal `_determine_tools_for_model()` path. Using that single internal
    entrypoint is less brittle than re-implementing several private team helpers in
    MindRoom. This logic is verified against `agno==2.5.13`; if Agno changes those
    internals, update this estimator to match the new team prompt builder.
    """
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
    return session, [tool for tool in prepared_tools if isinstance(tool, Function) or _is_tool_definition_dict(tool)]


@timed("system_prompt_assembly.history_prepare.static_token_estimate.tool_schema_prepare")
def _prepare_agent_prompt_inputs_for_estimation(
    agent: Agent,
) -> tuple[AgentSession, RunContext, list[Function | _ToolDefinition]]:
    """Reuse Agno's agent tool-preparation path for prompt budgeting.

    The estimator only needs model-visible schemas and tool instructions, not
    executable validate-call wrappers. Preparing those schemas here lets us reuse
    cached Function schema metadata across fresh Agent instances.
    """
    session, run_response, run_context = _agent_prompt_estimation_inputs(agent)
    with timed_block("system_prompt_assembly.history_prepare.static_token_estimate.agno_get_tools"):
        processed_tools = agent.get_tools(
            run_response=run_response,
            run_context=run_context,
            session=session,
            user_id=run_context.user_id,
        )
    with timed_block("system_prompt_assembly.history_prepare.static_token_estimate.tool_schema_prepare"):
        prepared_tools, tool_instructions = _prepare_tools_for_estimation(processed_tools)
    agent._tool_instructions = list(tool_instructions)
    return session, run_context, prepared_tools


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


def compute_prompt_token_breakdown(
    agent: Agent | None = None,
    team: Team | None = None,
    full_prompt: str | None = None,
) -> dict[str, int]:
    """Compute token breakdown for system prompt, tool defs, and current prompt."""
    breakdown: dict[str, int] = {}

    if agent is not None:
        sys_chars = len(agent.role or "")
        instructions = agent.instructions
        if isinstance(instructions, str):
            sys_chars += len(instructions)
        elif isinstance(instructions, list):
            for instruction in instructions:
                sys_chars += len(str(instruction))
        breakdown["role_instructions_tokens"] = sys_chars // 4

    tool_tokens = 0
    if agent is not None:
        tool_tokens = _estimate_tool_definition_tokens(agent)
    elif team is not None:
        prepared_tools, _tool_instructions = _prepare_tools_for_estimation(team.tools)
        tool_tokens = _estimate_prepared_tool_definition_tokens(prepared_tools)
    breakdown["tool_definition_tokens"] = tool_tokens

    if full_prompt is not None:
        breakdown["current_prompt_tokens"] = len(full_prompt) // 4

    return breakdown
