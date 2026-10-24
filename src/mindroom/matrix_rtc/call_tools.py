"""Materialize one MindRoom agent for realtime or cascaded voice calls.

The realtime model calls tools through livekit-agents' function-tool
mechanism. This module builds the agent's regular chat tools (the same
construction path as text conversations, including knowledge, skills,
workspace-aware base dirs, and worker routing) and wraps every function the
build offers as a raw livekit function tool that runs under the build's
tool-call policy. Tool calls run inside the standard MindRoom tool runtime
context for the call's room and sole Matrix requester. Tools needing
confirmation, external execution, or approval are omitted because voice has
no approval UI. Exact built-in room-recovery functions remain available so an
agent can restore the router needed by approval policy.

The cascaded backend delegates each transcript to the normal ``ai_response``
path instead. LiveKit receives no tools there; MindRoom's agent loop remains
the sole model and tool loop, preserving text-chat model resolution, prompts,
memory, and hooks.
"""

from __future__ import annotations

import asyncio
import functools
import json
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from mindroom.agent_loop.build import prompt_for_turn
from mindroom.agent_loop.tools import offered_functions
from mindroom.agent_run_context import append_knowledge_availability_enrichment
from mindroom.agents import create_agent
from mindroom.approval.tool_approval import tool_may_require_approval
from mindroom.background_tasks import wait_for_future_until_complete
from mindroom.budgets.monitor import budget_model
from mindroom.helper_usage import HelperUsageOwner, helper_usage_owner_context
from mindroom.history.interrupted_replay import persist_interrupted_replay
from mindroom.history.session_context import create_scope_session_storage, open_resolved_scope_session_context
from mindroom.history.turn_recorder import TurnRecorder
from mindroom.history.types import HistoryScope
from mindroom.hooks import EnrichmentItem
from mindroom.knowledge.utils import knowledge_runtime_identity, resolve_agent_knowledge_access_async
from mindroom.llm.messages import Message, model_provenance
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.llm.usage import MessageMetrics, ModelMetrics, RunMetrics
from mindroom.logging_config import get_logger
from mindroom.matrix_rtc.voice_agent import CallAgentResponse
from mindroom.message_target import MessageTarget
from mindroom.session_ids import create_session_id
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.session_storage.lanes import run_session_storage_operation
from mindroom.session_storage.types import ConversationSession
from mindroom.tool_jobs.execution import agent_tool_middleware
from mindroom.tool_system.agno_toolkits import AGNO_EMPTY_PARAMETERS
from mindroom.tool_system.declarations import (
    MATRIX_ROOM_RUNTIME_APPROVAL_TYPE,
    MATRIX_ROOM_RUNTIME_TOOL_NAMES,
)
from mindroom.tool_system.invocation import ToolCallContext, run_tool_call
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.tool_function import close_toolkits, connect_toolkits

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator, Sequence
    from contextlib import AbstractAsyncContextManager

    from livekit.agents.llm import RawFunctionTool

    from mindroom.agent_loop.build import AgentBuild
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.knowledge.search import AgentKnowledge
    from mindroom.matrix_rtc.voice_agent import LiveVoiceUsage, RealtimeCallUsage
    from mindroom.tool_system.agno_toolkits import Function
    from mindroom.tool_system.events import ToolTraceEntry
    from mindroom.tool_system.invocation import ToolCall, ToolMiddleware, ToolNext
    from mindroom.tool_system.runtime_context import ToolRuntimeContext, ToolRuntimeSupport
    from mindroom.tool_system.tool_function import AdaptedToolkit, ToolFunction, ToolkitClose
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


type _CallAuthorizationGuard = Callable[[], AbstractAsyncContextManager[bool]]

logger = get_logger(__name__)

_TEXT_CHAT_REQUIRED_MESSAGE = (
    "This tool needs an interactive or external execution flow that is unavailable during a voice call. "
    "Tell the user to ask for it in the text chat instead."
)

