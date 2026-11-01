"""The time limit of one host browser call, which the toolkit enforces and every process relaying the call outwaits."""

# Playwright bounds neither a page script nor a wait, so the toolkit bounds each host call itself, wherever it runs and
# whether or not it runs as a job, at the limit a worker gives a call by default.
HOST_CALL_TIMEOUT_SECONDS = 120.0
# Time for a worker to answer a call stopped at its limit, which closes the worker's browser first.
_RELAY_GRACE_SECONDS = 30.0


def browser_request_budget_seconds(tool_name: str, function_name: str) -> float:
    """Return how long a process relaying this call waits for its answer, or 0 for a call without a limit of its own."""
    if (tool_name, function_name) != ("browser", "browser_control"):
        return 0.0
    return HOST_CALL_TIMEOUT_SECONDS + _RELAY_GRACE_SECONDS
