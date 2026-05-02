"""Shared runtime context and support helpers for tool calls."""

from __future__ import annotations

import threading
from collections.abc import AsyncGenerator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, TypeVar, runtime_checkable
from uuid import uuid4

from mindroom.hooks import (
    CustomEventContext,
    HookContextSupport,
    HookRegistry,
    MessageEnvelope,
    build_hook_room_state_putter,
    build_hook_room_state_querier,
    emit,
    validate_event_name,
)
from mindroom.logging_config import get_logger
from mindroom.message_target import MessageTarget
from mindroom.tool_system.plugin_identity import validate_plugin_name
from mindroom.tool_system.worker_routing import build_tool_execution_identity

if TYPE_CHECKING:
    import asyncio
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
    from pathlib import Path

    import nio
    from structlog.stdlib import BoundLogger

    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.conversation_resolver import ConversationResolver
    from mindroom.hooks import HookMatrixAdmin, HookMessageSender, HookRoomStatePutter, HookRoomStateQuerier
    from mindroom.matrix.conversation_cache import ConversationCacheProtocol, ConversationEventCache
    from mindroom.matrix.identity import MatrixID
    from mindroom.scheduling import SchedulingRuntime
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity
    from mindroom.workers.models import WorkerReadyProgress

_ToolContextReturn = TypeVar("_ToolContextReturn")
_StreamChunk = TypeVar("_StreamChunk")


@runtime_checkable
class _AsyncClosableIterator(Protocol):
    """Minimal async-iterator surface that can be closed explicitly."""

    async def aclose(self) -> None:
        """Close the async iterator and release any underlying resources."""


@contextmanager
def _tool_runtime_context_scope(tool_context: ToolRuntimeContext | None) -> Iterator[None]:
    """Bind tool runtime state only for the duration of one concrete operation."""
    with tool_runtime_context(tool_context):
        yield


@dataclass(frozen=True)
class ToolRuntimeContext:
    """Shared runtime metadata available to all tools."""

    agent_name: str
    room_id: str
    thread_id: str | None
    resolved_thread_id: str | None
    requester_id: str
    client: nio.AsyncClient
    config: Config
    runtime_paths: RuntimePaths
    event_cache: ConversationEventCache
    conversation_cache: ConversationCacheProtocol
    transport_agent_name: str | None = None
    active_model_name: str | None = None
    session_id: str | None = None
    room: nio.MatrixRoom | None = None
    reply_to_event_id: str | None = None
    storage_path: Path | None = None
    attachment_ids: tuple[str, ...] = field(default_factory=tuple)
    runtime_attachment_ids: list[str] = field(default_factory=list)
    hook_registry: HookRegistry = field(default_factory=HookRegistry.empty)
    correlation_id: str | None = None
    hook_message_sender: HookMessageSender | None = None
    matrix_admin: HookMatrixAdmin | None = None
    room_state_querier: HookRoomStateQuerier | None = None
    room_state_putter: HookRoomStatePutter | None = None
    message_received_depth: int = 0


@dataclass(frozen=True)
class ToolDispatchContext:
    """Detached execution identity for tool dispatch outside a live Matrix runtime."""

    execution_identity: ToolExecutionIdentity

    @classmethod
    def from_target(
        cls,
        *,
        agent_name: str,
        runtime_paths: RuntimePaths,
        requester_user_id: str | None,
        target: MessageTarget | None,
    ) -> ToolDispatchContext:
        """Build the detached dispatch contract for one explicit Matrix target."""
        return cls(
            execution_identity=build_tool_execution_identity(
                channel="matrix",
                agent_name=agent_name,
                runtime_paths=runtime_paths,
                requester_id=requester_user_id,
                room_id=target.room_id if target is not None else None,
                thread_id=target.resolved_thread_id if target is not None else None,
                resolved_thread_id=target.resolved_thread_id if target is not None else None,
                session_id=target.session_id if target is not None else None,
            ),
        )


