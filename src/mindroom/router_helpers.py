"""Shared router-runtime helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.constants import ROUTER_AGENT_NAME

if TYPE_CHECKING:
    import nio

    from mindroom.matrix.cache.event_cache import ConversationEventCache
    from mindroom.matrix.conversation_cache import ConversationCacheProtocol
    from mindroom.orchestrator import MultiAgentOrchestrator


@dataclass(frozen=True)
class LiveRouterRuntime:
    """Live router collaborators that must travel together."""

    client: nio.AsyncClient
    conversation_cache: ConversationCacheProtocol
    event_cache: ConversationEventCache


def get_live_router_runtime(orchestrator: MultiAgentOrchestrator | None) -> LiveRouterRuntime | None:
    """Return the live router runtime when the orchestrator has one ready."""
    if orchestrator is None:
        return None

    router_bot = orchestrator.agent_bots.get(ROUTER_AGENT_NAME)
    if router_bot is None or router_bot.client is None:
        return None

    try:
        event_cache = router_bot.event_cache
    except RuntimeError:
        return None

    return LiveRouterRuntime(
        client=router_bot.client,
        conversation_cache=router_bot.conversation_cache,
        event_cache=event_cache,
    )


def get_live_router_client(orchestrator: MultiAgentOrchestrator | None) -> nio.AsyncClient | None:
    """Return the live router client when the orchestrator has one ready."""
    if orchestrator is None:
        return None

    router_bot = orchestrator.agent_bots.get(ROUTER_AGENT_NAME)
    if router_bot is None:
        return None
    return router_bot.client
