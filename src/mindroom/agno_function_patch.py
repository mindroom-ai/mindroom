"""Temporary Agno Function performance patch pending agno-agi/agno#9210."""

from functools import lru_cache
from typing import Any, cast

import agno.tools.function as agno_function


def apply_patch() -> None:
    """Cache Agno's Pydantic package-version lookup once per process."""
    agno_function.version = cast("Any", lru_cache(maxsize=1)(agno_function.version))
