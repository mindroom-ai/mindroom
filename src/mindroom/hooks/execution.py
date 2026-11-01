"""Hook execution helpers with timeouts and failure isolation."""

from __future__ import annotations

import asyncio
import time
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, cast

from mindroom.logging_config import get_logger
from mindroom.timing import elapsed_ms_since
from mindroom.tool_system.tool_environment import ToolEnvironment

from .context import (
    AgentLifecycleContext,
    BeforeResponseContext,
    CompactionHookContext,
    ContactBeforeSendContext,
    CustomEventContext,
    FinalResponseDraft,
    FinalResponseTransformContext,
    HookContext,
    MessageEnrichContext,
    MessageReceivedContext,
    ReactionReceivedContext,
    ResponseDraft,
    RoomMemberJoinedContext,
    RoomMemberLeftContext,
    ScheduleFiredContext,
    SessionHookContext,
    SystemEnrichContext,
    ToolAfterCallContext,
    ToolBeforeCallContext,
    ToolEnvironmentContext,
    message_envelope_for_hook_context,
)
from .types import (
    EVENT_MESSAGE_RECEIVED,
    EVENT_TOOL_ENVIRONMENT,
    EnrichmentItem,
    RegisteredHook,
    default_timeout_ms_for_event,
)

if TYPE_CHECKING:
    from .registry import HookRegistry

logger = get_logger(__name__)

_COLLECT_CONCURRENCY_LIMIT = 10
_MAX_EMIT_DEPTH = 3
_EMIT_DEPTH: ContextVar[int] = ContextVar("mindroom_hook_emit_depth", default=0)

type _TransformContext = BeforeResponseContext | FinalResponseTransformContext
type _TransformDraft = ResponseDraft | FinalResponseDraft


@dataclass(frozen=True, slots=True)
class _HookInvocationResult:
    succeeded: bool
    value: object | None = None


def _scope_agent_name(context: HookContext) -> str | None:
    agent_name: str | None = None
    if isinstance(
        context,
        ToolBeforeCallContext | ToolAfterCallContext | ToolEnvironmentContext | ContactBeforeSendContext,
    ):
        agent_name = context.agent_name
    else:
        envelope = message_envelope_for_hook_context(context)
        if envelope is not None:
            agent_name = envelope.agent_name
        elif isinstance(context, AgentLifecycleContext):
            agent_name = context.entity_name
        elif isinstance(
            context,
            CompactionHookContext | SessionHookContext | RoomMemberJoinedContext | RoomMemberLeftContext,
        ):
            agent_name = context.agent_name
    return agent_name


def _scope_room_ids(context: HookContext) -> tuple[str, ...]:
    if isinstance(
        context,
        ToolBeforeCallContext | ToolAfterCallContext | ToolEnvironmentContext | ContactBeforeSendContext,
    ):
        return (context.room_id,) if context.room_id else ()
    envelope = message_envelope_for_hook_context(context)
    if envelope is not None:
        return (envelope.room_id,)
    if isinstance(
        context,
        ScheduleFiredContext
        | ReactionReceivedContext
        | RoomMemberJoinedContext
        | RoomMemberLeftContext
        | CompactionHookContext
        | SessionHookContext,
    ):
        return (context.room_id,)
    if isinstance(context, AgentLifecycleContext):
        return context.rooms
    if isinstance(context, CustomEventContext) and context.room_id:
        return (context.room_id,)
    return ()


def _hook_in_scope(hook: RegisteredHook, context: HookContext) -> bool:
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


def _bind_hook_context(
    registry: HookRegistry,
    hook: RegisteredHook,
    context: HookContext,
) -> HookContext:
    """Return the copy of ``context`` one hook receives, under that hook's plugin name, settings, and logger."""
    replacement_kwargs: dict[str, object] = {
        "plugin_name": hook.plugin_name,
        "settings": dict(hook.settings),
        "logger": _context_logger(hook),
        "_hook_registry_snapshot": registry,
    }
    if isinstance(context, ToolBeforeCallContext | ToolAfterCallContext | ToolEnvironmentContext):
        replacement_kwargs["arguments"] = deepcopy(context.arguments)
    if isinstance(context, ToolAfterCallContext):
        replacement_kwargs["result"] = _snapshot_tool_observer_value(context.result)
        replacement_kwargs["error"] = _snapshot_tool_observer_error(context.error)
    if isinstance(context, MessageEnrichContext | SystemEnrichContext):
        replacement_kwargs["_items"] = []
    return replace(context, **replacement_kwargs)


