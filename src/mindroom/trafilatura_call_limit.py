"""The time limit of one Trafilatura crawl or batch extract, which the toolkit enforces and every process relaying it outwaits."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

CALL_SECONDS = 180.0
# Time for a call stopped at its limit to extract what it gathered and for the worker's answer to reach the relaying process.
_RELAY_GRACE_SECONDS = 30.0


def trafilatura_request_budget_seconds(
    tool_name: str,
    function_name: str,
    *,
    args: Sequence[object] = (),
    kwargs: Mapping[str, object] | None = None,
) -> float:
    """Return how long a process relaying this call waits for its answer, or 0 for a call without a limit of its own."""
    if tool_name != "trafilatura":
        return 0.0
    if function_name == "crawl_website":
        return CALL_SECONDS + _RELAY_GRACE_SECONDS
    if function_name == "trafilatura_extract":
        action = kwargs.get("action") if kwargs and "action" in kwargs else (args[0] if args else None)
        if action == "batch":
            return CALL_SECONDS + _RELAY_GRACE_SECONDS
    return 0.0
