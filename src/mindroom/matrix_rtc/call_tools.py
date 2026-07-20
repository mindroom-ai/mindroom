"""Materialize one MindRoom agent for realtime or cascaded voice calls.

The realtime model calls tools through livekit-agents' function-tool
mechanism. This module materializes the agent's regular chat toolkits (the
same construction path as text conversations, including knowledge, skills,
workspace-aware base dirs, and worker routing) and wraps every agno function
as a raw livekit function tool. Tool calls run inside the standard MindRoom
tool runtime context for the call's room and sole Matrix requester. Tools
needing confirmation, user input, external execution, or approval are omitted
because voice has no approval UI.

The cascaded backend delegates each transcript to the normal ``ai_response``
path instead. LiveKit receives no tools there; Agno remains the sole model and
tool loop, preserving text-chat model resolution, prompts, memory, and hooks.
"""

from __future__ import annotations

import asyncio
import functools
import json
from dataclasses import dataclass, field, replace
from inspect import isasyncgenfunction, iscoroutinefunction
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from agno.agent._tools import determine_tools_for_model
from agno.run import RunContext
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.session import AgentSession as AgnoAgentSession
from agno.tools.function import Function, FunctionCall

from mindroom.agent_run_context import append_knowledge_availability_enrichment
from mindroom.agents import create_agent
from mindroom.history.interrupted_replay import persist_interrupted_replay
from mindroom.history.runtime import open_resolved_scope_session_context
from mindroom.history.turn_recorder import TurnRecorder
from mindroom.history.types import HistoryScope
from mindroom.hooks import EnrichmentItem
from mindroom.knowledge.utils import resolve_agent_knowledge_access
from mindroom.logging_config import get_logger
from mindroom.message_target import MessageTarget
from mindroom.session_ids import create_session_id
from mindroom.tool_approval import tool_requires_approval_for_openai_compat
from mindroom.tool_system.runtime_context import tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from agno.agent import Agent as AgnoAgent
    from livekit.agents.llm import RawFunctionTool

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.events import ToolTraceEntry
    from mindroom.tool_system.runtime_context import ToolRuntimeContext, ToolRuntimeSupport
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)

_TEXT_CHAT_REQUIRED_MESSAGE = (
    "This tool needs an interactive or external execution flow that is unavailable during a voice call. "
    "Tell the user to ask for it in the text chat instead."
)

_MAX_TOOL_RESULT_CHARS = 8000
_CALL_UNAVAILABLE_COMPOSITE_FUNCTIONS = frozenset({"run_workflow", "sessions_send", "sessions_spawn"})


@dataclass(frozen=True)
class CallAgentResponse:
    """One normal MindRoom agent turn returned to the cascaded voice pipe."""

    text: str
    tool_names: tuple[str, ...] = ()
    turn_id: str | None = None


@dataclass(frozen=True)
class CallAgentTooling:
    """The call-session materialization of one chat agent.

    Realtime carries wrapped tools and the rendered prompt.
    Cascaded carries a responder using the normal agent-turn path.
    """

    tools: tuple[Any, ...]
    instructions: str
    execution_identity: ToolExecutionIdentity
    responder: Callable[[str, Callable[[list[str]], None] | None], Awaitable[CallAgentResponse]] | None = None
    finalize_spoken_response: Callable[[str | None, str, bool], Awaitable[None] | None] | None = None


@dataclass(frozen=True)
class _CallAgentRunState:
    """Durable facts needed to reconcile generated text with spoken text."""

    session_id: str
    run_id: str
    user_message: str
    completed_tools: tuple[ToolTraceEntry, ...]
    interrupted_tools: tuple[ToolTraceEntry, ...]
    run_metadata: dict[str, Any]
    outcome: str
    original_status: RunStatus | None