@dataclass(frozen=True)
class LiveToolDispatchContext(ToolDispatchContext):
    """Execution identity paired with one matching live Matrix runtime context."""

    runtime_context: ToolRuntimeContext

    def __post_init__(self) -> None:
        """Validate that the detached identity and live runtime represent the same dispatch."""
        if not execution_identity_matches_tool_runtime_context(self.execution_identity, self.runtime_context):
            msg = "Live tool dispatch execution_identity must match the provided tool runtime context"
            raise ValueError(msg)

    @classmethod
    def from_runtime_context(cls, runtime_context: ToolRuntimeContext) -> LiveToolDispatchContext:
        """Build the live dispatch contract represented by one tool runtime context."""
        return cls(
            execution_identity=build_execution_identity_from_runtime_context(runtime_context),
            runtime_context=runtime_context,
        )


@dataclass(frozen=True)
class ToolRuntimeHookBindings:
    """Resolved hook-facing bindings derived from one tool runtime context."""

    message_sender: HookMessageSender | None
    matrix_admin: HookMatrixAdmin | None
    room_state_querier: HookRoomStateQuerier | None
    room_state_putter: HookRoomStatePutter | None
    message_received_depth: int


@dataclass(frozen=True, slots=True)
class WorkerProgressEvent:
    """One worker warmup progress event routed back into streaming delivery."""

    tool_name: str
    function_name: str
    progress: WorkerReadyProgress


@dataclass
class WorkerProgressPump:
    """Bridge worker warmup progress from sync worker threads back to the stream loop."""

    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[WorkerProgressEvent]
    shutdown: threading.Event


