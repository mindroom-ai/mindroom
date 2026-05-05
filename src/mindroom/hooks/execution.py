"""Hook execution helpers with timeouts and failure isolation."""

from __future__ import annotations

import asyncio
import time
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, cast

from mindroom.logging_config import get_logger

from .context import (
    AfterResponseContext,
    AgentLifecycleContext,
    BeforeResponseContext,
    CancelledResponseContext,
    CompactionHookContext,
    CustomEventContext,
    FinalResponseDraft,
    FinalResponseTransformContext,
    HookContext,
    MessageEnrichContext,
    MessageEnvelope,
    MessageReceivedContext,
    ReactionReceivedContext,
    ResponseDraft,
    ScheduleFiredContext,
    SessionHookContext,
    SystemEnrichContext,
    ToolAfterCallContext,
    ToolBeforeCallContext,
)
from .types import EVENT_MESSAGE_RECEIVED, EnrichmentItem, RegisteredHook, default_timeout_ms_for_event

if TYPE_CHECKING:
    from agno.models.message import Message

    from .registry import HookRegistry

logger = get_logger(__name__)

_COLLECT_CONCURRENCY_LIMIT = 10
_MAX_EMIT_DEPTH = 3
_EMIT_DEPTH: ContextVar[int] = ContextVar("mindroom_hook_emit_depth", default=0)

type _HookExecutionContext = HookContext | ToolBeforeCallContext | ToolAfterCallContext
type _TransformContext = BeforeResponseContext | FinalResponseTransformContext
type _TransformDraft = ResponseDraft | FinalResponseDraft


@dataclass(frozen=True, slots=True)
class _HookInvocationResult:
    succeeded: bool
    value: object | None = None


def _scope_agent_name(context: _HookExecutionContext) -> str | None:
    if isinstance(context, ToolBeforeCallContext | ToolAfterCallContext):
        return context.agent_name
    envelope = _context_scope_envelope(context)
    if envelope is not None:
        return envelope.agent_name
    if isinstance(context, MessageEnrichContext | SystemEnrichContext):
        return context.target_entity_name
    if isinstance(context, AgentLifecycleContext):
        return context.entity_name
    if isinstance(context, CompactionHookContext | SessionHookContext):
        return context.agent_name
    return None


def _scope_room_ids(context: _HookExecutionContext) -> tuple[str, ...]:  # noqa: PLR0911
    if isinstance(context, ToolBeforeCallContext | ToolAfterCallContext):
        return (context.room_id,) if context.room_id else ()
    envelope = _context_scope_envelope(context)
    if envelope is not None:
        return (envelope.room_id,)
    if isinstance(context, ScheduleFiredContext | ReactionReceivedContext):
        return (context.room_id,)
    if isinstance(context, AgentLifecycleContext):
        return context.rooms
    if isinstance(context, CompactionHookContext):
        return (context.room_id,)
    if isinstance(context, SessionHookContext):
        return (context.room_id,)
    if isinstance(context, CustomEventContext) and context.room_id:
        return (context.room_id,)
    return ()


def _hook_in_scope(hook: RegisteredHook, context: _HookExecutionContext) -> bool:
    if hook.agents is not None:
        agent_name = _scope_agent_name(context)
        if agent_name is None or agent_name not in hook.agents:
            return False

    if hook.rooms is not None:
        room_ids = _scope_room_ids(context)
        if not any(room_id in hook.rooms for room_id in room_ids):
            return False

    return True


def _context_scope_envelope(context: _HookExecutionContext) -> MessageEnvelope | None:
    if isinstance(context, MessageReceivedContext | MessageEnrichContext | SystemEnrichContext):
        return context.envelope
    if isinstance(context, BeforeResponseContext | FinalResponseTransformContext):
        return context.draft.envelope
    if isinstance(context, AfterResponseContext):
        return context.result.envelope
    if isinstance(context, CancelledResponseContext):
        return context.info.envelope
    return None


def _context_logger(hook: RegisteredHook) -> object:
    return get_logger("mindroom.hooks").bind(
        plugin_name=hook.plugin_name,
        hook_name=hook.hook_name,
        event_name=hook.event_name,
    )


def _snapshot_tool_observer_value(value: object | None) -> object | None:
    """Return an observer-safe snapshot that cannot mutate caller-visible state."""
    if value is None:
        return None
    try:
        return deepcopy(value)
    except Exception:
        return repr(value)


def _snapshot_tool_observer_error(error: BaseException | None) -> BaseException | None:
    """Return an isolated exception snapshot for after-call observer hooks."""
    if error is None:
        return None
    try:
        copied = deepcopy(error)
    except Exception:
        return Exception(str(error))
    return copied if isinstance(copied, BaseException) else Exception(str(error))