@dataclass
class _CallResponseTracker:
    """Match LiveKit's exact playout transcript back to one Agno run."""

    agent_name: str
    config: Config
    runtime_paths: RuntimePaths
    execution_identity: ToolExecutionIdentity
    pending: dict[str, _CallAgentRunState] = field(default_factory=dict)
    settlements: set[asyncio.Task[None]] = field(default_factory=set)
    settlement_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def register(self, state: _CallAgentRunState) -> str:
        """Remember a completed agent response until LiveKit settles playout."""
        correlation_id = uuid4().hex
        self.pending[correlation_id] = state
        return correlation_id

    def finalize(self, token: str | None, spoken_text: str, interrupted: bool) -> Awaitable[None] | None:
        """Claim one response and reconcile playout or an underlying excluded run."""
        state = self._pop(token)
        if state is None or (not interrupted and state.outcome != "interrupted"):
            return None
        original_status = (
            state.original_status
            if state.outcome == "interrupted" and state.original_status is not None
            else RunStatus.cancelled
        )
        settlement = asyncio.create_task(
            self._persist_serialized(state, spoken_text, original_status),
            name=f"settle_call_response:{state.run_id}",
        )
        self.settlements.add(settlement)
        settlement.add_done_callback(self.settlements.discard)
        return settlement

    async def wait_for_settlements(self) -> None:
        """Do not start another persisted run while playout reconciliation is active."""
        while self.settlements:
            await asyncio.gather(
                *(asyncio.shield(task) for task in tuple(self.settlements)),
                return_exceptions=True,
            )

    async def persist_unspoken(self, state: _CallAgentRunState, *, default_status: RunStatus) -> None:
        """Persist a terminal generation before LiveKit received any text."""
        original_status = state.original_status or default_status
        await asyncio.shield(self._persist_serialized(state, "", original_status))

    async def _persist_serialized(
        self,
        state: _CallAgentRunState,
        spoken_text: str,
        original_status: RunStatus,
    ) -> None:
        async with self.settlement_lock:
            await asyncio.to_thread(self._persist, state, spoken_text, original_status)

    def _pop(self, token: str | None) -> _CallAgentRunState | None:
        if token is not None:
            return self.pending.pop(token, None)
        return self.pending.pop(next(iter(self.pending))) if self.pending else None

    def _persist(self, state: _CallAgentRunState, spoken_text: str, original_status: RunStatus) -> None:
        with open_resolved_scope_session_context(
            agent_name=self.agent_name,
            scope=HistoryScope(kind="agent", scope_id=self.agent_name),
            session_id=state.session_id,
            runtime_paths=self.runtime_paths,
            config=self.config,
            execution_identity=self.execution_identity,
        ) as scope_context:
            persist_interrupted_replay(
                scope_context=scope_context,
                session_id=state.session_id,
                run_id=state.run_id,
                user_message=state.user_message,
                user_message_is_structured=False,
                partial_text=spoken_text,
                completed_tools=state.completed_tools,
                interrupted_tools=state.interrupted_tools,
                run_metadata=state.run_metadata,
                is_team=False,
                original_status=original_status,
            )


async def build_call_tools(
    *,
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    tool_support: ToolRuntimeSupport,
    room_id: str,
    requester_id: str,
    session_id: str | None = None,
    enable_responder: bool = False,
    voice_instructions: str | None = None,
    active_model_name: str | None = None,
) -> CallAgentTooling:
    """Materialize the agent for the selected voice backend."""
    session_id = session_id or create_session_id(room_id, None)
    target = MessageTarget(
        room_id=room_id,
        source_thread_id=None,
        resolved_thread_id=None,
        reply_to_event_id=None,
        session_id=session_id,
    )
    context = tool_support.build_context(
        target,
        user_id=requester_id,
        agent_name=agent_name,
        active_model_name=active_model_name,
    )
    if context is None:
        msg = f"Tool runtime context unavailable for voice agent {agent_name}"
        raise RuntimeError(msg)
    context = replace(
        context,
        tool_function_filter=functools.partial(
            _function_available_during_call,
            config=config,
            agent_name=agent_name,
        ),
    )
    execution_identity = tool_support.build_execution_identity(
        target=target,
        user_id=requester_id,
        agent_name=agent_name,
    )
    if enable_responder:
        voice_enrichment_items: tuple[EnrichmentItem, ...] = ()
        if voice_instructions:
            voice_enrichment_items = (EnrichmentItem(key="voice_call", text=voice_instructions, cache_policy="stable"),)
        response_tracker = _CallResponseTracker(
            agent_name=agent_name,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
        )
        responder = functools.partial(
            _run_call_agent,
            agent_name=agent_name,
            config=config,
            runtime_paths=runtime_paths,
            tool_support=tool_support,
            context=context,
            execution_identity=execution_identity,
            room_id=room_id,
            requester_id=requester_id,
            session_id=session_id,
            voice_enrichment_items=voice_enrichment_items,
            response_tracker=response_tracker,
            active_model_name=active_model_name,
        )
        return CallAgentTooling(
            tools=(),
            instructions="",
            execution_identity=execution_identity,
            responder=responder,
            finalize_spoken_response=response_tracker.finalize,
        )

    refresh_scheduler = context.orchestrator.knowledge_refresh_scheduler if context.orchestrator is not None else None
    knowledge_resolution = resolve_agent_knowledge_access(
        agent_name,
        config,
        runtime_paths,
        refresh_scheduler=refresh_scheduler,
        execution_identity=execution_identity,
    )
    knowledge = knowledge_resolution.knowledge
    agent = await asyncio.to_thread(
        functools.partial(
            create_agent,
            agent_name,
            config,
            runtime_paths,
            execution_identity,
            session_id=session_id,
            hook_registry=context.hook_registry,
            knowledge=knowledge,
            include_interactive_questions=False,
            tool_function_filter=context.tool_function_filter,
            refresh_scheduler=refresh_scheduler,
            eager_deferred_tools=True,
        ),
    )
    run_id = f"{session_id}:voice"
    session = AgnoAgentSession(session_id=session_id, agent_id=agent_name, user_id=requester_id)
    run_output = RunOutput(
        run_id=run_id,
        agent_id=agent_name,
        agent_name=agent.name,
        session_id=session_id,
        user_id=requester_id,
    )
    run_context = RunContext(
        run_id=run_id,
        session_id=session_id,
        user_id=requester_id,
        session_state={},
    )
    processed_tools = await agent.aget_tools(
        run_response=run_output,
        run_context=run_context,
        session=session,
        user_id=requester_id,
    )
    effective_tools = determine_tools_for_model(
        agent,
        model=agent.model,
        processed_tools=processed_tools,
        run_response=run_output,
        run_context=run_context,
        session=session,
        async_mode=True,
    )
    tools: list[Any] = []
    visible_functions: list[Function | dict[Any, Any]] = []
    for tool in effective_tools:
        if not isinstance(tool, Function):
            msg = f"Voice calls cannot expose provider-native tool definitions for agent {agent_name}"
            raise TypeError(msg)
        if _function_requires_text_chat(tool, config):
            logger.info("call_tool_hidden_needs_text_chat", tool=tool.name, agent=agent_name)
            continue
        visible_functions.append(tool)
        tools.append(
            _wrap_agno_function(
                tool,
                context=context,
                agent_name=agent_name,
                config=config,
            ),
        )
    instructions = await _render_system_prompt(agent, session, run_context, visible_functions)
    logger.info("call_tools_built", agent=agent_name, room_id=room_id, tool_count=len(tools))
    return CallAgentTooling(
        tools=tuple(tools),
        instructions=instructions,
        execution_identity=execution_identity,
    )


