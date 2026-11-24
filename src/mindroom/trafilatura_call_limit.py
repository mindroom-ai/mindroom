"""The time limit of one Trafilatura crawl or batch extract, which the toolkit enforces and every process relaying it outwaits."""

CALL_SECONDS = 180.0
# Time for a call stopped at its limit to extract what it gathered and for the worker's answer to reach the relaying process.
_RELAY_GRACE_SECONDS = 30.0
_TIME_LIMITED_FUNCTIONS = frozenset({"crawl_website", "extract_batch"})


def trafilatura_request_budget_seconds(tool_name: str, function_name: str) -> float:
    """Return how long a process relaying this call waits for its answer, or 0 for a call without a limit of its own."""
    if tool_name != "trafilatura" or function_name not in _TIME_LIMITED_FUNCTIONS:
        return 0.0
    return CALL_SECONDS + _RELAY_GRACE_SECONDS