_MAX_TOOL_RESULT_CHARS = 8000
_CALL_UNAVAILABLE_COMPOSITE_FUNCTIONS = frozenset({"run_workflow"})


async def _save_call_usage(
    *,
    kind: Literal["live_voice", "realtime_voice"],
    usage_id: str,
    created_at: float,
    run: dict[str, object],
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity,
) -> None:
    """Upsert one call's billed usage in the same caller-owned ledger as its delegate."""
    agent_name = execution_identity.agent_name
    session_id = execution_identity.session_id
    if agent_name is None or session_id is None:
        msg = "Voice usage requires an owned agent conversation"
        raise ValueError(msg)
    owner = _call_usage_owner(
        agent_name,
        session_id,
        created_at=created_at,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
    )
    await run_session_storage_operation(
        owner.storage_factory,
        lambda storage: storage.save_independent_usage(
            session_id=owner.session_id,
            usage_id=f"{kind}:{usage_id}",
            kind=kind,
            requester_id=execution_identity.requester_id,
            initial_session=owner.initial_session,
            run=run,
        ),
    )


def _call_usage_owner(
    agent_name: str,
    session_id: str,
    *,
    created_at: float,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity,
) -> HelperUsageOwner:
    """Return the caller-owned ledger that a call's voice usage and its tools' helper usage share."""
    return HelperUsageOwner(
        storage_factory=functools.partial(
            create_session_storage,
            agent_name,
            config,
            runtime_paths,
            execution_identity,
        ),
        session_id=session_id,
        initial_session=ConversationSession(
            session_id=session_id,
            kind="agent",
            owner_id=agent_name,
            created_at=int(created_at),
            updated_at=int(created_at),
        ),
    )


@contextmanager
def call_helper_usage(
    execution_identity: ToolExecutionIdentity,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> Iterator[None]:
    """Charge helper work done for a call outside its tools, such as extracting memory from its transcript, to the caller."""
    agent_name = execution_identity.agent_name
    session_id = execution_identity.session_id
    owner = (
        None
        if agent_name is None or session_id is None
        else _call_usage_owner(
            agent_name,
            session_id,
            created_at=time.time(),
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
        )
    )
    with helper_usage_owner_context(owner):
        yield


async def record_call_voice_usage(
    usage: LiveVoiceUsage,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity,
) -> None:
    """Upsert billed GPT-Live voice duration for the caller."""
    await _save_call_usage(
        kind="live_voice",
        usage_id=usage.provider_session_id,
        created_at=usage.created_at,
        run={
            "model_provider": "OpenAI",
            "model": usage.model,
            "created_at": usage.created_at,
            "voice_seconds": usage.duration_seconds,
            "voice_finalized": usage.finalized,
        },
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
    )


async def record_call_token_usage(
    usage: RealtimeCallUsage,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity,
) -> None:
    """Upsert a realtime call's speech-model tokens for the caller, one request per response.

    The call's model has its entry under ``details`` even before any response, kept as main stores it.
    """
    metrics = RunMetrics(details={"model": [ModelMetrics(id=usage.model, provider="OpenAI")]})
    messages: list[Message] = []
    for response in usage.responses:
        response_metrics = MessageMetrics(
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            total_tokens=response.input_tokens + response.output_tokens,
            cache_read_tokens=response.cache_read_tokens,
            cache_write_tokens=response.cache_write_tokens,
            audio_input_tokens=response.audio_input_tokens,
            audio_output_tokens=response.audio_output_tokens,
            audio_total_tokens=response.audio_input_tokens + response.audio_output_tokens,
            reasoning_tokens=response.reasoning_tokens,
        )
        metrics.add(response_metrics, model_id=usage.model, provider="OpenAI")
        messages.append(
            Message(
                role="assistant",
                created_at=int(response.created_at),
                metrics=response_metrics,
                provider_data=model_provenance(usage.model, "OpenAI"),
            ),
        )
    await _save_call_usage(
        kind="realtime_voice",
        usage_id=usage.usage_id,
        created_at=usage.created_at,
        run=RunRecord(
            run_id=usage.usage_id,
            model=usage.model,
            model_provider="OpenAI",
            created_at=int(usage.created_at),
            metrics=metrics,
            messages=messages,
        ).to_dict(),
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
    )


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
    close: Callable[[], Awaitable[None]] | None = None
    get_system_prompt: Callable[[], Awaitable[str]] | None = None


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
    """Match LiveKit's exact playout transcript back to one agent run."""

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
                run_id=state.run_id,
                user_message=state.user_message,
                user_message_is_structured=False,
                partial_text=spoken_text,
                completed_tools=state.completed_tools,
                interrupted_tools=state.interrupted_tools,
                run_metadata=state.run_metadata,
                original_status=original_status,
            )


