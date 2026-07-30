"""Temporary Agno Function performance patch pending agno-agi/agno#9210."""

from functools import lru_cache
from typing import Any, cast

import agno.tools.function as agno_function

_PATCHED_VERSIONS: set[Any] = set()


def apply_patch() -> None:
    """Cache Agno's Pydantic package-version lookup once per process."""
    if agno_function.version in _PATCHED_VERSIONS:
        return
    patched_version = cast("Any", lru_cache(maxsize=1)(agno_function.version))
    _PATCHED_VERSIONS.add(patched_version)
    agno_function.version = patched_version
