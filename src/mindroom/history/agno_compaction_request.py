"""Agno-specific provider-request preparation for history compaction.

MindRoom treats warm-cache compaction as a hidden normal turn: send the same
provider-visible prefix a reply would have used, then append one final user
message that asks for an updated durable summary.
That shape matters because provider prompt caches key off the real request
prefix, including system messages, prior conversation messages, model settings,
and tool schemas.

Agno does not currently expose a public "fork this session and build the exact
provider request without running it" primitive.
Calling ``agent.arun()`` or ``team.arun()`` against a temporary copied session
would look simpler, but it would run the full agent lifecycle: persistence,
hooks, memory/learning side effects, metrics, metadata writes, and potentially
real tool calls.
Calling ``model.aresponse()`` directly with only persisted history would avoid
those side effects, but it would drop the normal Agno-assembled system prompt,
session summary placement, and tool schemas, defeating the prompt-cache goal.

This module is the intentionally narrow adapter for that missing Agno forked
request primitive.
It builds a synthetic in-memory Agno session containing only the compaction
prefix, temporarily forces Agno to replay that prefix, asks Agno's private
message/tool builders for the provider-visible request, then returns only inert
message copies and provider tool schemas.
Executable ``Function`` objects are not passed to the summary model; when tool
schemas are present the request also sets ``tool_choice="none"``.

Keep private Agno imports and synthetic-session tricks contained here.
The durable compaction module should only see ``CompactionProviderRequest`` and
builder callables, and if Agno grows a public forked-request API this module is
the place to replace.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, TypeGuard, cast
from uuid import uuid4

from agno.agent._messages import aget_run_messages
from agno.agent._tools import determine_tools_for_model
from agno.run import RunContext
from agno.run.agent import RunInput, RunOutput
from agno.run.base import RunStatus
from agno.run.team import TeamRunInput, TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.team import TeamSession
from agno.team._messages import _aget_run_messages as aget_team_run_messages
from agno.team._tools import _determine_tools_for_model as determine_team_tools_for_model
from agno.tools import Toolkit
from agno.tools.function import Function

from mindroom.prepared_conversation_chain import CompactionSummaryRequest
from mindroom.token_budget import estimate_text_tokens, stable_serialize

if TYPE_CHECKING:
    from agno.agent import Agent
    from agno.models.message import Message
    from agno.team import Team


type _ToolDefinition = dict[str, object]
type _PreparedTool = Function | _ToolDefinition
type CompactionProviderRequestBuilder = Callable[
    [CompactionSummaryRequest, AgentSession | TeamSession],
    Awaitable["CompactionProviderRequest"],
]


@dataclass(frozen=True)
class CompactionProviderRequest:
    """Provider-visible request payload for one compaction summary call."""

    messages: tuple[Message, ...]
    tools: tuple[_ToolDefinition, ...] = ()
    tool_choice: str | dict[str, object] | None = None


async def build_agent_compaction_provider_request(
    summary_request: CompactionSummaryRequest,
    session: AgentSession | TeamSession,
    *,
    agent: Agent,
) -> CompactionProviderRequest:
    """Build compaction as a normal agent request prefix plus one summary user turn."""
    if not isinstance(session, AgentSession):
        msg = "agent compaction request builder requires an AgentSession"
        raise TypeError(msg)
    compaction_session = _agent_compaction_session(
        agent=agent,
        source_session=session,
        summary_request=summary_request,
    )
    final_user_message = summary_request.messages[-1].model_copy(deep=True)
    run_id = f"compaction-summary-{uuid4()}"
    user_id = compaction_session.user_id
    run_response = RunOutput(
        run_id=run_id,
        agent_id=agent.id,
        agent_name=agent.name,
        session_id=compaction_session.session_id,
        user_id=user_id,
        input=RunInput(input_content=final_user_message),
        session_state={},
    )
    run_context = RunContext(
        run_id=run_id,
        session_id=compaction_session.session_id,
        user_id=user_id,
        session_state={},
    )
    with _temporary_replay_settings(agent):
        model = agent.model
        assert model is not None
        processed_tools = await agent.aget_tools(
            run_response=run_response,
            run_context=run_context,
            session=compaction_session,
            user_id=user_id,
        )
        prepared_tools = determine_tools_for_model(
            agent=agent,
            model=model,
            processed_tools=processed_tools,
            run_response=run_response,
            run_context=run_context,
            session=compaction_session,
            async_mode=True,
        )
        run_messages = await aget_run_messages(
            agent,
            run_response=run_response,
            run_context=run_context,
            input=final_user_message,
            session=compaction_session,
            user_id=user_id,
            add_history_to_context=True,
            add_dependencies_to_context=False,
            add_session_state_to_context=False,
            tools=prepared_tools,
        )
    return _provider_request(run_messages.messages, prepared_tools)


async def build_team_compaction_provider_request(
    summary_request: CompactionSummaryRequest,
    session: AgentSession | TeamSession,
    *,
    team: Team,
) -> CompactionProviderRequest:
    """Build compaction as a normal team request prefix plus one summary user turn."""
    if not isinstance(session, TeamSession):
        msg = "team compaction request builder requires a TeamSession"
        raise TypeError(msg)
    compaction_session = _team_compaction_session(
        team=team,
        source_session=session,
        summary_request=summary_request,
    )
    final_user_message = summary_request.messages[-1].model_copy(deep=True)
    run_id = f"compaction-summary-{uuid4()}"
    user_id = compaction_session.user_id
    run_response = TeamRunOutput(
        run_id=run_id,
        team_id=team.id,
        team_name=team.name,
        session_id=compaction_session.session_id,
        user_id=user_id,
        input=TeamRunInput(input_content=final_user_message),
        session_state={},
    )
    run_context = RunContext(
        run_id=run_id,
        session_id=compaction_session.session_id,
        user_id=user_id,
        session_state={},
    )
    with _temporary_replay_settings(team):
        model = team.model
        assert model is not None
        prepared_tools = determine_team_tools_for_model(
            team=team,
            model=model,
            run_response=run_response,
            run_context=run_context,
            team_run_context={},
            session=compaction_session,
            user_id=user_id,
            async_mode=True,
            input_message=final_user_message,
            add_history_to_context=True,
            add_dependencies_to_context=False,
            add_session_state_to_context=False,
            check_mcp_tools=False,
        )
        run_messages = await aget_team_run_messages(
            team,
            run_response=run_response,
            run_context=run_context,
            session=compaction_session,
            user_id=user_id,
            input_message=final_user_message,
            add_history_to_context=True,
            add_dependencies_to_context=False,
            add_session_state_to_context=False,
            tools=prepared_tools,
        )
    return _provider_request(run_messages.messages, prepared_tools)


def estimate_static_tokens(agent: Agent, full_prompt: str) -> int:
    """Estimate system and current-user prompt tokens outside persisted replay."""
    static_chars = len(agent.role or "")
    instructions = agent.instructions
    if isinstance(instructions, str):
        static_chars += len(instructions)
    elif isinstance(instructions, list):
        for instruction in instructions:
            static_chars += len(str(instruction))
    static_chars += len(full_prompt)
    return (static_chars // 4) + estimate_tool_definition_tokens(agent)


def estimate_agent_static_tokens(agent: Agent, full_prompt: str) -> int:
    """Estimate the non-history agent prompt using Agno's real system-message builder."""
    static_tokens = estimate_text_tokens(full_prompt)
    with _temporary_tool_instructions(agent):
        session, run_context, prepared_tools = _prepare_agent_prompt_inputs_for_estimation(agent)
        system_message = agent.get_system_message(
            session=session,
            run_context=run_context,
            tools=prepared_tools or None,
            add_session_state_to_context=False,
        )
    if system_message is not None and system_message.content is not None:
        static_tokens += estimate_text_tokens(str(system_message.content))
    return static_tokens + _estimate_prepared_tool_definition_tokens(prepared_tools)


