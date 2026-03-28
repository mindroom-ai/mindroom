"""Hook execution helpers with timeouts and failure isolation."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Hashable
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
_IMMUTABLE_SNAPSHOT_TYPES = (type(None), str, bytes, int, float, bool, complex)
_MISSING_SNAPSHOT = object()

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


def _snapshot_tool_observer_key(key: object, memo: dict[int, object]) -> object:
    snapshot_key = _snapshot_tool_observer_value(key, memo)
    if isinstance(snapshot_key, Hashable):
        return snapshot_key
    return repr(snapshot_key)


def _snapshot_exception(value: BaseException, memo: dict[int, object]) -> BaseException:
    snapshot_args = tuple(_snapshot_tool_observer_value(arg, memo) for arg in value.args)
    try:
        clone = type(value)(*snapshot_args)
    except Exception:
        clone = Exception(*snapshot_args) if snapshot_args else Exception(str(value))
    memo[id(value)] = clone
    clone.args = snapshot_args

    notes = getattr(value, "__notes__", None)
    if notes is not None:
        for note in notes:
            clone.add_note(str(note))

    for attr_name, attr_value in vars(value).items():
        setattr(clone, attr_name, _snapshot_tool_observer_value(attr_value, memo))
    return clone


def _snapshot_object_instance(value: object, memo: dict[int, object]) -> object:
    try:
        clone = object.__new__(type(value))
    except TypeError:
        return repr(value)

    memo[id(value)] = clone
    try:
        for attr_name, attr_value in vars(value).items():
            setattr(clone, attr_name, _snapshot_tool_observer_value(attr_value, memo))
    except Exception:
        return repr(value)

    slots = getattr(type(value), "__slots__", ())
    if isinstance(slots, str):
        slots = (slots,)
    for slot_name in slots:
        if slot_name in {"__dict__", "__weakref__"} or not hasattr(value, slot_name):
            continue
        try:
            setattr(clone, slot_name, _snapshot_tool_observer_value(getattr(value, slot_name), memo))
        except Exception:
            return repr(value)
    return clone


def _try_deepcopy_snapshot(value: object, memo: dict[int, object]) -> object:
    try:
        snapshot = deepcopy(value)
    except Exception:
        return _MISSING_SNAPSHOT
    memo[id(value)] = snapshot
    return snapshot


def _snapshot_mapping(value: dict[object, object], memo: dict[int, object]) -> dict[object, object | None]:
    snapshot_dict: dict[object, object | None] = {}
    memo[id(value)] = snapshot_dict
    for key, item in value.items():
        snapshot_dict[_snapshot_tool_observer_key(key, memo)] = _snapshot_tool_observer_value(item, memo)
    return snapshot_dict


def _snapshot_list(value: list[object], memo: dict[int, object]) -> list[object | None]:
    snapshot_list: list[object | None] = []
    memo[id(value)] = snapshot_list
    snapshot_list.extend(_snapshot_tool_observer_value(item, memo) for item in value)
    return snapshot_list


def _snapshot_tuple(value: tuple[object, ...], memo: dict[int, object]) -> tuple[object | None, ...]:
    snapshot_tuple = tuple(_snapshot_tool_observer_value(item, memo) for item in value)
    memo[id(value)] = snapshot_tuple
    return snapshot_tuple


def _snapshot_set(value: set[object], memo: dict[int, object]) -> set[object]:
    snapshot_set = {_snapshot_tool_observer_key(item, memo) for item in value}
    memo[id(value)] = snapshot_set
    return snapshot_set


def _snapshot_frozenset(value: frozenset[object], memo: dict[int, object]) -> frozenset[object]:
    snapshot_frozenset = frozenset(_snapshot_tool_observer_key(item, memo) for item in value)
    memo[id(value)] = snapshot_frozenset
    return snapshot_frozenset


def _snapshot_tool_observer_fallback(value: object, memo: dict[int, object]) -> object:
    snapshot: object
    if isinstance(value, BaseException):
        snapshot = _snapshot_exception(value, memo)
    elif isinstance(value, dict):
        snapshot = _snapshot_mapping(cast("dict[object, object]", value), memo)
    elif isinstance(value, list):
        snapshot = _snapshot_list(cast("list[object]", value), memo)
    elif isinstance(value, tuple):
        snapshot = _snapshot_tuple(value, memo)
    elif isinstance(value, set):
        snapshot = _snapshot_set(cast("set[object]", value), memo)
    elif isinstance(value, frozenset):
        snapshot = _snapshot_frozenset(value, memo)
    elif isinstance(value, bytearray):
        snapshot_bytes = bytes(value)
        memo[id(value)] = snapshot_bytes
        snapshot = snapshot_bytes
    elif hasattr(value, "__dict__") or getattr(type(value), "__slots__", ()):
        snapshot = _snapshot_object_instance(value, memo)
    else:
        snapshot = repr(value)
    return snapshot


def _snapshot_tool_observer_value(
    value: object | None,
    memo: dict[int, object] | None = None,
) -> object | None:
    """Return an observer-safe snapshot that cannot mutate caller-visible state."""
    if isinstance(value, _IMMUTABLE_SNAPSHOT_TYPES):
        return value

    if memo is None:
        memo = {}
    value_id = id(value)
    cached = memo.get(value_id)
    if cached is not None:
        return cached

    snapshot = _try_deepcopy_snapshot(value, memo)
    if snapshot is not _MISSING_SNAPSHOT:
        return snapshot
    return _snapshot_tool_observer_fallback(value, memo)


def _bind_hook_context(hook: RegisteredHook, context: HookExecutionContext) -> HookExecutionContext:
    replacement_kwargs: dict[str, object] = {
        "plugin_name": hook.plugin_name,
        "settings": dict(hook.settings),
        "logger": _context_logger(hook),
    }
    if isinstance(context, ToolBeforeCallContext | ToolAfterCallContext):
        replacement_kwargs["arguments"] = deepcopy(context.arguments)
    if isinstance(context, ToolAfterCallContext):
        replacement_kwargs["result"] = _snapshot_tool_observer_value(context.result)
        replacement_kwargs["error"] = cast("BaseException | None", _snapshot_tool_observer_value(context.error))
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