async def _run_call_agent(
    transcript: str,
    on_tools_executed: Callable[[list[str]], None] | None = None,
    *,
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    tool_support: ToolRuntimeSupport,
    context: ToolRuntimeContext,
    execution_identity: ToolExecutionIdentity,
    room_id: str,
    requester_id: str,
    session_id: str,
    voice_enrichment_items: tuple[EnrichmentItem, ...],
    response_tracker: _CallResponseTracker,
    active_model_name: str | None,
) -> CallAgentResponse:
    """Run one finalized call transcript through the normal MindRoom agent."""
    from mindroom.ai import ResponseTurnContext, ai_response  # noqa: PLC0415 - heavy optional call path

    await response_tracker.wait_for_settlements()
    refresh_scheduler = context.orchestrator.knowledge_refresh_scheduler if context.orchestrator is not None else None
    knowledge_resolution = resolve_agent_knowledge_access(
        agent_name,
        config,
        runtime_paths,
        refresh_scheduler=refresh_scheduler,
        execution_identity=execution_identity,
    )
    enrichment_items = append_knowledge_availability_enrichment(
        voice_enrichment_items,
        knowledge_resolution.unavailable,
    )
    recorder = TurnRecorder(user_message=transcript)
    fallback_run_id = f"{session_id}:turn:{uuid4().hex}"
    turn = ResponseTurnContext(
        entity_label=agent_name,
        session_id=session_id,
        run_id=None,
        correlation_id=uuid4().hex,
        reply_to_event_id=None,
        room_id=room_id,
        thread_id=None,
        requester_id=requester_id,
        matrix_run_metadata=None,
        active_model_name=active_model_name,
        system_enrichment_items=enrichment_items,
    )
    run_metadata: dict[str, Any] = {}

    async def _respond() -> str:
        try:
            return await ai_response(
                turn,
                prompt=transcript,
                runtime_paths=runtime_paths,
                config=config,
                knowledge=knowledge_resolution.knowledge,
                run_id_callback=recorder.set_run_id,
                include_interactive_questions=False,
                tool_function_filter=context.tool_function_filter,
                show_tool_calls=False,
                run_metadata_collector=run_metadata,
                execution_identity=execution_identity,
                refresh_scheduler=refresh_scheduler,
                turn_recorder=recorder,
                eager_deferred_tools=True,
            )
        finally:
            recorder.set_run_metadata({**(recorder.run_metadata or {}), **run_metadata})

    try:
        response = await tool_support.run_in_context(tool_context=context, operation=_respond)
    except asyncio.CancelledError:
        state = _call_agent_run_state(recorder, session_id=session_id, fallback_run_id=fallback_run_id)
        await response_tracker.persist_unspoken(state, default_status=RunStatus.cancelled)
        raise
    except Exception:
        state = _call_agent_run_state(recorder, session_id=session_id, fallback_run_id=fallback_run_id)
        await response_tracker.persist_unspoken(state, default_status=RunStatus.error)
        raise
    finally:
        tool_names = tuple(dict.fromkeys(entry.tool_name for entry in recorder.completed_tools))
        if on_tools_executed is not None and tool_names:
            on_tools_executed(list(tool_names))
    state = _call_agent_run_state(recorder, session_id=session_id, fallback_run_id=fallback_run_id)
    turn_id = response_tracker.register(state) if response else None
    if not response and state.outcome == "interrupted":
        await response_tracker.persist_unspoken(state, default_status=RunStatus.cancelled)
    return CallAgentResponse(text=response, tool_names=tool_names, turn_id=turn_id)


