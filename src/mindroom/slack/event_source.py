"""Slack reactive trigger EventSource, event emission, and one-time legacy watch migration."""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import TYPE_CHECKING, Any

import httpx

from mindroom.config.slack import has_personal_slack
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.delivery_replay import DeliveryReplayStoreError
from mindroom.file_locks import advisory_file_lock
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
from mindroom.reactive_triggers.event_sources import (
    EventSource,
    EventSourceContext,
    EventSourceDescriptor,
    ExternalBindingValue,
    ExternalMatchValue,
    ValidatedSubscription,
)
from mindroom.reactive_triggers.models import (
    TRIGGER_ID_PATTERN,
    ExternalEventSourceSpec,
    ReactiveTriggerRecord,
    TriggerEvent,
    TriggerTargetSpec,
)
from mindroom.requester_identity import (
    equivalent_requester_ids,
    is_access_checked_requester_id,
    resolve_human_requester_alias,
)
from mindroom.slack.api import SlackApiError, parse_conversation_target, slack_call
from mindroom.slack.app import SLACK_APP_NOT_CONFIGURED, load_slack_app
from mindroom.slack.events import is_direct_message, mentioned_user_ids
from mindroom.slack.oauth import slack_oauth_provider
from mindroom.slack.render import json_objects, next_cursor, slack_plain_text
from mindroom.slack.turns import conversation_label, delivery_failed, event_author, failure_reason, slack_replay_store
from mindroom.tool_system.worker_routing import build_requester_execution_identity

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.credentials.manager import CredentialsManager
    from mindroom.delivery_replay import DeliveryReplayStore
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
_LEGACY_MIGRATION_NAME = "slack_watches_v1"
_LEGACY_THREAD_TTL_SECONDS = 7 * 86_400
_MIGRATED_WATCH_PROMPT = (
    "Deliver this Slack message to the user, summarize it clearly, and wait for their reply before posting to Slack."
)
_SAFE_ID_CHARS = re.compile(r"[^A-Za-z0-9_-]")


def started_by_slack_trigger(context: ToolRuntimeContext | None) -> bool:
    """Return whether a Slack reactive trigger or legacy Slack watch started this turn."""
    if context is None or not context.hook_source:
        return False
    return context.hook_source == _SLACK_TRIGGER_HOOK_SOURCE or context.hook_source.startswith(
        _LEGACY_WATCH_SOURCE_PREFIX,
    )


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
    if not any(
        room_state.events.get(("m.room.member", user_id), {}).get("membership") == "join" for user_id in owner_ids
    ):
        return "you are not in this room"
    if any(
        is_access_checked_requester_id(member, config, runtime_paths)
        for member in room_state.present_user_ids() - owner_ids
    ):
        return "another person is in this room"
    visibility = room_state.events.get(("m.room.history_visibility", ""), {}).get("history_visibility")
    if visibility not in _PRIVATE_HISTORY_VISIBILITY:
        return "people who join this room later could read earlier messages"
    return None


def _owner_oauth_context(
    config: Config,
    runtime_paths: RuntimePaths,
    credentials_manager: CredentialsManager,
    agent_name: str,
    owner_user_id: str,
) -> OAuthCredentialContext:
    canonical_owner = resolve_human_requester_alias(owner_user_id, config, runtime_paths)
    identity = build_requester_execution_identity(
        agent_name=agent_name,
        runtime_paths=runtime_paths,
        requester_id=canonical_owner,
    )
    return resolve_oauth_credential_context(
        slack_oauth_provider(),
        runtime_paths,
        credentials_manager,
        None,
        execution_identity=identity,
        config=config,
    )


async def _load_owner_credentials(
    config: Config,
    runtime_paths: RuntimePaths,
    credentials_manager: CredentialsManager,
    agent_name: str,
    owner_user_id: str,
) -> tuple[dict[str, Any] | None, OAuthCredentialContext, str | None]:
    """Read the owner's usable Slack credentials without refreshing, returning ``(creds, context, error_reason)``."""
    provider = slack_oauth_provider()
    cred_context = _owner_oauth_context(config, runtime_paths, credentials_manager, agent_name, owner_user_id)
    if cred_context.worker_target is None:
        return None, cred_context, None
    try:
        credentials = (await load_oauth_credentials_snapshot(cred_context)).credentials
    except OAuthCredentialUnreadableError:
        return None, cred_context, OAUTH_RESET_REQUIRED_REASON
    usable = await asyncio.to_thread(oauth_credentials_usable, provider, runtime_paths, credentials)
    token = (credentials or {}).get("token")
    if credentials is None or not usable or not isinstance(token, str) or not token:
        return None, cred_context, None
    return credentials, cred_context, None