def estimate_tool_definition_tokens(agent: Agent) -> int:
    """Estimate the model-visible tool schema and tool instructions for one agent."""
    prepared_tools, tool_instructions = _prepare_tools_for_estimation(agent.tools)
    return _estimate_prepared_tool_definition_tokens(
        prepared_tools,
        tool_instructions=tool_instructions,
    )


def estimate_team_static_tokens(team: Team, full_prompt: str) -> int:
    """Estimate the non-history team prompt using Agno's team system-message builder."""
    static_tokens = estimate_text_tokens(full_prompt)
    with _temporary_tool_instructions(team):
        session, prepared_tools = _prepare_team_prompt_inputs_for_estimation(team)
        system_message = team.get_system_message(
            session=session,
            tools=prepared_tools or None,
            add_session_state_to_context=False,
        )
    if system_message is not None and system_message.content is not None:
        static_tokens += estimate_text_tokens(str(system_message.content))
    return static_tokens + _estimate_prepared_tool_definition_tokens(prepared_tools)


def agent_tool_definition_payloads_for_logging(agent: Agent) -> list[dict[str, object]]:
    """Return model-visible agent tool schemas using Agno's prompt-preparation path."""
    with _temporary_tool_instructions(agent):
        _session, _run_context, prepared_tools = _prepare_agent_prompt_inputs_for_estimation(agent)
    return _prepared_tool_definition_payloads(prepared_tools)


