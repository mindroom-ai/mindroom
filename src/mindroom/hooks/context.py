"""Hook context and transport dataclasses."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import TYPE_CHECKING, Any, Literal

from mindroom.constants import HOOK_MESSAGE_RECEIVED_DEPTH_KEY, ORIGINAL_SENDER_KEY, ROUTER_AGENT_NAME
from mindroom.entity_resolution import mindroom_user_id
from mindroom.logging_config import get_logger
from mindroom.runtime_protocols import SupportsClientConfigOrchestrator  # noqa: TC001
from mindroom.tool_system.plugin_identity import validate_plugin_name
from mindroom.tool_system.tool_environment import ToolEnvironment

from .registry import HookRegistry
from .state import (
    build_hook_room_state_putter,
    build_hook_room_state_querier,
    chain_hook_room_state_putters,
    chain_hook_room_state_queriers,
)
from .types import (
    EVENT_TOOL_AFTER_CALL,
    EVENT_TOOL_BEFORE_CALL,
    EVENT_TOOL_ENVIRONMENT,
    EnrichmentCachePolicy,
    EnrichmentItem,
    format_hook_source,
)


class _UnsetType:
    """Sentinel type for omitted optional hook arguments."""


_UNSET = _UnsetType()

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from pathlib import Path
    from typing import Protocol

    import structlog

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.history.types import HistoryScope
    from mindroom.matrix.agent_message_snapshot import AgentMessageSnapshot
    from mindroom.message_target import MessageTarget
    from mindroom.scheduling.tasks import ScheduledWorkflow
    from mindroom.tool_system.events import ToolTraceEntry
    from mindroom.turn_origin import TurnOrigin

    from .registry import HookRegistryState
    from .sender import HookMessageSender
    from .types import HookMatrixAdmin, HookRoomStatePutter, HookRoomStateQuerier

    class HookAgentMessageSnapshotReader(Protocol):
        """Callable contract for hook-facing agent-message snapshot reads."""

        def __call__(
            self,
            room_id: str,
            thread_id: str | None,
            sender: str,
        ) -> Awaitable[AgentMessageSnapshot | None]:
            """Read the latest visible sender message for one room or thread scope."""


@dataclass(frozen=True, slots=True)
class HookBindings:
    """The hook registry and the Matrix capabilities that hooks emitted for one bot or turn receive.

    A turn captures its bot's bindings when it starts; ``registry`` is the registry snapshot it emits to, and
    ``registry_state`` the live state a hook compares that snapshot with (``HookContext.is_active``).
    """

    registry: HookRegistry = field(default_factory=HookRegistry.empty)
    registry_state: HookRegistryState | None = None
    message_sender: HookMessageSender | None = None
    matrix_admin: HookMatrixAdmin | None = None
    room_state_querier: HookRoomStateQuerier | None = None
    room_state_putter: HookRoomStatePutter | None = None
    agent_message_snapshot_reader: HookAgentMessageSnapshotReader | None = None
    runtime_started_at: float | None = None

    def context_kwargs(
        self,
        event_name: str,
        *,
        config: Config,
        runtime_paths: RuntimePaths,
        correlation_id: str,
    ) -> dict[str, Any]:
        """Return the base fields of a ``HookContext`` for ``event_name`` that these bindings emit."""
        return {
            "event_name": event_name,
            "config": config,
            "runtime_paths": runtime_paths,
            "correlation_id": correlation_id,
            "runtime_started_at": self.runtime_started_at,
            "message_sender": self.message_sender,
            "agent_message_snapshot_reader": self.agent_message_snapshot_reader,
            "matrix_admin": self.matrix_admin,
            "room_state_querier": self.room_state_querier,
            "room_state_putter": self.room_state_putter,
            "_hook_registry_state": self.registry_state,
        }


@dataclass
class HookContextSupport:
    """Own a bot's live hook bindings, which follow its current Matrix client and orchestrator."""

    runtime: SupportsClientConfigOrchestrator
    runtime_paths: RuntimePaths
    agent_name: str
    hook_registry_state: HookRegistryState
    hook_send_message: HookMessageSender
    agent_message_snapshot_reader: HookAgentMessageSnapshotReader | None = None

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
            sender = orchestrator.hook_message_sender()
            if sender is not None:
                return sender
        return None

    def room_state_querier(self) -> HookRoomStateQuerier | None:
        """Return the room-state querier bound into hook contexts."""
        primary = build_hook_room_state_querier(self.runtime.client) if self.runtime.client is not None else None
        fallback = None
        orchestrator = self.runtime.orchestrator
        if self.agent_name != ROUTER_AGENT_NAME and orchestrator is not None:
            fallback = orchestrator.hook_room_state_querier()
        return chain_hook_room_state_queriers(primary, fallback)

    def room_state_putter(self) -> HookRoomStatePutter | None:
        """Return the room-state putter bound into hook contexts."""
        primary = build_hook_room_state_putter(self.runtime.client) if self.runtime.client is not None else None
        fallback = None
        orchestrator = self.runtime.orchestrator
        if self.agent_name != ROUTER_AGENT_NAME and orchestrator is not None:
            fallback = orchestrator.hook_room_state_putter()
        return chain_hook_room_state_putters(primary, fallback)

    def matrix_admin(self) -> HookMatrixAdmin | None:
        """Return the router-backed Matrix admin helper bound into hook contexts."""
        orchestrator = self.runtime.orchestrator
        if orchestrator is not None:
            admin = orchestrator.hook_matrix_admin()
            if admin is not None:
                return admin
        if self.agent_name == ROUTER_AGENT_NAME and self.runtime.client is not None:
            # Imported on first use so the tool-registry import chain stays free
            # of the nio matrix-client import (#1436).
            from .matrix_admin import build_hook_matrix_admin  # noqa: PLC0415

            return build_hook_matrix_admin(
                self.runtime.client,
                self.runtime_paths,
                config=self.runtime.config,
            )
        return None

    def bindings(self) -> HookBindings:
        """Return the bot's hook bindings as they are now."""
        return HookBindings(
            registry=self.registry,
            registry_state=self.hook_registry_state,
            message_sender=self.message_sender(),
            matrix_admin=self.matrix_admin(),
            room_state_querier=self.room_state_querier(),
            room_state_putter=self.room_state_putter(),
            agent_message_snapshot_reader=self.agent_message_snapshot_reader,
            runtime_started_at=self.runtime.runtime_started_at,
        )

    def base_kwargs(self, event_name: str, correlation_id: str) -> dict[str, Any]:
        """Return the base fields of a hook context the bot emits now."""
        return self.bindings().context_kwargs(
            event_name,
            config=self.runtime.config,
            runtime_paths=self.runtime_paths,
            correlation_id=correlation_id,
        )