def _snapshot_compaction_messages(messages: list[Message]) -> list[Message] | None:
    """Return one best-effort isolated snapshot of compaction messages."""
    try:
        return deepcopy(messages)
    except Exception as deepcopy_error:
        try:
            return [message.model_copy(deep=True) for message in messages]
        except Exception as model_copy_error:
            logger.warning(
                "Skipping compaction hooks after snapshot copy failures",
                deepcopy_error=repr(deepcopy_error),
                model_copy_error=repr(model_copy_error),
                message_count=len(messages),
            )
            return None


def _bind_hook_context(hook: RegisteredHook, context: _HookExecutionContext) -> _HookExecutionContext | None:
    replacement_kwargs: dict[str, object] = {
        "plugin_name": hook.plugin_name,
        "settings": dict(hook.settings),
        "logger": _context_logger(hook),
    }
    if isinstance(context, ToolBeforeCallContext | ToolAfterCallContext):
        replacement_kwargs["arguments"] = deepcopy(context.arguments)
    if isinstance(context, ToolAfterCallContext):
        replacement_kwargs["result"] = _snapshot_tool_observer_value(context.result)
        replacement_kwargs["error"] = _snapshot_tool_observer_error(context.error)
    if isinstance(context, CompactionHookContext):
        messages_snapshot = _snapshot_compaction_messages(context.messages)
        if messages_snapshot is None:
            return None
        replacement_kwargs["messages"] = messages_snapshot
    if isinstance(context, MessageEnrichContext | SystemEnrichContext):
        replacement_kwargs["_items"] = []
    return replace(context, **replacement_kwargs)


def _merge_observer_context_changes(
    context: _HookExecutionContext,
    hook_context: _HookExecutionContext,
) -> None:
    """Propagate mutable observer fields back to the caller-visible context."""
    if isinstance(context, ToolBeforeCallContext) and isinstance(hook_context, ToolBeforeCallContext):
        context.declined = hook_context.declined
        context.decline_reason = hook_context.decline_reason
    if isinstance(context, MessageReceivedContext) and isinstance(hook_context, MessageReceivedContext):
        context.suppress = hook_context.suppress
    if isinstance(context, ScheduleFiredContext) and isinstance(hook_context, ScheduleFiredContext):
        context.message_text = hook_context.message_text
        context.suppress = hook_context.suppress


def _effective_timeout_ms(hook: RegisteredHook) -> int:
    return hook.timeout_ms if hook.timeout_ms is not None else default_timeout_ms_for_event(hook.event_name)


async def _invoke_hook(hook: RegisteredHook, context: _HookExecutionContext) -> _HookInvocationResult:
    timeout_seconds = _effective_timeout_ms(hook) / 1000
    started_at = time.monotonic()
    try:
        async with asyncio.timeout(timeout_seconds):
            result = await hook.callback(context)
    except Exception:
        duration_ms = round((time.monotonic() - started_at) * 1000, 2)
        context.logger.exception(
            "Hook execution failed",
            correlation_id=context.correlation_id,
            duration_ms=duration_ms,
            timeout_ms=_effective_timeout_ms(hook),
        )
        return _HookInvocationResult(succeeded=False)

    duration_ms = round((time.monotonic() - started_at) * 1000, 2)
    context.logger.debug(
        "Hook execution succeeded",
        correlation_id=context.correlation_id,
        duration_ms=duration_ms,
    )
    return _HookInvocationResult(succeeded=True, value=result)


def _eligible_hooks(
    registry: HookRegistry,
    event_name: str,
    context: _HookExecutionContext,
) -> tuple[RegisteredHook, ...]:
    hooks = registry.hooks_for(event_name)
    if not hooks:
        return ()

    eligible_hooks: list[RegisteredHook] = []
    for hook in hooks:
        if not _hook_in_scope(hook, context):
            continue
        if (
            isinstance(context, MessageReceivedContext)
            and event_name == EVENT_MESSAGE_RECEIVED
            and hook.plugin_name in context.skip_plugin_names
        ):
            continue
        eligible_hooks.append(hook)
    return tuple(eligible_hooks)


async def emit(
    registry: HookRegistry,
    event_name: str,
    context: _HookExecutionContext,
    *,
    continue_on_cancelled: bool = False,
) -> None:
    """Run observer hooks serially for one event."""
    depth = _EMIT_DEPTH.get()
    if depth >= _MAX_EMIT_DEPTH:
        logger.warning(
            "Dropping nested hook emission after recursion limit",
            event_name=event_name,
            correlation_id=context.correlation_id,
            max_depth=_MAX_EMIT_DEPTH,
        )
        return

    token = _EMIT_DEPTH.set(depth + 1)
    try:
        for hook in _eligible_hooks(registry, event_name, context):
            hook_context = _bind_hook_context(hook, context)
            if hook_context is None:
                return
            try:
                await _invoke_hook(hook, hook_context)
            except asyncio.CancelledError:
                if not continue_on_cancelled:
                    raise
                hook_context.logger.warning(
                    "Hook execution cancelled during best-effort observer emission",
                    correlation_id=context.correlation_id,
                )
                continue
            _merge_observer_context_changes(context, hook_context)
    finally:
        _EMIT_DEPTH.reset(token)


