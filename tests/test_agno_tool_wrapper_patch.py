"""Agno's cached pydantic tool wrappers must not pin the frames that built them."""

from __future__ import annotations

import gc
import inspect
import weakref
from collections.abc import AsyncIterator  # noqa: TC003 - pydantic resolves the annotation at wrap time.
from typing import TYPE_CHECKING

from agno.tools.function import Function

from mindroom import agno_tool_wrapper_patch

if TYPE_CHECKING:
    from collections.abc import Callable


class _Sentinel:
    """Any object that would be a local of the frame wrapping a tool."""


def _tool(value: int, label: str = "x") -> str:
    """Echo the arguments."""
    return f"{label}:{value}"


async def _streaming_tool(value: int) -> AsyncIterator[str]:
    """Yield the argument twice."""
    yield str(value)
    yield str(value)


def _wrap_from_a_frame_holding(sentinel: _Sentinel, tool: Callable[..., object] = _tool) -> object:
    assert sentinel is not None
    return Function._wrap_callable_uncached(tool)


def test_wrapped_tool_does_not_retain_the_wrapping_frame() -> None:
    """The caller's locals must be collectable once the wrapper exists."""
    agno_tool_wrapper_patch.apply_patch()
    sentinel = _Sentinel()
    sentinel_ref = weakref.ref(sentinel)

    wrapped = _wrap_from_a_frame_holding(sentinel)
    del sentinel
    gc.collect()

    assert sentinel_ref() is None
    assert wrapped("3", label="y") == "y:3"  # type: ignore[operator]  # pydantic coercion still applies


def test_async_generator_tool_does_not_retain_the_wrapping_frame() -> None:
    """The async-generator shim closes over a second pydantic wrapper that must be released too."""
    agno_tool_wrapper_patch.apply_patch()
    sentinel = _Sentinel()
    sentinel_ref = weakref.ref(sentinel)

    wrapped = _wrap_from_a_frame_holding(sentinel, _streaming_tool)
    del sentinel
    gc.collect()

    assert sentinel_ref() is None
    assert inspect.isasyncgenfunction(wrapped)


def test_apply_patch_is_idempotent() -> None:
    """Repeat installs must keep the first wrapper in place."""
    agno_tool_wrapper_patch.apply_patch()
    installed = Function._wrap_callable_uncached
    agno_tool_wrapper_patch.apply_patch()
    assert Function._wrap_callable_uncached is installed
