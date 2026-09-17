"""Shared response envelope for ordinary and reconstructed approval execution."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from functools import wraps
from typing import cast

from mindroom.background_tasks import run_coroutine_until_complete
from mindroom.tool_jobs.consumption import ConsumptionOwner, consumption_context, finalize_consumption
from mindroom.tool_jobs.resources import ExecutionResources, bind_execution_resources, execution_resources
from mindroom.tool_system.context_bound_streams import closing_async_stream, context_bound_async_stream


def owned_tool_execution[FunctionT: Callable[..., object]](
    function: FunctionT,
    *,
    enabled: Callable[..., bool] = lambda *_args, **_kwargs: True,
) -> FunctionT:
    """Own resources and durable result claims for a coroutine or async stream."""
    if inspect.isasyncgenfunction(function):

        @wraps(function)
        def streaming(*args: object, **kwargs: object) -> AsyncIterator[object]:
            if not enabled(*args, **kwargs):
                return cast("Callable[..., AsyncIterator[object]]", function)(*args, **kwargs)
            resources = ExecutionResources()
            consumption = ConsumptionOwner()

            @contextmanager
            def bind() -> Iterator[None]:
                with bind_execution_resources(resources), consumption_context(consumption):
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
        async with execution_resources(), _consumption_context_async():
            return await cast("Callable[..., Awaitable[object]]", function)(*args, **kwargs)

    return cast("FunctionT", blocking)


@asynccontextmanager
async def _consumption_context_async() -> AsyncIterator[None]:
    """Finalize even canceled parents before releasing response resource owners."""
    with consumption_context(ConsumptionOwner()):
        try:
            yield
        finally:
            await finalize_consumption()