def team_tool_definition_payloads_for_logging(team: Team) -> list[dict[str, object]]:
    """Return model-visible team tool schemas using Agno's prompt-preparation path."""
    with _temporary_tool_instructions(team):
        _session, prepared_tools = _prepare_team_prompt_inputs_for_estimation(team)
    return _prepared_tool_definition_payloads(prepared_tools)


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
        tool_tokens = estimate_tool_definition_tokens(agent)
    elif team is not None:
        prepared_tools, _tool_instructions = _prepare_tools_for_estimation(team.tools)
        tool_tokens = _estimate_prepared_tool_definition_tokens(prepared_tools)
    breakdown["tool_definition_tokens"] = tool_tokens

    if full_prompt is not None:
        breakdown["current_prompt_tokens"] = len(full_prompt) // 4

    return breakdown


def _provider_request(
    messages: Sequence[Message],
    prepared_tools: Sequence[object],
) -> CompactionProviderRequest:
    tool_schemas = _provider_tool_schemas(prepared_tools)
    return CompactionProviderRequest(
        messages=tuple(message.model_copy(deep=True) for message in messages),
        tools=tuple(_provider_tool_definition_payloads(tool_schemas)),
        tool_choice="none" if tool_schemas else None,
    )


def _provider_tool_schemas(prepared_tools: Sequence[object]) -> list[_PreparedTool]:
    return [tool for tool in prepared_tools if isinstance(tool, Function) or _is_tool_definition_dict(tool)]


def _agent_compaction_session(
    *,
    agent: Agent,
    source_session: AgentSession,
    summary_request: CompactionSummaryRequest,
) -> AgentSession:
    return replace(
        source_session,
        agent_id=agent.id,
        session_data=deepcopy(source_session.session_data),
        metadata=deepcopy(source_session.metadata),
        agent_data=deepcopy(source_session.agent_data),
        runs=[
            RunOutput(
                run_id=_synthetic_compaction_run_id(summary_request),
                agent_id=agent.id,
                status=RunStatus.completed,
                messages=[message.model_copy(deep=True) for message in summary_request.chain.messages],
            ),
        ],
        summary=deepcopy(source_session.summary),
    )


def _team_compaction_session(
    *,
    team: Team,
    source_session: TeamSession,
    summary_request: CompactionSummaryRequest,
) -> TeamSession:
    return replace(
        source_session,
        team_id=team.id,
        team_data=deepcopy(source_session.team_data),
        session_data=deepcopy(source_session.session_data),
        metadata=deepcopy(source_session.metadata),
        runs=[
            TeamRunOutput(
                run_id=_synthetic_compaction_run_id(summary_request),
                team_id=team.id,
                status=RunStatus.completed,
                messages=[message.model_copy(deep=True) for message in summary_request.chain.messages],
            ),
        ],
        summary=deepcopy(source_session.summary),
    )


def _synthetic_compaction_run_id(summary_request: CompactionSummaryRequest) -> str:
    if summary_request.included_run_ids:
        return "+".join(summary_request.included_run_ids)
    return "compaction-summary-prefix"


@contextmanager
def _temporary_replay_settings(entity: Agent | Team) -> Iterator[None]:
    add_history_to_context = entity.add_history_to_context
    num_history_runs = entity.num_history_runs
    num_history_messages = entity.num_history_messages
    max_tool_calls_from_history = entity.max_tool_calls_from_history
    entity.add_history_to_context = True
    entity.num_history_runs = None
    entity.num_history_messages = None
    entity.max_tool_calls_from_history = None
    try:
        yield
    finally:
        entity.add_history_to_context = add_history_to_context
        entity.num_history_runs = num_history_runs
        entity.num_history_messages = num_history_messages
        entity.max_tool_calls_from_history = max_tool_calls_from_history


@contextmanager
def _temporary_tool_instructions(entity: Agent | Team) -> Iterator[None]:
    previous_tool_instructions = entity._tool_instructions
    try:
        yield
    finally:
        entity._tool_instructions = previous_tool_instructions


def _estimate_prepared_tool_definition_tokens(
    prepared_tools: Sequence[_PreparedTool],
    *,
    tool_instructions: Sequence[str] = (),
) -> int:
    tool_definitions = _prepared_tool_definition_payloads(prepared_tools)
    tool_definition_tokens = len(stable_serialize(tool_definitions)) // 4 if tool_definitions else 0
    instruction_tokens = sum(estimate_text_tokens(instruction) for instruction in tool_instructions)
    return tool_definition_tokens + instruction_tokens