@dataclass(frozen=True, slots=True)
class MessageEnvelope:
    """Normalized inbound message shape used by message hooks."""

    source_event_id: str
    target: MessageTarget
    body: str
    attachment_ids: tuple[str, ...]
    mentioned_agents: tuple[str, ...]
    agent_name: str
    origin: TurnOrigin = field(kw_only=True)
    hook_source: str | None = None
    message_received_depth: int = 0
    dispatch_policy_source_kind: str | None = None

    def __post_init__(self) -> None:
        """Validate runtime invariants not enforced by dataclass typing."""
        if self.origin is None:
            message = "MessageEnvelope.origin is required"
            raise TypeError(message)

    @property
    def room_id(self) -> str:
        """Return the canonical target room ID."""
        return self.target.room_id

    @property
    def requester_id(self) -> str:
        """Return the canonical requester ID."""
        return self.origin.requester_id

    @property
    def sender_id(self) -> str:
        """Return the canonical transport sender ID."""
        return self.origin.transport_sender_id

    @property
    def source_kind(self) -> str:
        """Return the canonical ingress source kind."""
        return self.origin.source_kind


@dataclass(slots=True)
class ResponseDraft:
    """Mutable outbound response candidate for before-response hooks."""

    response_text: str
    response_kind: str
    tool_trace: list[ToolTraceEntry] | None
    extra_content: dict[str, Any] | None
    envelope: MessageEnvelope
    suppress: bool = False


