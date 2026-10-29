"""Slack's entry in the integration registry."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mindroom.config.slack import drop_slack_room, validate_slack_agents
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.integrations import Integration

if TYPE_CHECKING:
    from collections.abc import Coroutine, Mapping

    from fastapi import APIRouter, FastAPI

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.bot import AgentBot, TeamBot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.hooks import MessageEnvelope


def _events_router() -> APIRouter:
    from mindroom.api.slack_events import router  # noqa: PLC0415

    return router


def _bind(
    api_app: FastAPI,
    bots: Mapping[str, AgentBot | TeamBot],
    agent_reply_memberships: AgentReplyMembershipIndex,
) -> None:
    from mindroom.api.slack_events import bind_slack_runtime  # noqa: PLC0415

    bind_slack_runtime(api_app, bots, agent_reply_memberships)


def _unbind(api_app: FastAPI) -> object | None:
    from mindroom.api.slack_events import unbind_slack_runtime  # noqa: PLC0415

    return unbind_slack_runtime(api_app)


def _mirror_reply(
    envelope: MessageEnvelope,
    response_text: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> Coroutine[Any, Any, None] | None:
    """Return the coroutine that posts a Slack project turn's final reply to its Slack thread, or None for other turns."""
    from mindroom.slack.api import slack_http_client  # noqa: PLC0415
    from mindroom.slack.reply_mirror import is_slack_project_turn, mirror_reply_to_slack  # noqa: PLC0415

    if not is_slack_project_turn(envelope):
        return None
    return mirror_reply_to_slack(
        envelope,
        response_text,
        config=config,
        runtime_paths=runtime_paths,
        credentials_manager=get_runtime_credentials_manager(runtime_paths),
        client_factory=slack_http_client,
    )


SLACK_INTEGRATION = Integration(
    name="slack",
    router=_events_router,
    bind=_bind,
    unbind=_unbind,
    reply_mirror=_mirror_reply,
    validate_config=validate_slack_agents,
    # A created agent has no rooms, so it keeps the creator's Slack channels to read but never answers mentions.
    adapt_created_agent_tools=drop_slack_room,
)
