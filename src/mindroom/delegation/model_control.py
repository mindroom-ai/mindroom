"""Pause fresh provider requests that could execute provider-native child tools."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.agno_compat_model_hooks import install_async_invocation_hooks
from mindroom.delegation.control import subagent_tool_checkpoint
from mindroom.tool_system.context_bound_streams import closing_async_stream

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Coroutine

    from agno.models.base import Model
    from agno.models.fallback import FallbackConfig
    from agno.models.response import ModelResponse

type _Invoke = Callable[..., Coroutine[object, object, ModelResponse]]
type _Stream = Callable[..., AsyncIterator[ModelResponse]]


def _wrap_invoke(original: _Invoke) -> _Invoke:
    async def invoke(*args: object, **kwargs: object) -> ModelResponse:
        await subagent_tool_checkpoint()
        return await original(*args, **kwargs)

    return invoke


def _wrap_stream(original: _Stream) -> _Stream:
    async def stream(*args: object, **kwargs: object) -> AsyncIterator[ModelResponse]:
        await subagent_tool_checkpoint()
        events = original(*args, **kwargs)
        async with closing_async_stream(events):
            async for event in events:
                yield event

    return stream


def install_subagent_model_control(model: Model, fallback_config: FallbackConfig | None) -> None:
    """Guard each primary and resolved fallback request using task-local child control."""
    models = [model]
    if fallback_config is not None:
        models.extend(
            fallback
            for fallback in (
                *fallback_config.on_error,
                *fallback_config.on_rate_limit,
                *fallback_config.on_context_overflow,
            )
            if not isinstance(fallback, str)
        )
    for candidate in models:
        install_async_invocation_hooks(
            candidate,
            marker="_mindroom_subagent_control_installed",
            wrap_invoke=_wrap_invoke,
            wrap_stream=_wrap_stream,
        )
