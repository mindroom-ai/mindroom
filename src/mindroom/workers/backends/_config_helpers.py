"""Shared environment readers for dedicated worker backend configuration."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from mindroom.workers.backend import WorkerBackendError

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "read_bool_env",
    "read_env",
    "read_float_env",
    "read_int_env",
    "read_json_mapping_env",
]


def read_env(env: Mapping[str, str], name: str, default: str = "") -> str:
    """Return one stripped env value, falling back to ``default`` when unset."""
    return env.get(name, default).strip()


def read_float_env(env: Mapping[str, str], name: str, default: float) -> float:
    """Return one float env value clamped to at least 1.0, using ``default`` when unparsable."""
    raw = read_env(env, name, str(default))
    try:
        value = float(raw)
    except ValueError:
        value = default
    return max(1.0, value)


def read_int_env(env: Mapping[str, str], name: str, default: int) -> int:
    """Return one int env value clamped to at least 1, using ``default`` when unparsable."""
    raw = read_env(env, name, str(default))
    try:
        value = int(raw)
    except ValueError:
        value = default
    return max(1, value)


def read_bool_env(env: Mapping[str, str], name: str, *, default: bool = False) -> bool:
    """Return one boolean env value, treating common truthy spellings as ``True``."""
    raw = env.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def read_json_mapping_env(env: Mapping[str, str], name: str) -> dict[str, str]:
    """Parse one JSON-object env value into a string mapping, failing loudly when malformed."""
    raw = read_env(env, name)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"{name} must contain a JSON object."
        raise WorkerBackendError(msg) from exc
    if not isinstance(parsed, dict):
        msg = f"{name} must contain a JSON object."
        raise WorkerBackendError(msg)
    cleaned: dict[str, str] = {}
    for key, value in parsed.items():
        if isinstance(value, str):
            cleaned[key] = value
        elif value is not None:
            cleaned[key] = str(value)
    return cleaned
