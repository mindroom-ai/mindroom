"""Public hook system exports."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .context import (
    AfterResponseContext,
    AgentLifecycleContext,
    BeforeResponseContext,
    CancelledResponseContext,
    CancelledResponseInfo,
    CompactionHookContext,
    ConfigReloadedContext,
    CustomEventContext,
    FinalResponseDraft,
    FinalResponseTransformContext,
    HookContext,
    HookContextSupport,
    MessageEnrichContext,
    MessageEnvelope,
    MessageReceivedContext,
    ReactionReceivedContext,
    ResponseDraft,
    ResponseResult,
    ScheduleFiredContext,
    SessionHookContext,
    SystemEnrichContext,
    ToolAfterCallContext,
    ToolBeforeCallContext,
)
from .decorators import get_hook_metadata, hook, iter_module_hooks
from .enrichment import (
    render_enrichment_block,
    render_system_enrichment_block,
)
from .execution import emit, emit_collect, emit_final_response_transform, emit_gate, emit_transform
from .ingress import (
    HookIngressPolicy,
    hook_ingress_policy,
    is_automation_source_kind,
    is_voice_event,
    should_handle_interactive_text_response,
)
from .registry import HookRegistry, HookRegistryPlugin, HookRegistryState
from .sender import build_hook_message_sender, send_hook_message
from .state import build_hook_room_state_putter, build_hook_room_state_querier
from .types import (
    BUILTIN_EVENT_NAMES,
    EVENT_AGENT_STARTED,
    EVENT_AGENT_STOPPED,
    EVENT_BOT_READY,
    EVENT_COMPACTION_AFTER,
    EVENT_COMPACTION_BEFORE,
    EVENT_CONFIG_RELOADED,
    EVENT_MESSAGE_AFTER_RESPONSE,
    EVENT_MESSAGE_BEFORE_RESPONSE,
    EVENT_MESSAGE_CANCELLED,
    EVENT_MESSAGE_ENRICH,
    EVENT_MESSAGE_FINAL_RESPONSE_TRANSFORM,
    EVENT_MESSAGE_RECEIVED,
    EVENT_REACTION_RECEIVED,
    EVENT_SCHEDULE_FIRED,
    EVENT_SESSION_STARTED,
    EVENT_SYSTEM_ENRICH,
    EVENT_TOOL_AFTER_CALL,
    EVENT_TOOL_BEFORE_CALL,
    EnrichmentItem,
    HookCallback,
    HookMatrixAdmin,
    HookMessageSender,
    HookRoomStatePutter,
    HookRoomStateQuerier,
    RegisteredHook,
    default_timeout_ms_for_event,
    validate_event_name,
)

if TYPE_CHECKING:
    import nio

    from mindroom.constants import RuntimePaths

__all__ = [
    "BUILTIN_EVENT_NAMES",
    "EVENT_AGENT_STARTED",
    "EVENT_AGENT_STOPPED",
    "EVENT_BOT_READY",
    "EVENT_COMPACTION_AFTER",
    "EVENT_COMPACTION_BEFORE",
    "EVENT_CONFIG_RELOADED",
    "EVENT_MESSAGE_AFTER_RESPONSE",
    "EVENT_MESSAGE_BEFORE_RESPONSE",
    "EVENT_MESSAGE_CANCELLED",
    "EVENT_MESSAGE_ENRICH",
    "EVENT_MESSAGE_FINAL_RESPONSE_TRANSFORM",
    "EVENT_MESSAGE_RECEIVED",
    "EVENT_REACTION_RECEIVED",
    "EVENT_SCHEDULE_FIRED",
    "EVENT_SESSION_STARTED",
    "EVENT_SYSTEM_ENRICH",
    "EVENT_TOOL_AFTER_CALL",
    "EVENT_TOOL_BEFORE_CALL",
    "AfterResponseContext",
    "AgentLifecycleContext",
    "BeforeResponseContext",
    "CancelledResponseContext",
    "CancelledResponseInfo",
    "CompactionHookContext",
    "ConfigReloadedContext",
    "CustomEventContext",
    "EnrichmentItem",
    "FinalResponseDraft",
    "FinalResponseTransformContext",
    "HookCallback",
    "HookContext",
    "HookContextSupport",
    "HookIngressPolicy",
    "HookMatrixAdmin",
    "HookMessageSender",
    "HookRegistry",
    "HookRegistryPlugin",
    "HookRegistryState",
    "HookRoomStatePutter",
    "HookRoomStateQuerier",
    "MessageEnrichContext",
    "MessageEnvelope",
    "MessageReceivedContext",
    "ReactionReceivedContext",
    "RegisteredHook",
    "ResponseDraft",
    "ResponseResult",
    "ScheduleFiredContext",
    "SessionHookContext",
    "SystemEnrichContext",
    "ToolAfterCallContext",
    "ToolBeforeCallContext",
    "build_hook_matrix_admin",
    "build_hook_message_sender",
    "build_hook_room_state_putter",
    "build_hook_room_state_querier",
    "default_timeout_ms_for_event",
    "emit",
    "emit_collect",
    "emit_final_response_transform",
    "emit_gate",
    "emit_transform",
    "get_hook_metadata",
    "hook",
    "hook_ingress_policy",
    "is_automation_source_kind",
    "is_voice_event",
    "iter_module_hooks",
    "render_enrichment_block",
    "render_system_enrichment_block",
    "send_hook_message",
    "should_handle_interactive_text_response",
    "validate_event_name",
]


def build_hook_matrix_admin(
    client: nio.AsyncClient,
    runtime_paths: RuntimePaths,
) -> HookMatrixAdmin:
    """Lazily import the concrete matrix admin builder to avoid package cycles."""
    from .matrix_admin import build_hook_matrix_admin  # noqa: PLC0415

    return build_hook_matrix_admin(client, runtime_paths)
