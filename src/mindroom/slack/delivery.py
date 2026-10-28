"""Deliver Slack events as Matrix turns: to the project agent answering a channel, then to personal watches."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from mindroom.authorization import is_sender_allowed_for_agent_reply_in_room
from mindroom.background_tasks import create_background_task
from mindroom.config.slack import slack_answering_agents
from mindroom.delivery_replay import DeliveryReplayScopeFullError, DeliveryReplayStoreError
from mindroom.logging_config import get_logger
from mindroom.matrix.state import resolve_room_id
from mindroom.response_admission import ResponseAdmissionRefusedError, admitted_response_decision
from mindroom.slack.api import SlackApiError, slack_call, slack_http_client
from mindroom.slack.app import load_slack_app, slack_bot_identity
from mindroom.slack.events import parse_message_event, project_thread_key, project_wake_agent
from mindroom.slack.turns import (
    SlackAttempt,
    SlackTurn,
    claim_and_send,
    event_author,
    is_bound,
    refuse_while_closed,
    retry_later,
    running_bot,
    skipped,
)
from mindroom.slack.watch_delivery import deliver_to_watches

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Mapping

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.bot import AgentBot, TeamBot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.credentials.manager import CredentialsManager
    from mindroom.slack.turns import DeliveryResult

logger = get_logger(__name__)

_NOTICE_SECONDS = 86_400
# Slack never resends an event the route acknowledged, so a failure after that is retried here.
_RETRY_DELAYS_SECONDS = (5, 30, 120)
_CANT_ANSWER = "MindRoom can't answer you here."
_UNRECEIVED = "MindRoom couldn't receive your message; try again"
# Failures a later attempt may not meet: Slack or the replay store unavailable, or responses not admitted right now.
_RETRYABLE_ERRORS = (
    SlackApiError,
    httpx.HTTPError,
    DeliveryReplayStoreError,
    DeliveryReplayScopeFullError,
    ResponseAdmissionRefusedError,
)


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
    client: httpx.AsyncClient,
    now: int,
    last_attempt: bool = False,
) -> DeliveryResult:
    """Deliver one Events API payload to its project agent, then its watches, and say whether to try again.

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
            client=client,
            now=now,
            last_attempt=last_attempt,
        )
    except _RETRYABLE_ERRORS as exc:
        logger.warning(
            "Slack event delivery failed and will be retried",
            event_id=_event_id(payload),
            reason=_reason(exc),
        )
    except Exception:
        logger.exception("Slack event delivery failed unexpectedly and will be retried", event_id=_event_id(payload))
    return "retry"


async def retry_slack_event(
    first_attempt: Awaitable[DeliveryResult],
    payload: Mapping[str, Any],
    *,
    runtime: SlackRuntime,
    config: Config,
    runtime_paths: RuntimePaths,
    credentials_manager: CredentialsManager,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Retry an event acknowledged before its first attempt finished, after 5, 30 and 120 s, until one succeeds."""
    if await first_attempt != "retry":
        return
    for number, delay in enumerate(_RETRY_DELAYS_SECONDS, start=1):
        await sleep(delay)
        async with runtime.http_client() as client:
            result = await deliver_slack_event(
                payload,
                runtime=runtime,
                config=config,
                runtime_paths=runtime_paths,
                credentials_manager=credentials_manager,
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
    client: httpx.AsyncClient,
    now: int,
    last_attempt: bool,
) -> DeliveryResult:
    app = await asyncio.to_thread(load_slack_app, credentials_manager)
    control_state_root = runtime_paths.control_state_root
    if app is None or control_state_root is None:
        return "ignored"
    # A cold cache asks Slack here, inside the route's timed delivery.
    identity = await slack_bot_identity(client, app)
    if payload.get("team_id") != identity.team_id:
        return "ignored"
    event = parse_message_event(payload, bot_user_id=identity.bot_user_id)
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
    project = await _deliver_to_project(attempt)
    return _combined([project, *await deliver_to_watches(attempt)])


def _combined(results: Iterable[DeliveryResult]) -> DeliveryResult:
    outcomes = set(results)
    if "retry" in outcomes:
        return "retry"
    return "delivered" if "delivered" in outcomes else "ignored"


async def _deliver_to_project(attempt: SlackAttempt) -> DeliveryResult:
    """Wake the agent answering the event's channel for an app mention, or for a reply in a thread it answers.

    The author must map to a MindRoom account the agent answers; anyone else gets a notice only they see.
    """
    event = attempt.event
    answering = project_wake_agent(event, slack_answering_agents(attempt.config), bound=True)
    if answering is None:
        return "ignored"
    scope = f"slack:agent:{answering.agent}"
    thread_key = project_thread_key(event)
    room_id = resolve_room_id(answering.room, attempt.runtime_paths)
    if not event.is_app_mention and not await is_bound(attempt, scope, thread_key, room_id):
        return "ignored"
    running = running_bot(attempt.runtime, answering.agent)
    if running is None:
        return retry_later(event, "the answering agent is not running")
    author = await event_author(attempt)
    bot, client = running
    async with admitted_response_decision(bot.admission_gate, refuse_while_closed):
        if author.matrix_id is None or not is_sender_allowed_for_agent_reply_in_room(
            author.matrix_id,
            answering.agent,
            attempt.config,
            room_id,
            attempt.runtime_paths,
            attempt.runtime.agent_reply_memberships,
        ):
            _notify_author(attempt, thread_key, _CANT_ANSWER)
            return skipped(event, "the author may not wake the answering agent")
        turn = SlackTurn(
            scope=scope,
            thread_key=thread_key,
            agent=answering.agent,
            room_id=room_id,
            header=f"Slack message from {author.display_name} in <#{event.channel_id}>: {attempt.permalink}",
            hook_source=f"slack/{event.channel_id}/{event.thread_ts or event.ts}",
            requester=author.matrix_id,
        )
        result = await claim_and_send(attempt, bot, client, turn)
    if result == "delivered":
        _call_slack_later(attempt, "reactions.add", channel=event.channel_id, timestamp=event.ts, name="eyes")
    elif result == "retry" and attempt.last_attempt:
        _notify_author(attempt, thread_key, _UNRECEIVED)
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
                reason=_reason(exc),
            )

    create_background_task(call(), name=f"slack_{method}", owner=runtime)


def _event_id(payload: Mapping[str, Any]) -> str | None:
    event_id = payload.get("event_id")
    return event_id if isinstance(event_id, str) else None


def _reason(exc: Exception) -> str:
    return exc.error if isinstance(exc, SlackApiError) else type(exc).__name__
