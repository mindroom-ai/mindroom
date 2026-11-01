"""The Matrix turn a Slack message becomes for one target: claimed once, then posted as the agent's own hook dispatch.

Every target claims a message by `channel:ts` in its own replay scope, so the `app_mention` and `message` events of one
mention, and Slack's retries, reach each target once.
"""

from __future__ import annotations

import asyncio
import html
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import httpx

from mindroom.constants import HOOK_SOURCE_KEY, ORIGINAL_SENDER_KEY, PER_FIRE_THREAD_ROOT_KEY, SOURCE_KIND_KEY
from mindroom.delivery_replay import (
    DeliveryEventClaim,
    DeliveryReplayScopeFullError,
    DeliveryReplayStore,
    DeliveryReplayStoreError,
    DeliveryThreadKeyClaim,
)
from mindroom.dispatch_source import HOOK_DISPATCH_SOURCE_KIND
from mindroom.hooks.sender import send_matrix_message
from mindroom.logging_config import get_logger
from mindroom.matrix.mentions import format_entity_mention
from mindroom.matrix.message_builder import build_message_content, markdown_to_html
from mindroom.response_admission import ResponseAdmissionRefusedError
from mindroom.slack.api import SlackApiError, slack_permalink
from mindroom.slack.identity import slack_author, slack_channel
from mindroom.slack.render import slack_plain_text

if TYPE_CHECKING:
    from collections.abc import Awaitable, Mapping
    from pathlib import Path

    import nio

    from mindroom.bot import AgentBot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.credentials.manager import CredentialsManager
    from mindroom.slack.app import SlackAppCredentials, SlackBotIdentity
    from mindroom.slack.delivery import SlackRuntime
    from mindroom.slack.events import SlackMessageEvent
    from mindroom.slack.identity import SlackAuthor

logger = get_logger(__name__)

type DeliveryResult = Literal["delivered", "ignored", "retry"]

# A claim is a processing lease: a delivery that dies holding it is redelivered once it lapses.
_CLAIM_SECONDS = 60
_DELIVERED_SECONDS = 86_400
# A Slack thread keeps reaching the same Matrix thread for a week after its last delivery.
_THREAD_SECONDS = 7 * 86_400
_PENDING_THREAD_SECONDS = 300
_TILDE_RUN = re.compile(r"~+")
# Failures a delivery expects: a Slack call that failed, the replay store unavailable, or responses not admitted now.
DELIVERY_ERRORS = (
    SlackApiError,
    httpx.HTTPError,
    DeliveryReplayStoreError,
    DeliveryReplayScopeFullError,
    ResponseAdmissionRefusedError,
)


def slack_replay_store(control_state_root: Path) -> DeliveryReplayStore:
    """Return the replay store of every Slack target, which also binds each Slack thread to its Matrix thread."""
    return DeliveryReplayStore(control_state_root / "slack" / "replay")


def project_scope(agent: str) -> str:
    """Return the replay scope of a project agent's Slack turns."""
    return f"slack:agent:{agent}"


@dataclass(frozen=True)
class SlackAttempt:
    """One delivery attempt of one Slack message, with everything its targets share."""

    runtime: SlackRuntime
    config: Config
    runtime_paths: RuntimePaths
    credentials_manager: CredentialsManager
    client: httpx.AsyncClient
    app: SlackAppCredentials
    identity: SlackBotIdentity
    control_state_root: Path
    payload: Mapping[str, Any]
    event: SlackMessageEvent
    now: int
    last_attempt: bool

    @property
    def store(self) -> DeliveryReplayStore:
        """Return the replay store of every Slack target."""
        return slack_replay_store(self.control_state_root)

    @property
    def permalink(self) -> str:
        """Return the link to the event's message, opening its thread for a reply."""
        event = self.event
        return slack_permalink(self.identity.workspace_url, event.channel_id, event.ts, thread_ts=event.thread_ts)


@dataclass(frozen=True)
class SlackTurn:
    """The Matrix turn a Slack message becomes for one target."""

    scope: str
    thread_key: str
    agent: str
    room_id: str
    header: str
    hook_source: str
    requester: str


async def event_author(attempt: SlackAttempt) -> SlackAuthor:
    """Look up the event's author with the bot token, against the bot's own team."""
    return await slack_author(
        attempt.client,
        attempt.app.bot_token,
        attempt.identity.team_id,
        attempt.event.user_id,
        attempt.config,
        attempt.runtime_paths,
    )


async def event_channel(attempt: SlackAttempt) -> dict[str, Any]:
    """Look up the event's channel afresh with the bot token, to decide whether the app may act there."""
    return await slack_channel(
        attempt.client,
        attempt.app.bot_token,
        attempt.identity.team_id,
        attempt.event.channel_id,
        max_age=0,
    )


