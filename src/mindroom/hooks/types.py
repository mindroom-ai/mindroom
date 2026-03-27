"""Types and constants for the MindRoom hook system."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable


EVENT_MESSAGE_RECEIVED = "message:received"
EVENT_MESSAGE_ENRICH = "message:enrich"
EVENT_MESSAGE_BEFORE_RESPONSE = "message:before_response"
EVENT_MESSAGE_AFTER_RESPONSE = "message:after_response"
EVENT_AGENT_STARTED = "agent:started"
EVENT_AGENT_STOPPED = "agent:stopped"
EVENT_SCHEDULE_FIRED = "schedule:fired"
EVENT_REACTION_RECEIVED = "reaction:received"
EVENT_CONFIG_RELOADED = "config:reloaded"
EVENT_TOOL_BEFORE_CALL = "tool:before_call"
EVENT_TOOL_AFTER_CALL = "tool:after_call"

BUILTIN_EVENT_NAMES = frozenset(
    {
        EVENT_MESSAGE_RECEIVED,
        EVENT_MESSAGE_ENRICH,
        EVENT_MESSAGE_BEFORE_RESPONSE,
        EVENT_MESSAGE_AFTER_RESPONSE,
        EVENT_AGENT_STARTED,
        EVENT_AGENT_STOPPED,
        EVENT_SCHEDULE_FIRED,
        EVENT_REACTION_RECEIVED,
        EVENT_CONFIG_RELOADED,
        EVENT_TOOL_BEFORE_CALL,
        EVENT_TOOL_AFTER_CALL,
    },
)
RESERVED_EVENT_NAMESPACES = frozenset({"message", "agent", "schedule", "reaction", "config", "tool"})
EVENT_NAME_PATTERN = re.compile(r"^[a-z0-9_.-]+(:[a-z0-9_.-]+)+$")
DEFAULT_EVENT_TIMEOUT_MS: dict[str, int] = {
    EVENT_MESSAGE_RECEIVED: 100,
    EVENT_MESSAGE_ENRICH: 2000,
    EVENT_MESSAGE_BEFORE_RESPONSE: 200,
    EVENT_MESSAGE_AFTER_RESPONSE: 3000,
    EVENT_REACTION_RECEIVED: 500,
    EVENT_SCHEDULE_FIRED: 1000,
    EVENT_AGENT_STARTED: 5000,
    EVENT_AGENT_STOPPED: 5000,
    EVENT_CONFIG_RELOADED: 5000,
    EVENT_TOOL_BEFORE_CALL: 200,
    EVENT_TOOL_AFTER_CALL: 300,
}
DEFAULT_CUSTOM_EVENT_TIMEOUT_MS = 1000

EnrichmentCachePolicy = Literal["stable", "volatile"]


class HookCallback(Protocol):
    """Async callback protocol implemented by hook functions."""

    def __call__(self, ctx: object) -> Awaitable[object | None]:
        """Run the hook callback."""


@dataclass(frozen=True, slots=True)
class EnrichmentItem:
    """One structured enrichment entry rendered into the model-facing prompt."""

    key: str
    text: str
    cache_policy: EnrichmentCachePolicy = "volatile"


@dataclass(frozen=True, slots=True)
class RegisteredHook:
    """One compiled hook entry in the immutable registry snapshot."""

    plugin_name: str
    hook_name: str
    event_name: str
    priority: int
    timeout_ms: int | None
    callback: HookCallback
    settings: dict[str, Any]
    plugin_order: int
    source_lineno: int
    agents: tuple[str, ...] | None
    rooms: tuple[str, ...] | None


def is_custom_event_name(event_name: str) -> bool:
    """Return whether *event_name* is outside the built-in event set."""
    return event_name not in BUILTIN_EVENT_NAMES


def default_timeout_ms_for_event(event_name: str) -> int:
    """Return the default timeout for one event name."""
    return DEFAULT_EVENT_TIMEOUT_MS.get(event_name, DEFAULT_CUSTOM_EVENT_TIMEOUT_MS)


def validate_event_name(event_name: str) -> str:
    """Validate one hook event name and return the normalized value."""
    normalized = event_name.strip()
    if normalized in BUILTIN_EVENT_NAMES:
        return normalized
    if not EVENT_NAME_PATTERN.fullmatch(normalized):
        msg = f"Invalid hook event name: {event_name!r}"
        raise ValueError(msg)

    namespace = normalized.split(":", 1)[0]
    if namespace in RESERVED_EVENT_NAMESPACES:
        msg = f"Custom hook event uses reserved namespace: {event_name!r}"
        raise ValueError(msg)
    return normalized
