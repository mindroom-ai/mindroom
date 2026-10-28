"""Deliver Slack events as Matrix turns: to the project agent answering a channel, then to personal watches."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from mindroom.authorization import ReplyMembershipPendingError, is_sender_allowed_for_agent_reply_in_room
from mindroom.background_tasks import create_background_task
from mindroom.config.slack import slack_answering_agents
from mindroom.logging_config import get_logger
from mindroom.matrix.state import resolve_room_id
from mindroom.response_admission import admitted_response_decision
from mindroom.slack.api import SlackApiError, slack_call, slack_http_client
from mindroom.slack.app import load_slack_app, slack_bot_identity
from mindroom.slack.events import parse_message_event, project_thread_key, project_wake_agent
from mindroom.slack.turns import (
    RETRYABLE_ERRORS,
    SlackAttempt,
    SlackTurn,
    claim_and_send,
    conversation_label,
    event_author,
    failure_reason,
    is_bound,
    project_scope,
    refuse_while_closed,
    retry_later,
    retry_on_failure,
    running_bot,
    skipped,
)
from mindroom.slack.watch_delivery import deliver_to_watches

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Mapping

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.bot import AgentBot, TeamBot
    from mindroom.config.main import Config
    from mindroom.config.slack import SlackAnsweringAgent
    from mindroom.constants import RuntimePaths
    from mindroom.credentials.manager import CredentialsManager
    from mindroom.slack.app import SlackAppCredentials
    from mindroom.slack.turns import DeliveryResult

logger = get_logger(__name__)

_NOTICE_SECONDS = 86_400
# Slack never resends an event the route acknowledged, so a failure after that is retried here.
_RETRY_DELAYS_SECONDS = (5, 30, 120)
_CANT_ANSWER = "MindRoom can't answer you here."
_UNRECEIVED = "MindRoom couldn't receive your message; try again"


@dataclass(frozen=True, eq=False)
class SlackRuntime:
    """The running agents Slack events reach; it also owns every Slack background task, which shutdown drains.

    `bots` is the orchestrator's own mapping, so it always holds the bots running now.
    """

    bots: Mapping[str, AgentBot | TeamBot]
    agent_reply_memberships: AgentReplyMembershipIndex
    http_client: Callable[[], httpx.AsyncClient] = slack_http_client
    # When each (Slack author, thread key) last got a notice; they get at most one a day, remembered in memory only.
    notices: dict[tuple[str, str], int] = field(default_factory=dict)


async def deliver_slack_event(
    payload: Mapping[str, Any],
    *,
    runtime: SlackRuntime,
    config: Config,
    runtime_paths: RuntimePaths,
    credentials_manager: CredentialsManager,
    app: SlackAppCredentials,
    client: httpx.AsyncClient,
    now: int,
    last_attempt: bool = False,
) -> DeliveryResult:
    """Deliver one Events API payload to its project agent, then its watches, and say whether to try again.

    `app` is the Slack app this attempt acts with, loaded once for it.
    `retry` means some target may still receive the message; targets that already did skip it next time.
    On the `last_attempt`, a project wake that still fails tells its author to try again.
    """
    try:
        return await _deliver(
            payload,
            runtime=runtime,
            config=config,
            runtime_paths=runtime_paths,
            credentials_manager=credentials_manager,
            app=app,
            client=client,
            now=now,
            last_attempt=last_attempt,
        )
    except RETRYABLE_ERRORS as exc:
        logger.warning(
            "Slack event delivery failed and will be retried",
            event_id=_event_id(payload),
            reason=failure_reason(exc),
        )
    except Exception:
        logger.exception("Slack event delivery failed unexpectedly and will be retried", event_id=_event_id(payload))
    return "retry"


async def retry_slack_event(
    first_attempt: Awaitable[DeliveryResult],
    payload: Mapping[str, Any],
    *,
    runtime: SlackRuntime,
    current_config: Callable[[], Config | None],
    runtime_paths: RuntimePaths,
    credentials_manager: CredentialsManager,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Retry an event acknowledged before its first attempt finished, after 5, 30 and 120 s, until one succeeds.

    Each retry reads `current_config` and the Slack app again, so a reload that revokes access stops later retries;
    None means no config is committed right now, which skips that retry, and a removed app ends them.
    """
    if await first_attempt != "retry":
        return
    for number, delay in enumerate(_RETRY_DELAYS_SECONDS, start=1):
        await sleep(delay)
        config = current_config()
        if config is None:
            logger.warning("Slack event retry skipped without a committed config", event_id=_event_id(payload))
            continue
        app = await asyncio.to_thread(load_slack_app, credentials_manager)
        if app is None:
            return
        async with runtime.http_client() as client:
            result = await deliver_slack_event(
                payload,
                runtime=runtime,
                config=config,
                runtime_paths=runtime_paths,
                credentials_manager=credentials_manager,
                app=app,
                client=client,
                now=int(time.time()),
                last_attempt=number == len(_RETRY_DELAYS_SECONDS),
            )
        if result != "retry":
            return