@dataclass
class ToolRuntimeSupport:
    """Own shared tool-runtime context building and scoped execution helpers."""

    runtime: BotRuntimeView
    logger: BoundLogger
    runtime_paths: RuntimePaths
    storage_path: Path
    agent_name: str
    matrix_id: MatrixID
    resolver: ConversationResolver
    hook_context: HookContextSupport

    def build_context(
        self,
        target: MessageTarget,
        *,
        user_id: str | None,
        session_id: str | None = None,
        agent_name: str | None = None,
        active_model_name: str | None = None,
        attachment_ids: list[str] | tuple[str, ...] | None = None,
        correlation_id: str | None = None,
        source_envelope: MessageEnvelope | None = None,
    ) -> ToolRuntimeContext | None:
        """Build shared runtime context for all tool calls."""
        client = self.runtime.client
        if client is None:
            return None
        event_cache = self.runtime.event_cache
        if event_cache is None:
            return None
        target_room_id = target.room_id
        target_thread_id = target.source_thread_id
        target_resolved_thread_id = target.resolved_thread_id
        target_reply_to_event_id = target.reply_to_event_id
        return ToolRuntimeContext(
            agent_name=agent_name or self.agent_name,
            room_id=target_room_id,
            thread_id=target_thread_id,
            resolved_thread_id=target_resolved_thread_id,
            requester_id=user_id or self.matrix_id.full_id,
            client=client,
            config=self.runtime.config,
            runtime_paths=self.runtime_paths,
            conversation_cache=self.resolver.deps.conversation_cache,
            event_cache=event_cache,
            transport_agent_name=self.agent_name,
            active_model_name=active_model_name,
            session_id=session_id or target.session_id,
            room=self.resolver.cached_room(target_room_id),
            reply_to_event_id=target_reply_to_event_id,
            storage_path=self.storage_path,
            attachment_ids=tuple(attachment_ids or ()),
            hook_registry=self.hook_context.registry,
            correlation_id=correlation_id,
            hook_message_sender=self.hook_context.message_sender(),
            matrix_admin=self.hook_context.matrix_admin(),
            room_state_querier=self.hook_context.room_state_querier(),
            room_state_putter=self.hook_context.room_state_putter(),
            message_received_depth=(source_envelope.message_received_depth if source_envelope is not None else 0),
        )

    def build_required_context(
        self,
        target: MessageTarget,
        *,
        user_id: str | None,
        session_id: str | None = None,
        agent_name: str | None = None,
        active_model_name: str | None = None,
        attachment_ids: list[str] | tuple[str, ...] | None = None,
        correlation_id: str | None = None,
        source_envelope: MessageEnvelope | None = None,
    ) -> ToolRuntimeContext:
        """Build one live Matrix tool runtime context or fail fast if runtime support is unavailable."""
        context = self.build_context(
            target,
            user_id=user_id,
            session_id=session_id,
            agent_name=agent_name,
            active_model_name=active_model_name,
            attachment_ids=attachment_ids,
            correlation_id=correlation_id,
            source_envelope=source_envelope,
        )
        if context is None:
            msg = "Live Matrix tool dispatch requires initialized tool runtime support"
            raise RuntimeError(msg)
        return context

    def build_dispatch_context(
        self,
        target: MessageTarget,
        *,
        user_id: str | None,
        session_id: str | None = None,
        agent_name: str | None = None,
        active_model_name: str | None = None,
        attachment_ids: list[str] | tuple[str, ...] | None = None,
        correlation_id: str | None = None,
        source_envelope: MessageEnvelope | None = None,
    ) -> ToolDispatchContext:
        """Build the canonical detached or live dispatch contract for one tool call."""
        execution_identity = self.build_execution_identity(
            target=target,
            user_id=user_id,
            session_id=session_id or target.session_id,
            agent_name=agent_name,
        )
        context = self.build_context(
            target,
            user_id=user_id,
            session_id=session_id,
            agent_name=agent_name,
            active_model_name=active_model_name,
            attachment_ids=attachment_ids,
            correlation_id=correlation_id,
            source_envelope=source_envelope,
        )
        if context is None:
            return ToolDispatchContext(execution_identity=execution_identity)
        return LiveToolDispatchContext.from_runtime_context(context)

    def build_required_live_dispatch_context(
        self,
        target: MessageTarget,
        *,
        user_id: str | None,
        session_id: str | None = None,
        agent_name: str | None = None,
        active_model_name: str | None = None,
        attachment_ids: list[str] | tuple[str, ...] | None = None,
        correlation_id: str | None = None,
        source_envelope: MessageEnvelope | None = None,
    ) -> LiveToolDispatchContext:
        """Build one live Matrix dispatch contract or fail fast if runtime support is unavailable."""
        context = self.build_required_context(
            target,
            user_id=user_id,
            session_id=session_id,
            agent_name=agent_name,
            active_model_name=active_model_name,
            attachment_ids=attachment_ids,
            correlation_id=correlation_id,
            source_envelope=source_envelope,
        )
        return LiveToolDispatchContext.from_runtime_context(context)

    def build_execution_identity(
        self,
        *,
        target: MessageTarget,
        user_id: str | None,
        session_id: str,
        agent_name: str | None = None,
    ) -> ToolExecutionIdentity:
        """Build the serializable execution identity used for worker routing."""
        return build_tool_execution_identity(
            channel="matrix",
            agent_name=agent_name or self.agent_name,
            transport_agent_name=self.agent_name,
            runtime_paths=self.runtime_paths,
            requester_id=user_id or self.matrix_id.full_id,
            room_id=target.room_id,
            thread_id=target.resolved_thread_id,
            resolved_thread_id=target.resolved_thread_id,
            session_id=session_id,
        )

    async def run_in_context(
        self,
        *,
        tool_context: ToolRuntimeContext | None,
        operation: Callable[[], Awaitable[_ToolContextReturn]],
    ) -> _ToolContextReturn:
        """Execute one async operation inside the ambient tool runtime context."""
        with _tool_runtime_context_scope(tool_context):
            return await operation()

    def stream_in_context(
        self,
        *,
        tool_context: ToolRuntimeContext | None,
        stream_factory: Callable[[], AsyncIterator[_StreamChunk]],
    ) -> AsyncIterator[_StreamChunk]:
        """Wrap one async iterator without spanning tool-runtime tokens across yields."""

        async def wrapped_stream() -> AsyncIterator[_StreamChunk]:
            stream: AsyncIterator[_StreamChunk] | None = None
            try:
                with _tool_runtime_context_scope(tool_context):
                    stream = stream_factory()
                while True:
                    try:
                        with _tool_runtime_context_scope(tool_context):
                            chunk = await anext(stream)
                    except StopAsyncIteration:
                        return
                    yield chunk
            finally:
                if isinstance(stream, (AsyncGenerator, _AsyncClosableIterator)):
                    with _tool_runtime_context_scope(tool_context):
                        await stream.aclose()

        return wrapped_stream()


_TOOL_RUNTIME_CONTEXT: ContextVar[ToolRuntimeContext | None] = ContextVar(
    "tool_runtime_context",
    default=None,
)
_WORKER_PROGRESS_PUMP: ContextVar[WorkerProgressPump | None] = ContextVar(
    "worker_progress_pump",
    default=None,
)


