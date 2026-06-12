"""Build forked Agno provider requests without running a full Agno turn.

Some callers need the exact provider-visible request that Agno would assemble
for an Agent, but against a copied message prefix and without creating a real
Agno run.
This is especially useful for prompt-cache-sensitive internal turns: the caller
can preserve the same system prompt, session summary placement, message format,
model settings, and tool schemas, then append its own final user message.

Agno does not currently expose a public "fork this session and build the exact
provider request without running it" primitive.
Calling ``agent.arun()`` against a temporary copied session would look simpler,
but it would run the full agent lifecycle: persistence, hooks, memory/learning
side effects, metrics, metadata writes, and potentially real tool calls.
Calling ``model.aresponse()`` directly with copied messages would avoid those
side effects, but it would also bypass Agno's normal request assembly.

This module is the intentionally narrow adapter for that missing Agno
primitive.
It builds a synthetic in-memory Agno session containing only the caller-provided
history runs, copies the Agent runtime object, configures replay on that copy,
asks Agno's private message/tool builders for the provider-visible request, then
returns inert message copies and provider tool schemas.
Executable ``Function`` objects are not passed onward; when tool schemas are
present the request also sets ``tool_choice="none"``.

Keep private Agno imports and synthetic-session tricks contained here.
This module deliberately has no ``mindroom.*`` imports; callers adapt their
domain objects into plain Agno sessions and messages before calling it.
If Agno grows a public forked-request API, this module is the place to replace.
"""

from __future__ import annotations

from copy import copy, deepcopy
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, TypeGuard, cast

import agno
from agno.agent._messages import aget_run_messages
from agno.agent._tools import determine_tools_for_model
from agno.run import RunContext
from agno.run.agent import RunInput, RunOutput
from agno.tools.function import Function

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agno.agent import Agent
    from agno.models.message import Message
    from agno.session.agent import AgentSession


_SUPPORTED_AGNO_VERSION = "2.6.12"
if agno.__version__ != _SUPPORTED_AGNO_VERSION:
    msg = (
        "MindRoom's forked Agno provider-request adapter is pinned to "
        f"agno=={_SUPPORTED_AGNO_VERSION}; found agno=={agno.__version__}."
    )
    raise RuntimeError(msg)


type _ToolDefinition = dict[str, object]


@dataclass(frozen=True)
class _AgnoProviderRequest:
    """Provider-visible request payload assembled by Agno."""

    messages: tuple[Message, ...]
    tools: tuple[_ToolDefinition, ...] = ()
    tool_choice: str | dict[str, object] | None = None


async def build_agent_provider_request_from_runs(
    *,
    agent: Agent,
    source_session: AgentSession,
    prefix_runs: Sequence[RunOutput],
    final_user_message: Message,
    synthetic_run_id: str,
) -> _AgnoProviderRequest:
    """Build an agent request from copied history runs plus one final user message.

    The synthetic session keeps the original run boundaries so Agno materializes
    exactly the history messages it would send for those runs in a real reply
    request.
    """
    request_agent = _request_agent(agent)
    forked_session = _agent_synthetic_session(
        agent=request_agent,
        source_session=source_session,
        prefix_runs=prefix_runs,
    )
    final_user_message = final_user_message.model_copy(deep=True)
    user_id = forked_session.user_id
    run_response = RunOutput(
        run_id=synthetic_run_id,
        agent_id=request_agent.id,
        agent_name=request_agent.name,
        session_id=forked_session.session_id,
        user_id=user_id,
        input=RunInput(input_content=final_user_message),
        session_state={},
    )
    run_context = RunContext(
        run_id=synthetic_run_id,
        session_id=forked_session.session_id,
        user_id=user_id,
        session_state={},
    )
    model = request_agent.model
    assert model is not None
    processed_tools = await request_agent.aget_tools(
        run_response=run_response,
        run_context=run_context,
        session=forked_session,
        user_id=user_id,
        check_mcp_tools=False,
    )
    prepared_tools = determine_tools_for_model(
        agent=request_agent,
        model=model,
        processed_tools=processed_tools,
        run_response=run_response,
        run_context=run_context,
        session=forked_session,
        async_mode=True,
    )
    run_messages = await aget_run_messages(
        request_agent,
        run_response=run_response,
        run_context=run_context,
        input=final_user_message,
        session=forked_session,
        user_id=user_id,
        add_history_to_context=True,
        add_dependencies_to_context=False,
        add_session_state_to_context=False,
        tools=prepared_tools,
    )
    return _provider_request(run_messages.messages, prepared_tools)


def _provider_request(
    messages: Sequence[Message],
    prepared_tools: Sequence[object],
) -> _AgnoProviderRequest:
    tool_schemas = _provider_tool_definition_payloads(
        [tool for tool in prepared_tools if isinstance(tool, Function) or _is_tool_definition_dict(tool)],
    )
    return _AgnoProviderRequest(
        messages=tuple(message.model_copy(deep=True) for message in messages),
        tools=tuple(tool_schemas),
        tool_choice="none" if tool_schemas else None,
    )


def _agent_synthetic_session(
    *,
    agent: Agent,
    source_session: AgentSession,
    prefix_runs: Sequence[RunOutput],
) -> AgentSession:
    return replace(
        source_session,
        agent_id=agent.id,
        session_data=deepcopy(source_session.session_data),
        metadata=deepcopy(source_session.metadata),
        agent_data=deepcopy(source_session.agent_data),
        runs=[deepcopy(run) for run in prefix_runs],
        summary=deepcopy(source_session.summary),
    )


def _request_agent(agent: Agent) -> Agent:
    request_agent = copy(agent)
    request_agent.add_history_to_context = True
    request_agent.num_history_runs = None
    request_agent.num_history_messages = None
    request_agent.max_tool_calls_from_history = None
    request_agent._tool_instructions = deepcopy(agent._tool_instructions)
    return request_agent


def _provider_tool_definition_payloads(
    prepared_tools: Sequence[Function | _ToolDefinition],
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


def _is_tool_definition_dict(tool: object) -> TypeGuard[_ToolDefinition]:
    if not isinstance(tool, dict):
        return False
    candidate_tool = cast("_ToolDefinition", tool)
    tool_name = candidate_tool.get("name")
    return isinstance(tool_name, str) and bool(tool_name)
