"""Shared response envelope for ordinary and reconstructed approval execution."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import contextmanager, nullcontext
from functools import wraps
from typing import Protocol, cast

from mindroom.background_tasks import run_coroutine_until_complete
from mindroom.shell.background_scope import (
    ShellBackgroundScope,
    bind_shell_background_scope,
    current_shell_background_scope,
    shell_background_scope,
)
from mindroom.tool_jobs.consumption import ConsumptionOwner, consumption_context, finalize_consumption
from mindroom.tool_jobs.notices import JobNotices, job_notice_context
from mindroom.tool_jobs.resources import ExecutionResources, bind_execution_resources, execution_resources
from mindroom.tool_system.context_bound_streams import closing_async_stream, context_bound_async_stream


class _OwnedExecution(Protocol):
    def __call__[FunctionT: Callable[..., object]](self, function: FunctionT, /) -> FunctionT: ...


async def _close_shell_scope(scope: ShellBackgroundScope | None) -> None:
    if scope is not None:
        await run_coroutine_until_complete(scope.close())


def owned_tool_execution(*, enabled: Callable[..., bool]) -> _OwnedExecution:
    """Own execution resources for a response, and durable claims and notices when jobs are enabled.

    The decorated function keeps its signature for type checking.
    """

    def decorate[FunctionT: Callable[..., object]](function: FunctionT, /) -> FunctionT:
        if inspect.isasyncgenfunction(function):

            @wraps(function)
            def streaming(*args: object, **kwargs: object) -> AsyncIterator[object]:
                jobs_enabled = enabled(*args, **kwargs)
                resources = ExecutionResources()
                consumption = ConsumptionOwner()
                notices = JobNotices()
                owned_shell_scope = ShellBackgroundScope() if current_shell_background_scope() is None else None

                @contextmanager
                def bind() -> Iterator[None]:
                    with (
                        bind_execution_resources(resources, response=True, jobs_enabled=jobs_enabled),
                        bind_shell_background_scope(owned_shell_scope)
                        if owned_shell_scope is not None
                        else nullcontext(),
                        consumption_context(consumption) if jobs_enabled else nullcontext(),
                        job_notice_context(notices) if jobs_enabled else nullcontext(),
                    ):
                        yield

                async def stream() -> AsyncIterator[object]:
                    try:
                        source = cast("Callable[..., AsyncIterator[object]]", function)(*args, **kwargs)
                        async with closing_async_stream(source):
                            async for chunk in source:
                                yield chunk
                    finally:
                        try:
                            if jobs_enabled:
                                await finalize_consumption()
                        finally:
                            try:
                                await _close_shell_scope(owned_shell_scope)
                            finally:
                                await run_coroutine_until_complete(resources.release_parent())

                return context_bound_async_stream(context_factory=bind, stream_factory=stream)

            return cast("FunctionT", streaming)

        @wraps(function)
        async def blocking(*args: object, **kwargs: object) -> object:
            jobs_enabled = enabled(*args, **kwargs)
            # Consumption finalizes, even for a cancelled parent, before response resources are released.
            async with (
                execution_resources(response=True, jobs_enabled=jobs_enabled),
                shell_background_scope(reuse_bound=True),
            ):
                if not jobs_enabled:
                    return await cast("Callable[..., Awaitable[object]]", function)(*args, **kwargs)
                with consumption_context(ConsumptionOwner()), job_notice_context(JobNotices()):
                    try:
                        return await cast("Callable[..., Awaitable[object]]", function)(*args, **kwargs)
                    finally:
                        await finalize_consumption()

        return cast("FunctionT", blocking)

    return decorate
