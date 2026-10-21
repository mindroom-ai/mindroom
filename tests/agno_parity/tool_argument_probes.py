"""Probe tools whose tool-call argument handling `tests/fixtures/agno_parity/tools/arguments.json` records.

Each probe returns the keyword arguments its body received, encoded by `encode_kwargs`, so a golden case pins what a
tool body sees after argument decoding and coercion.
This module imports no Agno code, so owned implementations keep using it after Agno is removed.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel


class ProbeFilter(BaseModel):
    """A structured argument that the model sends as a JSON object."""

    field: str
    limit: int = 10


def encode_kwargs(kwargs: dict[str, object]) -> dict[str, dict[str, object]]:
    """Encode each argument with its Python type name, so coercion such as "3" to 3 stays visible."""
    return {name: _encode(value) for name, value in kwargs.items()}


def _encode(value: object) -> dict[str, object]:
    if isinstance(value, BaseModel):
        return {"type": type(value).__name__, "value": value.model_dump(mode="json")}
    if value is None or isinstance(value, bool | int | float | str):
        return {"type": type(value).__name__, "value": value}
    if isinstance(value, list | tuple):
        return {"type": type(value).__name__, "value": [_encode(item) for item in value]}
    if isinstance(value, dict):
        return {"type": "dict", "value": {str(key): _encode(item) for key, item in value.items()}}
    # Injected framework objects, such as the calling agent, are recorded by type only.
    return {"type": type(value).__name__, "value": None}


def _received(**kwargs: object) -> str:
    return json.dumps(encode_kwargs(kwargs), sort_keys=True)


def probe_scalars(count: int, ratio: float, enabled: bool, label: str) -> str:
    """Record scalar arguments.

    Args:
        count: A whole number.
        ratio: A decimal number.
        enabled: A flag.
        label: Free text.

    """
    return _received(count=count, ratio=ratio, enabled=enabled, label=label)


def probe_optional(note: str | None = None, limit: int | None = None, flag: bool = False) -> str:
    """Record optional arguments and their defaults.

    Args:
        note: Optional free text.
        limit: Optional whole number.
        flag: A flag that defaults to false.

    """
    return _received(note=note, limit=limit, flag=flag)


def probe_collections(tags: list[str], weights: dict[str, float]) -> str:
    """Record list and mapping arguments.

    Args:
        tags: Text items.
        weights: Decimal numbers by name.

    """
    return _received(tags=tags, weights=weights)


def probe_choice(mode: Literal["fast", "slow"]) -> str:
    """Record a constrained choice.

    Args:
        mode: Either fast or slow.

    """
    return _received(mode=mode)


def probe_model(query_filter: ProbeFilter) -> str:
    """Record a structured argument.

    Args:
        query_filter: The filter to apply.

    """
    return _received(query_filter=query_filter)


def probe_no_arguments() -> str:
    """Record a call without arguments."""
    return _received()


async def probe_async(count: int) -> str:
    """Record an argument of an asynchronous tool.

    Args:
        count: A whole number.

    """
    return _received(count=count)


def probe_agent(query: str, agent: object = None) -> str:
    """Record an argument of a tool that also receives the calling agent.

    Args:
        query: Free text.
        agent: The calling agent, which the framework supplies.

    """
    return _received(query=query, agent=agent)


def probe_raises(message: str) -> str:
    """Fail with the given message.

    Args:
        message: The error message.

    """
    raise ValueError(message)


PROBES = (
    probe_scalars,
    probe_optional,
    probe_collections,
    probe_choice,
    probe_model,
    probe_no_arguments,
    probe_async,
    probe_agent,
    probe_raises,
)