def _normalize_slack_match(kind: str, match: Mapping[str, ExternalMatchValue]) -> dict[str, ExternalMatchValue]:
    unknown = sorted(set(match) - {"channel_id"})
    if unknown:
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
    if isinstance(raw, list):
        if not raw or any(not isinstance(item, str) for item in raw):
            msg = "channel_id must be a non-empty Slack channel ID or list of channel IDs"
            raise ValueError(msg)
        return {"channel_id": list(dict.fromkeys(parse_conversation_target(item)[0] for item in raw))}
    msg = "channel_id must be a Slack channel ID or list of channel IDs"
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

    async def describe_for_user(self, ctx: EventSourceContext) -> EventSourceDescriptor | None:
        """Advertise Slack watches when the Slack app has an app token and personal Slack is configured."""
        credentials_manager = ctx.credentials_manager or get_runtime_credentials_manager(ctx.runtime_paths)
        app = await asyncio.to_thread(load_slack_app, credentials_manager)
        if app is None or app.app_token is None:
            return None
        if ctx.target_agent is not None:
            if not has_personal_slack(ctx.config, ctx.target_agent):
                return None
        elif not any(has_personal_slack(ctx.config, name) for name in ctx.config.agents):
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
        credentials_manager = ctx.credentials_manager or get_runtime_credentials_manager(ctx.runtime_paths)
        app = await asyncio.to_thread(load_slack_app, credentials_manager)
        if app is None:
            raise ValueError(SLACK_APP_NOT_CONFIGURED)
        if app.app_token is None:
            raise ValueError(_NEEDS_APP_TOKEN)
        if not ctx.target_agent or not has_personal_slack(ctx.config, ctx.target_agent):
            msg = "The target agent does not have personal Slack configured"
            raise ValueError(msg)
        stripped_kind = kind.strip()
        if stripped_kind not in _ALLOWED_KINDS:
            msg = f"Unsupported Slack event kind {kind!r}; expected one of: {', '.join(_ALLOWED_KINDS)}"
            raise ValueError(msg)
        normalized_match = _normalize_slack_match(stripped_kind, match)
        credentials, cred_context, err_reason = await _load_owner_credentials(
            ctx.config,
            ctx.runtime_paths,
            credentials_manager,
            ctx.target_agent,
            ctx.owner_user_id,
        )
        if credentials is None:
            raise await asyncio.to_thread(oauth_connection_required, cred_context, reason=err_reason)
        team_id, slack_user_id = oauth_verified_claim(credentials, "team_id"), oauth_verified_claim(credentials, "sub")
        if team_id is None or slack_user_id is None:
            raise await asyncio.to_thread(
                oauth_connection_required,
                cred_context,
                reason=OAUTH_ACCESS_REJECTED_REASON,
            )
        if existing_binding is not None:
            if (team_id, slack_user_id) != (existing_binding.get("team_id"), existing_binding.get("slack_user_id")):
                raise ValueError(_ACCOUNT_CHANGED)
            binding: dict[str, ExternalBindingValue] = dict(existing_binding)
        else:
            binding = {"team_id": team_id, "slack_user_id": slack_user_id}
        problem = _watch_room_problem_from_state(
            ctx.target_room_state,
            ctx.owner_user_id,
            ctx.config,
            ctx.runtime_paths,
        )
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
        team_id, slack_user_id = rule.source.binding.get("team_id"), rule.source.binding.get("slack_user_id")
        if (
            not isinstance(team_id, str)
            or not team_id
            or not isinstance(slack_user_id, str)
            or not slack_user_id
            or team_id != event.data.get("team_id")
            or slack_user_id not in audience
            or slack_user_id == event.data.get("user_id")
        ):
            return False
        raw_mentioned = event.data.get("mentioned_user_ids", ())
        is_mention = isinstance(raw_mentioned, (list, tuple, set, frozenset)) and slack_user_id in raw_mentioned
        if bool(event.data.get("is_dm")):
            return rule.source.kind in ("message", "direct_message") or (rule.source.kind == "mention" and is_mention)
        channel_filter, event_channel = rule.source.match.get("channel_id"), event.data.get("channel_id")
        channel_ok = (
            channel_filter is None
            or (isinstance(channel_filter, str) and event_channel == channel_filter)
            or (isinstance(channel_filter, list) and event_channel in channel_filter)
        )
        return channel_ok and rule.source.kind in ("message", "mention") and (is_mention or has_thread_binding)

    def dry_run_audience(self, rule: ReactiveTriggerRecord) -> object:
        """Synthesize a single-owner audience for dry-run ``triggers(action="test")``."""
        if isinstance(rule.source, ExternalEventSourceSpec):
            uid = rule.source.binding.get("slack_user_id")
            if isinstance(uid, str) and uid:
                return frozenset({uid})
        return frozenset()

    async def check_delivery_access(
        self,
        ctx: EventSourceContext,
        *,
        rule: ReactiveTriggerRecord,
        event: TriggerEvent | None = None,
    ) -> DispatchResult | None:
        """Re-verify app token, personal Slack config, verified OAuth identity, and private target room."""
        del event
        credentials_manager = ctx.credentials_manager or get_runtime_credentials_manager(ctx.runtime_paths)
        app = await asyncio.to_thread(load_slack_app, credentials_manager)
        if app is None or app.app_token is None:
            return DispatchResult(status="skipped", reason=_NEEDS_APP_TOKEN)
        if not has_personal_slack(ctx.config, rule.target.agent):
            return DispatchResult(status="skipped", reason=_AGENT_NO_PERSONAL_SLACK)
        credentials, _, _ = await _load_owner_credentials(
            ctx.config,
            ctx.runtime_paths,
            credentials_manager,
            rule.target.agent,
            rule.owner_user_id,
        )
        if credentials is None:
            return DispatchResult(status="skipped", reason=_SLACK_NOT_CONNECTED)
        if not isinstance(rule.source, ExternalEventSourceSpec):
            return DispatchResult(status="skipped", reason=_ACCOUNT_CHANGED)
        account = (oauth_verified_claim(credentials, "team_id"), oauth_verified_claim(credentials, "sub"))
        if account != (rule.source.binding.get("team_id"), rule.source.binding.get("slack_user_id")):
            return DispatchResult(status="skipped", reason=_ACCOUNT_CHANGED)
        problem = _watch_room_problem_from_state(
            ctx.target_room_state,
            rule.owner_user_id,
            ctx.config,
            ctx.runtime_paths,
        )
        return DispatchResult(status="skipped", reason=problem) if problem is not None else None

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
        if _watch_room_problem_from_state(ctx.calling_room_state, rule.owner_user_id, ctx.config, ctx.runtime_paths):
            return False
        return await self.check_delivery_access(ctx, rule=rule, event=event) is None


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
        for authorization in json_objects(body, "authorizations"):
            user_id = authorization.get("user_id")
            if isinstance(user_id, str) and not authorization.get("is_bot"):
                people.add(user_id)
        cursor = next_cursor(body)
        if cursor is None:
            return frozenset(people)


