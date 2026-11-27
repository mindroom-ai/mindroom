"""Slack reactive trigger EventSource and visibility-checked event emission."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

import httpx

from mindroom.config.slack import has_personal_slack
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.event_sources import (
    EventSource,
    EventSourceContext,
    EventSourceDescriptor,
    ExternalBindingValue,
    ExternalMatchValue,
    ValidatedSubscription,
)
from mindroom.logging_config import get_logger
from mindroom.oauth.credential_lifecycle import (
    OAuthCredentialContext,
    OAuthCredentialUnreadableError,
    load_oauth_credentials_snapshot,
    oauth_credentials_usable,
    oauth_verified_claim,
    resolve_oauth_credential_context,
)
from mindroom.oauth.service import OAUTH_ACCESS_REJECTED_REASON, OAUTH_RESET_REQUIRED_REASON, oauth_connection_required
from mindroom.reactive_triggers.dispatch import DispatchResult
from mindroom.reactive_triggers.models import ExternalEventSourceSpec, ReactiveTriggerRecord, TriggerEvent
from mindroom.requester_identity import (
    equivalent_requester_ids,
    is_access_checked_requester_id,
    resolve_human_requester_alias,
)
from mindroom.slack.api import SlackApiError, parse_conversation_target, slack_call
from mindroom.slack.app import SLACK_APP_NOT_CONFIGURED, SlackAppCredentials, load_slack_app
from mindroom.slack.events import is_direct_message, mentioned_user_ids
from mindroom.slack.oauth import slack_oauth_provider
from mindroom.slack.render import json_objects, next_cursor, slack_plain_text
from mindroom.slack.turns import conversation_label, delivery_failed, event_author, failure_reason
from mindroom.tool_system.worker_routing import build_requester_execution_identity

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.credentials.manager import CredentialsManager
    from mindroom.matrix.room_reconciliation import RoomStateSnapshot
    from mindroom.reactive_triggers.service import ReactiveTriggerService
    from mindroom.reactive_triggers.store import ReactiveTriggerStore
    from mindroom.slack.turns import DeliveryResult, SlackAttempt
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

logger = get_logger(__name__)

SLACK_EVENT_SOURCE_ID = "slack"
_SLACK_TRIGGER_HOOK_SOURCE = f"event_source:{SLACK_EVENT_SOURCE_ID}"
# LEGACY_COMPAT: Turns emitted by legacy slack/watches.py carried `slack-watch/<watch_id>` hook sources.
# Legacy format: `hook_source` starting with `"slack-watch/"` on queued or resumed runs across upgrade.
# Last legacy release: v2026.11; replacement: `event_source:slack` on reactive trigger dispatches.
# Handling: Keep refusing Slack writes for both `event_source:slack` and `slack-watch/`.
# Coverage: tests/slack/test_toolkit.py::test_writes_refused_during_watch_turn.
_LEGACY_WATCH_SOURCE_PREFIX = "slack-watch/"
_ALLOWED_KINDS = ("message", "mention", "direct_message")
_PRIVATE_HISTORY_VISIBILITY = frozenset({"invited", "joined"})
_NEEDS_APP_TOKEN = "The MindRoom Slack app needs an app-level token for watches"  # noqa: S105
_AGENT_NO_PERSONAL_SLACK = "the watch's agent no longer has personal Slack"
_SLACK_NOT_CONNECTED = "Slack is not connected"
_ACCOUNT_CHANGED = "Slack account changed; recreate this subscription"


def started_by_slack_trigger(context: ToolRuntimeContext | None) -> bool:
    """Return whether a Slack reactive trigger or legacy Slack watch started this turn."""
    hook = context.hook_source if context is not None else None
    return bool(hook and (hook == _SLACK_TRIGGER_HOOK_SOURCE or hook.startswith(_LEGACY_WATCH_SOURCE_PREFIX)))


def _watch_room_problem_from_state(
    room_state: RoomStateSnapshot | None,
    owner_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> str | None:
    """Return why a room may not receive or display its owner's private Slack messages, or None when allowed."""
    if room_state is None:
        return "room members could not be checked"
    owner_ids = equivalent_requester_ids(owner_id, config, runtime_paths)
    if not any(room_state.events.get(("m.room.member", uid), {}).get("membership") == "join" for uid in owner_ids):
        return "you are not in this room"
    if any(is_access_checked_requester_id(m, config, runtime_paths) for m in room_state.present_user_ids() - owner_ids):
        return "another person is in this room"
    vis = room_state.events.get(("m.room.history_visibility", ""), {}).get("history_visibility")
    return None if vis in _PRIVATE_HISTORY_VISIBILITY else "people who join this room later could read earlier messages"


