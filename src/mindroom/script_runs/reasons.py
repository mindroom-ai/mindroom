"""Canonical durable reasons for background-script lifecycle changes."""

AGENT_ISOLATION_CHANGED = "Agent isolation changed during configuration reload."
WORKER_CONFIGURATION_CHANGED = "Worker configuration changed during configuration reload."
RUNTIME_SHUTDOWN = "MindRoom runtime shut down."
RUNTIME_RESTARTED = "MindRoom runtime restarted."
OWNER_AGENT_REMOVED = "Owning agent was removed by configuration reload."
SCRIPT_TOOL_REMOVED = "Background script tool was removed by configuration reload."
OWNER_AUTHORIZATION_REVOKED = "Script owner no longer has room-and-agent reply authorization."
PLUGIN_TOOLS_CHANGED = "Plugin tools changed during configuration reload."
SUPERVISOR_UNAVAILABLE = "Background script supervisor handle is unavailable."
AMBIGUOUS_LAUNCH = "Background script launch outcome is indeterminate."
MAX_RUNTIME_EXCEEDED = "Background script maximum runtime exceeded."
PROCESS_EXIT_OBSERVED = "Background script process exited."

INTERRUPTION_REASONS = frozenset(
    {
        AGENT_ISOLATION_CHANGED,
        WORKER_CONFIGURATION_CHANGED,
        RUNTIME_SHUTDOWN,
        RUNTIME_RESTARTED,
        OWNER_AGENT_REMOVED,
        SCRIPT_TOOL_REMOVED,
        OWNER_AUTHORIZATION_REVOKED,
        PLUGIN_TOOLS_CHANGED,
        SUPERVISOR_UNAVAILABLE,
        AMBIGUOUS_LAUNCH,
        MAX_RUNTIME_EXCEEDED,
    },
)

__all__ = [
    "AGENT_ISOLATION_CHANGED",
    "AMBIGUOUS_LAUNCH",
    "INTERRUPTION_REASONS",
    "MAX_RUNTIME_EXCEEDED",
    "OWNER_AGENT_REMOVED",
    "OWNER_AUTHORIZATION_REVOKED",
    "PLUGIN_TOOLS_CHANGED",
    "PROCESS_EXIT_OBSERVED",
    "RUNTIME_RESTARTED",
    "RUNTIME_SHUTDOWN",
    "SCRIPT_TOOL_REMOVED",
    "SUPERVISOR_UNAVAILABLE",
    "WORKER_CONFIGURATION_CHANGED",
]
