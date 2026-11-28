"""The time limit of one host browser call, which the toolkit enforces and every process relaying the call outwaits."""

BROWSER_FUNCTION_NAMES: tuple[str, str] = ("browser_read", "browser_control")
BROWSER_READ_PAGE_ACTIONS: frozenset[str] = frozenset({"screenshot", "snapshot"})

# Playwright bounds neither a page script nor a wait, so the toolkit bounds each host call itself, wherever it runs and
# whether or not it runs as a job, at the limit a worker gives a call by default.
HOST_CALL_TIMEOUT_SECONDS = 120.0
# Time for a call stopped at its limit to finish stopping and for the worker's answer to reach the relaying process.
_RELAY_GRACE_SECONDS = 30.0


class HostCallTimeLimitError(TimeoutError):
    """A host browser call the toolkit stopped at its time limit, which leaves the browser and its other tabs open."""


def browser_request_budget_seconds(tool_name: str, function_name: str) -> float:
    """Return how long a process relaying this call waits for its answer, or 0 for a call without a limit of its own."""
    if tool_name != "browser" or function_name not in BROWSER_FUNCTION_NAMES:
        return 0.0
    return HOST_CALL_TIMEOUT_SECONDS + _RELAY_GRACE_SECONDS
