"""The ``call_tool`` wrapper of portable tool search: its name and the argument parsing both of its ends share.

The request projection (``mindroom.providers.pipeline.deferred_tools``) shows stored calls to deferred functions
as ``call_tool`` calls, and the run loop (``mindroom.agent_loop.tool_search``) rewrites the model's ``call_tool``
calls to the functions they name.
"""

from __future__ import annotations

import ast
import json
from typing import Any, Final, cast

CALL_TOOL_NAME: Final = "call_tool"


def arguments_object(value: object) -> dict[str, Any] | None:
    """Return call arguments parsed like the tool executor parses them when they are a JSON-serializable object.

    Model output can be malformed in many ways (unhashable literals, sets, deep nesting), so any failure returns None,
    "not an object", rather than raising an error that would fail the run.
    """
    try:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                # Agno accepted Python-literal arguments on direct calls, so stored and incoming calls may hold them.
                value = ast.literal_eval(value)
        if value == []:
            # A call whose arguments are an empty list runs as a call without arguments.
            return {}
        if not isinstance(value, dict):
            return None
        json.dumps(value)
    except Exception:  # Any failure means undecodable, as the tool executor treats arguments.
        return None
    return cast("dict[str, Any]", value)


__all__ = ["CALL_TOOL_NAME", "arguments_object"]