@dataclass
class _CallAgentCache:
    """Own one serially reused agent build for the lifetime of a call."""

    agent_name: str
    config: Config
    runtime_paths: RuntimePaths
    context: ToolRuntimeContext
    execution_identity: ToolExecutionIdentity
    active_model_name: str | None
    build: AgentBuild | None = None
    knowledge_identity: tuple[int, ...] = ()
    refresh_scheduler: KnowledgeRefreshScheduler | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def get_system_prompt(self) -> str:
        """Render the full caller-bound prompt and retain its build for the call's turns."""
        scheduler = (
            self.context.orchestrator.knowledge_refresh_scheduler if self.context.orchestrator is not None else None
        )
        resolution = await resolve_agent_knowledge_access_async(
            self.agent_name,
            self.config,
            self.runtime_paths,
            refresh_scheduler=scheduler,
            execution_identity=self.execution_identity,
        )

        async def render(build: AgentBuild) -> str:
            return _call_instructions(build)

        return await self.run(
            knowledge=resolution.knowledge,
            refresh_scheduler=scheduler,
            active_model_name=self.active_model_name,
            operation=render,
        )

    async def run(
        self,
        *,
        knowledge: AgentKnowledge | None,
        refresh_scheduler: KnowledgeRefreshScheduler | None,
        active_model_name: str | None,
        operation: Callable[[AgentBuild], Awaitable[str]],
    ) -> str:
        """Run one turn, rebuilding only when call dependencies or the turn's model change."""
        knowledge_identity = knowledge_runtime_identity(knowledge)
        async with self.lock:
            build = self.build
            if (
                build is None
                or self.knowledge_identity != knowledge_identity
                or self.refresh_scheduler is not refresh_scheduler
                or self.active_model_name != active_model_name
            ):
                self.build = None
                self.active_model_name = active_model_name
                # Drain an accepted build before a cancellation propagates, so no construction outlives the call.
                build = await wait_for_future_until_complete(
                    asyncio.create_task(
                        asyncio.to_thread(self._build, knowledge=knowledge, refresh_scheduler=refresh_scheduler),
                    ),
                )
                self.build = build
                self.knowledge_identity = knowledge_identity
                self.refresh_scheduler = refresh_scheduler
            return await operation(build)

    def _build(
        self,
        *,
        knowledge: AgentKnowledge | None,
        refresh_scheduler: KnowledgeRefreshScheduler | None,
    ) -> AgentBuild:
        """Build the call's agent with canonical prompt-sanitizing history storage."""
        history_storage = create_scope_session_storage(
            agent_name=self.agent_name,
            scope=HistoryScope(kind="agent", scope_id=self.agent_name),
            config=self.config,
            runtime_paths=self.runtime_paths,
            execution_identity=self.execution_identity,
        )
        # Build the same tools and system prompt as text chat so call turns reuse its prompt cache.
        # The shell's mindroom-agent CLI stays off: call turns have no turn identity to own it.
        build = create_agent(
            self.agent_name,
            self.config,
            self.runtime_paths,
            execution_identity=self.execution_identity,
            hook_registry=self.context.hook_registry,
            knowledge=knowledge,
            history_storage=history_storage,
            active_model_name=self.active_model_name,
            refresh_scheduler=refresh_scheduler,
            supports_native_tool_approval=True,
            eager_deferred_tools=True,
        )
        return _refuse_text_chat_functions(build, self.config)


