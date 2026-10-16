"""Which conversation the knowledge graph's extraction calls are charged to."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


@dataclass(frozen=True)
class UsageTarget:
    """The conversation an ingested episode came from, which its extraction tokens are charged to."""

    agent_name: str
    session_id: str
    execution_identity: ToolExecutionIdentity | None


_usage_target: ContextVar[UsageTarget | None] = ContextVar("knowledge_graph_usage_target", default=None)


@contextmanager
def usage_target(target: UsageTarget) -> Iterator[None]:
    """Charge the extraction calls made inside this block to ``target``."""
    token = _usage_target.set(target)
    try:
        yield
    finally:
        _usage_target.reset(token)


def current_usage_target() -> UsageTarget | None:
    """Return the conversation the current extraction call is charged to, if any."""
    return _usage_target.get()