async def _author_display_name(attempt: SlackAttempt) -> str:
    """Resolve the Slack author's display name, failing soft to the raw Slack user ID."""
    try:
        return (await event_author(attempt)).display_name
    except (SlackApiError, httpx.HTTPError) as exc:
        logger.warning(
            "Slack author lookup failed for watch event",
            event_id=attempt.event.event_id,
            channel_id=attempt.event.channel_id,
            reason=failure_reason(exc),
        )
        return attempt.event.user_id


def _bound_slack_user_id(rule: ReactiveTriggerRecord) -> str | None:
    if isinstance(rule.source, ExternalEventSourceSpec):
        uid = rule.source.binding.get("slack_user_id")
        if isinstance(uid, str) and uid:
            return uid
    return None


async def _build_watch_trigger_event(
    attempt: SlackAttempt,
    *,
    is_dm: bool,
    mentioned: frozenset[str],
    thread_key: str,
    dedupe_key: str,
) -> TriggerEvent:
    event = attempt.event
    author_name = await _author_display_name(attempt)
    channel_label = "" if is_dm else await conversation_label(attempt)
    plain_text = slack_plain_text(event.text, {attempt.identity.bot_user_id: attempt.identity.bot_name})
    if is_dm:
        dm_label = "group direct message" if event.channel_type == "mpim" else "direct message"
        title, event_kind = f"Slack {dm_label} from {author_name}", "direct_message"
    else:
        title = f"Slack message from {author_name} in {channel_label}"
        event_kind = "mention" if mentioned else "message"
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
            "author_display_name": author_name,
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
    registered: EventSource,
    *,
    is_dm: bool,
    mentioned: frozenset[str],
    thread_key: str,
    dedupe_key: str,
) -> list[ReactiveTriggerRecord]:
    now = time.time()
    rules = await asyncio.to_thread(
        store.list_triggers,
        include_disabled=False,
        source_type="external_event",
        source_id=SLACK_EVENT_SOURCE_ID,
    )
    active_rules = [rule for rule in rules if rule.expires_at is None or rule.expires_at > now]
    candidate_audience = frozenset(uid for rule in active_rules if (uid := _bound_slack_user_id(rule)) is not None)
    if not active_rules or not candidate_audience:
        return []
    event = attempt.event
    bound_ids = (
        await asyncio.to_thread(store.get_bound_thread_keys, [r.trigger_id for r in active_rules], thread_key, now=now)
        if not is_dm and event.thread_ts is not None
        else frozenset()
    )
    prefilter_event = TriggerEvent(
        source_type="external_event",
        source_id=SLACK_EVENT_SOURCE_ID,
        kind="direct_message" if is_dm else "message",
        event_id=dedupe_key,
        timestamp=float(attempt.now),
        thread_key=thread_key,
        data={
            "team_id": event.team_id,
            "channel_id": event.channel_id,
            "channel_type": event.channel_type,
            "user_id": event.user_id,
            "is_dm": is_dm,
            "mentioned_user_ids": sorted(mentioned),
        },
    )
    return [
        rule
        for rule in active_rules
        if registered.matches_rule(
            rule,
            prefilter_event,
            audience=candidate_audience,
            has_thread_binding=rule.trigger_id in bound_ids,
        )
    ]