@dataclass(slots=True)
class FinalResponseDraft:
    """Mutable text-only outbound response candidate for final-response hooks."""

    response_text: str
    response_kind: str
    envelope: MessageEnvelope


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

    _hook_registry_state: HookRegistryState | None = field(default=None, kw_only=True, repr=False, compare=False)
    _hook_registry_snapshot: HookRegistry | None = field(default=None, kw_only=True, repr=False, compare=False)
    event_name: str
    config: Config
    runtime_paths: RuntimePaths
    correlation_id: str
    # `emit` fills these three with each hook's own before calling it.
    plugin_name: str = field(default="", kw_only=True)
    settings: dict[str, Any] = field(default_factory=dict, kw_only=True)
    logger: structlog.stdlib.BoundLogger = field(default_factory=lambda: get_logger("mindroom.hooks"), kw_only=True)
    runtime_started_at: float | None = field(default=None, kw_only=True)
    message_sender: HookMessageSender | None = field(default=None, kw_only=True)
    agent_message_snapshot_reader: HookAgentMessageSnapshotReader | None = field(
        default=None,
        kw_only=True,
    )
    matrix_admin: HookMatrixAdmin | None = field(default=None, kw_only=True)
    room_state_querier: HookRoomStateQuerier | None = field(default=None, kw_only=True)
    room_state_putter: HookRoomStatePutter | None = field(default=None, kw_only=True)

    def is_active(self) -> bool:
        """Return whether this hook still belongs to the live registry snapshot."""
        return (
            self._hook_registry_state is not None
            and self._hook_registry_snapshot is not None
            and self._hook_registry_state.registry is self._hook_registry_snapshot
        )

    @property
    def state_root(self) -> Path:
        """Return the plugin state root, creating it on first access."""
        plugin_root = self.runtime_paths.storage_root / "plugins" / validate_plugin_name(self.plugin_name)
        plugin_root.mkdir(parents=True, exist_ok=True)
        return plugin_root

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
        if self.message_sender is None:
            self.logger.warning("send_message called but no sender registered")
            return None
        resolved_extra_content = dict(extra_content or {})
        requester_id = _requester_id_for_hook_send(self, trigger_dispatch=trigger_dispatch)
        if requester_id:
            resolved_extra_content.setdefault(ORIGINAL_SENDER_KEY, requester_id)
        message_received_depth = _message_received_depth_for_hook_send(self)
        if message_received_depth > 0:
            resolved_extra_content[HOOK_MESSAGE_RECEIVED_DEPTH_KEY] = message_received_depth
        return await self.message_sender(
            room_id,
            text,
            thread_id,
            format_hook_source(self.plugin_name, self.event_name),
            resolved_extra_content or None,
            trigger_dispatch=trigger_dispatch,
        )

    async def query_room_state(
        self,
        room_id: str,
        event_type: str,
        state_key: str | None = None,
    ) -> dict[str, Any] | None:
        """Query Matrix room state and return the result when a querier is available."""
        if self.room_state_querier is None:
            self.logger.warning("No room state querier available")
            return None
        return await self.room_state_querier(room_id, event_type, state_key)

    async def get_latest_agent_message_snapshot(
        self,
        room_id: str,
        sender: str,
        *,
        thread_id: str | None = None,
    ) -> AgentMessageSnapshot | None:
        """Return the latest visible sender message when a reader is available."""
        if self.agent_message_snapshot_reader is None:
            self.logger.warning("No agent-message snapshot reader available")
            return None
        return await self.agent_message_snapshot_reader(
            room_id=room_id,
            thread_id=thread_id,
            sender=sender,
        )

    async def put_room_state(
        self,
        room_id: str,
        event_type: str,
        state_key: str,
        content: dict[str, Any],
    ) -> bool:
        """Write a Matrix room state event and return ``True`` on success."""
        if self.room_state_putter is None:
            self.logger.warning("No room state putter available")
            return False
        return await self.room_state_putter(room_id, event_type, state_key, content)


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
        persist: bool = True,
    ) -> None:
        """Append one enrichment item for this hook."""
        self._items.append(EnrichmentItem(key=key, text=text, cache_policy=cache_policy, persist=persist))


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
class FinalResponseTransformContext(HookContext):
    """Context for message:final_response_transform hooks."""

    draft: FinalResponseDraft