async def build_call_tools(
    *,
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    tool_support: ToolRuntimeSupport,
    room_id: str,
    requester_id: str,
    authorize_operation: _CallAuthorizationGuard,
    session_id: str | None = None,
    enable_responder: bool = False,
    reconcile_spoken_response: bool = True,
    voice_instructions: str | None = None,
    active_model_name: str | None = None,
    origin_brief: str | None = None,
) -> CallAgentTooling:
    """Materialize the agent for the selected voice backend."""
    session_id = session_id or create_session_id(room_id, None)
    runtime_model = await asyncio.to_thread(
        functools.partial(
            config.resolve_runtime_model,
            entity_name=agent_name,
            active_model_name=active_model_name,
            room_id=room_id,
            thread_id=None,
            runtime_paths=runtime_paths,
        ),
    )
    active_model_name = runtime_model.model_name
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
    requested_model_name = active_model_name
    # Realtime tools such as claude_agent also run on this model, so every backend applies the caller's budget.
    active_model_name = budget_model(
        config,
        runtime_paths,
        context.budget_monitor,
        requester_id,
        active_model_name,
    )
    context = replace(
        context,
        active_model_name=active_model_name,
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
        if origin_brief:
            voice_enrichment_items = (
                *voice_enrichment_items,
                EnrichmentItem(key="call_origin", text=origin_brief, cache_policy="stable"),
            )
        response_tracker = _CallResponseTracker(
            agent_name=agent_name,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
        )
        agent_cache = _CallAgentCache(
            agent_name=agent_name,
            config=config,
            runtime_paths=runtime_paths,
            context=context,
            execution_identity=execution_identity,
            active_model_name=active_model_name,
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
            agent_cache=agent_cache,
            active_model_name=requested_model_name,
            authorize_operation=authorize_operation,
            reconcile_spoken_response=reconcile_spoken_response,
        )
        return CallAgentTooling(
            tools=(),
            instructions="",
            execution_identity=execution_identity,
            responder=responder,
            finalize_spoken_response=response_tracker.finalize if reconcile_spoken_response else None,
            close=response_tracker.wait_for_settlements,
            get_system_prompt=agent_cache.get_system_prompt,
        )

    refresh_scheduler = context.orchestrator.knowledge_refresh_scheduler if context.orchestrator is not None else None
    knowledge_resolution = await resolve_agent_knowledge_access_async(
        agent_name,
        config,
        runtime_paths,
        refresh_scheduler=refresh_scheduler,
        execution_identity=execution_identity,
    )
    build = await asyncio.to_thread(
        functools.partial(
            create_agent,
            agent_name,
            config,
            runtime_paths,
            execution_identity,
            hook_registry=context.hook_registry,
            knowledge=knowledge_resolution.knowledge,
            include_interactive_questions=False,
            tool_function_filter=context.tool_function_filter,
            refresh_scheduler=refresh_scheduler,
            eager_deferred_tools=True,
            active_model_name=active_model_name,
        ),
    )
    # Paid helper tools, such as claude_agent, record their usage in the call's ledger for the caller.
    usage_owner = _call_usage_owner(
        agent_name,
        session_id,
        created_at=time.time(),
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
    )
    # The call is one run for its tools, which share its session state the way one chat turn's tools do.
    call_context = ToolCallContext(run_id=f"{session_id}:voice", session_id=session_id, user_id=requester_id)
    connections = _CallConnections()
    middleware = agent_tool_middleware(build)
    tools: list[RawFunctionTool] = []
    for toolkit, function in offered_functions(build.spec):
        # The build applied the call's function filter to the functions it built, not to ones added beside them.
        if _function_requires_text_chat(function, config):
            logger.info("call_tool_hidden_needs_text_chat", tool=function.name, agent=agent_name)
            continue
        tools.append(
            _wrap_tool_function(
                function,
                middleware=middleware,
                call_context=call_context,
                context=context,
                agent_name=agent_name,
                authorize_operation=authorize_operation,
                usage_owner=usage_owner,
                connect=functools.partial(connections.connect, toolkit),
            ),
        )
    logger.info("call_tools_built", agent=agent_name, room_id=room_id, tool_count=len(tools))
    return CallAgentTooling(
        tools=tuple(tools),
        instructions=_call_instructions(build),
        execution_identity=execution_identity,
        close=connections.close,
    )


@dataclass
class _CallConnections:
    """The connections a realtime call's tools hold, each opened by the first call that needs it and closed with the call.

    Opening one only once a tool needs it leaves nothing to close when the call never starts.
    """

    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _connected: list[AdaptedToolkit] = field(default_factory=list, init=False)
    _closes: list[ToolkitClose] = field(default_factory=list, init=False)
    _closed: bool = field(default=False, init=False)

    async def connect(self, toolkit: AdaptedToolkit) -> None:
        """Connect ``toolkit`` for the rest of the call, unless an earlier call did or the call has ended."""
        if toolkit.connect is None:
            return
        async with self._lock:
            if self._closed or any(connected is toolkit for connected in self._connected):
                return
            # A tool call cancelled while connecting leaves the connect to the next call that needs it.
            await connect_toolkits((toolkit,), self._closes)
            self._connected.append(toolkit)

    async def close(self) -> None:
        """Close what the call's tools connected once the call ends."""
        async with self._lock:
            self._closed = True
            await close_toolkits(self._closes)


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
    agent_cache: _CallAgentCache,
    active_model_name: str | None,
    authorize_operation: _CallAuthorizationGuard,
    reconcile_spoken_response: bool = True,
) -> CallAgentResponse:
    """Run one finalized transcript only while its current caller remains authorized."""
    await response_tracker.wait_for_settlements()
    async with authorize_operation() as authorized:
        if not authorized:
            return CallAgentResponse(text="")
        return await _run_authorized_call_agent(
            transcript,
            on_tools_executed,
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
            agent_cache=agent_cache,
            active_model_name=active_model_name,
            reconcile_spoken_response=reconcile_spoken_response,
        )