async def emit_slack_watch_event(
    attempt: SlackAttempt,
    reactive_triggers: ReactiveTriggerService | None,
) -> DeliveryResult:
    """Prefilter Slack watch rules, verify Slack event visibility, and emit onto ``ReactiveTriggerService``."""
    app_token, policy = attempt.app.app_token, attempt.config.external_trigger_policy
    registered = (
        reactive_triggers.event_sources.get(SLACK_EVENT_SOURCE_ID)
        if app_token is not None
        and reactive_triggers is not None
        and reactive_triggers.store is not None
        and policy.enabled
        and policy.reactive_enabled
        else None
    )
    if registered is None or reactive_triggers is None or reactive_triggers.store is None or app_token is None:
        return "ignored"

    event = attempt.event
    is_dm, mentioned = is_direct_message(event), mentioned_user_ids(event.text)
    thread_key = event.channel_id if is_dm else f"{event.channel_id}:{event.thread_ts or event.ts}"
    dedupe_key = f"{event.team_id}:{event.channel_id}:{event.ts}"
    candidates = await _prefilter_watch_candidates(
        attempt,
        reactive_triggers.store,
        registered,
        is_dm=is_dm,
        mentioned=mentioned,
        thread_key=thread_key,
        dedupe_key=dedupe_key,
    )
    if not candidates:
        return "ignored"

    try:
        visible_to = await _visible_to(attempt, app_token)
    except (SlackApiError, httpx.HTTPError) as exc:
        return delivery_failed(exc, event_id=event.event_id, channel_id=event.channel_id)
    if not any((uid := _bound_slack_user_id(rule)) is not None and uid in visible_to for rule in candidates):
        return "ignored"

    trigger_event = await _build_watch_trigger_event(
        attempt,
        is_dm=is_dm,
        mentioned=mentioned,
        thread_key=thread_key,
        dedupe_key=dedupe_key,
    )
    enqueued = await reactive_triggers.emit_event(
        SLACK_EVENT_SOURCE_ID,
        trigger_event,
        audience=visible_to,
        dedupe_key=dedupe_key,
    )
    return "delivered" if enqueued > 0 else "ignored"


def _legacy_watch_trigger_id(watch_id: str) -> str:
    candidate = f"slack_watch_{watch_id}"
    if TRIGGER_ID_PATTERN.fullmatch(candidate) is not None:
        return candidate
    sanitized = _SAFE_ID_CHARS.sub("_", watch_id)[:52].strip("_") or "migrated"
    return f"slack_watch_{sanitized}"