def _room_problem(room_state: RoomStateSnapshot | None, owner_id: str, ctx: EventSourceContext) -> str | None:
    return _watch_room_problem_from_state(room_state, owner_id, ctx.config, ctx.runtime_paths)


async def _slack_app_for_ctx(ctx: EventSourceContext) -> tuple[CredentialsManager, SlackAppCredentials | None]:
    creds_mgr = ctx.credentials_manager or get_runtime_credentials_manager(ctx.runtime_paths)
    return creds_mgr, await asyncio.to_thread(load_slack_app, creds_mgr)


async def _load_owner_credentials(
    ctx: EventSourceContext,
    creds_mgr: CredentialsManager,
    agent_name: str,
    owner_user_id: str,
) -> tuple[dict[str, Any] | None, OAuthCredentialContext, str | None]:
    """Read the owner's usable Slack credentials without refreshing, returning ``(creds, context, error_reason)``."""
    provider = slack_oauth_provider()
    owner = resolve_human_requester_alias(owner_user_id, ctx.config, ctx.runtime_paths)
    identity = build_requester_execution_identity(
        agent_name=agent_name,
        runtime_paths=ctx.runtime_paths,
        requester_id=owner,
    )
    cred_ctx = resolve_oauth_credential_context(
        provider,
        ctx.runtime_paths,
        creds_mgr,
        None,
        execution_identity=identity,
        config=ctx.config,
    )
    if cred_ctx.worker_target is None:
        return None, cred_ctx, None
    try:
        creds = (await load_oauth_credentials_snapshot(cred_ctx)).credentials
    except OAuthCredentialUnreadableError:
        return None, cred_ctx, OAUTH_RESET_REQUIRED_REASON
    usable = await asyncio.to_thread(oauth_credentials_usable, provider, ctx.runtime_paths, creds)
    if creds is None or not usable or not isinstance(creds.get("token"), str) or not creds["token"]:
        return None, cred_ctx, None
    return creds, cred_ctx, None


def _bound_slack_account(rule: ReactiveTriggerRecord) -> tuple[str, str] | None:
    if not isinstance(rule.source, ExternalEventSourceSpec):
        return None
    team_id, slack_user_id = rule.source.binding.get("team_id"), rule.source.binding.get("slack_user_id")
    if isinstance(team_id, str) and team_id and isinstance(slack_user_id, str) and slack_user_id:
        return team_id, slack_user_id
    return None


def _matches_slack_rule(
    source: ExternalEventSourceSpec,
    is_dm: bool,
    channel_id: object,
    is_mention: bool,
    has_thread: bool,
) -> bool:
    if is_dm:
        return source.kind in ("message", "direct_message") or (source.kind == "mention" and is_mention)
    filt = source.match.get("channel_id")
    channel_ok = (
        filt is None
        or (isinstance(filt, str) and channel_id == filt)
        or (isinstance(filt, list) and channel_id in filt)
    )
    return source.kind in ("message", "mention") and channel_ok and (is_mention or has_thread)


def _normalize_slack_match(kind: str, match: Mapping[str, ExternalMatchValue]) -> dict[str, ExternalMatchValue]:
    if unknown := sorted(set(match) - {"channel_id"}):
        msg = f"Unsupported Slack match field(s): {', '.join(unknown)}"
        raise ValueError(msg)
    if "channel_id" not in match:
        return {}
    if kind == "direct_message":
        msg = "Cannot filter direct_message Slack triggers by channel_id"
        raise ValueError(msg)
    raw = match["channel_id"]
    if isinstance(raw, str):
        return {"channel_id": parse_conversation_target(raw)[0]}
    if isinstance(raw, list) and raw and all(isinstance(item, str) for item in raw):
        return {"channel_id": list(dict.fromkeys(parse_conversation_target(item)[0] for item in raw))}
    msg = "channel_id must be a non-empty Slack channel ID or list of channel IDs"
    raise ValueError(msg)