def _call_agent_run_state(
    recorder: TurnRecorder,
    *,
    session_id: str,
    fallback_run_id: str,
) -> _CallAgentRunState:
    """Snapshot one real Agno run for later spoken-text reconciliation."""
    return _CallAgentRunState(
        session_id=session_id,
        run_id=recorder.run_id or fallback_run_id,
        user_message=recorder.user_message,
        completed_tools=tuple(recorder.completed_tools),
        interrupted_tools=tuple(recorder.interrupted_tools),
        run_metadata=dict(recorder.run_metadata or {}),
        outcome=recorder.outcome,
        original_status=recorder.original_status,
    )


async def _render_system_prompt(
    agent: AgnoAgent,
    session: AgnoAgentSession,
    run_context: RunContext,
    tools: list[Function | dict[Any, Any]],
) -> str:
    """Render the same system message the agent would use for a chat turn."""
    message = await agent.aget_system_message(session, run_context=run_context, tools=tools)
    if message is None:
        msg = "Agent produced no system prompt for its voice session"
        raise ValueError(msg)
    content = message.content
    if not isinstance(content, str) or not content.strip():
        msg = "Agent produced an empty system prompt for its voice session"
        raise ValueError(msg)
    return content


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


def _function_requires_async_execution(function: Function) -> bool:
    """Return whether an entrypoint or hook requires Agno's async executor."""
    callbacks = [function.entrypoint, function.pre_hook, function.post_hook, *(function.tool_hooks or [])]
    return any(
        callback is not None and (iscoroutinefunction(callback) or isasyncgenfunction(callback))
        for callback in callbacks
    )


def _function_requires_text_chat(function: Function, config: Config) -> bool:
    """Return whether voice must hide a function with no usable approval UI."""
    return (
        function.requires_confirmation
        or function.requires_user_input
        or function.external_execution
        or function.approval_type == "required"
        or function.name in _CALL_UNAVAILABLE_COMPOSITE_FUNCTIONS
        or tool_requires_approval_for_openai_compat(config, function.name)
    )


def _function_available_during_call(
    function: Function,
    *,
    config: Config,
    agent_name: str,
) -> bool:
    """Keep only functions whose execution can complete without text UI."""
    unavailable = _function_requires_text_chat(function, config)
    if unavailable:
        logger.debug("call_tool_hidden_needs_text_chat", tool=function.name, agent=agent_name)
    return not unavailable


def _wrap_agno_function(
    function: Function,
    *,
    context: ToolRuntimeContext,
    agent_name: str,
    config: Config,
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
        if _function_requires_text_chat(function, config):
            logger.info("call_tool_blocked_needs_text_chat", tool=function.name, agent=agent_name)
            return _TEXT_CHAT_REQUIRED_MESSAGE
        logger.info("call_tool_executing", tool=function.name, agent=agent_name, room_id=context.room_id)
        try:
            with tool_runtime_context(context):
                # create_agent installs MindRoom's canonical hook bridge on
                # every function. It owns approval evaluation, including the
                # defensive argument copy, so do not preflight policy here.
                execution = FunctionCall(function=function, arguments=raw_arguments)
                if _function_requires_async_execution(function):
                    result = await execution.aexecute()
                else:
                    # asyncio.to_thread copies the current contextvars context,
                    # so hooks and the tool see the call's runtime context.
                    result = await asyncio.to_thread(execution.execute)
        except Exception as error:
            logger.warning("call_tool_failed", tool=function.name, agent=agent_name, error=str(error))
            return f"Tool {function.name} failed: {error}"
        if result.status != "success":
            error = result.error or "unknown error"
            logger.warning("call_tool_failed", tool=function.name, agent=agent_name, error=error)
            return f"Tool {function.name} failed: {error}"
        return _normalize_tool_result(result.result)

    return llm.function_tool(_handler, raw_schema=raw_schema)