def _convert_legacy_watch(
    watch_id: str,
    fields: Mapping[str, Any],
    *,
    max_fires: int,
    now: float,
    replay_store: DeliveryReplayStore,
) -> tuple[ReactiveTriggerRecord, list[tuple[str, str, str, float]]] | None:
    mentions, direct_messages = bool(fields.get("mentions")), bool(fields.get("direct_messages"))
    if mentions and direct_messages:
        kind = "message"
    elif mentions:
        kind = "mention"
    elif direct_messages:
        kind = "direct_message"
    else:
        return None
    channels = list(
        dict.fromkeys(ch.strip() for ch in fields.get("channels") or () if isinstance(ch, str) and ch.strip()),
    )
    match: dict[str, ExternalMatchValue] = (
        {"channel_id": channels[0] if len(channels) == 1 else channels} if channels and kind != "direct_message" else {}
    )
    trigger_id, room_id = _legacy_watch_trigger_id(watch_id), str(fields["room_id"])
    record = ReactiveTriggerRecord(
        trigger_id=trigger_id,
        owner_user_id=str(fields["owner_id"]),
        description="Migrated Slack watch",
        source=ExternalEventSourceSpec(
            source_id=SLACK_EVENT_SOURCE_ID,
            kind=kind,
            match=match,
            binding={"team_id": str(fields["team_id"]), "slack_user_id": str(fields["slack_user_id"])},
        ),
        target=TriggerTargetSpec(
            agent=str(fields["agent_name"]),
            room_id=room_id,
            new_thread=True,
            thread_key_from_event=True,
            reopen_on_fire=True,
            prompt=_MIGRATED_WATCH_PROMPT,
        ),
        once=False,
        max_fires_per_hour=max_fires,
        created_at=float(fields.get("created_at", now)),
        updated_at=now,
    )
    thread_keys: list[tuple[str, str, str, float]] = []
    prefix = f"{watch_id}:"
    try:
        bound_roots = replay_store.list_bound_thread_roots(f"slack:watch:{watch_id}", now=int(now))
    except DeliveryReplayStoreError:
        logger.warning("Skipping corrupt legacy Slack watch replay scope", watch_id=watch_id, exc_info=True)
        return record, thread_keys
    for raw_key, (entry_room_id, thread_root_id, expires_at) in bound_roots.items():
        if (
            entry_room_id == room_id
            and expires_at > now
            and raw_key.startswith(prefix)
            and (stripped_key := raw_key.removeprefix(prefix))
        ):
            updated_at = min(now, float(expires_at - _LEGACY_THREAD_TTL_SECONDS))
            thread_keys.append((trigger_id, stripped_key, thread_root_id, updated_at))
    return record, thread_keys


# LEGACY_COMPAT: One-time migration of `<control_state>/slack/watches.json` and live replay watch thread roots.
# Legacy format: `watches.json` with `{"watches": {<id>: {owner_id, agent_name, room_id, team_id, slack_user_id,
#   mentions, direct_messages, channels, created_at}}}` plus `slack/replay` entries under scope `slack:watch:<id>`.
# Last legacy release: v2026.11; replacement: `reactive_triggers.db` rules with `source_id="slack"`.
# Handling: Atomically import into SQLite once under `watches.json.lock` with marker `slack_watches_v1`, then archive
#   `watches.json` to `watches.json.migrated` only after commit.
# Coverage: tests/slack/test_event_source.py::test_migrate_legacy_slack_watches_and_replay_roots_once.
def migrate_legacy_slack_watches(runtime_paths: RuntimePaths, store: ReactiveTriggerStore, config: Config) -> int:
    """Import legacy ``watches.json`` rules and unexpired replay watch thread roots once, then archive the file."""
    control_state_root = runtime_paths.control_state_root
    if control_state_root is None or store.has_migration(_LEGACY_MIGRATION_NAME):
        return 0
    watches_path = control_state_root / "slack" / "watches.json"
    if not watches_path.exists():
        return 0

    with advisory_file_lock(watches_path.with_name("watches.json.lock")):
        if not watches_path.exists() or store.has_migration(_LEGACY_MIGRATION_NAME):
            return 0
        stored = json.loads(watches_path.read_text(encoding="utf-8"))
        raw_watches = stored.get("watches") if isinstance(stored, dict) else None
        if not isinstance(raw_watches, dict):
            msg = "Legacy Slack watches.json has invalid structure"
            raise TypeError(msg)

        now = time.time()
        max_fires = max(1, min(60, config.external_trigger_policy.max_fires_per_hour))
        replay_store = slack_replay_store(control_state_root)
        records: list[ReactiveTriggerRecord] = []
        thread_keys: list[tuple[str, str, str, float]] = []
        for watch_id, fields in raw_watches.items():
            if not isinstance(watch_id, str) or not isinstance(fields, dict):
                continue
            converted = _convert_legacy_watch(watch_id, fields, max_fires=max_fires, now=now, replay_store=replay_store)
            if converted is not None:
                records.append(converted[0])
                thread_keys.extend(converted[1])

        imported = store.import_triggers_once(_LEGACY_MIGRATION_NAME, records, thread_keys, now=now)
        watches_path.replace(watches_path.with_name("watches.json.migrated"))
        return imported
