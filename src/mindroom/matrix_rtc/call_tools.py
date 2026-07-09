"""Bridge MindRoom agent tools into a LiveKit realtime voice session.

The realtime model calls tools through livekit-agents' function-tool
mechanism. This module materializes the agent's regular chat toolkits (the
same construction path as text conversations, including workspace-aware
base dirs and worker routing) and wraps every agno function as a raw
livekit function tool. Tool calls run inside the standard MindRoom tool
runtime context for the call's room and respect the configured
tool-approval rules: calls that would need human approval are refused with
a spoken-friendly message instead of silently executing.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agno.session import AgentSession as AgnoAgentSession

from mindroom.agents import create_agent
from mindroom.logging_config import get_logger
from mindroom.message_target import MessageTarget
from mindroom.session_ids import create_session_id
from mindroom.tool_approval import evaluate_tool_approval
from mindroom.tool_system.runtime_context import tool_runtime_context

if TYPE_CHECKING:
    from agno.agent import Agent as AgnoAgent
    from agno.tools.function import Function
    from livekit.agents.llm import RawFunctionTool

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.runtime_context import ToolRuntimeContext, ToolRuntimeSupport

logger = get_logger(__name__)

_APPROVAL_REQUIRED_MESSAGE = (
    "This tool requires human approval and cannot run during a voice call. "
    "Tell the user to ask for it in the text chat instead."
)

_MAX_TOOL_RESULT_CHARS = 8000


@dataclass(frozen=True)
class CallAgentTooling:
    """The realtime-session materialization of one chat agent.

    Carries the same tools and the same rendered system prompt the agent
    would use in a text conversation, so the voice agent is the same agent.
    """

    tools: list[Any]
    tool_names: tuple[str, ...]
    instructions: str | None = None


async def build_call_tools(
    *,
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    tool_support: ToolRuntimeSupport,
    room_id: str,
) -> CallAgentTooling:
    """Materialize the agent's toolkits and wrap them for the realtime session."""
    session_id = create_session_id(room_id, None)
    target = MessageTarget(
        room_id=room_id,
        source_thread_id=None,
        resolved_thread_id=None,
        reply_to_event_id=None,
        session_id=session_id,
    )
    context = tool_support.build_context(target, user_id=None, session_id=session_id)
    if context is None:
        logger.warning("call_tools_unavailable_no_runtime_context", agent=agent_name, room_id=room_id)
        return CallAgentTooling(tools=[], tool_names=())
    execution_identity = tool_support.build_execution_identity(
        target=target,
        user_id=None,
        session_id=session_id,
        agent_name=agent_name,
    )
    agent = await asyncio.to_thread(
        functools.partial(
            create_agent,
            agent_name,
            config,
            runtime_paths,
            execution_identity,
            session_id=session_id,
            include_interactive_questions=False,
        ),
    )
    instructions = await asyncio.to_thread(_render_system_prompt, agent, session_id)
    tools: list[Any] = []
    names: list[str] = []
    for toolkit in agent.tools or []:
        for function in toolkit.functions.values():
            tools.append(
                _wrap_agno_function(
                    function,
                    context=context,
                    config=config,
                    runtime_paths=runtime_paths,
                    agent_name=agent_name,
                ),
            )
            names.append(function.name)
    logger.info("call_tools_built", agent=agent_name, room_id=room_id, tool_count=len(names))
    return CallAgentTooling(tools=tools, tool_names=tuple(names), instructions=instructions)


def _render_system_prompt(agent: AgnoAgent, session_id: str) -> str | None:
    """Render the same system message the agent would use for a chat turn."""
    try:
        message = agent.get_system_message(AgnoAgentSession(session_id=session_id))
    except Exception as error:
        logger.warning("call_system_prompt_render_failed", error=str(error))
        return None
    if message is None:
        return None
    content = message.content
    return content if isinstance(content, str) and content.strip() else None


def _normalize_tool_result(result: object) -> str:
    if isinstance(result, str):
        text = result
    else:
        try:
            text = json.dumps(result, default=str)
        except (TypeError, ValueError):
            text = str(result)
    if len(text) > _MAX_TOOL_RESULT_CHARS:
        return text[:_MAX_TOOL_RESULT_CHARS] + " …(truncated)"
    return text


def _wrap_agno_function(
    function: Function,
    *,
    context: ToolRuntimeContext,
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
) -> RawFunctionTool:
    """Wrap one agno function as a livekit raw function tool."""
    from livekit.agents import llm  # noqa: PLC0415

    # Toolkit functions carry an empty parameters schema until processed;
    # without this the realtime model sees zero-argument tools.
    function.process_entrypoint()
    parameters = function.parameters if isinstance(function.parameters, dict) else {}
    if not parameters:
        parameters = {"type": "object", "properties": {}}
    raw_schema = {
        "name": function.name,
        "description": function.description or function.name,
        "parameters": parameters,
    }

    async def _handler(raw_arguments: dict[str, Any]) -> str:
        requires_approval, _timeout = await evaluate_tool_approval(
            config,
            runtime_paths,
            function.name,
            raw_arguments,
            agent_name,
        )
        if requires_approval:
            logger.info("call_tool_blocked_needs_approval", tool=function.name, agent=agent_name)
            return _APPROVAL_REQUIRED_MESSAGE
        entrypoint = function.entrypoint
        if entrypoint is None:
            return f"Tool {function.name} has no entrypoint and cannot run."
        logger.info("call_tool_executing", tool=function.name, agent=agent_name, room_id=context.room_id)
        try:
            with tool_runtime_context(context):
                if inspect.iscoroutinefunction(entrypoint):
                    result = await entrypoint(**raw_arguments)
                else:
                    # asyncio.to_thread copies the current contextvars context,
                    # so the tool runtime context stays visible in the thread.
                    result = await asyncio.to_thread(functools.partial(entrypoint, **raw_arguments))
        except Exception as error:
            logger.warning("call_tool_failed", tool=function.name, agent=agent_name, error=str(error))
            return f"Tool {function.name} failed: {error}"
        return _normalize_tool_result(result)

    return llm.function_tool(_handler, raw_schema=raw_schema)