async def _run_authorized_call_agent(
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
    agent_cache: _CallAgentCache,
    active_model_name: str | None,
    reconcile_spoken_response: bool = True,
) -> CallAgentResponse:
    """Run one admitted call transcript through the normal MindRoom agent."""
    from mindroom.ai import ResponseTurnContext, ai_response  # noqa: PLC0415 - heavy optional call path

    if active_model_name is not None:
        # Spend can cross the cap during a call, so each reply checks the budget again.
        active_model_name = budget_model(config, runtime_paths, context.budget_monitor, requester_id, active_model_name)
        context = replace(context, active_model_name=active_model_name)
    recorder = TurnRecorder(user_message=transcript)
    fallback_run_id = f"{session_id}:turn:{uuid4().hex}"
    try:
        refresh_scheduler = (
            context.orchestrator.knowledge_refresh_scheduler if context.orchestrator is not None else None
        )
        knowledge_resolution = await resolve_agent_knowledge_access_async(
            agent_name,
            config,
            runtime_paths,
            refresh_scheduler=refresh_scheduler,
            execution_identity=execution_identity,
        )
        transient_enrichment_items = append_knowledge_availability_enrichment(
            (),
            knowledge_resolution.unavailable,
        )
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
            transient_enrichment_items=transient_enrichment_items,
            system_enrichment_items=voice_enrichment_items,
            inline_tools=True,
        )
        run_metadata: dict[str, Any] = {}

        async def _respond(build: AgentBuild) -> str:
            try:
                return await ai_response(
                    turn,
                    prompt=transcript,
                    runtime_paths=runtime_paths,
                    config=config,
                    knowledge=knowledge_resolution.knowledge,
                    run_id_callback=lambda run_id, _model_name: recorder.set_run_id(run_id),
                    show_tool_calls=False,
                    run_metadata_collector=run_metadata,
                    execution_identity=execution_identity,
                    refresh_scheduler=refresh_scheduler,
                    turn_recorder=recorder,
                    eager_deferred_tools=True,
                    reusable_build=build,
                )
            finally:
                recorder.set_run_metadata({**(recorder.run_metadata or {}), **run_metadata})

        async def _run_with_build(build: AgentBuild) -> str:
            return await tool_support.run_in_context(
                tool_context=context,
                operation=functools.partial(_respond, build),
            )

        response = await agent_cache.run(
            knowledge=knowledge_resolution.knowledge,
            refresh_scheduler=refresh_scheduler,
            active_model_name=active_model_name,
            operation=_run_with_build,
        )
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
    # Live commentary can be paraphrased or omitted; only cascaded TTS owns a
    # one-to-one mapping between this answer and the eventual spoken turn.
    turn_id = response_tracker.register(state) if response and reconcile_spoken_response else None
    if (not response or not reconcile_spoken_response) and state.outcome == "interrupted":
        await response_tracker.persist_unspoken(state, default_status=RunStatus.cancelled)
    return CallAgentResponse(text=response, tool_names=tool_names, turn_id=turn_id)


