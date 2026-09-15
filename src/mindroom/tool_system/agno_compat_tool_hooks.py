"""Adapt Agno's private tool-hook chains to MindRoom's sync/async bridge.

MindRoom supplies result resolution and synchronous completion ownership;
this module owns only the Agno chain builders and their result-cache plumbing.
"""

from __future__ import annotations

import inspect
from functools import reduce, wraps
from typing import TYPE_CHECKING, Any

from agno.tools.function import FunctionCall, _detached, _record_entrypoint_result, _start_entrypoint_call

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

# Reason: Agno's hook chains do not unwrap deferred async bridge results or expose
# a public way to retain application ownership of an offloaded synchronous leaf.
# Upstream issue: No matching issue identified for this hook-chain extension point.
# Upstream PR: None identified; the missing extension point remains untracked.
# Remove when: Public hook APIs support deferred results and owner-controlled sync
# execution while preserving cache hits, argument rewrites, and raw-result recording.
# Coverage: tests/test_tool_hooks.py::test_sync_function_call_execute_inside_running_loop_unwraps_tool_hook_result;
# tests/test_tool_hooks.py::test_sync_tool_aexecute_keeps_hooks_on_loop_and_body_off_loop;
# tests/test_tool_hooks.py::test_sync_tool_aexecute_cancellation_during_before_hook_never_starts_body.

_ToolHookResult = Any
type _SyncEntrypointRunner = Callable[[Callable[..., Any], dict[str, Any]], Awaitable[Any]]

_ORIGINAL_BUILD_NESTED_EXECUTION_CHAIN_ASYNC = FunctionCall._build_nested_execution_chain_async
_ORIGINAL_BUILD_NESTED_EXECUTION_CHAIN = FunctionCall._build_nested_execution_chain
_AGNO_ASYNC_TOOL_HOOK_CHAIN_PATCHED = False
_AGNO_SYNC_TOOL_HOOK_CHAIN_PATCHED = False


def _patch_sync_tool_hook_chain(resolve_sync_result: Callable[[Any], Any]) -> None:
    """Teach Agno's sync tool hook chain to unwrap deferred async bridge results."""
    global _AGNO_SYNC_TOOL_HOOK_CHAIN_PATCHED

    if _AGNO_SYNC_TOOL_HOOK_CHAIN_PATCHED:
        return

    @wraps(_ORIGINAL_BUILD_NESTED_EXECUTION_CHAIN)
    def _patched_build_nested_execution_chain(
        self: FunctionCall,
        entrypoint_args: dict[str, Any],
        cached_result: Any | None = None,  # noqa: ANN401
        raw_results: list[Any] | None = None,
        cache_key: str | None = None,
    ) -> Callable[..., _ToolHookResult]:
        execution_chain = _ORIGINAL_BUILD_NESTED_EXECUTION_CHAIN(
            self,
            entrypoint_args,
            cached_result=cached_result,
            raw_results=raw_results,
            cache_key=cache_key,
        )

        def _wrapped_execution_chain(name: str, func: Callable[..., Any], args: dict[str, Any]) -> _ToolHookResult:
            return resolve_sync_result(execution_chain(name, func, args))

        return _wrapped_execution_chain

    type.__setattr__(FunctionCall, "_build_nested_execution_chain", _patched_build_nested_execution_chain)
    _AGNO_SYNC_TOOL_HOOK_CHAIN_PATCHED = True