def _merge_observer_context_changes(
    context: HookContext,
    hook_context: HookContext,
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


async def _invoke_hook(hook: RegisteredHook, context: HookContext) -> _HookInvocationResult:
    timeout_seconds = _effective_timeout_ms(hook) / 1000
    started_at = time.monotonic()
    try:
        async with asyncio.timeout(timeout_seconds):
            result = await hook.callback(context)
    except asyncio.CancelledError:
        raise
    except (Exception, SystemExit) as error:
        duration_ms = elapsed_ms_since(started_at, ndigits=2)
        context.logger.exception(
            "Hook execution failed",
            correlation_id=context.correlation_id,
            duration_ms=duration_ms,
            timeout_ms=_effective_timeout_ms(hook),
        )
        if hook.required:
            msg = f"Required startup hook {hook.plugin_name}:{hook.hook_name} failed"
            raise RuntimeError(msg) from error
        return _HookInvocationResult(succeeded=False)

    duration_ms = elapsed_ms_since(started_at, ndigits=2)
    context.logger.debug(
        "Hook execution succeeded",
        correlation_id=context.correlation_id,
        duration_ms=duration_ms,
    )
    return _HookInvocationResult(succeeded=True, value=result)


def _eligible_hooks(
    registry: HookRegistry,
    event_name: str,
    context: HookContext,
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
    context: HookContext,
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
            hook_context = _bind_hook_context(registry, hook, context)
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
            hook_context = cast(
                "ToolBeforeCallContext",
                _bind_hook_context(registry, hook, context),
            )
            invocation = await _invoke_hook(hook, hook_context)
            if not invocation.succeeded:
                continue
            context.declined = hook_context.declined
            context.decline_reason = hook_context.decline_reason
            if context.declined:
                return
    finally:
        _EMIT_DEPTH.reset(token)


async def emit_policy(
    registry: HookRegistry,
    event_name: str,
    context: ContactBeforeSendContext,
) -> None:
    """Run policy hooks serially in priority order, failing closed.

    After each hook ``context`` takes its ``text``, ``verdict``, and ``feedback``, and the first verdict other than
    ``send`` stops the chain.
    A hook that fails or times out denies the send, as does a policy run nested too deeply.
    """
    depth = _EMIT_DEPTH.get()
    if depth >= _MAX_EMIT_DEPTH:
        logger.warning(
            "Denying contact policy emission after recursion limit",
            event_name=event_name,
            correlation_id=context.correlation_id,
            max_depth=_MAX_EMIT_DEPTH,
        )
        context.deny("Contact policy hooks nested too deeply.")
        return

    token = _EMIT_DEPTH.set(depth + 1)
    try:
        for hook in _eligible_hooks(registry, event_name, context):
            hook_context = cast(
                "ContactBeforeSendContext",
                _bind_hook_context(registry, hook, context),
            )
            invocation = await _invoke_hook(hook, hook_context)
            if not invocation.succeeded:
                context.deny("A contact policy hook failed.")
                return
            context.text = hook_context.text
            context.verdict = hook_context.verdict
            context.feedback = hook_context.feedback
            if context.verdict != "send":
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


async def _collect[C: HookContext](
    registry: HookRegistry,
    event_name: str,
    context: C,
) -> list[tuple[C, object | None]]:
    """Run collector hooks concurrently; return each finished hook's own context and return value, in hook order."""
    hooks = _eligible_hooks(registry, event_name, context)
    if not hooks:
        return []

    semaphore = asyncio.Semaphore(_COLLECT_CONCURRENCY_LIMIT)

    async def run_hook(hook: RegisteredHook) -> tuple[C, object | None] | None:
        async with semaphore:
            hook_context = cast("C", _bind_hook_context(registry, hook, context))
            invocation = await _invoke_hook(hook, hook_context)
            return (hook_context, invocation.value) if invocation.succeeded else None

    results = await asyncio.gather(*(run_hook(hook) for hook in hooks))
    return [result for result in results if result is not None]


async def emit_collect(
    registry: HookRegistry,
    event_name: str,
    context: MessageEnrichContext | SystemEnrichContext,
) -> list[EnrichmentItem]:
    """Run collector hooks concurrently and return merged enrichment items."""
    return [
        item
        for hook_context, result in await _collect(registry, event_name, context)
        for item in _normalize_collector_result(result, hook_context)
    ]


async def emit_collect_environment(registry: HookRegistry, context: ToolEnvironmentContext) -> ToolEnvironment:
    """Run tool:environment hooks concurrently and return their changes, a later hook's over an earlier one's.

    The first hook in hook order that declines the call marks ``context`` declined with its reason.
    """
    environment = ToolEnvironment()
    for hook_context, _result in await _collect(registry, EVENT_TOOL_ENVIRONMENT, context):
        environment = environment.then(hook_context._environment)
        if hook_context.declined and not context.declined:
            context.decline(hook_context.decline_reason)
    return environment


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
        hook_draft = deepcopy(current_draft) if copy_on_write else current_draft
        hook_context = cast("_TransformContext", _bind_hook_context(registry, hook, replace(context, draft=hook_draft)))
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