class SlackEventSource(EventSource):
    """Built-in EventSource for personal Slack mentions, bound thread replies, and direct messages."""

    source_id: str = SLACK_EVENT_SOURCE_ID
    description: str = (
        "Personal Slack mentions, bound thread replies, and direct messages for your connected Slack account "
        "(private rooms only)."
    )
    allowed_kinds: tuple[str, ...] = _ALLOWED_KINDS
    match_fields: tuple[str, ...] = ("channel_id",)
    example: str | None = (
        'triggers(action="subscribe_event", source_id="slack", kind="message", '
        'prompt="Summarize this Slack message and ask if I want to reply.")'
    )
    default_once: bool = False
    default_new_thread: bool = True
    default_thread_key_from_event: bool = True
    default_reopen_on_fire: bool = True
    private_room_only: bool = True
    default_max_fires_per_hour: int | None = 60

    async def describe_for_user(self, ctx: EventSourceContext) -> EventSourceDescriptor | None:
        """Advertise Slack watches when the Slack app has an app token and personal Slack is configured."""
        _, app = await _slack_app_for_ctx(ctx)
        if app is None or app.app_token is None:
            return None
        agents = (ctx.target_agent,) if ctx.target_agent is not None else ctx.config.agents
        if not any(has_personal_slack(ctx.config, name) for name in agents):
            return None
        return await super().describe_for_user(ctx)

    async def validate_subscription(
        self,
        ctx: EventSourceContext,
        *,
        kind: str,
        match: Mapping[str, ExternalMatchValue],
        existing_binding: Mapping[str, ExternalBindingValue] | None = None,
    ) -> ValidatedSubscription:
        """Validate Slack app token, personal Slack agent, kind/match, verified OAuth identity, and room privacy."""
        creds_mgr, app = await _slack_app_for_ctx(ctx)
        if app is None or app.app_token is None:
            raise ValueError(SLACK_APP_NOT_CONFIGURED if app is None else _NEEDS_APP_TOKEN)
        if not ctx.target_agent or not has_personal_slack(ctx.config, ctx.target_agent):
            msg = "The target agent does not have personal Slack configured"
            raise ValueError(msg)
        stripped_kind = kind.strip()
        if stripped_kind not in _ALLOWED_KINDS:
            msg = f"Unsupported Slack event kind {kind!r}; expected one of: {', '.join(_ALLOWED_KINDS)}"
            raise ValueError(msg)
        normalized_match = _normalize_slack_match(stripped_kind, match)
        creds, cred_ctx, err = await _load_owner_credentials(ctx, creds_mgr, ctx.target_agent, ctx.owner_user_id)
        if creds is None:
            raise await asyncio.to_thread(oauth_connection_required, cred_ctx, reason=err)
        team_id, slack_user_id = oauth_verified_claim(creds, "team_id"), oauth_verified_claim(creds, "sub")
        if team_id is None or slack_user_id is None:
            raise await asyncio.to_thread(oauth_connection_required, cred_ctx, reason=OAUTH_ACCESS_REJECTED_REASON)
        if existing_binding is not None and (team_id, slack_user_id) != (
            existing_binding.get("team_id"),
            existing_binding.get("slack_user_id"),
        ):
            raise ValueError(_ACCOUNT_CHANGED)
        binding: dict[str, ExternalBindingValue] = (
            dict(existing_binding)
            if existing_binding is not None
            else {"team_id": team_id, "slack_user_id": slack_user_id}
        )
        problem = _room_problem(ctx.target_room_state, ctx.owner_user_id, ctx)
        if problem is not None:
            msg = f"Cannot watch Slack here: {problem}"
            raise ValueError(msg)
        return ValidatedSubscription(kind=stripped_kind, match=normalized_match, binding=binding)

    def matches_rule(
        self,
        rule: ReactiveTriggerRecord,
        event: TriggerEvent,
        *,
        audience: object = None,
        has_thread_binding: bool = False,
    ) -> bool:
        """Match one Slack event against a personal Slack watch rule using ephemeral visibility audience."""
        if not isinstance(rule.source, ExternalEventSourceSpec) or not isinstance(audience, frozenset) or not audience:
            return False
        if (bound := _bound_slack_account(rule)) is None:
            return False
        team_id, uid = bound
        if team_id != event.data.get("team_id") or uid not in audience or uid == event.data.get("user_id"):
            return False
        raw_mentioned = event.data.get("mentioned_user_ids", ())
        is_mention = isinstance(raw_mentioned, (list, tuple, set, frozenset)) and uid in raw_mentioned
        is_dm, channel_id = bool(event.data.get("is_dm")), event.data.get("channel_id")
        return _matches_slack_rule(rule.source, is_dm, channel_id, is_mention, has_thread_binding)

    def dry_run_audience(self, rule: ReactiveTriggerRecord) -> object:
        """Synthesize a single-owner audience for dry-run ``triggers(action="test")``."""
        bound = _bound_slack_account(rule)
        return frozenset({bound[1]}) if bound is not None else frozenset()

    async def check_delivery_access(
        self,
        ctx: EventSourceContext,
        *,
        rule: ReactiveTriggerRecord,
        event: TriggerEvent | None = None,
    ) -> DispatchResult | None:
        """Re-verify app token, personal Slack config, verified OAuth identity, and private target room."""
        del event
        reason = await self._delivery_pause_reason(ctx, rule)
        return DispatchResult(status="skipped", reason=reason) if reason is not None else None

    async def _delivery_pause_reason(self, ctx: EventSourceContext, rule: ReactiveTriggerRecord) -> str | None:
        if (bound := _bound_slack_account(rule)) is None:
            return _ACCOUNT_CHANGED
        creds_mgr, app = await _slack_app_for_ctx(ctx)
        if app is None or app.app_token is None:
            return _NEEDS_APP_TOKEN
        if not has_personal_slack(ctx.config, rule.target.agent):
            return _AGENT_NO_PERSONAL_SLACK
        creds, _, _ = await _load_owner_credentials(ctx, creds_mgr, rule.target.agent, rule.owner_user_id)
        if creds is None:
            return _SLACK_NOT_CONNECTED
        if (oauth_verified_claim(creds, "team_id"), oauth_verified_claim(creds, "sub")) != bound:
            return _ACCOUNT_CHANGED
        return _room_problem(ctx.target_room_state, rule.owner_user_id, ctx)

    async def check_history_access(
        self,
        ctx: EventSourceContext,
        *,
        rule: ReactiveTriggerRecord,
        event: TriggerEvent | None = None,
    ) -> bool:
        """Allow inspecting private Slack firing payloads only for the owner in a private room."""
        if ctx.owner_user_id not in equivalent_requester_ids(rule.owner_user_id, ctx.config, ctx.runtime_paths):
            return False
        del event
        if _room_problem(ctx.calling_room_state, rule.owner_user_id, ctx):
            return False
        return await self._delivery_pause_reason(ctx, rule) is None