async def emit_gate(
    registry: HookRegistry,
    event_name: str,
    context: ToolBeforeCallContext,
) -> None:
    """Run gate hooks serially and stop at the first explicit decline."""
    depth = _EMIT_DEPTH.get()
    if depth >= _MAX_EMIT_DEPTH:
        logger.warning(
            "Dropping nested hook emission after recursion limit",
            event_name=event_name,
            correlation_id=context.correlation_id,
            max_depth=_MAX_EMIT_DEPTH,
        )
        return

    token = _EMIT_DEPTH.set(depth + 1)
    try:
        for hook in _eligible_hooks(registry, event_name, context):
            hook_context = cast("ToolBeforeCallContext", _bind_hook_context(hook, context))
            if hook_context is None:
                return
            invocation = await _invoke_hook(hook, hook_context)
            if not invocation.succeeded:
                continue
            context.declined = hook_context.declined
            context.decline_reason = hook_context.decline_reason
            if context.declined:
                return
    finally:
        _EMIT_DEPTH.reset(token)


def _normalize_collector_result(
    result: object | None,
    hook_context: MessageEnrichContext | SystemEnrichContext,
) -> list[EnrichmentItem]:
    items = list(hook_context._items)
    if isinstance(result, EnrichmentItem):
        items.append(result)
        return items
    if isinstance(result, list) and all(isinstance(item, EnrichmentItem) for item in result):
        items.extend(cast("list[EnrichmentItem]", result))
    return items


async def emit_collect(
    registry: HookRegistry,
    event_name: str,
    context: MessageEnrichContext | SystemEnrichContext,
) -> list[EnrichmentItem]:
    """Run collector hooks concurrently and return merged enrichment items."""
    hooks = _eligible_hooks(registry, event_name, context)
    if not hooks:
        return []

    semaphore = asyncio.Semaphore(_COLLECT_CONCURRENCY_LIMIT)

    async def run_hook(hook: RegisteredHook) -> list[EnrichmentItem]:
        async with semaphore:
            hook_context = cast("MessageEnrichContext | SystemEnrichContext", _bind_hook_context(hook, context))
            invocation = await _invoke_hook(hook, hook_context)
            return _normalize_collector_result(invocation.value, hook_context)

    results = await asyncio.gather(*(run_hook(hook) for hook in hooks))
    merged: list[EnrichmentItem] = []
    for hook_items in results:
        merged.extend(hook_items)
    return merged


async def emit_transform(
    registry: HookRegistry,
    event_name: str,
    context: BeforeResponseContext,
) -> ResponseDraft:
    """Run transformer hooks serially and return the final draft."""
    return cast(
        "ResponseDraft",
        await _emit_serial_transform(
            registry,
            event_name,
            context,
            copy_on_write=False,
            preserve_failed_draft=True,
            continue_on_cancelled=False,
        ),
    )


async def emit_final_response_transform(
    registry: HookRegistry,
    event_name: str,
    context: FinalResponseTransformContext,
) -> FinalResponseDraft:
    """Run final-response transform hooks serially with best-effort isolation."""
    return cast(
        "FinalResponseDraft",
        await _emit_serial_transform(
            registry,
            event_name,
            context,
            copy_on_write=True,
            preserve_failed_draft=False,
            continue_on_cancelled=True,
        ),
    )


def _copy_transform_draft(draft: _TransformDraft) -> _TransformDraft:
    return deepcopy(draft)


def _transform_context_with_draft(context: _TransformContext, draft: _TransformDraft) -> _TransformContext:
    return replace(context, draft=draft)


def _next_transform_draft(
    current_draft: _TransformDraft,
    hook_context: _TransformContext,
    invocation: _HookInvocationResult,
    *,
    preserve_failed_draft: bool,
) -> _TransformDraft:
    if not invocation.succeeded:
        return hook_context.draft if preserve_failed_draft else current_draft
    if isinstance(invocation.value, type(current_draft)):
        return invocation.value
    return hook_context.draft


async def _emit_serial_transform(
    registry: HookRegistry,
    event_name: str,
    context: _TransformContext,
    *,
    copy_on_write: bool,
    preserve_failed_draft: bool,
    continue_on_cancelled: bool,
) -> _TransformDraft:
    current_draft = context.draft
    for hook in _eligible_hooks(registry, event_name, context):
        hook_draft = _copy_transform_draft(current_draft) if copy_on_write else current_draft
        bound_context = _bind_hook_context(hook, _transform_context_with_draft(context, hook_draft))
        if bound_context is None:
            return current_draft
        hook_context = cast("_TransformContext", bound_context)
        try:
            invocation = await _invoke_hook(hook, hook_context)
        except asyncio.CancelledError:
            if not continue_on_cancelled:
                raise
            hook_context.logger.warning(
                "Hook execution cancelled during best-effort response transform",
                correlation_id=context.correlation_id,
            )
            continue
        current_draft = _next_transform_draft(
            current_draft,
            hook_context,
            invocation,
            preserve_failed_draft=preserve_failed_draft,
        )
    return current_draft
