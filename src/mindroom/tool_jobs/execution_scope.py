"""Shared response envelope for ordinary and reconstructed approval execution."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from typing import Protocol, cast

from mindroom.background_tasks import run_coroutine_until_complete
from mindroom.tool_jobs.consumption import ConsumptionOwner, consumption_context, finalize_consumption
from mindroom.tool_jobs.notices import JobNotices, job_notice_context
from mindroom.tool_jobs.resources import ExecutionResources, bind_execution_resources, execution_resources
from mindroom.tool_system.context_bound_streams import closing_async_stream, context_bound_async_stream


class _OwnedExecution(Protocol):
    def __call__[FunctionT: Callable[..., object]](self, function: FunctionT, /) -> FunctionT: ...


def owned_tool_execution(*, enabled: Callable[..., bool] = lambda *_args, **_kwargs: True) -> _OwnedExecution:
    """Decorate a coroutine or async stream to own resources, durable result claims, and job notices when ``enabled``.

    The decorated function keeps its signature for type checking.
    """

    def decorate[FunctionT: Callable[..., object]](function: FunctionT, /) -> FunctionT:
        if inspect.isasyncgenfunction(function):

            @wraps(function)
            def streaming(*args: object, **kwargs: object) -> AsyncIterator[object]:
                if not enabled(*args, **kwargs):
                    return cast("Callable[..., AsyncIterator[object]]", function)(*args, **kwargs)
                resources = ExecutionResources()
                consumption = ConsumptionOwner()
                notices = JobNotices()

                @contextmanager
                def bind() -> Iterator[None]:
                    with (
                        bind_execution_resources(resources),
                        consumption_context(consumption),
                        job_notice_context(notices),
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
                            await finalize_consumption()
                        finally:
                            await run_coroutine_until_complete(resources.release_parent())

                return context_bound_async_stream(context_factory=bind, stream_factory=stream)

            return cast("FunctionT", streaming)

        @wraps(function)
        async def blocking(*args: object, **kwargs: object) -> object:
            if not enabled(*args, **kwargs):
                return await cast("Callable[..., Awaitable[object]]", function)(*args, **kwargs)
            # Consumption finalizes, even for a cancelled parent, before response resources are released.
            async with execution_resources():
                with consumption_context(ConsumptionOwner()), job_notice_context(JobNotices()):
                    try:
                        return await cast("Callable[..., Awaitable[object]]", function)(*args, **kwargs)
                    finally:
                        await finalize_consumption()

        return cast("FunctionT", blocking)

    return decorate