@dataclass(slots=True)
class AfterResponseContext(HookContext):
    """Context for message:after_response hooks."""

    result: ResponseResult


@dataclass(frozen=True, slots=True)
class CancelledResponseInfo:
    """Facts available when final delivery ends on the cancelled/failure cleanup path."""

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


@dataclass(frozen=True, slots=True)
class TranscriptLine:
    """One thing said in a compacted conversation: who said it and the words they wrote."""

    # "user" for a message the agent was sent, "assistant" for the agent's or team's own reply.
    role: Literal["user", "assistant"]
    # A user line's sender: a Matrix ID, or a placeholder such as "unknown" when none was recorded; None for a reply.
    sender: str | None
    # The display name a user line's message carried, as the sender had it then; None when it carried none.
    display_name: str | None
    # The words without the context hooks attached to the message; not redacted.
    text: str


@dataclass(slots=True)
class CompactionHookContext(HookContext):
    """Context for compaction lifecycle observer hooks."""

    agent_name: str
    scope: HistoryScope
    room_id: str
    thread_id: str | None
    transcript: tuple[TranscriptLine, ...]
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
        # Named base call: zero-argument super() breaks in slots dataclasses.
        return await HookContext.send_message(
            self,
            room_id,
            text,
            thread_id=self.thread_id if isinstance(thread_id, _UnsetType) else thread_id,
            extra_content=extra_content,
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
class RoomMemberJoinedContext(HookContext):
    """Context for room:member_joined hooks."""

    agent_name: str
    room_id: str
    event_id: str
    user_id: str
    sender_id: str
    display_name: str | None
    avatar_url: str | None
    membership: str
    prev_membership: str | None


@dataclass(slots=True)
class RoomMemberLeftContext(HookContext):
    """Context for room:member_left hooks."""

    agent_name: str
    room_id: str
    event_id: str
    user_id: str
    sender_id: str
    display_name: str | None
    avatar_url: str | None
    membership: str
    prev_membership: str | None


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


@dataclass(slots=True, kw_only=True)
class ToolBeforeCallContext(HookContext):
    """Context passed to tool:before_call hook callbacks."""

    event_name: str = EVENT_TOOL_BEFORE_CALL
    tool_name: str
    arguments: dict[str, Any]
    agent_name: str
    room_id: str | None
    thread_id: str | None
    requester_id: str | None
    session_id: str | None
    message_received_depth: int = 0
    declined: bool = False
    decline_reason: str = ""

    def decline(self, reason: str) -> None:
        """Mark the tool call as declined with one model-facing reason."""
        self.declined = True
        self.decline_reason = reason


@dataclass(slots=True, kw_only=True)
class ToolAfterCallContext(HookContext):
    """Context passed to tool:after_call hook callbacks."""

    event_name: str = EVENT_TOOL_AFTER_CALL
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
    message_received_depth: int = 0


@dataclass(slots=True, kw_only=True)
class ToolEnvironmentContext(HookContext):
    """Context passed to tool:environment hook callbacks, which change the environment of the call's shell commands."""

    event_name: str = EVENT_TOOL_ENVIRONMENT
    function_name: str
    registered_toolkit: str | None
    arguments: dict[str, Any]
    agent_name: str
    room_id: str | None
    requester_id: str | None
    message_received_depth: int = 0
    declined: bool = False
    decline_reason: str = ""
    _environment: ToolEnvironment = field(default_factory=ToolEnvironment, repr=False)

    def set_env(self, name: str, value: str) -> None:
        """Give the call's shell commands the variable ``name`` with ``value``."""
        _check_env_name(name)
        if not isinstance(value, str) or "\0" in value:
            msg = f"Environment variable {name} needs a string value without NUL characters"
            raise ValueError(msg)
        self._environment = self._environment.then(ToolEnvironment(values={name: value}))

    def unset_env(self, name: str) -> None:
        """Keep the variable ``name`` from the call's shell commands; ``PATH`` cannot be removed, only set."""
        _check_env_name(name)
        if name == "PATH":
            msg = "PATH cannot be removed, since no command would run without it; set it instead"
            raise ValueError(msg)
        self._environment = self._environment.then(ToolEnvironment(values={name: None}))

    def pass_through(self, name: str) -> None:
        """Give the call's shell commands MindRoom's own variables that ``name``, a name or glob pattern, matches."""
        _check_env_name(name)
        self._environment = self._environment.then(ToolEnvironment(pass_through=(name,)))

    def prepend_path(self, directory: str) -> None:
        """Put ``directory`` first on the call's shell commands' ``PATH``, ahead of the shell tool's own entries."""
        if (
            not isinstance(directory, str)
            or not PurePath(directory).is_absolute()
            or os.pathsep in directory
            or "\0" in directory
        ):
            msg = f"A PATH entry must be one absolute directory: {directory!r}"
            raise ValueError(msg)
        self._environment = self._environment.then(ToolEnvironment(path_prepend=(directory,)))

    def decline(self, reason: str) -> None:
        """Refuse the tool call with one model-facing reason, so it does not run."""
        self.declined = True
        self.decline_reason = reason


def _check_env_name(name: str) -> None:
    if not name or any(char.isspace() or char in "=,\0" for char in name):
        msg = f"Invalid environment variable name: {name!r}"
        raise ValueError(msg)


def _requester_id_for_hook_send(
    context: HookContext,
    *,
    trigger_dispatch: bool = False,
) -> str | None:
    """Return the requester identity to preserve on hook-originated sends."""
    envelope = message_envelope_for_hook_context(context)
    if envelope is not None:
        requester_id = envelope.requester_id
    elif isinstance(context, ScheduleFiredContext):
        requester_id = context.created_by
    elif isinstance(context, ReactionReceivedContext | CustomEventContext):
        requester_id = context.sender_id
    elif isinstance(context, ToolBeforeCallContext | ToolAfterCallContext | ToolEnvironmentContext):
        requester_id = context.requester_id
    else:
        requester_id = None
    if requester_id is not None:
        return requester_id
    if trigger_dispatch:
        return mindroom_user_id(context.config, context.runtime_paths)
    return None


def _message_received_depth_for_hook_send(context: HookContext) -> int:
    """Return the synthetic hook-chain depth to preserve on hook sends."""
    return _next_message_received_depth(_current_message_received_depth(context))


def _current_message_received_depth(context: HookContext) -> int:
    """Return the inbound synthetic hook-chain depth for one hook context."""
    envelope = message_envelope_for_hook_context(context)
    if envelope is not None:
        return envelope.message_received_depth
    if isinstance(
        context,
        CustomEventContext | ToolBeforeCallContext | ToolAfterCallContext | ToolEnvironmentContext,
    ):
        return context.message_received_depth
    return 0


def message_envelope_for_hook_context(context: HookContext) -> MessageEnvelope | None:
    """Return the message envelope carried by one hook context when present."""
    if isinstance(context, MessageReceivedContext | MessageEnrichContext | SystemEnrichContext):
        return context.envelope
    if isinstance(context, BeforeResponseContext | FinalResponseTransformContext):
        return context.draft.envelope
    if isinstance(context, AfterResponseContext):
        return context.result.envelope
    if isinstance(context, CancelledResponseContext):
        return context.info.envelope
    return None


def _next_message_received_depth(current_depth: int) -> int:
    """Return the next synthetic-chain depth after one downstream hook hop."""
    return current_depth + 1
