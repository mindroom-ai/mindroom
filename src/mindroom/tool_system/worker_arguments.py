"""Serialize tool call arguments without exporting primary runtime objects."""

from __future__ import annotations

import inspect
import math
from pathlib import Path
from typing import TYPE_CHECKING, cast

from agno.agent import Agent
from agno.team import Team

if TYPE_CHECKING:
    from collections.abc import Callable


def _argument_value(value: object) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_argument_value(item) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _argument_value(item) for key, item in value.items()}
    msg = f"Unsupported worker argument type: {type(value).__name__}"
    raise TypeError(msg)


def prepare_worker_call_arguments(
    args: tuple[object, ...],
    kwargs: dict[str, object],
    *,
    entrypoint: Callable[..., object] | None,
    inert_agent: bool,
) -> tuple[list[object], dict[str, object]]:
    """Retain call shape, replacing only a declared unused Agent parameter."""
    if inert_agent and entrypoint is not None:
        bound = inspect.signature(entrypoint).bind(*args, **kwargs)
        if "agent" in bound.arguments:
            agent = bound.arguments["agent"]
            if agent is not None and not isinstance(agent, (Agent, Team)):
                msg = "Unsupported worker agent argument"
                raise TypeError(msg)
            if "agent" in kwargs:
                kwargs = {**kwargs, "agent": None}
            else:
                bound.arguments["agent"] = None
                args, kwargs = bound.args, bound.kwargs
    return [_argument_value(arg) for arg in args], cast("dict[str, object]", _argument_value(kwargs))