def _build_sync_async_execution_chain(
    function_call: FunctionCall,
    entrypoint: Callable[..., _ToolHookResult],
    entrypoint_args: dict[str, Any],
    *,
    run_sync_entrypoint: _SyncEntrypointRunner,
    cached_result: Any | None,  # noqa: ANN401
    raw_results: list[Any] | None,
    cache_key: str | None,
) -> Callable[..., Awaitable[_ToolHookResult]]:
    """Build Agno's async hook chain around one offloaded synchronous leaf.

    Mirrors ``FunctionCall._build_nested_execution_chain_async``: a cache hit
    stands in for the entrypoint call unless a hook rewrote the keyed
    arguments, and every real entrypoint return is recorded in ``raw_results``
    so the caller can store it.
    """

    async def execute_sync_entrypoint(
        _name: str,
        _func: Callable[..., Any],
        _args: dict[str, Any],
    ) -> _ToolHookResult:
        if cached_result is not None and not function_call._moved_its_key(cache_key, entrypoint_args):
            return _detached(cached_result)
        arguments = entrypoint_args.copy()
        if function_call.arguments is not None:
            arguments.update(function_call.arguments)
        slot = _start_entrypoint_call(raw_results) if raw_results is not None else -1
        result = await run_sync_entrypoint(entrypoint, arguments)
        if raw_results is not None:
            _record_entrypoint_result(raw_results, slot, result)
        return result

    def create_hook_wrapper(
        inner_func: Callable[..., Awaitable[_ToolHookResult]],
        hook: Callable[..., Any],
    ) -> Callable[..., Awaitable[_ToolHookResult]]:
        async def wrapper(
            name: str,
            func: Callable[..., Any],
            args: dict[str, Any],
        ) -> _ToolHookResult:
            async def next_func(**kwargs: object) -> _ToolHookResult:
                return await inner_func(name, func, kwargs)

            hook_args = function_call._build_hook_args(hook, name, next_func, args)
            if inspect.iscoroutinefunction(hook):
                return await function_call._safe_hook_call_async(hook, hook_args)
            return function_call._safe_hook_call(hook, hook_args)

        return wrapper

    return reduce(
        create_hook_wrapper,
        reversed(function_call.function.tool_hooks or []),
        execute_sync_entrypoint,
    )


def _patch_async_tool_hook_chain(
    resolve_async_result: Callable[[Any], Awaitable[Any]],
    run_sync_entrypoint: _SyncEntrypointRunner,
    has_completion_tracker: Callable[[], bool],
) -> None:
    """Teach Agno's async tool hook chain to unwrap deferred sync-hook awaitables."""
    global _AGNO_ASYNC_TOOL_HOOK_CHAIN_PATCHED

    if _AGNO_ASYNC_TOOL_HOOK_CHAIN_PATCHED:
        return

    @wraps(_ORIGINAL_BUILD_NESTED_EXECUTION_CHAIN_ASYNC)
    async def _patched_build_nested_execution_chain_async(
        self: FunctionCall,
        entrypoint_args: dict[str, Any],
        cached_result: Any | None = None,  # noqa: ANN401
        raw_results: list[Any] | None = None,
        cache_key: str | None = None,
    ) -> Callable[..., Awaitable[_ToolHookResult]]:
        entrypoint = self.function.entrypoint
        if (
            not has_completion_tracker()
            or entrypoint is None
            or inspect.iscoroutinefunction(entrypoint)
            or inspect.isasyncgenfunction(entrypoint)
            or inspect.isgeneratorfunction(entrypoint)
        ):
            execution_chain = await _ORIGINAL_BUILD_NESTED_EXECUTION_CHAIN_ASYNC(
                self,
                entrypoint_args,
                cached_result=cached_result,
                raw_results=raw_results,
                cache_key=cache_key,
            )
        else:
            execution_chain = _build_sync_async_execution_chain(
                self,
                entrypoint,
                entrypoint_args,
                run_sync_entrypoint=run_sync_entrypoint,
                cached_result=cached_result,
                raw_results=raw_results,
                cache_key=cache_key,
            )

        async def _wrapped_execution_chain(
            name: str,
            func: Callable[..., Any],
            args: dict[str, Any],
        ) -> _ToolHookResult:
            result = await execution_chain(name, func, args)
            return await resolve_async_result(result)

        return _wrapped_execution_chain

    type.__setattr__(FunctionCall, "_build_nested_execution_chain_async", _patched_build_nested_execution_chain_async)
    _AGNO_ASYNC_TOOL_HOOK_CHAIN_PATCHED = True


def install_patch(
    *,
    resolve_sync_result: Callable[[Any], Any],
    resolve_async_result: Callable[[Any], Awaitable[Any]],
    run_sync_entrypoint: _SyncEntrypointRunner,
    has_completion_tracker: Callable[[], bool],
) -> None:
    """Install both chain adapters once, using the owning bridge's callbacks."""
    _patch_sync_tool_hook_chain(resolve_sync_result)
    _patch_async_tool_hook_chain(resolve_async_result, run_sync_entrypoint, has_completion_tracker)
