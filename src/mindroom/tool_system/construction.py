"""Immutable evidence of the factory selected for one concrete toolkit."""

from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from agno.tools import Toolkit


def tool_config_signature(overrides: Mapping[str, object] | None) -> str:
    """Digest constructor options without retaining secrets; function filters use current grants."""
    serialized = json.dumps(
        {key: value for key, value in (overrides or {}).items() if key not in {"include_tools", "exclude_tools"}},
        sort_keys=True,
    )
    return hashlib.sha256(serialized.encode()).hexdigest()


@dataclass(frozen=True)
class ToolConstruction:
    """Concrete registry name and selected factory origin, or an explicit direct constructor."""

    name: str
    factory_origin: tuple[str, str] | None
    config_signature: str = tool_config_signature(None)

    @classmethod
    def from_factory(
        cls,
        name: str,
        factory: Callable[[], type[Toolkit]],
        *,
        tool_config_overrides: Mapping[str, object] | None = None,
    ) -> ToolConstruction:
        """Snapshot the selected callable before any factory or constructor can run."""
        origin = (factory.__module__, factory.__qualname__) if inspect.isfunction(factory) else None
        return cls(name, origin, tool_config_signature(tool_config_overrides))


_CONSTRUCTIONS: WeakKeyDictionary[Toolkit, ToolConstruction] = WeakKeyDictionary()


def bind_toolkit_construction(toolkit: Toolkit, construction: ToolConstruction) -> Toolkit:
    """Retain selected construction evidence on the final returned wrapper's lifetime."""
    _CONSTRUCTIONS[toolkit] = construction
    return toolkit


def get_toolkit_construction(toolkit: Toolkit) -> ToolConstruction | None:
    """Read evidence captured by construction without looking at the current registry."""
    return _CONSTRUCTIONS.get(toolkit)
