"""Hook context and transport dataclasses."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mindroom.constants import HOOK_MESSAGE_RECEIVED_DEPTH_KEY, ORIGINAL_SENDER_KEY, ROUTER_AGENT_NAME
from mindroom.logging_config import get_logger
from mindroom.runtime_protocols import SupportsClientConfigOrchestrator  # noqa: TC001
from mindroom.tool_system.plugin_identity import validate_plugin_name

from .state import (
    build_hook_room_state_putter,
    build_hook_room_state_querier,
    chain_hook_room_state_putters,
    chain_hook_room_state_queriers,
)
from .types import (
    EVENT_TOOL_AFTER_CALL,
    EVENT_TOOL_BEFORE_CALL,
    EnrichmentCachePolicy,
    EnrichmentItem,
)


class _UnsetType:
    """Sentinel type for omitted optional hook arguments."""


_UNSET = _UnsetType()

if TYPE_CHECKING:
    from pathlib import Path

    import structlog
    from agno.models.message import Message

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.history.types import HistoryScope
    from mindroom.message_target import MessageTarget
    from mindroom.scheduling import ScheduledWorkflow
    from mindroom.tool_system.events import ToolTraceEntry

    from .registry import HookRegistry, HookRegistryState
    from .sender import HookMessageSender
    from .types import HookMatrixAdmin, HookRoomStatePutter, HookRoomStateQuerier


def _resolve_plugin_state_root(
    runtime_paths: RuntimePaths | None,
    plugin_name: str,
) -> Path:
    """Return the plugin state root, creating it on first access."""
    if runtime_paths is None:
        msg = "runtime_paths are required to access hook state_root"
        raise RuntimeError(msg)
    plugin_root = runtime_paths.storage_root / "plugins" / validate_plugin_name(plugin_name)
    plugin_root.mkdir(parents=True, exist_ok=True)
    return plugin_root


async def _send_bound_message(
    logger: structlog.stdlib.BoundLogger,
    message_sender: HookMessageSender | None,
    plugin_name: str,
    event_name: str,
    room_id: str,
    text: str,
    *,
    thread_id: str | None = None,
    extra_content: dict[str, Any] | None = None,
    requester_id: str | None = None,
    message_received_depth: int = 0,
    trigger_dispatch: bool = False,
) -> str | None:
    """Send one hook-originated Matrix message through a bound sender."""
    if message_sender is None:
        logger.warning("send_message called but no sender registered")
        return None
    source_hook = f"{plugin_name}:{event_name}"
    resolved_extra_content = dict(extra_content or {})
    if requester_id:
        resolved_extra_content.setdefault(ORIGINAL_SENDER_KEY, requester_id)
    if message_received_depth > 0:
        resolved_extra_content[HOOK_MESSAGE_RECEIVED_DEPTH_KEY] = message_received_depth
    return await message_sender(
        room_id,
        text,
        thread_id,
        source_hook,
        resolved_extra_content or None,
        trigger_dispatch=trigger_dispatch,
    )


async def _query_bound_room_state(
    logger: structlog.stdlib.BoundLogger,
    room_state_querier: HookRoomStateQuerier | None,
    room_id: str,
    event_type: str,
    state_key: str | None = None,
) -> dict[str, Any] | None:
    """Query Matrix room state through a bound hook querier when available."""
    if room_state_querier is None:
        logger.warning("No room state querier available")
        return None
    return await room_state_querier(room_id, event_type, state_key)


async def _put_bound_room_state(
    logger: structlog.stdlib.BoundLogger,
    room_state_putter: HookRoomStatePutter | None,
    room_id: str,
    event_type: str,
    state_key: str,
    content: dict[str, Any],
) -> bool:
    """Write Matrix room state through a bound hook putter when available."""
    if room_state_putter is None:
        logger.warning("No room state putter available")
        return False
    return await room_state_putter(room_id, event_type, state_key, content)


@dataclass
class HookContextSupport:
    """Own live hook bindings and shared hook-context base fields."""

    runtime: SupportsClientConfigOrchestrator
    logger: structlog.stdlib.BoundLogger
    runtime_paths: RuntimePaths
    agent_name: str
    hook_registry_state: HookRegistryState
    hook_send_message: HookMessageSender

    @property
    def registry(self) -> HookRegistry:
        """Return the currently active hook registry snapshot."""
        return self.hook_registry_state.registry

    def message_sender(self) -> HookMessageSender | None:
        """Return the current sender bound into hook contexts."""
        if self.runtime.client is not None:
            return self.hook_send_message
        orchestrator = self.runtime.orchestrator
        if orchestrator is not None:
            sender = orchestrator._hook_message_sender()
            if sender is not None:
                return sender
        return None

    def room_state_querier(self) -> HookRoomStateQuerier | None:
        """Return the room-state querier bound into hook contexts."""
        primary = build_hook_room_state_querier(self.runtime.client) if self.runtime.client is not None else None
        fallback = None
        orchestrator = self.runtime.orchestrator
        if self.agent_name != ROUTER_AGENT_NAME and orchestrator is not None:
            fallback = orchestrator._hook_room_state_querier()
        return chain_hook_room_state_queriers(primary, fallback)

    def room_state_putter(self) -> HookRoomStatePutter | None:
        """Return the room-state putter bound into hook contexts."""
        primary = build_hook_room_state_putter(self.runtime.client) if self.runtime.client is not None else None
        fallback = None
        orchestrator = self.runtime.orchestrator
        if self.agent_name != ROUTER_AGENT_NAME and orchestrator is not None:
            fallback = orchestrator._hook_room_state_putter()
        return chain_hook_room_state_putters(primary, fallback)

    def matrix_admin(self) -> HookMatrixAdmin | None:
        """Return the router-backed Matrix admin helper bound into hook contexts."""
        orchestrator = self.runtime.orchestrator
        if orchestrator is not None:
            admin = orchestrator._hook_matrix_admin()
            if admin is not None:
                return admin
        if self.agent_name == ROUTER_AGENT_NAME and self.runtime.client is not None:
            from .matrix_admin import build_hook_matrix_admin  # noqa: PLC0415

            return build_hook_matrix_admin(self.runtime.client, self.runtime_paths)
        return None

    def base_kwargs(self, event_name: str, correlation_id: str) -> dict[str, Any]:
        """Return shared base fields for hook context construction."""
        return {
            "event_name": event_name,
            "plugin_name": "",
            "settings": {},
            "config": self.runtime.config,
            "runtime_paths": self.runtime_paths,
            "logger": self.logger.bind(event_name=event_name),
            "correlation_id": correlation_id,
            "message_sender": self.message_sender(),
            "matrix_admin": self.matrix_admin(),
            "room_state_querier": self.room_state_querier(),
            "room_state_putter": self.room_state_putter(),
        }


@dataclass(frozen=True, slots=True)
class MessageEnvelope:
    """Normalized inbound message shape used by message hooks."""

    source_event_id: str
    room_id: str
    target: MessageTarget
    requester_id: str
    sender_id: str
    body: str
    attachment_ids: tuple[str, ...]
    mentioned_agents: tuple[str, ...]
    agent_name: str
    source_kind: str
    hook_source: str | None = None
    message_received_depth: int = 0


@dataclass(slots=True)
class ResponseDraft:
    """Mutable outbound response candidate for before-response hooks."""

    response_text: str
    response_kind: str
    tool_trace: list[ToolTraceEntry] | None
    extra_content: dict[str, Any] | None
    envelope: MessageEnvelope
    suppress: bool = False


@dataclass(frozen=True, slots=True)
class ResponseResult:
    """Final outcome after send or edit."""

    response_text: str
    response_event_id: str
    delivery_kind: str
    response_kind: str
    envelope: MessageEnvelope


@dataclass(slots=True)
class HookContext:
    """Base fields available to every hook."""

    event_name: str
    plugin_name: str
    settings: dict[str, Any]
    config: Config
    runtime_paths: RuntimePaths
    logger: structlog.stdlib.BoundLogger
    correlation_id: str
    message_sender: HookMessageSender | None = field(default=None, kw_only=True)
    matrix_admin: HookMatrixAdmin | None = field(default=None, kw_only=True)
    room_state_querier: HookRoomStateQuerier | None = field(default=None, kw_only=True)
    room_state_putter: HookRoomStatePutter | None = field(default=None, kw_only=True)

    @property
    def state_root(self) -> Path:
        """Return the plugin state root, creating it on first access."""
        return _resolve_plugin_state_root(self.runtime_paths, self.plugin_name)

    async def send_message(
        self,
        room_id: str,
        text: str,
        *,
        thread_id: str | None = None,
        extra_content: dict[str, Any] | None = None,
        trigger_dispatch: bool = False,
    ) -> str | None:
        """Send a Matrix message from a hook and return the event ID when available.

        Plain ``hook`` sends may still dispatch when they satisfy the
        usual routing rules. When *trigger_dispatch* is True the message
        uses source_kind ``hook_dispatch``, which also bypasses the
        normal "ignore other agent unless mentioned" ingress gate before
        re-entering the normal dispatch pipeline. Automation that
        originates from ``message:received`` re-enters
        ``message:received`` at most once: MindRoom skips the origin
        plugin on the first synthetic hop, then suppresses deeper
        ``message:received`` re-entry for the rest of that synthetic
        chain to avoid cross-plugin feedback loops.
        """
        return await _send_bound_message(
            self.logger,
            self.message_sender,
            self.plugin_name,
            self.event_name,
            room_id,
            text,
            thread_id=thread_id,
            extra_content=extra_content,
            requester_id=_requester_id_for_hook_send(self, trigger_dispatch=trigger_dispatch),
            message_received_depth=_message_received_depth_for_hook_send(self),
            trigger_dispatch=trigger_dispatch,
        )

    async def query_room_state(
        self,
        room_id: str,
        event_type: str,
        state_key: str | None = None,
    ) -> dict[str, Any] | None:
        """Query Matrix room state and return the result when a querier is available."""
        return await _query_bound_room_state(
            self.logger,
            self.room_state_querier,
            room_id,
            event_type,
            state_key,
        )

    async def put_room_state(
        self,
        room_id: str,
        event_type: str,
        state_key: str,
        content: dict[str, Any],
    ) -> bool:
        """Write a Matrix room state event and return ``True`` on success."""
        return await _put_bound_room_state(
            self.logger,
            self.room_state_putter,
            room_id,
            event_type,
            state_key,
            content,
        )


@dataclass(slots=True)
class MessageReceivedContext(HookContext):
    """Context for message:received hooks."""

    envelope: MessageEnvelope
    skip_plugin_names: frozenset[str] = field(default_factory=frozenset)
    suppress: bool = False


@dataclass(slots=True)
class MessageEnrichContext(HookContext):
    """Context for message:enrich hooks."""

    envelope: MessageEnvelope
    target_entity_name: str
    target_member_names: tuple[str, ...] | None
    _items: list[EnrichmentItem] = field(default_factory=list)

    def add_metadata(
        self,
        key: str,
        text: str,
        *,
        cache_policy: EnrichmentCachePolicy = "volatile",
    ) -> None:
        """Append one enrichment item for this hook."""
        self._items.append(EnrichmentItem(key=key, text=text, cache_policy=cache_policy))


@dataclass(slots=True)
class SystemEnrichContext(HookContext):
    """Context for system:enrich hooks."""

    envelope: MessageEnvelope
    target_entity_name: str
    target_member_names: tuple[str, ...] | None
    _items: list[EnrichmentItem] = field(default_factory=list)

    def add_instruction(
        self,
        key: str,
        text: str,
        *,
        cache_policy: EnrichmentCachePolicy = "volatile",
    ) -> None:
        """Append one system-prompt enrichment item."""
        self._items.append(EnrichmentItem(key=key, text=text, cache_policy=cache_policy))


@dataclass(slots=True)
class BeforeResponseContext(HookContext):
    """Context for message:before_response hooks."""

    draft: ResponseDraft


@dataclass(slots=True)
class AfterResponseContext(HookContext):
    """Context for message:after_response hooks."""

    result: ResponseResult


@dataclass(frozen=True, slots=True)
class CancelledResponseInfo:
    """Facts available when a response is cancelled mid-stream."""

    envelope: MessageEnvelope
    visible_response_event_id: str | None = None
    response_kind: str = "ai"
    failure_reason: str | None = None


@dataclass(slots=True)
class CancelledResponseContext(HookContext):
    """Context for message:cancelled hooks."""

    info: CancelledResponseInfo


@dataclass(slots=True)
class AgentLifecycleContext(HookContext):
    """Context for agent lifecycle observer hooks."""

    entity_name: str
    entity_type: str
    rooms: tuple[str, ...]
    matrix_user_id: str
    joined_room_ids: tuple[str, ...] = ()
    stop_reason: str | None = None


@dataclass(slots=True)
class CompactionHookContext(HookContext):
    """Context for compaction lifecycle observer hooks."""

    agent_name: str
    scope: HistoryScope
    room_id: str
    thread_id: str | None
    messages: list[Message]
    session_id: str
    token_count_before: int
    token_count_after: int | None
    compaction_summary: str | None


@dataclass(slots=True)
class ScheduleFiredContext(HookContext):
    """Context for schedule:fired hooks."""

    task_id: str
    workflow: ScheduledWorkflow
    room_id: str
    thread_id: str | None
    created_by: str | None
    message_text: str
    suppress: bool = False

    async def send_message(
        self,
        room_id: str,
        text: str,
        *,
        thread_id: str | None | _UnsetType = _UNSET,
        extra_content: dict[str, Any] | None = None,
        trigger_dispatch: bool = False,
    ) -> str | None:
        """Send a Matrix message from a schedule hook and return the event ID when available."""
        resolved_thread_id = self.thread_id if isinstance(thread_id, _UnsetType) else thread_id
        return await _send_bound_message(
            self.logger,
            self.message_sender,
            self.plugin_name,
            self.event_name,
            room_id,
            text,
            thread_id=resolved_thread_id,
            extra_content=extra_content,
            requester_id=_requester_id_for_hook_send(self, trigger_dispatch=trigger_dispatch),
            message_received_depth=_message_received_depth_for_hook_send(self),
            trigger_dispatch=trigger_dispatch,
        )


@dataclass(slots=True)
class ReactionReceivedContext(HookContext):
    """Context for reaction:received hooks."""

    room_id: str
    event_id: str
    sender_id: str
    reaction_key: str
    target_event_id: str
    thread_id: str | None


@dataclass(slots=True)
class ConfigReloadedContext(HookContext):
    """Context for config:reloaded hooks."""

    changed_entities: tuple[str, ...]
    added_entities: tuple[str, ...]
    removed_entities: tuple[str, ...]
    plugin_changes: tuple[str, ...]


@dataclass(slots=True)
class SessionHookContext(HookContext):
    """Context for session lifecycle observer hooks."""

    agent_name: str
    scope: HistoryScope
    session_id: str
    room_id: str
    thread_id: str | None


@dataclass(slots=True)
class CustomEventContext(HookContext):
    """Context for custom plugin-emitted hook events."""

    payload: dict[str, Any]
    source_plugin: str
    room_id: str | None
    thread_id: str | None
    sender_id: str | None
    message_received_depth: int = 0


@dataclass(slots=True)
class ToolBeforeCallContext:
    """Context passed to tool:before_call hook callbacks."""

    tool_name: str
    arguments: dict[str, Any]
    agent_name: str
    room_id: str | None
    thread_id: str | None
    requester_id: str | None
    session_id: str | None
    declined: bool = False
    decline_reason: str = ""
    event_name: str = EVENT_TOOL_BEFORE_CALL
    plugin_name: str = ""
    settings: dict[str, Any] = field(default_factory=dict)
    config: Config | None = None
    runtime_paths: RuntimePaths | None = None
    logger: Any = field(default_factory=lambda: get_logger("mindroom.hooks.tool"))
    correlation_id: str = ""
    message_sender: HookMessageSender | None = field(default=None, kw_only=True)
    matrix_admin: HookMatrixAdmin | None = field(default=None, kw_only=True)
    room_state_querier: HookRoomStateQuerier | None = field(default=None, kw_only=True)
    room_state_putter: HookRoomStatePutter | None = field(default=None, kw_only=True)
    message_received_depth: int = 0

    def decline(self, reason: str) -> None:
        """Mark the tool call as declined with one model-facing reason."""
        self.declined = True
        self.decline_reason = reason

    @property
    def state_root(self) -> Path:
        """Return the plugin state root, creating it on first access."""
        return _resolve_plugin_state_root(self.runtime_paths, self.plugin_name)

    async def send_message(
        self,
        room_id: str,
        text: str,
        *,
        thread_id: str | None = None,
        extra_content: dict[str, Any] | None = None,
        trigger_dispatch: bool = False,
    ) -> str | None:
        """Send a Matrix message from a tool hook and return the event ID when available."""
        return await _send_bound_message(
            self.logger,
            self.message_sender,
            self.plugin_name,
            self.event_name,
            room_id,
            text,
            thread_id=thread_id,
            extra_content=extra_content,
            requester_id=self.requester_id,
            message_received_depth=_message_received_depth_for_hook_send(self),
            trigger_dispatch=trigger_dispatch,
        )

    async def query_room_state(
        self,
        room_id: str,
        event_type: str,
        state_key: str | None = None,
    ) -> dict[str, Any] | None:
        """Query Matrix room state and return the result when a querier is available."""
        return await _query_bound_room_state(
            self.logger,
            self.room_state_querier,
            room_id,
            event_type,
            state_key,
        )

    async def put_room_state(
        self,
        room_id: str,
        event_type: str,
        state_key: str,
        content: dict[str, Any],
    ) -> bool:
        """Write a Matrix room state event and return ``True`` on success."""
        return await _put_bound_room_state(
            self.logger,
            self.room_state_putter,
            room_id,
            event_type,
            state_key,
            content,
        )


@dataclass(slots=True)
class ToolAfterCallContext:
    """Context passed to tool:after_call hook callbacks."""

    tool_name: str
    arguments: dict[str, Any]
    agent_name: str
    room_id: str | None
    thread_id: str | None
    requester_id: str | None
    session_id: str | None
    result: object | None
    error: BaseException | None
    blocked: bool
    duration_ms: float
    event_name: str = EVENT_TOOL_AFTER_CALL
    plugin_name: str = ""
    settings: dict[str, Any] = field(default_factory=dict)
    config: Config | None = None
    runtime_paths: RuntimePaths | None = None
    logger: Any = field(default_factory=lambda: get_logger("mindroom.hooks.tool"))
    correlation_id: str = ""
    message_sender: HookMessageSender | None = field(default=None, kw_only=True)
    matrix_admin: HookMatrixAdmin | None = field(default=None, kw_only=True)
    room_state_querier: HookRoomStateQuerier | None = field(default=None, kw_only=True)
    room_state_putter: HookRoomStatePutter | None = field(default=None, kw_only=True)
    message_received_depth: int = 0

    @property
    def state_root(self) -> Path:
        """Return the plugin state root, creating it on first access."""
        return _resolve_plugin_state_root(self.runtime_paths, self.plugin_name)

    async def send_message(
        self,
        room_id: str,
        text: str,
        *,
        thread_id: str | None = None,
        extra_content: dict[str, Any] | None = None,
        trigger_dispatch: bool = False,
    ) -> str | None:
        """Send a Matrix message from a tool hook and return the event ID when available."""
        return await _send_bound_message(
            self.logger,
            self.message_sender,
            self.plugin_name,
            self.event_name,
            room_id,
            text,
            thread_id=thread_id,
            extra_content=extra_content,
            requester_id=self.requester_id,
            message_received_depth=_message_received_depth_for_hook_send(self),
            trigger_dispatch=trigger_dispatch,
        )

    async def query_room_state(
        self,
        room_id: str,
        event_type: str,
        state_key: str | None = None,
    ) -> dict[str, Any] | None:
        """Query Matrix room state and return the result when a querier is available."""
        return await _query_bound_room_state(
            self.logger,
            self.room_state_querier,
            room_id,
            event_type,
            state_key,
        )

    async def put_room_state(
        self,
        room_id: str,
        event_type: str,
        state_key: str,
        content: dict[str, Any],
    ) -> bool:
        """Write a Matrix room state event and return ``True`` on success."""
        return await _put_bound_room_state(
            self.logger,
            self.room_state_putter,
            room_id,
            event_type,
            state_key,
            content,
        )


def _requester_id_for_hook_send(
    context: HookContext,
    *,
    trigger_dispatch: bool = False,
) -> str | None:
    """Return the requester identity to preserve on hook-originated sends."""
    if isinstance(context, MessageReceivedContext | MessageEnrichContext | SystemEnrichContext):
        requester_id = context.envelope.requester_id
    elif isinstance(context, BeforeResponseContext):
        requester_id = context.draft.envelope.requester_id
    elif isinstance(context, AfterResponseContext):
        requester_id = context.result.envelope.requester_id
    elif isinstance(context, CancelledResponseContext):
        requester_id = context.info.envelope.requester_id
    elif isinstance(context, ScheduleFiredContext):
        requester_id = context.created_by
    elif isinstance(context, ReactionReceivedContext | CustomEventContext):
        requester_id = context.sender_id
    else:
        requester_id = None
    if requester_id is not None:
        return requester_id
    if trigger_dispatch:
        return context.config.get_mindroom_user_id(context.runtime_paths)
    return None


def _message_received_depth_for_hook_send(context: object) -> int:
    """Return the synthetic hook-chain depth to preserve on hook sends."""
    return _next_message_received_depth(_current_message_received_depth(context))


def _current_message_received_depth(context: object) -> int:
    """Return the inbound synthetic hook-chain depth for one hook context."""
    if isinstance(context, MessageReceivedContext | MessageEnrichContext):
        return context.envelope.message_received_depth
    if isinstance(context, BeforeResponseContext):
        return context.draft.envelope.message_received_depth
    if isinstance(context, AfterResponseContext):
        return context.result.envelope.message_received_depth
    if isinstance(context, CancelledResponseContext):
        return context.info.envelope.message_received_depth
    if isinstance(context, CustomEventContext | ToolBeforeCallContext | ToolAfterCallContext):
        return context.message_received_depth
    return 0


def _next_message_received_depth(current_depth: int) -> int:
    """Return the next synthetic-chain depth after one downstream hook hop."""
    return current_depth + 1