async def _visible_to(attempt: SlackAttempt, app_token: str) -> frozenset[str]:
    """Ask Slack which people can see the event, paginating ``apps.event.authorizations.list``."""
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
        for auth in json_objects(body, "authorizations"):
            if isinstance(uid := auth.get("user_id"), str) and not auth.get("is_bot"):
                people.add(uid)
        if (cursor := next_cursor(body)) is None:
            return frozenset(people)


async def _build_watch_trigger_event(
    attempt: SlackAttempt,
    is_dm: bool,
    mentioned: frozenset[str],
    thread_key: str,
    dedupe_key: str,
) -> TriggerEvent:
    event = attempt.event
    try:
        author = (await event_author(attempt)).display_name
    except (SlackApiError, httpx.HTTPError) as exc:
        logger.warning(
            "Slack author lookup failed for watch event",
            event_id=event.event_id,
            channel_id=event.channel_id,
            reason=failure_reason(exc),
        )
        author = event.user_id
    channel_label = "" if is_dm else await conversation_label(attempt)
    plain_text = slack_plain_text(event.text, {attempt.identity.bot_user_id: attempt.identity.bot_name})
    if is_dm:
        dm_label = "group direct message" if event.channel_type == "mpim" else "direct message"
        title, event_kind = f"Slack {dm_label} from {author}", "direct_message"
    else:
        title, event_kind = f"Slack message from {author} in {channel_label}", ("mention" if mentioned else "message")
    return TriggerEvent(
        source_type="external_event",
        source_id=SLACK_EVENT_SOURCE_ID,
        kind=event_kind,
        event_id=dedupe_key,
        timestamp=float(attempt.now),
        actor=event.user_id,
        title=title,
        summary=f"{title}: {attempt.permalink}",
        text=plain_text,
        thread_key=thread_key,
        data={
            "team_id": event.team_id,
            "channel_id": event.channel_id,
            "channel_type": event.channel_type,
            "channel_label": channel_label,
            "user_id": event.user_id,
            "author_display_name": author,
            "ts": event.ts,
            "thread_ts": event.thread_ts,
            "permalink": attempt.permalink,
            "is_dm": is_dm,
            "mentioned_user_ids": sorted(mentioned),
        },
    )


