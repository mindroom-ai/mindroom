"""Depth-safe copies of JSON trees received from other users."""

from __future__ import annotations

import json
from typing import Any


def copy_json(value: Any) -> Any:
    """Copy a JSON tree through the C codec.

    Event trees come from other users and can nest deeper than deepcopy's
    Python-level recursion allows, which would raise RecursionError where
    one event must not stop sync.
    """
    return json.loads(json.dumps(value))
