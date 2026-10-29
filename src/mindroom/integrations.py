"""Built-in integrations: how each one plugs into the API, the orchestrator's lifecycle, final replies and config.

Those core owners reach an integration only through the helpers here, which call every registered integration in order.
Config validation runs them too, so an integration keeps its import light and loads its runtime on first use.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Mapping

    from fastapi import APIRouter, FastAPI

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.bot import AgentBot, TeamBot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.hooks import MessageEnvelope


@dataclass(frozen=True)
class Integration:
    """The hooks one built-in integration plugs into core with."""

    name: str
    # Returns the public routes, which authenticate their own requests; a function, so config never loads FastAPI.
    router: Callable[[], APIRouter] | None = None
    # Lets the integration's events reach the orchestrator's live bots.
    bind: Callable[[FastAPI, Mapping[str, AgentBot | TeamBot], AgentReplyMembershipIndex], None] | None = None
    # Refuses the integration's events from now on and returns the runtime that owns its running deliveries, if bound.
    unbind: Callable[[FastAPI], object | None] | None = None
    # Returns the coroutine that passes a final agent reply on, or None when the reply is not for this integration.
    reply_mirror: Callable[[MessageEnvelope, str, Config, RuntimePaths], Coroutine[Any, Any, None] | None] | None = None
    validate_config: Callable[[Config], None] | None = None
    # Edits a created agent's raw tool list in place.
    adapt_created_agent_tools: Callable[[list[Any]], None] | None = None


@cache
def _integrations() -> tuple[Integration, ...]:
    # Deferred: each integration imports `Integration` from this module.
    from mindroom.slack.integration import slack_integration  # noqa: PLC0415

    return (slack_integration(),)


def integration_routers() -> list[APIRouter]:
    """Return every integration's public routes."""
    return [integration.router() for integration in _integrations() if integration.router is not None]


def bind_integrations(
    api_app: FastAPI,
    bots: Mapping[str, AgentBot | TeamBot],
    agent_reply_memberships: AgentReplyMembershipIndex,
) -> None:
    """Let every integration's events reach the orchestrator's live bots."""
    for integration in _integrations():
        if integration.bind is not None:
            integration.bind(api_app, bots, agent_reply_memberships)


def unbind_integrations(api_app: FastAPI) -> list[tuple[str, object]]:
    """Refuse every integration's events from now on and return each unbound runtime by name, for shutdown to drain."""
    return [
        (integration.name, runtime)
        for integration in _integrations()
        if integration.unbind is not None and (runtime := integration.unbind(api_app)) is not None
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
        if integration.reply_mirror is not None:
            mirror = integration.reply_mirror(envelope, response_text, config, runtime_paths)
            if mirror is not None:
                return mirror
    return None


def validate_integrations(config: Config) -> None:
    """Run every integration's config checks, which raise `ValueError`."""
    for integration in _integrations():
        if integration.validate_config is not None:
            integration.validate_config(config)


def adapt_created_agent_tools(tools: list[Any]) -> None:
    """Let every integration edit a created agent's raw tool list in place."""
    for integration in _integrations():
        if integration.adapt_created_agent_tools is not None:
            integration.adapt_created_agent_tools(tools)