async def conversation_label(attempt: SlackAttempt) -> str:
    """Name the event's channel for a turn header as `#name`, from a lookup up to ten minutes old.

    A channel whose name Slack cannot give is named by its ID: the name is only for display,
    so its lookup must never fail or retry a delivery.
    """
    event = attempt.event
    try:
        info = await slack_channel(attempt.client, attempt.app.bot_token, attempt.identity.team_id, event.channel_id)
    except (SlackApiError, httpx.HTTPError) as exc:
        logger.warning(
            "Slack channel name lookup failed",
            event_id=event.event_id,
            channel_id=event.channel_id,
            reason=failure_reason(exc),
        )
        info = {}
    name = info.get("name")
    return f"#{name}" if isinstance(name, str) and name else f"#{event.channel_id}"


def running_bot(runtime: SlackRuntime, agent: str) -> tuple[AgentBot, nio.AsyncClient] | None:
    """Return an agent's bot and Matrix client while it runs."""
    bot = runtime.bots.get(agent)
    if bot is None or not bot.running or bot.client is None:
        return None
    return bot, bot.client


async def refuse_while_closed() -> bool:
    """Refuse at once while a runtime replacement holds admission, so Slack's own retry meets the replaced config."""
    return False


async def is_bound(attempt: SlackAttempt, scope: str, thread_key: str, room_id: str) -> bool:
    """Return whether a reply's Slack thread already reaches a Matrix thread in this scope and room."""
    if attempt.event.thread_ts is None:
        return False
    bound = await asyncio.to_thread(attempt.store.bound_thread_root, scope, thread_key, now=attempt.now)
    return bound is not None and bound[0] == room_id


async def claim_and_send(
    attempt: SlackAttempt,
    bot: AgentBot,
    client: nio.AsyncClient,
    turn: SlackTurn,
) -> DeliveryResult:
    """Post the message once in the turn's scope, opening its Matrix thread or continuing the bound one."""
    store, now = attempt.store, attempt.now
    message_key = f"{attempt.event.channel_id}:{attempt.event.ts}"
    claim = await asyncio.to_thread(store.claim_event_id, turn.scope, message_key, now=now, ttl_seconds=_CLAIM_SECONDS)
    if claim is DeliveryEventClaim.DELIVERED:
        return "ignored"
    if claim is DeliveryEventClaim.IN_PROGRESS:
        return "retry"
    try:
        thread_claim, root, reservation = await asyncio.to_thread(
            store.claim_thread_key,
            turn.scope,
            turn.thread_key,
            room_id=turn.room_id,
            now=now,
            pending_ttl_seconds=_PENDING_THREAD_SECONDS,
        )
    except (DeliveryReplayStoreError, DeliveryReplayScopeFullError):
        await _release(attempt, turn, message_key, None)
        raise
    if thread_claim is DeliveryThreadKeyClaim.PENDING:
        # Another delivery is opening this thread; this message joins it on a later attempt.
        await _release(attempt, turn, message_key, None)
        return "retry"
    matrix_event_id = None
    try:
        matrix_event_id = await _send_slack_turn(attempt, bot, client, turn, root)
    finally:
        if matrix_event_id is None:
            await _release(attempt, turn, message_key, reservation)
    if matrix_event_id is None:
        return "retry"
    await _record_delivery(attempt, turn, message_key, root or matrix_event_id, reservation)
    return "delivered"


async def _record_delivery(
    attempt: SlackAttempt,
    turn: SlackTurn,
    message_key: str,
    root: str,
    reservation: str | None,
) -> None:
    """Bind the Slack thread to its Matrix thread, then mark the message delivered; it is already posted either way."""
    store, now = attempt.store, attempt.now
    try:
        bound_root = await asyncio.to_thread(
            store.bind_thread_root,
            turn.scope,
            turn.thread_key,
            root,
            room_id=turn.room_id,
            reservation=reservation,
            now=now,
            ttl_seconds=_THREAD_SECONDS,
        )
        await asyncio.to_thread(
            store.mark_event_delivered,
            turn.scope,
            message_key,
            now=now,
            ttl_seconds=_DELIVERED_SECONDS,
        )
    except DeliveryReplayStoreError as exc:
        _log_store_failure(attempt.event, exc)
        return
    if bound_root != root:
        # This delivery outlived its reservation and another one opened the thread meanwhile.
        logger.warning(
            "Slack delivery lost its thread key",
            event_id=attempt.event.event_id,
            channel_id=attempt.event.channel_id,
        )


async def _release(attempt: SlackAttempt, turn: SlackTurn, message_key: str, reservation: str | None) -> None:
    """Drop this attempt's claims so the next one starts over; claims a failing store keeps expire on their own."""
    store = attempt.store
    try:
        await asyncio.to_thread(store.release_event_id, turn.scope, message_key)
        if reservation is not None:
            await asyncio.to_thread(store.release_thread_key, turn.scope, turn.thread_key, reservation=reservation)
    except DeliveryReplayStoreError as exc:
        _log_store_failure(attempt.event, exc)