async def _prefilter_watch_candidates(
    attempt: SlackAttempt,
    store: ReactiveTriggerStore,
    is_dm: bool,
    mentioned: frozenset[str],
    thread_key: str,
) -> frozenset[str]:
    """Return bound Slack user IDs whose enabled registered ``slack`` rules could match this event."""
    now, event = time.time(), attempt.event
    rules = await asyncio.to_thread(
        store.list_triggers,
        include_disabled=False,
        source_type="external_event",
        source_id=SLACK_EVENT_SOURCE_ID,
    )
    matched: set[str] = set()
    thread_candidates: list[tuple[str, str]] = []
    has_thread = not is_dm and event.thread_ts is not None
    for rule in rules:
        if (
            (rule.expires_at is not None and rule.expires_at <= now)
            or not isinstance(rule.source, ExternalEventSourceSpec)
            or rule.source.source_origin != "registered"
            or (bound := _bound_slack_account(rule)) is None
            or bound[0] != event.team_id
            or bound[1] == event.user_id
        ):
            continue
        is_mention = bound[1] in mentioned
        if not _matches_slack_rule(rule.source, is_dm, event.channel_id, is_mention, has_thread):
            continue
        if is_dm or is_mention:
            matched.add(bound[1])
        else:
            thread_candidates.append((rule.trigger_id, bound[1]))
    if thread_candidates:
        bound_ids = await asyncio.to_thread(
            store.get_bound_thread_keys,
            [tid for tid, _ in thread_candidates],
            thread_key,
            now=now,
        )
        matched.update(uid for tid, uid in thread_candidates if tid in bound_ids)
    return frozenset(matched)


async def emit_slack_watch_event(
    attempt: SlackAttempt,
    reactive_triggers: ReactiveTriggerService | None,
) -> DeliveryResult:
    """Prefilter Slack watch rules, verify Slack event visibility, and emit onto ``ReactiveTriggerService``."""
    app_token, policy = attempt.app.app_token, attempt.config.external_trigger_policy
    if (
        app_token is None
        or reactive_triggers is None
        or reactive_triggers.store is None
        or not (policy.enabled and policy.reactive_enabled)
        or reactive_triggers.event_sources.get(SLACK_EVENT_SOURCE_ID) is None
    ):
        return "ignored"

    event = attempt.event
    is_dm, mentioned = is_direct_message(event), mentioned_user_ids(event.text)
    thread_key = event.channel_id if is_dm else f"{event.channel_id}:{event.thread_ts or event.ts}"
    dedupe_key = f"{event.team_id}:{event.channel_id}:{event.ts}"
    candidates = await _prefilter_watch_candidates(attempt, reactive_triggers.store, is_dm, mentioned, thread_key)
    if not candidates:
        return "ignored"
    try:
        visible_to = await _visible_to(attempt, app_token)
    except (SlackApiError, httpx.HTTPError) as exc:
        return delivery_failed(exc, event_id=event.event_id, channel_id=event.channel_id)
    if not (candidates & visible_to):
        return "ignored"
    trigger_event = await _build_watch_trigger_event(attempt, is_dm, mentioned, thread_key, dedupe_key)
    enqueued = await reactive_triggers.emit_event(
        SLACK_EVENT_SOURCE_ID,
        trigger_event,
        audience=visible_to,
        dedupe_key=dedupe_key,
    )
    return "delivered" if enqueued > 0 else "ignored"