def get_tool_runtime_context() -> ToolRuntimeContext | None:
    """Get the current shared tool runtime context."""
    return _TOOL_RUNTIME_CONTEXT.get()


def get_worker_progress_pump() -> WorkerProgressPump | None:
    """Get the current worker progress pump bound to the stream task."""
    return _WORKER_PROGRESS_PUMP.get()


def resolve_tool_runtime_hook_bindings(context: ToolRuntimeContext) -> ToolRuntimeHookBindings:
    """Return the canonical hook-facing bindings for one tool runtime context."""
    return ToolRuntimeHookBindings(
        message_sender=context.hook_message_sender,
        matrix_admin=context.matrix_admin,
        room_state_querier=context.room_state_querier or build_hook_room_state_querier(context.client),
        room_state_putter=context.room_state_putter or build_hook_room_state_putter(context.client),
        message_received_depth=context.message_received_depth,
    )


def resolve_current_session_id(
    *,
    execution_identity: ToolExecutionIdentity | None = None,
    runtime_context: ToolRuntimeContext | None = None,
) -> str | None:
    """Resolve the current session ID from explicit execution/runtime state."""
    if execution_identity is not None and execution_identity.session_id is not None:
        return execution_identity.session_id

    resolved_runtime_context = runtime_context if runtime_context is not None else get_tool_runtime_context()
    if resolved_runtime_context is not None and resolved_runtime_context.session_id is not None:
        return resolved_runtime_context.session_id

    return None


def build_execution_identity_from_runtime_context(context: ToolRuntimeContext) -> ToolExecutionIdentity:
    """Build the canonical execution identity represented by one live runtime context."""
    target = MessageTarget.from_runtime_context(context)
    return build_tool_execution_identity(
        channel="matrix",
        agent_name=context.agent_name,
        transport_agent_name=context.transport_agent_name or context.agent_name,
        runtime_paths=context.runtime_paths,
        requester_id=context.requester_id,
        room_id=target.room_id,
        thread_id=target.resolved_thread_id,
        resolved_thread_id=target.resolved_thread_id,
        session_id=target.session_id,
    )


def execution_identity_matches_tool_runtime_context(
    execution_identity: ToolExecutionIdentity,
    context: ToolRuntimeContext,
) -> bool:
    """Return whether one execution identity represents the same live Matrix tool runtime."""
    target = MessageTarget.from_runtime_context(context)
    valid_thread_ids = {target.source_thread_id, target.resolved_thread_id}
    return (
        execution_identity.channel == "matrix"
        and execution_identity.agent_name == context.agent_name
        and execution_identity.requester_id == context.requester_id
        and execution_identity.room_id == context.room_id
        and execution_identity.thread_id in valid_thread_ids
        and execution_identity.resolved_thread_id == target.resolved_thread_id
        and execution_identity.session_id == target.session_id
        and execution_identity.tenant_id == context.runtime_paths.env_value("CUSTOMER_ID")
        and execution_identity.account_id == context.runtime_paths.env_value("ACCOUNT_ID")
        and (execution_identity.transport_agent_name or execution_identity.agent_name)
        == (context.transport_agent_name or context.agent_name)
    )


def runtime_context_from_dispatch_context(dispatch_context: ToolDispatchContext) -> ToolRuntimeContext | None:
    """Return the live runtime context when one dispatch contract is runtime-bound."""
    if isinstance(dispatch_context, LiveToolDispatchContext):
        return dispatch_context.runtime_context
    return None


def build_scheduling_runtime_from_tool_runtime_context(context: ToolRuntimeContext) -> SchedulingRuntime:
    """Build the canonical live scheduling runtime for one Matrix tool context."""
    from mindroom.scheduling import SchedulingRuntime  # noqa: PLC0415

    if context.room is None:
        msg = "Scheduling runtime requires a cached Matrix room in tool runtime context"
        raise RuntimeError(msg)
    return SchedulingRuntime(
        client=context.client,
        config=context.config,
        runtime_paths=context.runtime_paths,
        room=context.room,
        conversation_cache=context.conversation_cache,
        event_cache=context.event_cache,
        matrix_admin=context.matrix_admin,
    )


