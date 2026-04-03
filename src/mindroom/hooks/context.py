"""Hook context and transport dataclasses."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mindroom.constants import ORIGINAL_SENDER_KEY
from mindroom.logging_config import get_logger

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

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.scheduling import ScheduledWorkflow
    from mindroom.tool_system.events import ToolTraceEntry

    from .sender import HookMessageSender
    from .types import HookRoomStatePutter, HookRoomStateQuerier


def _resolve_plugin_state_root(
    runtime_paths: RuntimePaths | None,
    plugin_name: str,
) -> Path:
    """Return the plugin state root, creating it on first access."""
    if runtime_paths is None:
        msg = "runtime_paths are required to access hook state_root"
        raise RuntimeError(msg)
    plugin_root = runtime_paths.storage_root / "plugins" / plugin_name
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


@dataclass(frozen=True, slots=True)
class _EnvelopeTargetView:
    """Compatibility view exposing thread targeting as one object."""

    room_id: str
    thread_id: str | None
    resolved_thread_id: str | None


@dataclass(frozen=True, slots=True)
class MessageEnvelope:
    """Normalized inbound message shape used by message hooks."""

    source_event_id: str
    room_id: str
    thread_id: str | None
    resolved_thread_id: str | None
    requester_id: str
    sender_id: str
    body: str
    attachment_ids: tuple[str, ...]
    mentioned_agents: tuple[str, ...]
    agent_name: str
    source_kind: str

    @property
    def target(self) -> _EnvelopeTargetView:
        """Return a compatibility target view for newer plugin code."""
        return _EnvelopeTargetView(
            room_id=self.room_id,
            thread_id=self.thread_id,
            resolved_thread_id=self.resolved_thread_id,
        )


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
    room_state_querier: HookRoomStateQuerier | None = field(default=None, kw_only=True)
    room_state_putter: HookRoomStatePutter | None = field(default=None, kw_only=True)

    @property
    def state_root(self) -> Path:
        """Return the plugin state root, creating it on first access."""
        return _resolve_plugin_state_root(self.runtime_paths, self.plugin_name)

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
        re-entering the normal dispatch pipeline. If a
        ``message:received`` hook emitted the synthetic event, MindRoom
        skips re-running that same plugin's ``message:received`` hooks
        on the relay to avoid immediate recursion.
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
            trigger_dispatch=trigger_dispatch,
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
class BeforeResponseContext(HookContext):
    """Context for message:before_response hooks."""

    draft: ResponseDraft


@dataclass(slots=True)
class AfterResponseContext(HookContext):
    """Context for message:after_response hooks."""

    result: ResponseResult


@dataclass(slots=True)
class AgentLifecycleContext(HookContext):
    """Context for agent lifecycle observer hooks."""

    entity_name: str
    entity_type: str
    rooms: tuple[str, ...]
    matrix_user_id: str
    stop_reason: str | None = None


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
class CustomEventContext(HookContext):
    """Context for custom plugin-emitted hook events."""

    payload: dict[str, Any]
    source_plugin: str
    room_id: str | None
    thread_id: str | None
    sender_id: str | None


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
    room_state_querier: HookRoomStateQuerier | None = field(default=None, kw_only=True)
    room_state_putter: HookRoomStatePutter | None = field(default=None, kw_only=True)

    def decline(self, reason: str) -> None:
        """Mark the tool call as declined with one model-facing reason."""
        self.declined = True
        self.decline_reason = reason

    @property
    def state_root(self) -> Path:
        """Return the plugin state root when runtime paths are available."""
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
    room_state_querier: HookRoomStateQuerier | None = field(default=None, kw_only=True)
    room_state_putter: HookRoomStatePutter | None = field(default=None, kw_only=True)

    @property
    def state_root(self) -> Path:
        """Return the plugin state root when runtime paths are available."""
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
    if isinstance(context, MessageReceivedContext | MessageEnrichContext):
        requester_id = context.envelope.requester_id
    elif isinstance(context, BeforeResponseContext):
        requester_id = context.draft.envelope.requester_id
    elif isinstance(context, AfterResponseContext):
        requester_id = context.result.envelope.requester_id
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
