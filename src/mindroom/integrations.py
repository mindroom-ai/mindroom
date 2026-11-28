"""The built-in integration registry: how core plugs each integration into the API, its lifecycle, and replies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.slack.integration import bind_slack, mirror_slack_reply, unbind_slack

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Mapping

    from fastapi import APIRouter, FastAPI

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.bot import AgentBot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.hooks import MessageEnvelope
    from mindroom.reactive_triggers.service import ReactiveTriggerService


@dataclass(frozen=True)
class _Integration:
    """The hooks one built-in integration plugs into core with."""

    name: str
    # Returns the public routes, which authenticate their own requests; they load on first use, never at import.
    router: Callable[[], APIRouter]
    bind: Callable[[FastAPI, Mapping[str, AgentBot], AgentReplyMembershipIndex, ReactiveTriggerService | None], None]
    unbind: Callable[[FastAPI], object | None]
    # Returns the coroutine that passes a final agent reply on, or None when the reply is not for this integration.
    reply_mirror: Callable[[MessageEnvelope, str, Config, RuntimePaths], Coroutine[Any, Any, None] | None]


def _slack_events_router() -> APIRouter:
    # why-lazy: the route module loads FastAPI and the Slack delivery stack, which importing the registry must not.
    from mindroom.api.slack_events import router  # noqa: PLC0415

    return router


_SLACK_INTEGRATION = _Integration(
    name="slack",
    router=_slack_events_router,
    bind=bind_slack,
    unbind=unbind_slack,
    reply_mirror=mirror_slack_reply,
)


def _integrations() -> tuple[_Integration, ...]:
    return (_SLACK_INTEGRATION,)


def integration_routers() -> list[APIRouter]:
    """Return every integration's public routes."""
    return [integration.router() for integration in _integrations()]


def bind_integrations(
    api_app: FastAPI,
    bots: Mapping[str, AgentBot],
    agent_reply_memberships: AgentReplyMembershipIndex,
    reactive_triggers: ReactiveTriggerService | None = None,
) -> None:
    """Let every integration's events reach the orchestrator's live bots."""
    for integration in _integrations():
        integration.bind(api_app, bots, agent_reply_memberships, reactive_triggers)


def unbind_integrations(api_app: FastAPI) -> list[tuple[str, object]]:
    """Refuse every integration's events from now on and return each unbound runtime by name, for shutdown to drain."""
    return [
        (integration.name, runtime)
        for integration in _integrations()
        if (runtime := integration.unbind(api_app)) is not None
    ]


def reply_mirror_for(
    envelope: MessageEnvelope,
    response_text: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> Coroutine[Any, Any, None] | None:
    """Return the first integration's coroutine that passes a final agent reply on, or None when no integration takes it."""
    for integration in _integrations():
        if (mirror := integration.reply_mirror(envelope, response_text, config, runtime_paths)) is not None:
            return mirror
    return None
