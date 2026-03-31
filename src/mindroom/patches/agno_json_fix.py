"""Monkey-patch for Agno's double-encoded JSON bug in SQLite session storage.

Agno's ``serialize_session_json_fields`` pre-serializes JSON fields with
``json.dumps()`` before passing them to SQLAlchemy ``JSON`` columns, which
apply ``json.dumps()`` again -- double-encoding.  On read, only one layer
is peeled by ``deserialize_session_json_fields``, leaving a string where
``AgentSession.from_dict()`` expects a list/dict.

The fix:
  * **serialize** -- pass native Python objects through; normalize only
    ``runs`` and ``summary`` via ``CustomJSONEncoder`` to convert UUIDs,
    datetimes, Message, and Metrics to JSON-safe primitives.
  * **deserialize** -- after the normal ``json.loads()`` pass, apply a
    second ``json.loads()`` if the result is still a ``str`` (legacy
    double-encoded data).
"""

import contextlib
import importlib
import json
import logging
from collections.abc import Callable
from types import ModuleType
from typing import Protocol, cast

import agno.db.utils as _agno_db_utils
from agno.db.utils import CustomJSONEncoder

logger = logging.getLogger(__name__)

# Fields that use CustomJSONEncoder and may contain UUID / datetime / Message / Metrics.
_CUSTOM_ENCODER_FIELDS = ("runs", "summary")

# All JSON fields handled by the original serialize/deserialize functions.
_ALL_JSON_FIELDS = (
    "session_data",
    "agent_data",
    "team_data",
    "workflow_data",
    "metadata",
    "chat_history",
    "summary",
    "runs",
)


class _SessionJsonPatchTarget(Protocol):
    serialize_session_json_fields: Callable[[dict], dict]
    deserialize_session_json_fields: Callable[[dict], dict]


def _patched_serialize(session: dict) -> dict:
    """Serialize session JSON fields *without* calling json.dumps.

    SQLAlchemy's ``JSON`` column handles final serialization.  We only
    normalize ``runs`` and ``summary`` through ``CustomJSONEncoder`` so
    that UUID, datetime, Message, and Metrics objects become plain
    JSON-safe primitives.
    """
    for field in _CUSTOM_ENCODER_FIELDS:
        value = session.get(field)
        if value is not None:
            # Round-trip through CustomJSONEncoder to normalize special types,
            # but return native Python objects (not a JSON string).
            session[field] = json.loads(json.dumps(value, cls=CustomJSONEncoder))
    # All other JSON fields are already JSON-serializable; pass through as-is.
    return session


def _patched_deserialize(session: dict) -> dict:
    """Deserialize session JSON fields, tolerating double-encoded legacy data.

    After SQLAlchemy's ``JSON`` column deserializes once:
      * Correctly-encoded data is already a native object -- skip.
      * Legacy double-encoded data is a ``str`` -- apply ``json.loads()``.
      * If *still* a ``str`` after one ``json.loads()`` (triple-encoded
        edge case), apply one more round.
    """
    for field in _ALL_JSON_FIELDS:
        value = session.get(field)
        if value is None:
            continue
        # Peel string layers until we get a native object (or give up).
        for _ in range(2):
            if not isinstance(value, str):
                break
            try:
                value = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                break
        session[field] = value
    return session


def _patch_module(mod: ModuleType) -> None:
    """Replace serialize/deserialize on a single module."""
    patched_module = cast("_SessionJsonPatchTarget", mod)
    patched_module.serialize_session_json_fields = _patched_serialize
    patched_module.deserialize_session_json_fields = _patched_deserialize


def apply() -> None:
    """Replace the upstream functions on the ``agno.db.utils`` module."""
    _patch_module(_agno_db_utils)

    # The SQLite modules import the names directly, so patch those too.
    for modname in ("agno.db.sqlite.sqlite", "agno.db.sqlite.async_sqlite"):
        with contextlib.suppress(ImportError):
            _patch_module(importlib.import_module(modname))

    logger.debug("agno_json_fix: monkey-patch applied")


apply()