def attachment_id_available_in_tool_runtime_context(
    context: ToolRuntimeContext,
    attachment_id: str,
) -> bool:
    """Return whether an attachment ID is currently available in context."""
    normalized_attachment_id = attachment_id.strip()
    if not normalized_attachment_id:
        return False
    return (
        normalized_attachment_id in context.attachment_ids or normalized_attachment_id in context.runtime_attachment_ids
    )


def list_tool_runtime_attachment_ids(context: ToolRuntimeContext) -> list[str]:
    """Return all attachment IDs currently available in runtime context order."""
    attachment_ids: list[str] = []
    for attachment_id in (*context.attachment_ids, *context.runtime_attachment_ids):
        if attachment_id and attachment_id not in attachment_ids:
            attachment_ids.append(attachment_id)
    return attachment_ids


def append_tool_runtime_attachment_id(attachment_id: str) -> ToolRuntimeContext | None:
    """Append an attachment ID to the current tool context, preserving order."""
    context = get_tool_runtime_context()
    if context is None:
        return None

    normalized_attachment_id = attachment_id.strip()
    if not normalized_attachment_id:
        return context
    if attachment_id_available_in_tool_runtime_context(context, normalized_attachment_id):
        return context

    context.runtime_attachment_ids.append(normalized_attachment_id)
    return context


def get_plugin_state_root(
    plugin_name: str,
    *,
    runtime_paths: RuntimePaths | None = None,
) -> Path:
    """Return the canonical plugin state root used by hooks and plugin tools."""
    normalized_plugin_name = validate_plugin_name(plugin_name)

    context = get_tool_runtime_context()
    resolved_runtime_paths = runtime_paths or (context.runtime_paths if context is not None else None)
    if resolved_runtime_paths is None:
        msg = "runtime_paths are required when no tool runtime context is active"
        raise RuntimeError(msg)

    plugin_root = resolved_runtime_paths.storage_root / "plugins" / normalized_plugin_name
    plugin_root.mkdir(parents=True, exist_ok=True)
    return plugin_root


async def emit_custom_event(
    plugin_name: str,
    event_name: str,
    payload: dict[str, object],
) -> None:
    """Emit a namespaced custom hook event from tool code on the primary process."""
    validate_event_name(event_name)
    context = get_tool_runtime_context()
    if context is None:
        msg = "emit_custom_event() requires an active tool runtime context"
        raise RuntimeError(msg)
    if not context.hook_registry.has_hooks(event_name):
        return

    correlation_id = context.correlation_id or f"{event_name}:{uuid4().hex}"
    bindings = resolve_tool_runtime_hook_bindings(context)
    hook_context = CustomEventContext(
        event_name=event_name,
        plugin_name="",
        settings={},
        config=context.config,
        runtime_paths=context.runtime_paths,
        logger=get_logger("mindroom.hooks.tools").bind(event_name=event_name),
        correlation_id=correlation_id,
        message_sender=bindings.message_sender,
        matrix_admin=bindings.matrix_admin,
        room_state_querier=bindings.room_state_querier,
        room_state_putter=bindings.room_state_putter,
        payload=payload,
        source_plugin=plugin_name,
        room_id=context.room_id,
        thread_id=context.resolved_thread_id,
        sender_id=context.requester_id,
        message_received_depth=bindings.message_received_depth,
    )
    await emit(context.hook_registry, event_name, hook_context)


@contextmanager
def tool_runtime_context(context: ToolRuntimeContext | None) -> Iterator[None]:
    """Set shared tool runtime context for the current async execution scope."""
    token = _TOOL_RUNTIME_CONTEXT.set(context)
    try:
        yield
    finally:
        _TOOL_RUNTIME_CONTEXT.reset(token)


@contextmanager
def worker_progress_pump_scope(
    loop: asyncio.AbstractEventLoop,
    queue: asyncio.Queue[WorkerProgressEvent],
) -> Iterator[WorkerProgressPump]:
    """Bind one worker progress pump for the lifetime of one streaming response."""
    pump = WorkerProgressPump(loop=loop, queue=queue, shutdown=threading.Event())
    token = _WORKER_PROGRESS_PUMP.set(pump)
    try:
        yield pump
    finally:
        pump.shutdown.set()
        _WORKER_PROGRESS_PUMP.reset(token)
