"""Hook context and transport dataclasses."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mindroom.constants import ORIGINAL_SENDER_KEY

from .types import EnrichmentCachePolicy, EnrichmentItem

if TYPE_CHECKING:
    from pathlib import Path

    import structlog

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.scheduling import ScheduledWorkflow
    from mindroom.tool_system.events import ToolTraceEntry

    from .sender import HookMessageSender


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

    @property
    def state_root(self) -> Path:
        """Return the plugin state root, creating it on first access."""
        plugin_root = self.runtime_paths.storage_root / "plugins" / self.plugin_name
        plugin_root.mkdir(parents=True, exist_ok=True)
        return plugin_root

    async def send_message(
        self,
        room_id: str,
        text: str,
        *,
        thread_id: str | None = None,
        extra_content: dict[str, Any] | None = None,
    ) -> str | None:
        """Send a Matrix message from a hook and return the event ID when available."""
        sender = self.message_sender
        if sender is None:
            self.logger.warning("send_message called but no sender registered")
            return None
        source_hook = f"{self.plugin_name}:{self.event_name}"
        resolved_extra_content = dict(extra_content or {})
        requester_id = _requester_id_for_hook_send(self)
        if requester_id:
            resolved_extra_content.setdefault(ORIGINAL_SENDER_KEY, requester_id)
        return await sender(room_id, text, thread_id, source_hook, resolved_extra_content or None)


@dataclass(slots=True)
class MessageReceivedContext(HookContext):
    """Context for observer hooks that inspect inbound messages."""

    envelope: MessageEnvelope
    suppress: bool = False


@dataclass(slots=True)
class MessageEnrichContext(HookContext):
    """Context for hooks that append model-facing message enrichment."""

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
    """Context for hooks that mutate or suppress an outbound draft."""

    draft: ResponseDraft


@dataclass(slots=True)
class AfterResponseContext(HookContext):
    """Context for observer hooks that inspect a sent response."""

    result: ResponseResult


@dataclass(slots=True)
class AgentLifecycleContext(HookContext):
    """Context emitted around agent and team startup or shutdown."""

    entity_name: str
    entity_type: str
    rooms: tuple[str, ...]
    matrix_user_id: str
    stop_reason: str | None = None


@dataclass(slots=True)
class ScheduleFiredContext(HookContext):
    """Context for hooks that intercept scheduled workflow delivery."""

    task_id: str
    workflow: ScheduledWorkflow
    room_id: str
    thread_id: str | None
    created_by: str | None
    message_text: str
    suppress: bool = False


@dataclass(slots=True)
class ReactionReceivedContext(HookContext):
    """Context for hooks that observe Matrix reactions."""

    room_id: str
    event_id: str
    sender_id: str
    reaction_key: str
    target_event_id: str
    thread_id: str | None


@dataclass(slots=True)
class ConfigReloadedContext(HookContext):
    """Context for hooks fired after a config reload completes."""

    changed_entities: tuple[str, ...]
    added_entities: tuple[str, ...]
    removed_entities: tuple[str, ...]
    plugin_changes: tuple[str, ...]


@dataclass(slots=True)
class CustomEventContext(HookContext):
    """Context for plugin-defined custom events emitted from tools."""

    payload: dict[str, Any]
    source_plugin: str
    room_id: str | None
    thread_id: str | None
    sender_id: str | None


def _requester_id_for_hook_send(context: HookContext) -> str | None:
    """Return the requester identity to preserve on hook-originated sends."""
    if isinstance(context, MessageReceivedContext | MessageEnrichContext):
        return context.envelope.requester_id
    if isinstance(context, BeforeResponseContext):
        return context.draft.envelope.requester_id
    if isinstance(context, AfterResponseContext):
        return context.result.envelope.requester_id
    if isinstance(context, ScheduleFiredContext):
        return context.created_by
    if isinstance(context, ReactionReceivedContext | CustomEventContext):
        return context.sender_id
    return None