def _call_agent_run_state(
    recorder: TurnRecorder,
    *,
    session_id: str,
    fallback_run_id: str,
) -> _CallAgentRunState:
    """Snapshot one real agent run for later spoken-text reconciliation.

    The recorder's snapshot leaves out the calls a mid-turn fold summarized, as any interrupted record does.
    """
    snapshot = recorder.interrupted_snapshot()
    return _CallAgentRunState(
        session_id=session_id,
        run_id=recorder.run_id or fallback_run_id,
        user_message=recorder.user_message,
        completed_tools=snapshot.completed_tools,
        interrupted_tools=snapshot.interrupted_tools,
        run_metadata=dict(recorder.run_metadata or {}),
        outcome=recorder.outcome,
        original_status=recorder.original_status,
    )


def _call_instructions(build: AgentBuild) -> str:
    """Return the system message a chat turn of the agent starts from, without the turn's own context."""
    content = prompt_for_turn(build).content
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


def _function_requires_text_chat(function: Function | ToolFunction, config: Config) -> bool:
    """Return whether a function needs text-chat UI that a voice call lacks."""
    is_matrix_room_runtime_function = (
        function.name in MATRIX_ROOM_RUNTIME_TOOL_NAMES and function.approval_type == MATRIX_ROOM_RUNTIME_APPROVAL_TYPE
    )
    return bool(
        function.requires_confirmation
        or function.external_execution
        or function.approval_type == "required"
        or function.name in _CALL_UNAVAILABLE_COMPOSITE_FUNCTIONS
        or (not is_matrix_room_runtime_function and tool_may_require_approval(config, function.name)),
    )


