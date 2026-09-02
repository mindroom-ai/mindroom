"""Drop the caller-frame namespace pydantic keeps on Agno's cached tool wrappers.

Agno 3 derives each tool's pydantic ``validate_call`` wrapper once and caches it
across runs (module-wide for plain callables, per owner for bound methods).
pydantic builds that wrapper with the locals of the frame that called it, for
forward-reference resolution, and on Python 3.13 ``frame.f_locals`` is a proxy
that keeps the frame and its entire call chain alive. Every cached wrapper
therefore pinned the Agent, session, and run state on the stack when the tool
was first wrapped, for as long as the cache entry lived.

The validators are built eagerly, so the namespace is dead weight once the
wrapper exists and is replaced with an empty resolver.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agno.tools.function import Function
from pydantic._internal._namespace_utils import NsResolver
from pydantic._internal._validate_call import ValidateCallWrapper

if TYPE_CHECKING:
    from collections.abc import Callable

_ORIGINAL_WRAP_CALLABLE_UNCACHED = Function._wrap_callable_uncached
_PATCHED = False


def _release_frame_namespace(wrapped: Callable[..., Any]) -> None:
    """Replace the captured caller namespace on every pydantic wrapper ``wrapped`` closes over."""
    for cell in getattr(wrapped, "__closure__", None) or ():
        try:
            contents = cell.cell_contents
        except ValueError:
            continue
        validator = getattr(contents, "__self__", contents)
        if isinstance(validator, ValidateCallWrapper):
            validator.ns_resolver = NsResolver()
        elif callable(contents) and getattr(contents, "_wrapped_for_validation", False):
            # Async-generator tools wrap the validated callable in one more shim.
            _release_frame_namespace(contents)


def _wrap_callable_without_frame_namespace(func: Callable[..., Any]) -> Callable[..., Any]:
    wrapped = _ORIGINAL_WRAP_CALLABLE_UNCACHED(func)
    if wrapped is not func:
        _release_frame_namespace(wrapped)
    return wrapped


def apply_patch() -> None:
    """Route Agno's tool wrapping through the namespace-releasing wrapper once per process."""
    global _PATCHED
    if _PATCHED:
        return
    type.__setattr__(Function, "_wrap_callable_uncached", staticmethod(_wrap_callable_without_frame_namespace))
    _PATCHED = True
