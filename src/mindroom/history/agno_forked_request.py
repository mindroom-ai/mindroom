"""Build forked Agno provider requests without running a full Agno turn.

Some callers need the exact provider-visible request that Agno would assemble
for an Agent or Team, but against a copied message prefix and without creating a
real Agno run.
This is especially useful for prompt-cache-sensitive internal turns: the caller
can preserve the same system prompt, session summary placement, message format,
model settings, and tool schemas, then append its own final user message.

Agno does not currently expose a public "fork this session and build the exact
provider request without running it" primitive.
Calling ``agent.arun()`` or ``team.arun()`` against a temporary copied session
would look simpler, but it would run the full agent lifecycle: persistence,
hooks, memory/learning side effects, metrics, metadata writes, and potentially
real tool calls.
Calling ``model.aresponse()`` directly with copied messages would avoid those
side effects, but it would also bypass Agno's normal request assembly.

This module is the intentionally narrow adapter for that missing Agno primitive.
It builds a synthetic in-memory Agno session containing only the caller-provided
prefix, temporarily forces Agno to replay that prefix, asks Agno's private
message/tool builders for the provider-visible request, then returns inert
message copies and provider tool schemas.
Executable ``Function`` objects are not passed to the summary model; when tool
schemas are present the request also sets ``tool_choice="none"``.

Keep private Agno imports and synthetic-session tricks contained here.
This module deliberately has no ``mindroom.*`` imports; callers adapt their
domain objects into plain Agno sessions and messages before calling it.
If Agno grows a public forked-request API, this module is the place to replace.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, TypeGuard, cast

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

if TYPE_CHECKING:
    from agno.agent import Agent
    from agno.models.message import Message
    from agno.team import Team


type ToolDefinition = dict[str, object]
type PreparedTool = Function | ToolDefinition


@dataclass(frozen=True)
class AgnoProviderRequest:
    """Provider-visible request payload assembled by Agno."""

    messages: tuple[Message, ...]
    tools: tuple[ToolDefinition, ...] = ()
    tool_choice: str | dict[str, object] | None = None


async def build_agent_provider_request_from_prefix(
    *,
    agent: Agent,
    source_session: AgentSession,
    prefix_messages: Sequence[Message],
    final_user_message: Message,
    synthetic_run_id: str,
) -> AgnoProviderRequest:
    """Build an agent request from copied history plus one final user message."""
    compaction_session = _agent_synthetic_session(
        agent=agent,
        source_session=source_session,
        prefix_messages=prefix_messages,
        synthetic_run_id=synthetic_run_id,
    )
    final_user_message = final_user_message.model_copy(deep=True)
    run_id = synthetic_run_id
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


async def build_team_provider_request_from_prefix(
    *,
    team: Team,
    source_session: TeamSession,
    prefix_messages: Sequence[Message],
    final_user_message: Message,
    synthetic_run_id: str,
) -> AgnoProviderRequest:
    """Build a team request from copied history plus one final user message."""
    compaction_session = _team_synthetic_session(
        team=team,
        source_session=source_session,
        prefix_messages=prefix_messages,
        synthetic_run_id=synthetic_run_id,
    )
    final_user_message = final_user_message.model_copy(deep=True)
    run_id = synthetic_run_id
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


def agent_tool_definition_payloads_for_logging(agent: Agent) -> list[dict[str, object]]:
    """Return model-visible agent tool schemas using Agno's prompt-preparation path."""
    with preserve_tool_instructions(agent):
        _session, _run_context, prepared_tools = prepare_agent_prompt_inputs_for_estimation(agent)
    return prepared_tool_definition_payloads(prepared_tools)


def team_tool_definition_payloads_for_logging(team: Team) -> list[dict[str, object]]:
    """Return model-visible team tool schemas using Agno's prompt-preparation path."""
    with preserve_tool_instructions(team):
        _session, prepared_tools = prepare_team_prompt_inputs_for_estimation(team)
    return prepared_tool_definition_payloads(prepared_tools)


def _provider_request(
    messages: Sequence[Message],
    prepared_tools: Sequence[object],
) -> AgnoProviderRequest:
    tool_schemas = _provider_tool_schemas(prepared_tools)
    return AgnoProviderRequest(
        messages=tuple(message.model_copy(deep=True) for message in messages),
        tools=tuple(_provider_tool_definition_payloads(tool_schemas)),
        tool_choice="none" if tool_schemas else None,
    )


def _provider_tool_schemas(prepared_tools: Sequence[object]) -> list[PreparedTool]:
    return [tool for tool in prepared_tools if isinstance(tool, Function) or _is_tool_definition_dict(tool)]


