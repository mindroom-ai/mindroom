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
    CustomEventContext,
    HookContext,
    MessageEnrichContext,
    MessageReceivedContext,
    ReactionReceivedContext,
    ResponseDraft,
    ScheduleFiredContext,
    ToolAfterCallContext,
    ToolBeforeCallContext,
)
from .types import EnrichmentItem, RegisteredHook, default_timeout_ms_for_event

if TYPE_CHECKING:
    from .registry import HookRegistry

logger = get_logger(__name__)

_CIRCUIT_BREAKER_FAILURE_THRESHOLD = 5
_CIRCUIT_BREAKER_COOLDOWN_SECONDS = 5 * 60
_COLLECT_CONCURRENCY_LIMIT = 10
_MAX_EMIT_DEPTH = 3
_EMIT_DEPTH: ContextVar[int] = ContextVar("mindroom_hook_emit_depth", default=0)


@dataclass(slots=True)
class _HookFailureState:
    consecutive_failures: int = 0
    cooldown_until_monotonic: float = 0.0


_HOOK_FAILURES: dict[tuple[str, str], _HookFailureState] = {}

type HookExecutionContext = HookContext | ToolBeforeCallContext | ToolAfterCallContext


@dataclass(frozen=True, slots=True)
class _HookInvocationResult:
    succeeded: bool
    value: object | None = None


def reset_hook_execution_state() -> None:
    """Reset global execution state for unit tests."""
    _HOOK_FAILURES.clear()


def _scope_agent_name(context: HookExecutionContext) -> str | None:  # noqa: PLR0911
    if isinstance(context, ToolBeforeCallContext | ToolAfterCallContext):
        return context.agent_name
    if isinstance(context, MessageEnrichContext):
        return context.target_entity_name
    if isinstance(context, MessageReceivedContext):
        return context.envelope.agent_name
    if isinstance(context, BeforeResponseContext):
        return context.draft.envelope.agent_name
    if isinstance(context, AfterResponseContext):
        return context.result.envelope.agent_name
    if isinstance(context, AgentLifecycleContext):
        return context.entity_name
    return None


def _scope_room_ids(context: HookExecutionContext) -> tuple[str, ...]:  # noqa: PLR0911
    if isinstance(context, ToolBeforeCallContext | ToolAfterCallContext):
        return (context.room_id,) if context.room_id else ()
    if isinstance(context, MessageReceivedContext | MessageEnrichContext):
        return (context.envelope.room_id,)
    if isinstance(context, BeforeResponseContext):
        return (context.draft.envelope.room_id,)
    if isinstance(context, AfterResponseContext):
        return (context.result.envelope.room_id,)
    if isinstance(context, ScheduleFiredContext | ReactionReceivedContext):
        return (context.room_id,)
    if isinstance(context, AgentLifecycleContext):
        return context.rooms
    if isinstance(context, CustomEventContext) and context.room_id:
        return (context.room_id,)
    return ()


def _hook_in_scope(hook: RegisteredHook, context: HookExecutionContext) -> bool:
    if hook.agents is not None:
        agent_name = _scope_agent_name(context)
        if agent_name is None or agent_name not in hook.agents:
            return False

    if hook.rooms is not None:
        room_ids = _scope_room_ids(context)
        if not any(room_id in hook.rooms for room_id in room_ids):
            return False

    return True


def _context_logger(hook: RegisteredHook) -> object:
    return get_logger("mindroom.hooks").bind(
        plugin_name=hook.plugin_name,
        hook_name=hook.hook_name,
        event_name=hook.event_name,
    )


def _copy_tool_after_result(result: object | None) -> object | None:
    """Isolate observer hooks from mutating the caller-visible tool result."""
    try:
        return deepcopy(result)
    except Exception:
        return result


def _bind_hook_context(hook: RegisteredHook, context: HookExecutionContext) -> HookExecutionContext:
    replacement_kwargs: dict[str, object] = {
        "plugin_name": hook.plugin_name,
        "settings": dict(hook.settings),
        "logger": _context_logger(hook),
    }
    if isinstance(context, ToolBeforeCallContext | ToolAfterCallContext):
        replacement_kwargs["arguments"] = deepcopy(context.arguments)
    if isinstance(context, ToolAfterCallContext):
        replacement_kwargs["result"] = _copy_tool_after_result(context.result)
    if isinstance(context, MessageEnrichContext):
        replacement_kwargs["_items"] = []
    return replace(context, **replacement_kwargs)