def _prepare_tools_for_estimation(tools: object) -> tuple[list[_PreparedTool], list[str]]:
    if not isinstance(tools, Sequence):
        return [], []

    prepared_tools: list[_PreparedTool] = []
    tool_instructions: list[str] = []
    seen_names: set[str] = set()
    for tool in tools:
        for prepared_tool in _prepare_tool_for_estimation(tool):
            tool_name = _prepared_tool_name(prepared_tool)
            if tool_name is None or tool_name in seen_names:
                continue
            seen_names.add(tool_name)
            prepared_tools.append(prepared_tool)

        if isinstance(tool, Toolkit) and tool.add_instructions and tool.instructions is not None:
            tool_instructions.append(tool.instructions)
        if isinstance(tool, Function) and tool.add_instructions and tool.instructions is not None:
            tool_instructions.append(tool.instructions)
    return prepared_tools, tool_instructions


def _prepare_tool_for_estimation(tool: object) -> list[_PreparedTool]:
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
        prepared_function.process_entrypoint(strict=effective_strict)
    return prepared_function


def _prepared_tool_definition_payloads(
    prepared_tools: Sequence[_PreparedTool],
) -> list[dict[str, object]]:
    payloads_by_name: dict[str, dict[str, object]] = {}
    for tool in prepared_tools:
        payload = _function_payload(tool) if isinstance(tool, Function) else _dict_tool_payload(tool)
        tool_name = payload.get("name")
        if isinstance(tool_name, str) and tool_name:
            payloads_by_name[tool_name] = payload
    return list(payloads_by_name.values())


def _provider_tool_definition_payloads(
    prepared_tools: Sequence[_PreparedTool],
) -> list[dict[str, object]]:
    payloads_by_name: dict[str, dict[str, object]] = {}
    for tool in prepared_tools:
        payload = _function_provider_payload(tool) if isinstance(tool, Function) else deepcopy(tool)
        tool_name = _provider_payload_name(payload)
        if tool_name:
            payloads_by_name[tool_name] = payload
    return list(payloads_by_name.values())


def _function_provider_payload(function: Function) -> dict[str, object]:
    return {"type": "function", "function": function.to_dict()}


def _provider_payload_name(payload: dict[str, object]) -> str | None:
    function_payload = payload.get("function")
    if isinstance(function_payload, dict):
        function_definition = cast("dict[str, object]", function_payload)
        tool_name = function_definition.get("name")
        if isinstance(tool_name, str) and tool_name:
            return tool_name
    tool_name = payload.get("name")
    if isinstance(tool_name, str) and tool_name:
        return tool_name
    return None


def _prepared_tool_name(tool: _PreparedTool) -> str | None:
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


def _prepare_team_prompt_inputs_for_estimation(
    team: Team,
) -> tuple[TeamSession, list[_PreparedTool]]:
    """Reuse Agno's own team tool-preparation path for prompt budgeting.

    Agno exposes `Team.get_system_message()` publicly, but the exact prepared tool
    payload and `_tool_instructions` state that feed that prompt are only built by
    the internal `_determine_tools_for_model()` path. Using that single internal
    entrypoint is less brittle than re-implementing several private team helpers in
    MindRoom. This logic is verified against `agno==2.5.13`; if Agno changes those
    internals, update this estimator to match the new team prompt builder.
    """
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
    model = team.model
    assert model is not None
    prepared_tools = determine_team_tools_for_model(
        team=team,
        model=model,
        run_response=run_response,
        run_context=run_context,
        team_run_context={},
        session=session,
        check_mcp_tools=False,
    )
    return session, _provider_tool_schemas(prepared_tools)


def _prepare_agent_prompt_inputs_for_estimation(
    agent: Agent,
) -> tuple[AgentSession, RunContext, list[_PreparedTool]]:
    """Reuse Agno's agent tool-preparation path for prompt budgeting.

    Agno exposes `Agent.get_system_message()` publicly, but the prepared tool
    payload and `_tool_instructions` that feed that prompt are only finalized by
    the shared `agno.agent._tools.determine_tools_for_model()` path. Using that
    single internal entrypoint keeps MindRoom aligned with Agno without
    re-implementing several private agent helpers.
    """
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
    model = agent.model
    assert model is not None
    processed_tools = agent.get_tools(
        run_response=run_response,
        run_context=run_context,
        session=session,
        user_id=budget_user_id,
    )
    prepared_tools = determine_tools_for_model(
        agent=agent,
        model=model,
        processed_tools=processed_tools,
        run_response=run_response,
        run_context=run_context,
        session=session,
        async_mode=False,
    )
    return session, run_context, _provider_tool_schemas(prepared_tools)
