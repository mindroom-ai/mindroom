"""Slack's entry in the integration registry."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from mindroom.config.slack import drop_slack_room, validate_slack_agents
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.integrations import Integration
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Coroutine, Mapping

    from fastapi import APIRouter, FastAPI

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.bot import AgentBot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.event_sources import EventSourceEmitter
    from mindroom.hooks import MessageEnvelope
    from mindroom.reactive_triggers.service import ReactiveTriggerService
    from mindroom.slack.delivery import SlackRuntime
    from mindroom.slack.event_source import SlackEventSource

_NAME = "slack"
logger = get_logger(__name__)


def bound_slack_runtime(api_app: FastAPI) -> SlackRuntime | None:
    """Return the runtime Slack events reach, or None while it is unbound."""
    return cast("SlackRuntime | None", _runtimes(api_app).get(_NAME))


def _runtimes(api_app: FastAPI) -> dict[str, object]:
    from mindroom.api import config_lifecycle  # noqa: PLC0415

    return config_lifecycle.app_state(api_app).integration_runtimes


def _events_router() -> APIRouter:
    from mindroom.api.slack_events import router  # noqa: PLC0415

    return router


def _unregister_slack_event_source(runtime: SlackRuntime | None) -> None:
    if runtime is None or runtime.reactive_triggers is None or runtime.event_source_emitter is None:
        return
    emitter = runtime.event_source_emitter
    runtime.reactive_triggers.event_sources.unregister(emitter.source_id, emitter.token)


def _bind(
    api_app: FastAPI,
    bots: Mapping[str, AgentBot],
    agent_reply_memberships: AgentReplyMembershipIndex,
    reactive_triggers: ReactiveTriggerService | None = None,
) -> None:
    """Let Slack events reach live bots, migrate legacy watches once, and register SlackEventSource."""
    from mindroom.slack.delivery import SlackRuntime  # noqa: PLC0415
    from mindroom.slack.event_source import SlackEventSource  # noqa: PLC0415
    from mindroom.slack.legacy_watch_migration import migrate_legacy_slack_watches  # noqa: PLC0415

    runtime = bound_slack_runtime(api_app)
    if runtime is not None and runtime.bots is bots and runtime.reactive_triggers is reactive_triggers:
        return
    _unregister_slack_event_source(runtime)

    event_source: SlackEventSource | None = None
    emitter: EventSourceEmitter | None = None
    if reactive_triggers is not None:
        slack_source = SlackEventSource()
        try:
            emitter = reactive_triggers.event_sources.register(slack_source)
        except ValueError as exc:
            logger.warning("Could not register built-in Slack EventSource", reason=str(exc))
        else:
            event_source = slack_source
            if (store := reactive_triggers.store) is not None:

                def _migrate() -> int:
                    return migrate_legacy_slack_watches(
                        reactive_triggers.runtime_paths,
                        store,
                        reactive_triggers.config_provider(),
                    )

                try:
                    _migrate()
                except Exception:
                    slack_source.retry_migration = _migrate
                    logger.exception("Failed to migrate legacy Slack watches.json")

    _runtimes(api_app)[_NAME] = SlackRuntime(
        bots,
        agent_reply_memberships,
        reactive_triggers=reactive_triggers,
        event_source=event_source,
        event_source_emitter=emitter,
    )


def _unbind(api_app: FastAPI) -> object | None:
    """Refuse Slack events, unregister SlackEventSource, and return the runtime that owns running deliveries."""
    runtime = cast("SlackRuntime | None", _runtimes(api_app).pop(_NAME, None))
    _unregister_slack_event_source(runtime)
    return runtime


def _mirror_reply(
    envelope: MessageEnvelope,
    response_text: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> Coroutine[Any, Any, None] | None:
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
    name=_NAME,
    router=_events_router,
    bind=_bind,
    unbind=_unbind,
    reply_mirror=_mirror_reply,
    validate_config=validate_slack_agents,
    # A created agent has no rooms, so it keeps the creator's Slack channels to read but never answers mentions.
    adapt_created_agent_tools=drop_slack_room,
)
