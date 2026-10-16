"""What an exploration turn may do: only the operator's allowlisted tools, no approval pauses, and a call cap.

The trusted hook source of the exploration prompt marks the turn, so a person replying in the same thread runs with
the agent's ordinary tools.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mindroom.config.main import Config

EXPLORATION_HOOK_SOURCE = "automation/exploration"
# Threads whose call counts are kept; exploration runs one thread at a time per agent, so a few suffice.
_MAX_TRACKED_THREADS = 256
_calls: OrderedDict[tuple[str, str], int] = OrderedDict()
_lock = threading.Lock()


@dataclass(frozen=True)
class ExplorationPolicy:
    """The functions an exploration turn may call and the most calls its thread may make."""

    tools: tuple[str, ...]
    max_tool_calls: int


# What a turn started by an exploration prompt may do when its entity has no exploration settings, for example after
# the entry was removed while the prompt waited, or for another entity the prompt reached: nothing.
_CLOSED = ExplorationPolicy(tools=("knowledge_graph",), max_tool_calls=0)


def exploration_policy_for_turn(config: Config, agent_name: str, hook_source: str | None) -> ExplorationPolicy | None:
    """Return the restrictions for a turn the exploration prompt started, or None for any other turn."""
    if hook_source != EXPLORATION_HOOK_SOURCE:
        return None
    entry = config.agent_exploration(agent_name) if agent_name in config.agents else None
    if entry is None:
        return _CLOSED
    # The graph lookups let the agent check what it already knows before exploring.
    return ExplorationPolicy(tools=(*entry.tools, "knowledge_graph"), max_tool_calls=entry.max_tool_calls)


def claim_exploration_call(agent_name: str, thread_key: str, cap: int) -> bool:
    """Count one tool call of an exploration thread and return whether it is within ``cap``.

    Agno counts tool calls per response loop, and a continuation starts a new loop, so the thread keeps its own count.
    """
    key = (agent_name, thread_key)
    with _lock:
        used = _calls.pop(key, 0)
        _calls[key] = used + 1
        while len(_calls) > _MAX_TRACKED_THREADS:
            _calls.popitem(last=False)
    return used < cap
