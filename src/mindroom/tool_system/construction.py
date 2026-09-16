"""Immutable evidence of the factory selected for one concrete toolkit."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary

if TYPE_CHECKING:
    from collections.abc import Callable

    from agno.tools import Toolkit


@dataclass(frozen=True)
class ToolConstruction:
    """Concrete registry name and selected factory origin, or an explicit direct constructor."""

    name: str
    factory_origin: tuple[str, str] | None

    @classmethod
    def from_factory(cls, name: str, factory: Callable[[], type[Toolkit]]) -> ToolConstruction:
        """Snapshot the selected callable before any factory or constructor can run."""
        origin = (factory.__module__, factory.__qualname__) if inspect.isfunction(factory) else None
        return cls(name, origin)


_CONSTRUCTIONS: WeakKeyDictionary[Toolkit, ToolConstruction] = WeakKeyDictionary()


def bind_toolkit_construction(toolkit: Toolkit, construction: ToolConstruction) -> Toolkit:
    """Retain selected construction evidence on the final returned wrapper's lifetime."""
    _CONSTRUCTIONS[toolkit] = construction
    return toolkit


def get_toolkit_construction(toolkit: Toolkit) -> ToolConstruction | None:
    """Read evidence captured by construction without looking at the current registry."""
    return _CONSTRUCTIONS.get(toolkit)
