"""Deliver Slack events to personal watches: into the owner's room, while that room may receive them."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from mindroom.config.slack import has_personal_slack
from mindroom.logging_config import get_logger
from mindroom.oauth.credential_lifecycle import (
    OAuthCredentialUnreadableError,
    load_oauth_credentials_snapshot,
    oauth_credentials_usable,
    resolve_oauth_credential_context,
)
from mindroom.response_admission import admitted_response_decision
from mindroom.slack.api import slack_call
from mindroom.slack.events import (
    is_direct_message,
    mentioned_user_ids,
    watch_accepts,
    watch_candidates,
    watch_thread_key,
)
from mindroom.slack.oauth import slack_oauth_provider
from mindroom.slack.render import json_objects, next_cursor
from mindroom.slack.turns import (
    SlackTurn,
    claim_and_send,
    event_author,
    is_bound,
    refuse_while_closed,
    retry_later,
    retry_on_failure,
    running_bot,
    skipped,
)
from mindroom.slack.watches import SlackWatchStore, watch_problem
from mindroom.tool_system.worker_routing import build_requester_execution_identity

if TYPE_CHECKING:
    from mindroom.slack.turns import DeliveryResult, SlackAttempt
    from mindroom.slack.watches import SlackWatch

logger = get_logger(__name__)


async def deliver_to_watches(attempt: SlackAttempt) -> list[DeliveryResult]:
    """Deliver the event to every watch it is for whose owner can see it; watches need the app-level token."""
    app_token = attempt.app.app_token
    if app_token is None:
        return []
    event = attempt.event
    try:
        watches = await asyncio.to_thread(SlackWatchStore(attempt.control_state_root).all)
    except Exception:
        # An unreadable watch file must not stop the project delivery, nor every later event.
        logger.exception("Slack watches could not be read and are skipped", event_id=event.event_id)
        return []
    # The watches the event could be for if their threads were bound; only theirs are worth a replay store read.
    possible = watch_candidates(event, watches, bound_watch_ids=frozenset(watch.watch_id for watch in watches))
    bound = frozenset(
        [
            watch.watch_id
            for watch in possible
            if await is_bound(attempt, _watch_scope(watch), watch_thread_key(watch.watch_id, event), watch.room_id)
        ],
    )
    candidates = watch_candidates(event, possible, bound_watch_ids=bound)
    if not candidates:
        return []
    visible_to = await _visible_to(attempt, app_token)
    return [
        await retry_on_failure(
            event,
            _deliver_to_watch(attempt, watch, bound=watch.watch_id in bound, visible_to=visible_to),
        )
        for watch in candidates
    ]


async def _visible_to(attempt: SlackAttempt, app_token: str) -> frozenset[str]:
    """Ask Slack which people can see the event, since Slack sends one event however many may."""
    event_context = attempt.payload.get("event_context")
    if not isinstance(event_context, str) or not event_context:
        return frozenset()
    people: set[str] = set()
    cursor = None
    while True:
        body = await slack_call(
            attempt.client,
            "apps.event.authorizations.list",
            app_token,
            event_context=event_context,
            cursor=cursor,
        )
        for authorization in json_objects(body, "authorizations"):
            user_id = authorization.get("user_id")
            if isinstance(user_id, str) and not authorization.get("is_bot"):
                people.add(user_id)
        cursor = next_cursor(body)
        if cursor is None:
            return frozenset(people)


async def _deliver_to_watch(
    attempt: SlackAttempt,
    watch: SlackWatch,
    *,
    bound: bool,
    visible_to: frozenset[str],
) -> DeliveryResult:
    """Deliver the event to one watch while it may receive it: see `watch_problem` for when it pauses."""
    event = attempt.event
    if not watch_accepts(watch, event, bound=bound, visible_to=visible_to):
        return "ignored"
    if not has_personal_slack(attempt.config, watch.agent_name):
        return skipped(event, "the watch's agent no longer has personal Slack")
    running = running_bot(attempt.runtime, watch.agent_name)
    if running is None:
        return retry_later(event, "the watch's agent is not running")
    bot, client = running
    author = await event_author(attempt)
    if is_direct_message(event):
        kind = "direct message"
    elif watch.slack_user_id in mentioned_user_ids(event.text):
        kind = "mention"
    else:
        kind = "thread reply"
    async with admitted_response_decision(bot.admission_gate, refuse_while_closed):
        problem = await watch_problem(
            watch,
            await _owner_credentials(attempt, watch),
            client,
            attempt.config,
            attempt.runtime_paths,
        )
        if problem is not None:
            return skipped(event, problem)
        turn = SlackTurn(
            scope=_watch_scope(watch),
            thread_key=watch_thread_key(watch.watch_id, event),
            agent=watch.agent_name,
            room_id=watch.room_id,
            header=(
                f"Slack {kind} for you from {author.display_name} (<@{event.user_id}>) "
                f"in <#{event.channel_id}>: {attempt.permalink}"
            ),
            hook_source=f"slack-watch/{watch.watch_id}",
            requester=watch.owner_id,
        )
        return await claim_and_send(attempt, bot, client, turn)


def _watch_scope(watch: SlackWatch) -> str:
    return f"slack:watch:{watch.watch_id}"


async def _owner_credentials(attempt: SlackAttempt, watch: SlackWatch) -> dict[str, Any] | None:
    """Return the owner's usable Slack credentials, read where their `slack` toolkit keeps them, never refreshed."""
    provider = slack_oauth_provider()
    context = resolve_oauth_credential_context(
        provider,
        attempt.runtime_paths,
        attempt.credentials_manager,
        None,
        execution_identity=build_requester_execution_identity(
            agent_name=watch.agent_name,
            runtime_paths=attempt.runtime_paths,
            requester_id=watch.owner_id,
        ),
        config=attempt.config,
    )
    try:
        credentials = (await load_oauth_credentials_snapshot(context)).credentials
    except OAuthCredentialUnreadableError:
        return None
    usable = await asyncio.to_thread(oauth_credentials_usable, provider, attempt.runtime_paths, credentials)
    return credentials if usable else None
