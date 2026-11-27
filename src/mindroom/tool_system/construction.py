"""Immutable evidence of the factory selected for one concrete toolkit."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


def tool_config_signature(overrides: Mapping[str, object] | None) -> str:
    """Digest constructor options without retaining secrets; function filters use current grants."""
    # YAML overrides may hold values JSON cannot encode natively, such as an unquoted date.
    serialized = json.dumps(
        {key: value for key, value in (overrides or {}).items() if key not in {"include_tools", "exclude_tools"}},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(serialized.encode()).hexdigest()


@dataclass(frozen=True)
class ToolConstruction:
    """Concrete registry name and selected factory origin, or an explicit direct constructor."""

    name: str
    factory_origin: tuple[str, str] | None
    config_signature: str = tool_config_signature(None)
