"""The built-in integration registry: how core plugs each integration into the API, its lifecycle, replies and config."""

from __future__ import annotations

from dataclasses import dataclass
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
    # Returns the public routes, which authenticate their own requests.
    # Config validation reads the registry, so an integration loads its route and runtime on first use, never at import.
    router: Callable[[], APIRouter]
    bind: Callable[[FastAPI, Mapping[str, AgentBot | TeamBot], AgentReplyMembershipIndex], None]
    unbind: Callable[[FastAPI], object | None]
    # Returns the coroutine that passes a final agent reply on, or None when the reply is not for this integration.
    reply_mirror: Callable[[MessageEnvelope, str, Config, RuntimePaths], Coroutine[Any, Any, None] | None]
    validate_config: Callable[[Config], None]
    adapt_created_agent_tools: Callable[[list[Any]], None]


def _integrations() -> tuple[Integration, ...]:
    # Deferred: `slack.integration` imports `Integration` from this module.
    from mindroom.slack.integration import SLACK_INTEGRATION  # noqa: PLC0415

    return (SLACK_INTEGRATION,)


def integration_routers() -> list[APIRouter]:
    """Return every integration's public routes."""
    return [integration.router() for integration in _integrations()]


def bind_integrations(
    api_app: FastAPI,
    bots: Mapping[str, AgentBot | TeamBot],
    agent_reply_memberships: AgentReplyMembershipIndex,
) -> None:
    """Let every integration's events reach the orchestrator's live bots."""
    for integration in _integrations():
        integration.bind(api_app, bots, agent_reply_memberships)


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


def validate_integrations(config: Config) -> None:
    """Run every integration's config checks, which raise `ValueError`."""
    for integration in _integrations():
        integration.validate_config(config)


def adapt_created_agent_tools(tools: list[Any]) -> None:
    """Let every integration edit a created agent's raw tool list in place."""
    for integration in _integrations():
        integration.adapt_created_agent_tools(tools)