async def _send_slack_turn(
    attempt: SlackAttempt,
    bot: AgentBot,
    client: nio.AsyncClient,
    turn: SlackTurn,
    thread_id: str | None,
) -> str | None:
    """Post a Slack message to Matrix through the agent's own account as a hook dispatch for the turn's requester.

    Only the agent is mentioned. Slack's text is shown as plain text in a `slack-message` block and never parsed for
    mentions, so it cannot wake anyone else. Without a `thread_id`, the message is the root of a new thread.
    """
    plain_target, mentioned_user_ids, markdown_target = format_entity_mention(
        turn.agent,
        attempt.config,
        attempt.runtime_paths,
    )
    # The app's own mention is the one name known without a lookup.
    text = slack_plain_text(attempt.event.text, {attempt.identity.bot_user_id: attempt.identity.bot_name})
    # Longer than any run of `~` in the text, so the text cannot close the fence and reaches the agent unchanged.
    fence = "~" * max([3, *(len(run) + 1 for run in _TILDE_RUN.findall(text))])
    latest_thread_event_id = None
    if thread_id is not None:
        latest_thread_event_id = await bot.latest_thread_event_id_if_needed(turn.room_id, thread_id)
    metadata: dict[str, Any] = {
        SOURCE_KIND_KEY: HOOK_DISPATCH_SOURCE_KIND,
        HOOK_SOURCE_KEY: turn.hook_source,
        ORIGINAL_SENDER_KEY: turn.requester,
    }
    if thread_id is None:
        metadata[PER_FIRE_THREAD_ROOT_KEY] = True
    # Only the target's mention, which MindRoom builds, is rendered as markup: Slack's names and text are escaped as
    # they are, so they cannot add links, images, or mentions.
    target_html = markdown_to_html(markdown_target).strip().removeprefix("<p>").removesuffix("</p>")
    content = build_message_content(
        body=f"{plain_target} {turn.header}\n{fence}slack-message\n{text}\n{fence}",
        formatted_body=(
            f"<p>{target_html} {html.escape(turn.header)}</p>\n"
            f'<pre><code class="language-slack-message">{html.escape(text)}\n</code></pre>\n'
        ),
        mentioned_user_ids=mentioned_user_ids,
        thread_event_id=thread_id,
        latest_thread_event_id=latest_thread_event_id,
        extra_content=metadata,
    )
    delivered = await send_matrix_message(client, turn.room_id, content)
    return None if delivered is None else delivered.event_id


async def retry_on_failure(event: SlackMessageEvent, delivery: Awaitable[DeliveryResult]) -> DeliveryResult:
    """Await one target's delivery, so that its failure spares the others; see `delivery_failed` for what follows."""
    try:
        return await delivery
    except DELIVERY_ERRORS as exc:
        return delivery_failed(exc, event_id=event.event_id, channel_id=event.channel_id)
    except Exception:
        logger.exception(
            "Slack event delivery failed unexpectedly and will be retried",
            event_id=event.event_id,
            channel_id=event.channel_id,
        )
        return "retry"


def delivery_failed(exc: Exception, *, event_id: str | None, channel_id: str | None = None) -> DeliveryResult:
    """Log one of the `DELIVERY_ERRORS` and say what follows.

    A Slack error that fails every attempt alike skips the target, so Slack is not asked to send the event again;
    any other failure is retried.
    """
    if isinstance(exc, SlackApiError) and not exc.transient:
        logger.error(
            "Slack refused a call, so the event is not delivered",
            event_id=event_id,
            channel_id=channel_id,
            reason=failure_reason(exc),
        )
        return "ignored"
    logger.warning(
        "Slack event delivery will be retried",
        event_id=event_id,
        channel_id=channel_id,
        reason=failure_reason(exc),
    )
    return "retry"


def failure_reason(exc: Exception) -> str:
    """Return what a log may say about a failure: Slack's error code, or the error's type."""
    return exc.error if isinstance(exc, SlackApiError) else type(exc).__name__


def skipped(event: SlackMessageEvent, reason: str) -> DeliveryResult:
    """Log why a target does not receive an event, which is final."""
    logger.info("Slack event not delivered", event_id=event.event_id, channel_id=event.channel_id, reason=reason)
    return "ignored"


def retry_later(event: SlackMessageEvent, reason: str) -> DeliveryResult:
    """Log why a target cannot receive an event yet."""
    logger.warning(
        "Slack event delivery will be retried",
        event_id=event.event_id,
        channel_id=event.channel_id,
        reason=reason,
    )
    return "retry"


def _log_store_failure(event: SlackMessageEvent, exc: Exception) -> None:
    logger.warning(
        "Slack delivery replay store failed",
        event_id=event.event_id,
        channel_id=event.channel_id,
        reason=str(exc),
    )