async def _deliver(
    payload: Mapping[str, Any],
    *,
    runtime: SlackRuntime,
    config: Config,
    runtime_paths: RuntimePaths,
    credentials_manager: CredentialsManager,
    app: SlackAppCredentials,
    client: httpx.AsyncClient,
    now: int,
    last_attempt: bool,
) -> DeliveryResult:
    control_state_root = runtime_paths.control_state_root
    if control_state_root is None:
        return "ignored"
    # A cold cache asks Slack here, inside the route's timed delivery.
    identity = await slack_bot_identity(client, app)
    if payload.get("team_id") != identity.team_id:
        return "ignored"
    answering = slack_answering_agents(config)
    event = parse_message_event(
        payload,
        bot_user_id=identity.bot_user_id,
        answered_channels=frozenset(channel for agent in answering for channel in agent.channels),
    )
    if event is None:
        return "ignored"
    attempt = SlackAttempt(
        runtime=runtime,
        config=config,
        runtime_paths=runtime_paths,
        credentials_manager=credentials_manager,
        client=client,
        app=app,
        identity=identity,
        control_state_root=control_state_root,
        payload=payload,
        event=event,
        now=now,
        last_attempt=last_attempt,
    )
    project = await _deliver_to_project(attempt, answering)
    return _combined([project, *await deliver_to_watches(attempt)])


def _combined(results: Iterable[DeliveryResult]) -> DeliveryResult:
    outcomes = set(results)
    if "retry" in outcomes:
        return "retry"
    return "delivered" if "delivered" in outcomes else "ignored"


async def _deliver_to_project(attempt: SlackAttempt, answering: tuple[SlackAnsweringAgent, ...]) -> DeliveryResult:
    """Wake the agent answering the event's channel; on the last attempt, a wake that still fails tells its author."""
    event = attempt.event
    agent = project_wake_agent(event, answering)
    if agent is None:
        return "ignored"
    thread_key = project_thread_key(event.team_id, event.channel_id, event.thread_ts or event.ts)
    result = await retry_on_failure(event, _wake_project_agent(attempt, agent, thread_key))
    if result == "retry" and attempt.last_attempt:
        _notify_author(attempt, thread_key, _UNRECEIVED)
    return result


async def _wake_project_agent(attempt: SlackAttempt, answering: SlackAnsweringAgent, thread_key: str) -> DeliveryResult:
    """Post an app mention, or a reply in a thread the agent answers, as a turn of the author's MindRoom account.

    The author must map to a MindRoom account the agent answers; anyone else gets a notice only they see.
    """
    event = attempt.event
    scope = project_scope(answering.agent)
    room_id = resolve_room_id(answering.room, attempt.runtime_paths)
    if not event.is_app_mention and not await is_bound(attempt, scope, thread_key, room_id):
        return "ignored"
    running = running_bot(attempt.runtime, answering.agent)
    if running is None:
        return retry_later(event, "the answering agent is not running")
    author = await event_author(attempt)
    conversation = await conversation_label(attempt)
    bot, client = running
    requester = author.matrix_id
    async with admitted_response_decision(bot.admission_gate, refuse_while_closed):
        try:
            allowed = requester is not None and is_sender_allowed_for_agent_reply_in_room(
                requester,
                answering.agent,
                attempt.config,
                room_id,
                attempt.runtime_paths,
                attempt.runtime.agent_reply_memberships,
                require_resolved_membership=True,
            )
        except ReplyMembershipPendingError:
            # Room membership is not known yet, as just after a start or a join; the decision waits for it.
            return retry_later(event, "membership not resolved")
        if requester is None or not allowed:
            _notify_author(attempt, thread_key, _CANT_ANSWER)
            return skipped(event, "the author may not wake the answering agent")
        turn = SlackTurn(
            scope=scope,
            thread_key=thread_key,
            agent=answering.agent,
            room_id=room_id,
            header=f"Slack message from {author.display_name} in {conversation}: {attempt.permalink}",
            hook_source=f"slack/{event.channel_id}/{event.thread_ts or event.ts}",
            requester=requester,
        )
        result = await claim_and_send(attempt, bot, client, turn)
    if result == "delivered":
        _call_slack_later(attempt, "reactions.add", channel=event.channel_id, timestamp=event.ts, name="eyes")
    return result


def _notify_author(attempt: SlackAttempt, thread_key: str, text: str) -> None:
    """Tell the event's author something only they see in their thread, at most once a day per author and thread."""
    event, notices, now = attempt.event, attempt.runtime.notices, attempt.now
    key = (event.user_id, thread_key)
    sent_at = notices.get(key)
    if sent_at is not None and now - sent_at < _NOTICE_SECONDS:
        return
    for stale in [stale for stale, at in notices.items() if now - at >= _NOTICE_SECONDS]:
        del notices[stale]
    notices[key] = now
    _call_slack_later(
        attempt,
        "chat.postEphemeral",
        channel=event.channel_id,
        user=event.user_id,
        text=text,
        thread_ts=event.thread_ts or event.ts,
    )


def _call_slack_later(attempt: SlackAttempt, method: str, **params: str) -> None:
    """Call Slack with the bot token in the background, after the route answers; a failure is only logged."""
    runtime, event = attempt.runtime, attempt.event

    async def call() -> None:
        try:
            async with runtime.http_client() as client:
                await slack_call(client, method, attempt.app.bot_token, **params)
        except (SlackApiError, httpx.HTTPError) as exc:
            logger.warning(
                "Slack call after a delivery failed",
                method=method,
                event_id=event.event_id,
                channel_id=event.channel_id,
                reason=failure_reason(exc),
            )

    create_background_task(call(), name=f"slack_{method}", owner=runtime)


def _event_id(payload: Mapping[str, Any]) -> str | None:
    event_id = payload.get("event_id")
    return event_id if isinstance(event_id, str) else None