def _agent_synthetic_session(
    *,
    agent: Agent,
    source_session: AgentSession,
    prefix_messages: Sequence[Message],
    synthetic_run_id: str,
) -> AgentSession:
    return replace(
        source_session,
        agent_id=agent.id,
        session_data=deepcopy(source_session.session_data),
        metadata=deepcopy(source_session.metadata),
        agent_data=deepcopy(source_session.agent_data),
        runs=[
            RunOutput(
                run_id=synthetic_run_id,
                agent_id=agent.id,
                status=RunStatus.completed,
                messages=[message.model_copy(deep=True) for message in prefix_messages],
            ),
        ],
        summary=deepcopy(source_session.summary),
    )


def _team_synthetic_session(
    *,
    team: Team,
    source_session: TeamSession,
    prefix_messages: Sequence[Message],
    synthetic_run_id: str,
) -> TeamSession:
    return replace(
        source_session,
        team_id=team.id,
        team_data=deepcopy(source_session.team_data),
        session_data=deepcopy(source_session.session_data),
        metadata=deepcopy(source_session.metadata),
        runs=[
            TeamRunOutput(
                run_id=synthetic_run_id,
                team_id=team.id,
                status=RunStatus.completed,
                messages=[message.model_copy(deep=True) for message in prefix_messages],
            ),
        ],
        summary=deepcopy(source_session.summary),
    )


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
def preserve_tool_instructions(entity: Agent | Team) -> Iterator[None]:
    """Restore Agno's mutable tool-instruction cache after request assembly."""
    previous_tool_instructions = entity._tool_instructions
    try:
        yield
    finally:
        entity._tool_instructions = previous_tool_instructions


def prepare_tools_for_estimation(tools: object) -> tuple[list[PreparedTool], list[str]]:
    """Prepare configured Agno tools without requiring a live run."""
    if not isinstance(tools, Sequence):
        return [], []

    prepared_tools: list[PreparedTool] = []
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


def _prepare_tool_for_estimation(tool: object) -> list[PreparedTool]:
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


def prepared_tool_definition_payloads(
    prepared_tools: Sequence[PreparedTool],
) -> list[dict[str, object]]:
    """Return stable function-style payloads for prepared Agno tools."""
    payloads_by_name: dict[str, dict[str, object]] = {}
    for tool in prepared_tools:
        payload = _function_payload(tool) if isinstance(tool, Function) else _dict_tool_payload(tool)
        tool_name = payload.get("name")
        if isinstance(tool_name, str) and tool_name:
            payloads_by_name[tool_name] = payload
    return list(payloads_by_name.values())


def _provider_tool_definition_payloads(
    prepared_tools: Sequence[PreparedTool],
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


def _prepared_tool_name(tool: PreparedTool) -> str | None:
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


def _is_tool_definition_dict(tool: object) -> TypeGuard[ToolDefinition]:
    if not isinstance(tool, dict):
        return False
    candidate_tool = cast("ToolDefinition", tool)
    tool_name = candidate_tool.get("name")
    return isinstance(tool_name, str) and bool(tool_name)


def _dict_tool_payload(tool: ToolDefinition) -> dict[str, object]:
    parameters = tool.get("parameters")
    return {
        "name": str(tool["name"]),
        "description": str(tool.get("description", "")),
        "parameters": parameters if isinstance(parameters, dict) else _default_function_parameters(),
    }


def _default_function_parameters() -> dict[str, object]:
    return {"type": "object", "properties": {}, "required": []}


def prepare_team_prompt_inputs_for_estimation(
    team: Team,
) -> tuple[TeamSession, list[PreparedTool]]:
    """Reuse Agno's own team tool-preparation path for prompt budgeting.

    Agno exposes `Team.get_system_message()` publicly, but the exact prepared tool
    payload and `_tool_instructions` state that feed that prompt are only built by
    the internal `_determine_tools_for_model()` path. Using that single internal
    entrypoint is less brittle than re-implementing several private team helpers.
    This logic is verified against `agno==2.5.13`; if Agno changes those
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


def prepare_agent_prompt_inputs_for_estimation(
    agent: Agent,
) -> tuple[AgentSession, RunContext, list[PreparedTool]]:
    """Reuse Agno's agent tool-preparation path for prompt budgeting.

    Agno exposes `Agent.get_system_message()` publicly, but the prepared tool
    payload and `_tool_instructions` that feed that prompt are only finalized by
    the shared `agno.agent._tools.determine_tools_for_model()` path. Using that
    single internal entrypoint avoids re-implementing several private agent
    helpers.
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