def _refuse_text_chat_functions(build: AgentBuild, config: Config) -> AgentBuild:
    """Keep functions that need text chat visible, so the prompt matches chat's, but refuse them when called."""
    refused: set[str] = set()

    def callable_in_a_call(function: ToolFunction) -> ToolFunction:
        if function.external_execution_silent:
            # Chat hands delegation to its driver; calls run the child turn directly.
            function = replace(function, external_execution=False, external_execution_silent=False)
        if not _function_requires_text_chat(function, config):
            return function
        refused.add(function.name)
        # The loop pauses a run for these flags before any tool-call policy can refuse the call.
        return replace(function, requires_confirmation=False, external_execution=False, approval_type=None)

    async def refuse(call: ToolCall, call_next: ToolNext) -> object:
        if call.function.name in refused:
            logger.info("call_tool_blocked_needs_text_chat", tool=call.function.name)
            return _TEXT_CHAT_REQUIRED_MESSAGE
        return await call_next(call.arguments)

    toolkits = tuple(
        replace(toolkit, functions=tuple(callable_in_a_call(function) for function in toolkit.functions))
        for toolkit in build.spec.toolkits
    )
    # A call's turns run their tools inline, so job admission runs every call in place; the refusal answers before
    # the rest of the policy sees the call.
    return replace(build, spec=replace(build.spec, toolkits=toolkits), tool_policy=(refuse, *build.tool_policy))


def matrix_message_available_during_call(config: Config, agent_name: str) -> bool:
    """Return whether the call agent can send Matrix messages; tools that may need approval are unavailable in calls."""
    return "matrix_message" in config.resolve_entity(agent_name).available_tools and not tool_may_require_approval(
        config,
        "matrix_message",
    )


def _function_available_during_call(
    function: Function | ToolFunction,
    *,
    config: Config,
    agent_name: str,
) -> bool:
    """Keep only functions whose execution can complete without text UI."""
    unavailable = _function_requires_text_chat(function, config)
    if unavailable:
        logger.debug("call_tool_hidden_needs_text_chat", tool=function.name, agent=agent_name)
    return not unavailable


def _realtime_parameters(function: ToolFunction) -> dict[str, Any]:
    """Return the realtime model's parameters schema: the function's schema after a second Agno processing pass.

    A second pass closes a schema Agno's processing produced to other keys once it differs from Agno's empty schema,
    which the voice_call_realtime golden pins, and leaves a schema the tool supplies as is
    (``skip_entrypoint_processing``, as MCP tools do) unchanged. An empty schema becomes an object without properties.
    """
    schema = dict(function.parameters)
    if not schema:
        return {"type": "object", "properties": {}}
    # TODO(simplify): `additionalProperties: false` here exists only because main processed each function twice. Offer
    # the function's schema as it is. The realtime model would see tool schemas without it; the golden pins it.
    if function.entrypoint_processed and schema != AGNO_EMPTY_PARAMETERS:
        schema["additionalProperties"] = False
    return schema


def _wrap_tool_function(
    function: ToolFunction,
    *,
    middleware: Sequence[ToolMiddleware],
    call_context: ToolCallContext,
    context: ToolRuntimeContext,
    agent_name: str,
    authorize_operation: _CallAuthorizationGuard,
    usage_owner: HelperUsageOwner,
    connect: Callable[[], Awaitable[None]],
) -> RawFunctionTool:
    """Wrap one of the build's functions as a livekit raw function tool that runs under the build's policy.

    ``connect`` connects the function's toolkit for the call before the function first runs, when it holds one.
    """
    from livekit.agents import llm  # noqa: PLC0415

    raw_schema = {
        "name": function.name,
        "description": function.description or function.name,
        "parameters": _realtime_parameters(function),
    }

    async def _handler(raw_arguments: dict[str, Any]) -> str:
        async with authorize_operation() as authorized:
            if not authorized:
                return "Call access was revoked before this tool could run."
            logger.info("call_tool_executing", tool=function.name, agent=agent_name, room_id=context.room_id)
            try:
                await connect()
                with tool_runtime_context(context), helper_usage_owner_context(usage_owner):
                    result = await run_tool_call(function, raw_arguments, call_context, middleware=middleware)
            except Exception as error:
                logger.warning("call_tool_failed", tool=function.name, agent=agent_name, error=str(error))
                return f"Tool {function.name} failed: {error}"
        return _normalize_tool_result(result)

    return llm.function_tool(_handler, raw_schema=raw_schema)