def _merge_observer_context_changes(
    context: HookExecutionContext,
    hook_context: HookExecutionContext,
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


def _circuit_breaker_key(hook: RegisteredHook) -> tuple[str, str]:
    return (hook.plugin_name, hook.hook_name)


def _is_hook_on_cooldown(hook: RegisteredHook) -> bool:
    failure_state = _HOOK_FAILURES.get(_circuit_breaker_key(hook))
    if failure_state is None:
        return False
    return failure_state.cooldown_until_monotonic > time.monotonic()


def _record_hook_success(hook: RegisteredHook) -> None:
    failure_state = _HOOK_FAILURES.get(_circuit_breaker_key(hook))
    if failure_state is None:
        return
    failure_state.consecutive_failures = 0
    failure_state.cooldown_until_monotonic = 0.0


def _record_hook_failure(hook: RegisteredHook) -> None:
    failure_state = _HOOK_FAILURES.setdefault(_circuit_breaker_key(hook), _HookFailureState())
    failure_state.consecutive_failures += 1
    if failure_state.consecutive_failures >= _CIRCUIT_BREAKER_FAILURE_THRESHOLD:
        failure_state.cooldown_until_monotonic = time.monotonic() + _CIRCUIT_BREAKER_COOLDOWN_SECONDS


async def _invoke_hook(hook: RegisteredHook, context: HookExecutionContext) -> _HookInvocationResult:
    timeout_seconds = _effective_timeout_ms(hook) / 1000
    started_at = time.monotonic()
    try:
        async with asyncio.timeout(timeout_seconds):
            result = await hook.callback(context)
    except Exception:
        duration_ms = round((time.monotonic() - started_at) * 1000, 2)
        _record_hook_failure(hook)
        context.logger.exception(
            "Hook execution failed",
            correlation_id=context.correlation_id,
            duration_ms=duration_ms,
            timeout_ms=_effective_timeout_ms(hook),
        )
        return _HookInvocationResult(succeeded=False)

    duration_ms = round((time.monotonic() - started_at) * 1000, 2)
    _record_hook_success(hook)
    context.logger.debug(
        "Hook execution succeeded",
        correlation_id=context.correlation_id,
        duration_ms=duration_ms,
    )
    return _HookInvocationResult(succeeded=True, value=result)


def _eligible_hooks(
    registry: HookRegistry,
    event_name: str,
    context: HookExecutionContext,
) -> tuple[RegisteredHook, ...]:
    hooks = registry.hooks_for(event_name)
    if not hooks:
        return ()

    eligible_hooks: list[RegisteredHook] = []
    for hook in hooks:
        if not _hook_in_scope(hook, context):
            continue
        if _is_hook_on_cooldown(hook):
            logger.warning(
                "Skipping hook on cooldown",
                plugin_name=hook.plugin_name,
                hook_name=hook.hook_name,
                event_name=hook.event_name,
                correlation_id=context.correlation_id,
            )
            continue
        eligible_hooks.append(hook)
    return tuple(eligible_hooks)


async def emit(registry: HookRegistry, event_name: str, context: HookExecutionContext) -> None:
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
            await _invoke_hook(hook, hook_context)
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
            invocation = await _invoke_hook(hook, hook_context)
            if not invocation.succeeded:
                continue
            context.declined = hook_context.declined
            context.decline_reason = hook_context.decline_reason
            if context.declined:
                return
    finally:
        _EMIT_DEPTH.reset(token)


def _normalize_collector_result(result: object | None, hook_context: MessageEnrichContext) -> list[EnrichmentItem]:
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
    context: MessageEnrichContext,
) -> list[EnrichmentItem]:
    """Run collector hooks concurrently and return merged enrichment items."""
    hooks = _eligible_hooks(registry, event_name, context)
    if not hooks:
        return []

    semaphore = asyncio.Semaphore(_COLLECT_CONCURRENCY_LIMIT)

    async def run_hook(hook: RegisteredHook) -> list[EnrichmentItem]:
        async with semaphore:
            hook_context = cast("MessageEnrichContext", _bind_hook_context(hook, context))
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
    current_draft = context.draft
    for hook in _eligible_hooks(registry, event_name, context):
        hook_context = cast("BeforeResponseContext", _bind_hook_context(hook, replace(context, draft=current_draft)))
        invocation = await _invoke_hook(hook, hook_context)
        if isinstance(invocation.value, ResponseDraft):
            current_draft = invocation.value
            continue
        current_draft = hook_context.draft
    return current_draft
