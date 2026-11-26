"""Local-only unified `triggers` toolkit for reactive triggers and direct signed webhooks."""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from functools import partial
from typing import TYPE_CHECKING, Any, ClassVar, Literal

import nio

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.authorization import (
    ReplyMembershipPendingError,
    is_sender_allowed_for_agent_reply_in_room,
)
from mindroom.background_tasks import run_blocking_until_complete
from mindroom.config.external_trigger_policy import (
    Ed25519EventSourceConfig,
    GitHubEventSourceConfig,
    HmacEventSourceConfig,
)
from mindroom.custom_tools.attachment_helpers import explicit_thread_root
from mindroom.custom_tools.external_trigger_context import (
    ExternalTriggerContextError,
    require_external_trigger_owner_context,
)
from mindroom.custom_tools.tool_payloads import custom_tool_payload
from mindroom.entity_resolution import configured_routable_entity_names_for_room, names_room
from mindroom.entity_rooms import get_rooms_for_entity
from mindroom.external_triggers.policy import is_external_trigger_administrator, is_source_subscriber_allowed
from mindroom.external_triggers.store import (
    ExternalTriggerRecord,
    ExternalTriggerStore,
    ExternalTriggerStoreError,
    ExternalTriggerTarget,
)
from mindroom.logging_config import get_logger
from mindroom.matrix.room_reconciliation import read_room_state
from mindroom.matrix.state import resolve_room_id
from mindroom.reactive_triggers import github_pr as ghpr
from mindroom.reactive_triggers.external_sources import (
    check_external_event_source_access,
    validate_subscription_against_source,
)
from mindroom.reactive_triggers.management import (
    delete_trigger_sync,
    get_trigger_record,
    is_trigger_owner,
    read_thread_tag_present,
    refresh_reactive_state_on_reenable,
    set_trigger_enabled_sync,
)
from mindroom.reactive_triggers.matching import matches_external_event, matches_room_message
from mindroom.reactive_triggers.models import (
    TRIGGER_ID_PATTERN,
    ExternalEventSourceSpec,
    FiringRecord,
    GitHubPRCheckpoint,
    GitHubPREventType,
    GitHubPRSourceSpec,
    ReactiveTriggerRecord,
    RoomCursor,
    RoomMessageSourceSpec,
    ThreadCheckpoint,
    ThreadTagSourceSpec,
    TriageSpec,
    TriggerEvent,
    TriggerSourceSpec,
    TriggerTargetSpec,
    generate_trigger_id,
)
from mindroom.reactive_triggers.store import (
    ReactiveTriggerDuplicateError,
    ReactiveTriggerStore,
    ReactiveTriggerStoreError,
    trigger_id_creation_lock,
)
from mindroom.reactive_triggers.triage import evaluate_triage
from mindroom.requester_identity import (
    equivalent_requester_ids,
    is_human_requester_id,
    managed_entity_user_ids,
)
from mindroom.thread_tags import ThreadTagsError, normalize_tag_name
from mindroom.tool_system.authoring import Toolkit

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.bot import AgentBot
    from mindroom.config.external_trigger_policy import ExternalTriggerPolicyConfig
    from mindroom.matrix.conversation_reads import ConversationReader
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

logger = get_logger(__name__)

_MAX_EXPIRES_IN_HOURS = 8760.0
_MAX_COOLDOWN_SECONDS = 86400
_DEFAULT_HISTORY_LIMIT = 20
_MAX_HISTORY_LIMIT = 100
_HISTORY_TEXT_PREVIEW_CHARS = 240
_TOOL_ERRORS = (
    ExternalTriggerContextError,
    ExternalTriggerStoreError,
    ReactiveTriggerStoreError,
    ThreadTagsError,
    TypeError,
    ValueError,
)


_UPDATABLE_FIELDS: tuple[str, ...] = (
    "prompt",
    "description",
    "pattern",
    "senders",
    "include_bots",
    "match",
    "triage_instructions",
    "triage_model",
    "triage_provider",
    "triage_threshold",
    "cooldown_seconds",
    "max_fires_per_hour",
    "expires_in_hours",
    "reopen_on_fire",
    "once",
    "enabled",
)
_COMMON_REACTIVE_PARAMS: frozenset[str] = frozenset(
    {
        "trigger_id",
        "prompt",
        "description",
        "target_agent",
        "target_room_id",
        "target_thread_id",
        "new_thread",
        "thread_key",
        "reopen_on_fire",
        "once",
        "max_fires_per_hour",
        "cooldown_seconds",
        "expires_in_hours",
    },
)
_TRIAGE_PARAMS: frozenset[str] = frozenset(
    {
        "triage_instructions",
        "triage_model",
        "triage_provider",
        "triage_threshold",
    },
)
_ACTION_ALLOWED_PARAMS: dict[str, frozenset[str]] = {
    "watch_room": _COMMON_REACTIVE_PARAMS
    | _TRIAGE_PARAMS
    | frozenset({"room_id", "thread_id", "senders", "pattern", "include_bots"}),
    "watch_thread": _COMMON_REACTIVE_PARAMS | frozenset({"room_id", "thread_id", "tag", "transition"}),
    "subscribe_event": _COMMON_REACTIVE_PARAMS | _TRIAGE_PARAMS | frozenset({"source_id", "kind", "match"}),
    "create": frozenset(
        {
            "trigger_id",
            "public_key",
            "key_id",
            "description",
            "target_agent",
            "target_room_id",
            "target_thread_id",
            "new_thread",
            "reopen_on_fire",
            "allowed_kinds",
            "replay_window_seconds",
            "max_body_bytes",
        },
    ),
    "list": frozenset({"include_disabled"}),
    "update": frozenset({"trigger_id", *_UPDATABLE_FIELDS}),
    "set_enabled": frozenset({"trigger_id", "enabled"}),
    "delete": frozenset({"trigger_id"}),
    "rotate_key": frozenset({"trigger_id", "public_key", "key_id"}),
    "history": frozenset({"trigger_id", "limit"}),
    "test": frozenset({"trigger_id", "text", "actor", "kind", "sample_data"}),
}
_NON_NONE_PARAM_DEFAULTS: dict[str, object] = {
    "new_thread": False,
    "include_disabled": True,
}


type _TriggerParams = Mapping[str, Any]


class TriggersTools(Toolkit):
    """Manage reactive triggers and signed direct external triggers in the primary runtime."""

    _TOOL_NAME: ClassVar[str] = "triggers"
    _CONTEXT_LABEL: ClassVar[str] = "Triggers"

    def __init__(self) -> None:
        super().__init__(
            name=self._TOOL_NAME,
            tools=[self.triggers, self.watch_pr],
        )

    @classmethod
    def _payload(cls, status: str, **fields: object) -> str:
        return custom_tool_payload(cls._TOOL_NAME, status, **fields)

    @staticmethod
    def _is_admin(context: ToolRuntimeContext) -> bool:
        return is_external_trigger_administrator(
            context.config,
            context.runtime_paths,
            context.requester_id,
        )

    @staticmethod
    def _memberships(context: ToolRuntimeContext) -> AgentReplyMembershipIndex:
        if context.agent_reply_memberships is not None:
            return context.agent_reply_memberships
        if context.orchestrator is not None:
            return context.orchestrator.agent_reply_memberships
        return AgentReplyMembershipIndex()

    @staticmethod
    def _require_reactive_policy(context: ToolRuntimeContext) -> ExternalTriggerPolicyConfig:
        policy = context.config.external_trigger_policy
        if not policy.enabled:
            msg = "External triggers are disabled by configuration."
            raise ReactiveTriggerStoreError(msg)
        if not policy.reactive_enabled:
            msg = "Reactive triggers are disabled by configuration."
            raise ReactiveTriggerStoreError(msg)
        return policy

    @staticmethod
    def _validate_reactive_common(
        context: ToolRuntimeContext,
        args: _TriggerParams,
        *,
        policy: ExternalTriggerPolicyConfig,
        now: float,
        allow_triage: bool = True,
    ) -> tuple[str | None, TriageSpec | None, float | None, int, int]:
        max_fires = 10 if args["max_fires_per_hour"] is None else args["max_fires_per_hour"]
        cooldown = 0 if args["cooldown_seconds"] is None else args["cooldown_seconds"]
        validated_trigger_id = _validate_optional_trigger_id(args["trigger_id"])
        _validate_max_fires_per_hour(max_fires, policy=policy)
        _validate_cooldown_seconds(cooldown)
        expires_at = _validate_expires_in_hours(args["expires_in_hours"], now=now)
        triage = (
            _build_triage_spec(
                context,
                triage_instructions=args["triage_instructions"],
                triage_model=args["triage_model"],
                triage_provider="llm" if args["triage_provider"] is None else args["triage_provider"],
                triage_threshold=args["triage_threshold"],
            )
            if allow_triage
            else None
        )
        return validated_trigger_id, triage, expires_at, max_fires, cooldown

    @classmethod
    async def _direct_target(cls, context: ToolRuntimeContext, args: _TriggerParams) -> ExternalTriggerTarget:
        target_agent = args["target_agent"]
        target_room_id = args["target_room_id"]
        target_thread_id = args["target_thread_id"]
        new_thread = args["new_thread"]
        reopen_on_fire = False if args["reopen_on_fire"] is None else args["reopen_on_fire"]
        is_admin = cls._is_admin(context)
        if not is_admin and target_agent not in (None, context.agent_name):
            msg = "Only external trigger admins can target a different agent or team."
            raise ExternalTriggerStoreError(msg)
        if new_thread and target_thread_id is not None:
            msg = "target_thread_id and new_thread are mutually exclusive"
            raise ValueError(msg)
        raw_room_id = target_room_id or context.room_id
        resolved_room_id = resolve_room_id(raw_room_id, context.runtime_paths)
        current_room_id = resolve_room_id(context.room_id, context.runtime_paths)
        if not is_admin and resolved_room_id != current_room_id:
            msg = "Only external trigger admins can target a different room."
            raise ExternalTriggerStoreError(msg)
        resolved_thread_id: str | None
        if new_thread or target_thread_id == "room":
            resolved_thread_id = None
        elif target_thread_id is None:
            resolved_thread_id = (
                (context.resolved_thread_id or context.thread_id) if resolved_room_id == current_room_id else None
            )
        else:
            if not target_thread_id.strip():
                msg = "thread_id must not be empty"
                raise ValueError(msg)
            resolved_thread_id = await explicit_thread_root(
                context,
                room_id=resolved_room_id,
                thread_id=target_thread_id,
            )
        return ExternalTriggerTarget(
            room_id=raw_room_id,
            thread_id=resolved_thread_id,
            agent=target_agent or context.agent_name,
            new_thread=new_thread,
            reopen_on_fire=reopen_on_fire,
        )

    @classmethod
    async def _resolve_reactive_target(
        cls,
        context: ToolRuntimeContext,
        args: _TriggerParams,
        *,
        prompt: str,
        default_thread_id: Callable[[str], str | None],
        allow_origin: bool,
    ) -> TriggerTargetSpec:
        target_agent = args["target_agent"]
        target_room_id = args["target_room_id"]
        target_thread_id = args["target_thread_id"]
        new_thread = args["new_thread"]
        thread_key = args["thread_key"]
        reopen_on_fire = False if args["reopen_on_fire"] is None else args["reopen_on_fire"]
        if new_thread and target_thread_id is not None:
            msg = "target_thread_id and new_thread are mutually exclusive"
            raise ValueError(msg)
        if target_thread_id == "origin" and not allow_origin:
            msg = "target_thread_id='origin' is only allowed for watch_room triggers"
            raise ValueError(msg)

        resolved_agent = (target_agent if target_agent is not None else context.agent_name).strip()
        if not resolved_agent or (
            resolved_agent not in context.config.agents and resolved_agent not in context.config.teams
        ):
            msg = f"Target references unknown agent or team: {resolved_agent or target_agent!r}"
            raise ReactiveTriggerStoreError(msg)

        raw_target_room = target_room_id or context.room_id
        if not names_room(context.config, raw_target_room, context.runtime_paths):
            msg = "Target room must be a room key MindRoom manages, that room's full alias, or a ! room ID."
            raise ReactiveTriggerStoreError(msg)
        resolved_target_room_id = resolve_room_id(raw_target_room, context.runtime_paths)
        current_room_id = resolve_room_id(context.room_id, context.runtime_paths)

        _require_target_room_configured_for_entity(
            context,
            agent_name=resolved_agent,
            raw_room_id=raw_target_room,
            resolved_room_id=resolved_target_room_id,
            current_room_id=current_room_id,
        )

        observed_room = _observed_room_for(context, resolved_target_room_id, preferred_agent=resolved_agent)
        memberships = cls._memberships(context)
        try:
            allowed = is_sender_allowed_for_agent_reply_in_room(
                context.requester_id,
                resolved_agent,
                context.config,
                resolved_target_room_id,
                context.runtime_paths,
                memberships,
                observed_room=observed_room,
            )
        except ReplyMembershipPendingError as exc:
            msg = f"Membership authorization for room {resolved_target_room_id} is still pending."
            raise ReactiveTriggerStoreError(msg) from exc
        if not allowed:
            msg = f"You are not authorized to target agent {resolved_agent!r} in room {resolved_target_room_id}."
            raise ReactiveTriggerStoreError(msg)

        await _verify_owner_joined_room(
            context,
            room_id=resolved_target_room_id,
            preferred_agent=resolved_agent,
        )

        resolved_thread_id: str | None
        if new_thread or target_thread_id == "room":
            resolved_thread_id = None
        elif target_thread_id is None:
            resolved_thread_id = default_thread_id(resolved_target_room_id)
        elif target_thread_id == "origin":
            resolved_thread_id = "origin"
        else:
            resolved_thread_id = await explicit_thread_root(
                context,
                room_id=resolved_target_room_id,
                thread_id=target_thread_id,
            )

        return TriggerTargetSpec(
            agent=resolved_agent,
            room_id=resolved_target_room_id,
            thread_id=resolved_thread_id,
            new_thread=new_thread,
            thread_key=thread_key,
            reopen_on_fire=reopen_on_fire,
            prompt=prompt,
        )

    @classmethod
    async def _verify_watched_room_access(
        cls,
        context: ToolRuntimeContext,
        *,
        raw_room_id: str,
        resolved_room_id: str,
        preferred_agent: str | None,
        require_live_membership: bool = False,
    ) -> None:
        if resolved_room_id == resolve_room_id(context.room_id, context.runtime_paths) and not require_live_membership:
            return
        await _verify_owner_joined_room(
            context,
            room_id=resolved_room_id,
            preferred_agent=preferred_agent,
            require_live=require_live_membership,
        )
        candidate_entities = _watch_room_candidate_entities(
            context,
            raw_room_id=raw_room_id,
            resolved_room_id=resolved_room_id,
        )
        if not candidate_entities:
            msg = f"No configured or running agent is available to watch room {resolved_room_id}."
            raise ReactiveTriggerStoreError(msg)

        observed_room = _observed_room_for(context, resolved_room_id, preferred_agent=preferred_agent)
        memberships = cls._memberships(context)
        for entity_name in candidate_entities:
            try:
                if is_sender_allowed_for_agent_reply_in_room(
                    context.requester_id,
                    entity_name,
                    context.config,
                    resolved_room_id,
                    context.runtime_paths,
                    memberships,
                    observed_room=observed_room,
                ):
                    return
            except ReplyMembershipPendingError:
                continue
        msg = f"You are not authorized to watch room {resolved_room_id}."
        raise ReactiveTriggerStoreError(msg)

    @classmethod
    async def _resolve_watched_room(
        cls,
        context: ToolRuntimeContext,
        *,
        room_id: str | None,
        preferred_agent: str | None,
    ) -> str:
        raw_watch_room = room_id or context.room_id
        if not names_room(context.config, raw_watch_room, context.runtime_paths):
            msg = "Watched room must be a room key MindRoom manages, that room's full alias, or a ! room ID."
            raise ReactiveTriggerStoreError(msg)
        resolved_watch_room_id = resolve_room_id(raw_watch_room, context.runtime_paths)
        await cls._verify_watched_room_access(
            context,
            raw_room_id=raw_watch_room,
            resolved_room_id=resolved_watch_room_id,
            preferred_agent=preferred_agent,
        )
        return resolved_watch_room_id

    @classmethod
    async def _has_source_access_for_history(
        cls,
        context: ToolRuntimeContext,
        rule: ReactiveTriggerRecord,
        *,
        room_access_cache: dict[str, bool],
    ) -> bool:
        if isinstance(rule.source, (RoomMessageSourceSpec, ThreadTagSourceSpec)):
            resolved_room_id = resolve_room_id(rule.source.room_id, context.runtime_paths)
            cached = room_access_cache.get(resolved_room_id)
            if cached is not None:
                return cached
            try:
                await cls._verify_watched_room_access(
                    context,
                    raw_room_id=rule.source.room_id,
                    resolved_room_id=resolved_room_id,
                    preferred_agent=rule.target.agent,
                    require_live_membership=True,
                )
            except ReactiveTriggerStoreError:
                room_access_cache[resolved_room_id] = False
                return False
            room_access_cache[resolved_room_id] = True
            return True

        if isinstance(rule.source, GitHubPRSourceSpec):
            return _gh_pr_allowed(context, context.requester_id, rule.source)
        cfg = context.config.external_trigger_policy.event_sources.get(rule.source.source_id)
        allowed = cfg is not None and is_source_subscriber_allowed(
            cfg,
            context.requester_id,
            context.config,
            context.runtime_paths,
        )
        if allowed and cfg is not None:
            try:
                validate_subscription_against_source(cfg, kind=rule.source.kind, match=rule.source.match)
            except ValueError:
                allowed = False
        return allowed

    @staticmethod
    def _firing_allowed_for_history(
        context: ToolRuntimeContext,
        rule: ReactiveTriggerRecord,
        firing: FiringRecord,
    ) -> bool:
        if firing.rule_uid != rule.rule_uid:
            return False
        if isinstance(rule.source, GitHubPRSourceSpec):
            return _gh_pr_allowed(context, rule.owner_user_id, rule.source)
        if not isinstance(rule.source, ExternalEventSourceSpec):
            return True
        return (
            check_external_event_source_access(
                owner_user_id=rule.owner_user_id,
                source_spec=rule.source,
                event=firing.event,
                config=context.config,
                runtime_paths=context.runtime_paths,
            )
            is None
        )

    @staticmethod
    def _record_payload(record: ExternalTriggerRecord) -> dict[str, object]:
        return {
            "trigger_id": record.trigger_id,
            "trigger_kind": "direct",
            "enabled": record.enabled,
            "auth": record.auth,
            "delivery_mode": record.delivery_mode,
            "description": record.description,
            "owner_user_id": record.owner_user_id,
            "target": record.target.model_dump(mode="json"),
            "key_id": record.key_id,
            "public_key_fingerprint": record.public_key_fingerprint,
            "allowed_kinds": list(record.allowed_kinds),
            "replay_window_seconds": record.replay_window_seconds,
            "max_body_bytes": record.max_body_bytes,
            "version": record.version,
            "auth_epoch": record.auth_epoch,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "last_fired_at": record.last_fired_at,
        }

    @staticmethod
    def _reactive_record_payload(record: ReactiveTriggerRecord) -> dict[str, object]:
        return {"trigger_kind": "reactive", **record.model_dump(mode="json", exclude={"rule_uid"})}

    @staticmethod
    def _notify_reactive_service(context: ToolRuntimeContext) -> None:
        if context.orchestrator is not None:
            context.orchestrator.reactive_triggers.notify_work()

    @classmethod
    def _require_reactive_owner_or_admin(
        cls,
        context: ToolRuntimeContext,
        record: ReactiveTriggerRecord,
        *,
        verb: str = "changed",
    ) -> None:
        if not (
            is_trigger_owner(context.requester_id, record.owner_user_id, context.config, context.runtime_paths)
            or cls._is_admin(context)
        ):
            msg = f"trigger can only be {verb} by its owner or an external trigger admin"
            raise ReactiveTriggerStoreError(msg)

    async def _create_reactive_rule(
        self,
        context: ToolRuntimeContext,
        args: _TriggerParams,
        *,
        policy: ExternalTriggerPolicyConfig,
        common: tuple[str | None, TriageSpec | None, float | None, int, int],
        source: TriggerSourceSpec,
        target: TriggerTargetSpec,
        default_once: bool,
        now: float,
        initial_cursor: RoomCursor | None = None,
        initial_checkpoint: ThreadCheckpoint | None = None,
        initial_github_pr_checkpoint: GitHubPRCheckpoint | None = None,
        max_github_pr_per_owner: int | None = None,
    ) -> ReactiveTriggerRecord:
        validated_trigger_id, triage, expires_at, max_fires_per_hour, cooldown_seconds = common
        external_store = ExternalTriggerStore(context.runtime_paths)
        reactive_store = ReactiveTriggerStore(context.runtime_paths)

        def _create() -> ReactiveTriggerRecord:
            with trigger_id_creation_lock(context.runtime_paths):
                resolved_trigger_id = _resolve_unique_reactive_trigger_id(
                    validated_trigger_id,
                    external_store=external_store,
                    reactive_store=reactive_store,
                )
                record = ReactiveTriggerRecord(
                    trigger_id=resolved_trigger_id,
                    owner_user_id=context.requester_id,
                    description="" if args["description"] is None else args["description"],
                    source=source,
                    triage=triage,
                    target=target,
                    once=default_once if args["once"] is None else args["once"],
                    max_fires_per_hour=max_fires_per_hour,
                    cooldown_seconds=cooldown_seconds,
                    expires_at=expires_at,
                    created_at=now,
                    updated_at=now,
                )
                return reactive_store.create_trigger(
                    record,
                    initial_cursor=initial_cursor,
                    initial_checkpoint=initial_checkpoint,
                    initial_github_pr_checkpoint=initial_github_pr_checkpoint,
                    max_per_owner=policy.max_reactive_triggers_per_owner,
                    max_github_pr_per_owner=max_github_pr_per_owner,
                )

        created = await run_blocking_until_complete(_create)
        self._notify_reactive_service(context)
        return created

    async def watch_pr(
        self,
        repo: str,
        number: int,
        events: list[Literal["activity", "ci", "merge", "close"]] | None = None,
        prompt: str | None = None,
        description: str | None = None,
        ignore_actors: list[str] | None = None,
        ignore_bots: bool = True,
        auto_disable_on_close: bool = True,
        reopen_on_fire: bool = True,
        poll_interval_seconds: int | None = None,
        target_agent: str | None = None,
        target_room_id: str | None = None,
        target_thread_id: str | None = None,
        new_thread: bool = False,
        once: bool = False,
        expires_in_hours: float | None = 720.0,
        trigger_id: str | None = None,
    ) -> str:
        """Watch one open GitHub PR for activity, CI failures/all-green, merge, or close.

        Uses ETag-backed GitHub REST polling (nudged immediately by an authorized shared GitHub webhook source covering
        `repo` with zero idle polling). Auto-reopens resolved target threads (`reopen_on_fire=True`) and auto-disables
        on PR merge/close.
        """
        try:
            ctx = require_external_trigger_owner_context(self._CONTEXT_LABEL)
            pol, now = self._require_reactive_policy(ctx), time.time()
            tid, exp = _validate_optional_trigger_id(trigger_id), _validate_expires_in_hours(expires_in_hours, now=now)
            if isinstance(poll_interval_seconds, bool):
                msg = "poll_interval_seconds must be an integer."
                raise TypeError(msg)
            raw_p = 300 if poll_interval_seconds is None else poll_interval_seconds
            poll_s = max(pol.min_github_pr_poll_interval_seconds, min(raw_p, 86400))
            evs: list[GitHubPREventType] = ["activity", "ci", "merge", "close"] if events is None else list(events)
            sd: dict[str, Any] = {"repo": repo, "number": number, "events": evs, "ignore_bots": ignore_bots}
            sd |= {"ignore_actors": list(ignore_actors or ()), "auto_disable_on_close": auto_disable_on_close}
            src = GitHubPRSourceSpec.model_validate({**sd, "poll_interval_seconds": poll_s})
            find_wh = ghpr.find_covering_github_webhook_source
            wh_id = find_wh(ctx.requester_id, src.repo, src.events, ctx.config, ctx.runtime_paths)
            src = src.model_copy(update={"webhook_source_id": wh_id})
            p_def = f"GitHub PR {src.repo}#{src.number} had new activity. Inspect the event data below and take any needed action."
            p_text = p_def if prompt is None else _require_non_empty_arg(prompt, "prompt", "watch_pr")
            desc = f"Watch PR {src.repo}#{src.number}" if description is None else description
            ta: dict[str, Any] = {"target_agent": target_agent, "target_room_id": target_room_id, "once": once}
            ta |= {"target_thread_id": target_thread_id, "new_thread": new_thread, "thread_key": None}
            ta |= {"reopen_on_fire": reopen_on_fire, "description": desc}
            rm_id, th_id = resolve_room_id(ctx.room_id, ctx.runtime_paths), ctx.resolved_thread_id or ctx.thread_id

            def _dth(d: str) -> str | None:
                return th_id if d == rm_id else None

            fn = self._resolve_reactive_target
            target = await fn(ctx, ta, prompt=p_text, default_thread_id=_dth, allow_origin=False)
            if wh_id is None and not pol.github_pr_polling_enabled:
                msg = f"GitHub PR polling is disabled and no webhook source covers {src.repo}."
                raise ReactiveTriggerStoreError(msg)
            seeded = await ghpr.seed_github_pr_watch(
                src.repo,
                src.number,
                src.events,
                owner_user_id=ctx.requester_id,
                config=ctx.config,
                runtime_paths=ctx.runtime_paths,
                poll_interval_seconds=poll_s,
                webhook_source_id=wh_id,
                now=now,
            )
            created = await self._create_reactive_rule(
                ctx,
                ta,
                policy=pol,
                common=(tid, None, exp, min(10, pol.max_fires_per_hour), 0),
                source=src,
                target=target,
                default_once=False,
                now=now,
                initial_github_pr_checkpoint=seeded.checkpoint,
                max_github_pr_per_owner=pol.max_github_pr_watches_per_owner,
            )
            out: dict[str, Any] = {"action": "watch_pr", "trigger": self._reactive_record_payload(created)}
            out |= {"delivery_mode": "webhook" if wh_id is not None else "poll", "webhook_source_id": wh_id}
            cp = seeded.checkpoint
            out |= {"auth_mode": seeded.auth_mode, "pr_state": cp.pr_state, "head_sha": cp.head_sha}
            return self._payload("ok", **out)
        except _TOOL_ERRORS as exc:
            return self._payload("error", message=str(exc))

    async def triggers(
        self,
        action: Literal[
            "watch_room",
            "watch_thread",
            "subscribe_event",
            "create",
            "list",
            "update",
            "set_enabled",
            "delete",
            "rotate_key",
            "history",
            "test",
        ],
        trigger_id: str | None = None,
        prompt: str | None = None,
        description: str | None = None,
        room_id: str | None = None,
        thread_id: str | None = None,
        senders: list[str] | None = None,
        pattern: str | None = None,
        include_bots: bool | None = None,
        tag: str | None = None,
        transition: Literal["added", "removed"] | None = None,
        source_id: str | None = None,
        kind: str | None = None,
        match: dict[str, str | bool | int] | None = None,
        triage_instructions: str | None = None,
        triage_model: str | None = None,
        triage_provider: Literal["llm", "typesafe", "openai_decisions"] | None = None,
        triage_threshold: float | None = None,
        target_agent: str | None = None,
        target_room_id: str | None = None,
        target_thread_id: str | None = None,
        new_thread: bool = False,
        thread_key: str | None = None,
        reopen_on_fire: bool | None = None,
        once: bool | None = None,
        max_fires_per_hour: int | None = None,
        cooldown_seconds: int | None = None,
        expires_in_hours: float | None = None,
        enabled: bool | None = None,
        include_disabled: bool = True,
        public_key: str | None = None,
        key_id: str | None = None,
        allowed_kinds: list[str] | None = None,
        replay_window_seconds: int | None = None,
        max_body_bytes: int | None = None,
        limit: int | None = None,
        text: str | None = None,
        actor: str | None = None,
        sample_data: dict[str, Any] | None = None,
    ) -> str:
        """Manage reactive triggers (room messages, thread tags, event sources, optional triage) and signed Ed25519 webhooks (`/api/triggers/{trigger_id}`).

        Actions (key params):
        - watch_room (`prompt`): watch room messages (filters: `room_id`, `thread_id`, `senders`, case-insensitive regex `pattern`, `include_bots`=False never self; default `once`=False).
        - watch_thread (`prompt`): watch thread tag change (`room_id`, `thread_id`, `tag`='resolved', `transition`='added'|'removed'; default `once`=True).
        - subscribe_event (`source_id` from `list`, `kind`, `prompt`): subscribe to event source (`match` exact event.data filter with dot-paths; default `once`=True).
        - create (`trigger_id`, `public_key`): direct Ed25519 webhook (raw base64/OpenSSH/PEM; optional `key_id`='default', `allowed_kinds`, `replay_window_seconds`, `max_body_bytes`).
        - list (optional `include_disabled`=True): list triggers and authorized `event_sources`.
        - update (`trigger_id`): patch reactive rule (`''` clears `pattern`/`triage_instructions`; `[]` clears `senders`).
        - set_enabled (`trigger_id`; default `enabled`=False), delete (`trigger_id`), rotate_key (`trigger_id`, `public_key`, optional `key_id`).
        - history (optional `trigger_id`, `limit` 1..100 default 20): recent firings.
        - test (`trigger_id`): dry-run matching/triage with sample `text`, `actor`, `kind`, `sample_data`.

        Delivery & limits: `trigger_id` auto-generates on reactive create. `room_id`/`target_room_id`/`target_agent` default to current. `target_thread_id` accepts `'room'` (room timeline) or `'origin'` (same-room watch_room); `new_thread=True` opens a fresh thread (grouped by `thread_key`); `reopen_on_fire=True` clears `resolved` before delivery. Caps: `once`, `max_fires_per_hour` (default 10), `cooldown_seconds` (default 0), `expires_in_hours` (<= 8760).
        Triage: `triage_instructions` (tool-less criteria), `triage_provider` (`llm`|`typesafe`|`openai_decisions`), `triage_model` (required for `llm`), `triage_threshold` (0..1).
        """
        args: _TriggerParams = {key: value for key, value in locals().items() if key not in {"self", "action"}}
        try:
            context = require_external_trigger_owner_context(self._CONTEXT_LABEL)
            handlers: dict[str, Callable[[ToolRuntimeContext, _TriggerParams], Awaitable[str]]] = {
                "watch_room": self._watch_room,
                "watch_thread": self._watch_thread,
                "subscribe_event": self._subscribe_event,
                "create": self._create_trigger,
                "list": self._list_triggers,
                "update": self._update_trigger,
                "set_enabled": self._set_enabled,
                "delete": self._delete_trigger,
                "rotate_key": self._rotate_key,
                "history": self._history,
                "test": self._test_trigger,
            }
            handler = handlers.get(action)
            if handler is None:
                return self._payload("error", message=f"Unsupported triggers action: {action!r}")
            supplied = {key for key, value in args.items() if value is not _NON_NONE_PARAM_DEFAULTS.get(key)}
            if unsupported := sorted(supplied - _ACTION_ALLOWED_PARAMS[action]):
                return self._payload(
                    "error",
                    message=f"Unsupported parameter(s) for triggers(action={action!r}): {', '.join(unsupported)}",
                )
            return await handler(context, args)
        except _TOOL_ERRORS as exc:
            return self._payload("error", message=str(exc))

    async def _watch_room(self, context: ToolRuntimeContext, args: _TriggerParams) -> str:
        resolved_prompt = _require_non_empty_arg(args["prompt"], "prompt", "watch_room")
        policy = self._require_reactive_policy(context)
        now = time.time()
        common = self._validate_reactive_common(context, args, policy=policy, now=now)
        resolved_watch_room_id = await self._resolve_watched_room(
            context,
            room_id=args["room_id"],
            preferred_agent=args["target_agent"] or context.agent_name,
        )
        resolved_watch_thread_id = (
            await explicit_thread_root(context, room_id=resolved_watch_room_id, thread_id=args["thread_id"])
            if args["thread_id"] is not None
            else None
        )
        target = await self._resolve_reactive_target(
            context,
            args,
            prompt=resolved_prompt,
            default_thread_id=lambda dest_room: "origin" if dest_room == resolved_watch_room_id else None,
            allow_origin=True,
        )
        source = RoomMessageSourceSpec(
            room_id=resolved_watch_room_id,
            thread_id=resolved_watch_thread_id,
            senders=list(args["senders"] or ()),
            pattern=args["pattern"],
            include_bots=False if args["include_bots"] is None else args["include_bots"],
        )
        initial_cursor = await _read_initial_room_cursor(
            context,
            room_id=resolved_watch_room_id,
            preferred_agent=target.agent,
            now=now,
        )
        created = await self._create_reactive_rule(
            context,
            args,
            policy=policy,
            common=common,
            source=source,
            target=target,
            default_once=False,
            now=now,
            initial_cursor=initial_cursor,
        )
        return self._payload("ok", action="watch_room", trigger=self._reactive_record_payload(created))

    async def _watch_thread(self, context: ToolRuntimeContext, args: _TriggerParams) -> str:
        resolved_prompt = _require_non_empty_arg(args["prompt"], "prompt", "watch_thread")
        policy = self._require_reactive_policy(context)
        now = time.time()
        common = self._validate_reactive_common(context, args, policy=policy, now=now, allow_triage=False)
        normalized_tag = _normalize_watch_thread_tag("resolved" if args["tag"] is None else args["tag"])
        resolved_transition: Literal["added", "removed"] = "added" if args["transition"] is None else args["transition"]
        resolved_watch_room_id = await self._resolve_watched_room(
            context,
            room_id=args["room_id"],
            preferred_agent=args["target_agent"] or context.agent_name,
        )
        current_room_id = resolve_room_id(context.room_id, context.runtime_paths)
        raw_watch_thread_id = args["thread_id"]
        if raw_watch_thread_id is None and resolved_watch_room_id == current_room_id:
            raw_watch_thread_id = context.resolved_thread_id or context.thread_id
        if raw_watch_thread_id is None or not raw_watch_thread_id.strip():
            msg = "thread_id is required for watch_thread when not called inside a thread in the watched room."
            raise ValueError(msg)
        resolved_watch_thread_id = await explicit_thread_root(
            context,
            room_id=resolved_watch_room_id,
            thread_id=raw_watch_thread_id,
        )
        current_thread_id = context.resolved_thread_id or context.thread_id
        target = await self._resolve_reactive_target(
            context,
            args,
            prompt=resolved_prompt,
            default_thread_id=lambda dest_room: current_thread_id if dest_room == current_room_id else None,
            allow_origin=False,
        )
        source = ThreadTagSourceSpec(
            room_id=resolved_watch_room_id,
            thread_id=resolved_watch_thread_id,
            tag=normalized_tag,
            transition=resolved_transition,
        )
        tag_present = await _read_thread_tag_present(context, source, preferred_agent=target.agent)
        initial_checkpoint = ThreadCheckpoint(tag_present=tag_present, state_event_id=None, updated_at=now)
        already_in_state = (source.transition == "added" and tag_present) or (
            source.transition == "removed" and not tag_present
        )
        created = await self._create_reactive_rule(
            context,
            args,
            policy=policy,
            common=common,
            source=source,
            target=target,
            default_once=True,
            now=now,
            initial_checkpoint=initial_checkpoint,
        )
        extra: dict[str, object] = {"already_in_state": already_in_state}
        if already_in_state:
            extra["note"] = "Thread already has this state; trigger will fire only on a future transition."
        return self._payload(
            "ok",
            action="watch_thread",
            trigger=self._reactive_record_payload(created),
            **extra,
        )

    async def _subscribe_event(self, context: ToolRuntimeContext, args: _TriggerParams) -> str:
        resolved_source_id = _require_non_empty_arg(args["source_id"], "source_id", "subscribe_event")
        resolved_kind = _require_non_empty_arg(args["kind"], "kind", "subscribe_event")
        resolved_prompt = _require_non_empty_arg(args["prompt"], "prompt", "subscribe_event")
        policy = self._require_reactive_policy(context)
        now = time.time()
        common = self._validate_reactive_common(context, args, policy=policy, now=now)
        source_config = policy.event_sources.get(resolved_source_id)
        if source_config is None or not is_source_subscriber_allowed(
            source_config,
            context.requester_id,
            context.config,
            context.runtime_paths,
        ):
            allowed_source_ids = sorted(
                sid
                for sid, cfg in policy.event_sources.items()
                if is_source_subscriber_allowed(cfg, context.requester_id, context.config, context.runtime_paths)
            )
            if allowed_source_ids:
                msg = (
                    f"Event source {resolved_source_id!r} is unavailable. "
                    f"Available event sources: {', '.join(allowed_source_ids)}"
                )
            else:
                msg = f"Event source {resolved_source_id!r} is unavailable. No event sources are available to you."
            raise ValueError(msg)

        source_spec = ExternalEventSourceSpec(
            source_id=resolved_source_id,
            kind=resolved_kind,
            match=dict(args["match"]) if args["match"] is not None else {},
        )
        validate_subscription_against_source(source_config, kind=source_spec.kind, match=source_spec.match)
        current_room_id = resolve_room_id(context.room_id, context.runtime_paths)
        current_thread_id = context.resolved_thread_id or context.thread_id
        target = await self._resolve_reactive_target(
            context,
            args,
            prompt=resolved_prompt,
            default_thread_id=lambda dest_room: current_thread_id if dest_room == current_room_id else None,
            allow_origin=False,
        )
        created = await self._create_reactive_rule(
            context,
            args,
            policy=policy,
            common=common,
            source=source_spec,
            target=target,
            default_once=True,
            now=now,
        )
        return self._payload("ok", action="subscribe_event", trigger=self._reactive_record_payload(created))

    async def _create_trigger(self, context: ToolRuntimeContext, args: _TriggerParams) -> str:
        resolved_trigger_id = _require_non_empty_arg(args["trigger_id"], "trigger_id", "create")
        resolved_public_key = _require_non_empty_arg(args["public_key"], "public_key", "create")
        resolved_key_id = "default" if args["key_id"] is None else args["key_id"]
        external_store = ExternalTriggerStore(context.runtime_paths)
        reactive_store = ReactiveTriggerStore(context.runtime_paths)
        target = await self._direct_target(context, args)

        def _create_direct() -> ExternalTriggerRecord:
            with trigger_id_creation_lock(context.runtime_paths):
                if reactive_store.get_trigger(resolved_trigger_id) is not None:
                    msg = f"external trigger already exists: {resolved_trigger_id}"
                    raise ExternalTriggerStoreError(msg)
                return external_store.create_record(
                    trigger_id=resolved_trigger_id,
                    owner_user_id=context.requester_id,
                    created_by_agent_name=context.agent_name,
                    created_in_room_id=context.room_id,
                    created_in_thread_id=context.resolved_thread_id or context.thread_id,
                    target=target,
                    public_key=resolved_public_key,
                    key_id=resolved_key_id,
                    description="" if args["description"] is None else args["description"],
                    allowed_kinds=args["allowed_kinds"] or (),
                    replay_window_seconds=args["replay_window_seconds"],
                    max_body_bytes=args["max_body_bytes"],
                    config=context.config,
                )

        record = await run_blocking_until_complete(_create_direct)
        return self._payload(
            "ok",
            action="create",
            trigger=self._record_payload(record),
            endpoint_path=f"/api/triggers/{record.trigger_id}",
            public_key_fingerprint=record.public_key_fingerprint,
        )

    async def _list_triggers(self, context: ToolRuntimeContext, args: _TriggerParams) -> str:
        include_disabled = args["include_disabled"]

        def _list_records() -> str:
            external_store = ExternalTriggerStore(context.runtime_paths)
            reactive_store = ReactiveTriggerStore(context.runtime_paths)
            if self._is_admin(context):
                external_records = external_store.list_records(owner_user_id=None)
                reactive_records = reactive_store.list_triggers(owner_user_id=None, include_disabled=include_disabled)
            else:
                owner_ids = set(equivalent_requester_ids(context.requester_id, context.config, context.runtime_paths))
                external_records = [
                    record for record in external_store.list_records() if record.owner_user_id in owner_ids
                ]
                reactive_records = [
                    record
                    for record in reactive_store.list_triggers(include_disabled=include_disabled)
                    if record.owner_user_id in owner_ids
                ]
            if not include_disabled:
                external_records = [record for record in external_records if record.enabled]
            external_payloads = [self._record_payload(record) for record in external_records]
            reactive_payloads = [self._reactive_record_payload(record) for record in reactive_records]
            return self._payload(
                "ok",
                action="list",
                triggers=[*external_payloads, *reactive_payloads],
                external_triggers=external_payloads,
                reactive_triggers=reactive_payloads,
                event_sources=_visible_event_sources_payload(context),
            )

        return await run_blocking_until_complete(_list_records)

    async def _update_trigger(self, context: ToolRuntimeContext, args: _TriggerParams) -> str:
        resolved_trigger_id = _validate_update_args(args)
        external_store = ExternalTriggerStore(context.runtime_paths)
        reactive_store = ReactiveTriggerStore(context.runtime_paths)
        existing = await run_blocking_until_complete(
            partial(
                _load_reactive_or_error,
                external_store,
                reactive_store,
                resolved_trigger_id,
                direct_error="In-place update is only supported for reactive triggers.",
            ),
        )
        if not is_trigger_owner(context.requester_id, existing.owner_user_id, context.config, context.runtime_paths):
            msg = "Only the trigger owner can update a reactive trigger; administrators can use set_enabled or delete."
            raise ReactiveTriggerStoreError(msg)

        now = time.time()
        validated = _build_updated_reactive_record(context, existing=existing, args=args, now=now)
        reenabling = args["enabled"] is True and not existing.enabled
        refreshed_tag_present = await _read_reenable_tag_state(context, existing, enabling=reenabling)

        def _persist() -> ReactiveTriggerRecord:
            if reenabling:
                refresh_reactive_state_on_reenable(
                    reactive_store,
                    existing,
                    now=now,
                    refreshed_tag_present=refreshed_tag_present,
                )
            pol = context.config.external_trigger_policy
            updated = reactive_store.update_trigger(
                validated,
                expected_rule_uid=existing.rule_uid,
                update_enabled=args["enabled"] is not None,
                max_per_owner=pol.max_reactive_triggers_per_owner,
                max_github_pr_per_owner=pol.max_github_pr_watches_per_owner,
            )
            if updated is None:
                msg = f"Reactive trigger not found: {resolved_trigger_id}"
                raise ReactiveTriggerStoreError(msg)
            return updated

        updated = await run_blocking_until_complete(_persist)
        self._notify_reactive_service(context)
        return self._payload("ok", action="update", trigger=self._reactive_record_payload(updated))

    async def _set_enabled(self, context: ToolRuntimeContext, args: _TriggerParams) -> str:
        resolved_trigger_id = _require_non_empty_arg(args["trigger_id"], "trigger_id", "set_enabled")
        target_enabled = False if args["enabled"] is None else args["enabled"]
        refreshed_tag_present: bool | None = None
        if target_enabled:
            record = await run_blocking_until_complete(
                partial(get_trigger_record, context.runtime_paths, resolved_trigger_id),
            )
            if isinstance(record, ReactiveTriggerRecord) and not record.enabled:
                self._require_reactive_owner_or_admin(context, record)
                refreshed_tag_present = await _read_reenable_tag_state(context, record, enabling=True)
        updated = await run_blocking_until_complete(
            partial(
                set_trigger_enabled_sync,
                context.runtime_paths,
                context.config,
                resolved_trigger_id,
                enabled=target_enabled,
                actor_user_id=context.requester_id,
                refreshed_tag_present=refreshed_tag_present,
                authorize_reactive=partial(self._require_reactive_owner_or_admin, context),
            ),
        )
        if isinstance(updated, ExternalTriggerRecord):
            return self._payload("ok", action="set_enabled", trigger=self._record_payload(updated))
        if target_enabled:
            self._notify_reactive_service(context)
        return self._payload("ok", action="set_enabled", trigger=self._reactive_record_payload(updated))

    async def _delete_trigger(self, context: ToolRuntimeContext, args: _TriggerParams) -> str:
        resolved_trigger_id = _require_non_empty_arg(args["trigger_id"], "trigger_id", "delete")
        await run_blocking_until_complete(
            partial(
                delete_trigger_sync,
                context.runtime_paths,
                context.config,
                resolved_trigger_id,
                actor_user_id=context.requester_id,
                authorize_reactive=partial(self._require_reactive_owner_or_admin, context),
            ),
        )
        return self._payload("ok", action="delete", trigger_id=resolved_trigger_id)

    async def _rotate_key(self, context: ToolRuntimeContext, args: _TriggerParams) -> str:
        resolved_trigger_id = _require_non_empty_arg(args["trigger_id"], "trigger_id", "rotate_key")
        resolved_public_key = _require_non_empty_arg(args["public_key"], "public_key", "rotate_key")
        resolved_key_id = "default" if args["key_id"] is None else args["key_id"]

        def _rotate() -> str:
            if isinstance(get_trigger_record(context.runtime_paths, resolved_trigger_id), ReactiveTriggerRecord):
                msg = "Key rotation applies only to direct signed external triggers."
                raise ExternalTriggerStoreError(msg)
            record = ExternalTriggerStore(context.runtime_paths).rotate_key(
                resolved_trigger_id,
                public_key=resolved_public_key,
                key_id=resolved_key_id,
                actor_user_id=context.requester_id,
                config=context.config,
            )
            return self._payload(
                "ok",
                action="rotate_key",
                trigger=self._record_payload(record),
                public_key_fingerprint=record.public_key_fingerprint,
            )

        return await run_blocking_until_complete(_rotate)

    async def _history(self, context: ToolRuntimeContext, args: _TriggerParams) -> str:
        raw_limit = args["limit"]
        if raw_limit is None:
            clamped_limit = _DEFAULT_HISTORY_LIMIT
        elif isinstance(raw_limit, bool):
            msg = "limit must be an integer."
            raise ValueError(msg)
        else:
            clamped_limit = max(1, min(raw_limit, _MAX_HISTORY_LIMIT))

        external_store = ExternalTriggerStore(context.runtime_paths)
        reactive_store = ReactiveTriggerStore(context.runtime_paths)
        owner_ids = set(equivalent_requester_ids(context.requester_id, context.config, context.runtime_paths))
        is_admin = self._is_admin(context)
        room_access_cache: dict[str, bool] = {}

        if args["trigger_id"] is not None:
            stripped_id = args["trigger_id"].strip()
            if not stripped_id:
                msg = "trigger_id must be a non-empty string when provided."
                raise ValueError(msg)
            record = await run_blocking_until_complete(
                partial(
                    _load_reactive_or_error,
                    external_store,
                    reactive_store,
                    stripped_id,
                    direct_error="Firing history is only available for reactive triggers.",
                ),
            )
            self._require_reactive_owner_or_admin(context, record, verb="viewed")
            if not is_admin:
                if not await self._has_source_access_for_history(
                    context,
                    record,
                    room_access_cache=room_access_cache,
                ):
                    msg = f"You no longer have access to the source for trigger {record.trigger_id}."
                    raise ReactiveTriggerStoreError(msg)
                firings = await self._list_owner_history(
                    context,
                    reactive_store,
                    limit=clamped_limit,
                    room_access_cache=room_access_cache,
                    trigger_record=record,
                )
                serialized = [_firing_summary_payload(firing) for firing in firings]
            else:
                firings = await run_blocking_until_complete(
                    partial(reactive_store.list_firings, trigger_id=stripped_id, limit=clamped_limit),
                )
                firings = [firing for firing in firings if firing.rule_uid == record.rule_uid]
                owner_has_access = record.owner_user_id in owner_ids and await self._has_source_access_for_history(
                    context,
                    record,
                    room_access_cache=room_access_cache,
                )
                serialized = [
                    _firing_summary_payload(
                        firing,
                        include_event=owner_has_access and self._firing_allowed_for_history(context, record, firing),
                    )
                    for firing in firings
                ]
        elif is_admin:
            serialized = await self._list_admin_history(
                context,
                reactive_store,
                owner_ids=owner_ids,
                limit=clamped_limit,
                room_access_cache=room_access_cache,
            )
        else:
            firings = await self._list_owner_history(
                context,
                reactive_store,
                limit=clamped_limit,
                room_access_cache=room_access_cache,
            )
            serialized = [_firing_summary_payload(firing) for firing in firings]

        return self._payload("ok", action="history", firings=serialized, count=len(serialized))

    async def _list_admin_history(
        self,
        context: ToolRuntimeContext,
        reactive_store: ReactiveTriggerStore,
        *,
        owner_ids: set[str],
        limit: int,
        room_access_cache: dict[str, bool],
    ) -> list[dict[str, object]]:
        firings = await run_blocking_until_complete(
            partial(reactive_store.list_firings, limit=limit),
        )
        referenced_ids = {firing.trigger_id for firing in firings}
        owned_rules = (
            await run_blocking_until_complete(
                partial(reactive_store.list_triggers_with_firings, owner_user_ids=owner_ids),
            )
            if referenced_ids
            else []
        )
        allowed_owned_rules: dict[str, ReactiveTriggerRecord] = {}
        for rule in owned_rules:
            if rule.trigger_id in referenced_ids and await self._has_source_access_for_history(
                context,
                rule,
                room_access_cache=room_access_cache,
            ):
                allowed_owned_rules[rule.trigger_id] = rule
        return [
            _firing_summary_payload(
                firing,
                include_event=(
                    (owned_rule := allowed_owned_rules.get(firing.trigger_id)) is not None
                    and self._firing_allowed_for_history(context, owned_rule, firing)
                ),
            )
            for firing in firings
        ]

    async def _list_owner_history(
        self,
        context: ToolRuntimeContext,
        reactive_store: ReactiveTriggerStore,
        *,
        limit: int,
        room_access_cache: dict[str, bool],
        trigger_record: ReactiveTriggerRecord | None = None,
    ) -> list[FiringRecord]:
        owner_ids: set[str] | None = None
        if trigger_record is not None:
            allowed_rules = {trigger_record.trigger_id: trigger_record}
        else:
            resolved_owner_ids = equivalent_requester_ids(context.requester_id, context.config, context.runtime_paths)
            owner_ids = resolved_owner_ids
            candidate_rules = await run_blocking_until_complete(
                partial(reactive_store.list_triggers_with_firings, owner_user_ids=resolved_owner_ids),
            )
            allowed_rules = {}
            for candidate in candidate_rules:
                if await self._has_source_access_for_history(
                    context,
                    candidate,
                    room_access_cache=room_access_cache,
                ):
                    allowed_rules[candidate.trigger_id] = candidate
            if not allowed_rules:
                return []

        batch_size = max(limit, 50)
        visible: list[FiringRecord] = []
        before_id: int | None = None
        while len(visible) < limit:
            batch = await run_blocking_until_complete(
                partial(
                    reactive_store.list_firings,
                    trigger_id=trigger_record.trigger_id if trigger_record is not None else None,
                    trigger_ids=None if trigger_record is not None else allowed_rules,
                    owner_user_ids=owner_ids,
                    before_id=before_id,
                    limit=batch_size,
                ),
            )
            for firing in batch:
                rule = allowed_rules.get(firing.trigger_id)
                if rule is not None and self._firing_allowed_for_history(context, rule, firing):
                    visible.append(firing)
                    if len(visible) >= limit:
                        break
            if len(batch) < batch_size:
                break
            before_id = batch[-1].id
        return visible

    async def _test_trigger(self, context: ToolRuntimeContext, args: _TriggerParams) -> str:
        resolved_trigger_id = _require_non_empty_arg(args["trigger_id"], "trigger_id", "test")
        if args["sample_data"] is not None and not isinstance(args["sample_data"], dict):
            msg = "sample_data must be a JSON object."
            raise ValueError(msg)

        rule = await run_blocking_until_complete(
            partial(
                _load_reactive_or_error,
                ExternalTriggerStore(context.runtime_paths),
                ReactiveTriggerStore(context.runtime_paths),
                resolved_trigger_id,
                direct_error="Dry-run testing is only supported for reactive triggers.",
            ),
        )
        self._require_reactive_owner_or_admin(context, rule, verb="tested")

        now = time.time()
        event, matched, skip_note = _build_dry_run_event_and_match(context, rule=rule, args=args, now=now)

        triage_decision: bool | None = None
        triage_reason: str | None = None
        if matched and rule.triage is not None:
            outcome = await evaluate_triage(
                rule,
                event,
                config=context.config,
                runtime_paths=context.runtime_paths,
            )
            triage_decision = outcome.act
            triage_reason = outcome.reason if outcome.act else (outcome.failure or outcome.reason)

        expired = rule.expires_at is not None and now >= rule.expires_at
        if not rule.enabled:
            would_fire = False
            note = "Dry run only (no state changed or message sent); trigger is currently disabled."
        elif expired:
            would_fire = False
            note = "Dry run only (no state changed or message sent); trigger is currently expired."
        elif skip_note is not None:
            would_fire = False
            note = skip_note
        else:
            would_fire = matched and (triage_decision is not False)
            if would_fire:
                matched_what = "rule and triage" if rule.triage is not None else "rule"
                note = (
                    f"Dry run matched {matched_what} "
                    "(does not evaluate live policy, sender permissions, cooldown, or rate limits)."
                )
            else:
                note = "Dry run only: no state changed, no rate-limit/once quota consumed, and no Matrix message sent."

        return self._payload(
            "ok",
            action="test",
            trigger_id=rule.trigger_id,
            matched=matched,
            triage_decision=triage_decision,
            triage_reason=triage_reason,
            would_fire=would_fire,
            note=note,
        )


def _load_reactive_or_error(
    external_store: ExternalTriggerStore,
    reactive_store: ReactiveTriggerStore,
    trigger_id: str,
    *,
    direct_error: str,
) -> ReactiveTriggerRecord:
    record = reactive_store.get_trigger(trigger_id)
    if record is not None:
        return record
    if external_store.get_record(trigger_id) is not None:
        raise ReactiveTriggerStoreError(direct_error)
    msg = f"Reactive trigger not found: {trigger_id}"
    raise ReactiveTriggerStoreError(msg)


def _validate_update_args(args: _TriggerParams) -> str:
    resolved_trigger_id = _require_non_empty_arg(args["trigger_id"], "trigger_id", "update")
    if all(args[name] is None for name in _UPDATABLE_FIELDS):
        msg = "Provide at least one field to update."
        raise ValueError(msg)
    return resolved_trigger_id


def _require_non_empty_arg(value: str | None, name: str, action: str) -> str:
    if value is None or not isinstance(value, str) or not value.strip():
        msg = f"{name} is required for action={action!r}."
        raise ValueError(msg)
    return value.strip()


def _validate_max_fires_per_hour(max_fires_per_hour: int, *, policy: ExternalTriggerPolicyConfig) -> None:
    if isinstance(max_fires_per_hour, bool) or max_fires_per_hour < 1:
        msg = "max_fires_per_hour must be an integer of at least 1."
        raise ValueError(msg)
    if max_fires_per_hour > policy.max_fires_per_hour:
        msg = f"max_fires_per_hour ({max_fires_per_hour}) exceeds configured policy cap ({policy.max_fires_per_hour})."
        raise ValueError(msg)


def _validate_cooldown_seconds(cooldown_seconds: int) -> None:
    if isinstance(cooldown_seconds, bool) or cooldown_seconds < 0 or cooldown_seconds > _MAX_COOLDOWN_SECONDS:
        msg = f"cooldown_seconds must be an integer between 0 and {_MAX_COOLDOWN_SECONDS}."
        raise ValueError(msg)


def _validate_expires_in_hours(expires_in_hours: float | None, *, now: float) -> float | None:
    if expires_in_hours is None:
        return None
    if (
        isinstance(expires_in_hours, bool)
        or not math.isfinite(expires_in_hours)
        or expires_in_hours <= 0
        or expires_in_hours > _MAX_EXPIRES_IN_HOURS
    ):
        msg = f"expires_in_hours must be positive, finite, and at most {int(_MAX_EXPIRES_IN_HOURS)}."
        raise ValueError(msg)
    return now + expires_in_hours * 3600.0


def _visible_event_sources_payload(context: ToolRuntimeContext) -> list[dict[str, object]]:
    policy = context.config.external_trigger_policy
    if not policy.enabled or not policy.reactive_enabled:
        return []
    sources: list[dict[str, object]] = []
    for source_id in sorted(policy.event_sources):
        cfg = policy.event_sources[source_id]
        if not is_source_subscriber_allowed(cfg, context.requester_id, context.config, context.runtime_paths):
            continue
        if isinstance(cfg, GitHubEventSourceConfig):
            sources.append(
                {
                    "source_id": source_id,
                    "kind": cfg.kind,
                    "allowed_repos": list(cfg.allowed_repos),
                    "allowed_events": list(cfg.allowed_events),
                },
            )
        elif isinstance(cfg, (Ed25519EventSourceConfig, HmacEventSourceConfig)):
            sources.append(
                {
                    "source_id": source_id,
                    "kind": cfg.kind,
                    "allowed_kinds": list(cfg.allowed_kinds),
                },
            )
    return sources


def _truncate_preview(text: str | None, limit: int) -> str | None:
    if text is None or len(text) <= limit:
        return text
    return f"{text[: limit - 3]}..."


def _firing_summary_payload(firing: FiringRecord, *, include_event: bool = True) -> dict[str, object]:
    event_payload: dict[str, object] | None = (
        {
            "source_type": firing.event.source_type,
            "kind": firing.event.kind,
            "actor": firing.event.actor,
            "event_id": firing.event.event_id,
            "room_id": firing.event.room_id,
            "thread_id": firing.event.thread_id,
            "title": firing.event.title,
            "text": _truncate_preview(firing.event.text, _HISTORY_TEXT_PREVIEW_CHARS),
        }
        if include_event
        else None
    )
    return {
        "id": firing.id,
        "trigger_id": firing.trigger_id,
        "dedupe_key": firing.dedupe_key,
        "status": firing.status,
        "triage_reason": firing.triage_reason,
        "error_message": firing.error_message,
        "matrix_event_id": firing.matrix_event_id,
        "attempt_count": firing.attempt_count,
        "created_at": firing.created_at,
        "updated_at": firing.updated_at,
        "event": event_payload,
    }


def _build_updated_reactive_record(
    context: ToolRuntimeContext,
    *,
    existing: ReactiveTriggerRecord,
    args: _TriggerParams,
    now: float,
) -> ReactiveTriggerRecord:
    policy = context.config.external_trigger_policy
    new_source = _resolve_updated_source(context, existing=existing, args=args)
    new_triage = _resolve_updated_triage(context, existing=existing, args=args)
    if args["max_fires_per_hour"] is not None:
        _validate_max_fires_per_hour(args["max_fires_per_hour"], policy=policy)
    if args["cooldown_seconds"] is not None:
        _validate_cooldown_seconds(args["cooldown_seconds"])
    new_expires_at = (
        existing.expires_at
        if args["expires_in_hours"] is None
        else _validate_expires_in_hours(args["expires_in_hours"], now=now)
    )
    target_updates: dict[str, Any] = {}
    if args["prompt"] is not None:
        target_updates["prompt"] = args["prompt"]
    if args["reopen_on_fire"] is not None:
        target_updates["reopen_on_fire"] = args["reopen_on_fire"]
    new_target = (
        existing.target
        if not target_updates
        else TriggerTargetSpec.model_validate({**existing.target.model_dump(), **target_updates})
    )
    if args["enabled"] is None:
        new_enabled, new_disabled_reason = existing.enabled, existing.disabled_reason
    elif args["enabled"]:
        new_enabled, new_disabled_reason = True, None
    else:
        new_enabled = False
        new_disabled_reason = existing.disabled_reason if not existing.enabled else "manual"
    return ReactiveTriggerRecord.model_validate(
        {
            **existing.model_dump(),
            "description": existing.description if args["description"] is None else args["description"],
            "source": new_source.model_dump(),
            "triage": new_triage.model_dump() if new_triage is not None else None,
            "target": new_target.model_dump(),
            "enabled": new_enabled,
            "disabled_reason": new_disabled_reason,
            "once": existing.once if args["once"] is None else args["once"],
            "max_fires_per_hour": (
                existing.max_fires_per_hour if args["max_fires_per_hour"] is None else args["max_fires_per_hour"]
            ),
            "cooldown_seconds": (
                existing.cooldown_seconds if args["cooldown_seconds"] is None else args["cooldown_seconds"]
            ),
            "expires_at": new_expires_at,
            "updated_at": now,
        },
    )


def _resolve_updated_source(
    context: ToolRuntimeContext,
    *,
    existing: ReactiveTriggerRecord,
    args: _TriggerParams,
) -> TriggerSourceSpec:
    if any(args[name] is not None for name in ("pattern", "senders", "include_bots")) and not isinstance(
        existing.source,
        RoomMessageSourceSpec,
    ):
        msg = "pattern, senders, and include_bots are only valid for room_message triggers."
        raise ValueError(msg)
    if args["match"] is not None and not isinstance(existing.source, ExternalEventSourceSpec):
        msg = "match is only valid for external_event triggers."
        raise ValueError(msg)

    if isinstance(existing.source, RoomMessageSourceSpec):
        new_pattern = (
            existing.source.pattern if args["pattern"] is None else (None if args["pattern"] == "" else args["pattern"])
        )
        new_senders = list(existing.source.senders) if args["senders"] is None else list(args["senders"])
        new_include_bots = existing.source.include_bots if args["include_bots"] is None else args["include_bots"]
        return RoomMessageSourceSpec(
            room_id=existing.source.room_id,
            thread_id=existing.source.thread_id,
            senders=new_senders,
            pattern=new_pattern,
            include_bots=new_include_bots,
        )

    if isinstance(existing.source, ExternalEventSourceSpec) and args["match"] is not None:
        source_config = context.config.external_trigger_policy.event_sources.get(existing.source.source_id)
        if source_config is None or not is_source_subscriber_allowed(
            source_config,
            context.requester_id,
            context.config,
            context.runtime_paths,
        ):
            msg = f"Event source {existing.source.source_id!r} is unavailable."
            raise ValueError(msg)
        new_match = dict(args["match"])
        validate_subscription_against_source(source_config, kind=existing.source.kind, match=new_match)
        return ExternalEventSourceSpec(
            source_id=existing.source.source_id,
            kind=existing.source.kind,
            match=new_match,
        )

    return existing.source


def _resolve_updated_triage(
    context: ToolRuntimeContext,
    *,
    existing: ReactiveTriggerRecord,
    args: _TriggerParams,
) -> TriageSpec | None:
    if all(
        args[name] is None for name in ("triage_instructions", "triage_model", "triage_provider", "triage_threshold")
    ):
        return existing.triage
    if existing.source.source_type == "thread_tag":
        msg = "Triage is not supported for thread_tag triggers."
        raise ValueError(msg)
    if args["triage_instructions"] == "":
        if args["triage_model"] is not None or args["triage_threshold"] is not None:
            msg = "Cannot set triage_model or triage_threshold while clearing triage_instructions."
            raise ValueError(msg)
        return None
    if args["triage_instructions"] is not None:
        effective_instructions = args["triage_instructions"]
    elif existing.triage is not None:
        effective_instructions = existing.triage.instructions
    else:
        msg = "triage_instructions is required when configuring triage."
        raise ValueError(msg)

    effective_provider: Literal["llm", "typesafe", "openai_decisions"] = (
        args["triage_provider"]
        if args["triage_provider"] is not None
        else (existing.triage.provider if existing.triage is not None else "llm")
    )
    effective_model = (
        args["triage_model"]
        if args["triage_model"] is not None
        else (
            existing.triage.model
            if effective_provider == "llm" and existing.triage is not None and existing.triage.provider == "llm"
            else None
        )
    )
    effective_threshold = (
        args["triage_threshold"]
        if args["triage_threshold"] is not None
        else (
            existing.triage.threshold
            if effective_provider != "llm" and existing.triage is not None and existing.triage.provider != "llm"
            else None
        )
    )
    return _build_triage_spec(
        context,
        triage_instructions=effective_instructions,
        triage_model=effective_model,
        triage_provider=effective_provider,
        triage_threshold=effective_threshold,
    )


def _build_dry_run_event_and_match(
    context: ToolRuntimeContext,
    *,
    rule: ReactiveTriggerRecord,
    args: _TriggerParams,
    now: float,
) -> tuple[TriggerEvent, bool, str | None]:
    extra_data = dict(args["sample_data"]) if args["sample_data"] is not None else {}
    raw_kind = args["kind"]
    if isinstance(rule.source, RoomMessageSourceSpec):
        sample_actor = args["actor"] if args["actor"] is not None else context.requester_id
        raw_thread_id = extra_data.get("thread_id")
        sample_thread_id = (
            raw_thread_id
            if "thread_id" in extra_data and (raw_thread_id is None or isinstance(raw_thread_id, str))
            else rule.source.thread_id
        )
        event = TriggerEvent(
            source_type="room_message",
            source_id=rule.source.room_id,
            kind=raw_kind.strip() if raw_kind and raw_kind.strip() else "room.message",
            event_id="dry_run",
            timestamp=now,
            room_id=rule.source.room_id,
            thread_id=sample_thread_id,
            actor=sample_actor,
            text=args["text"] if args["text"] is not None else "",
            data={"room_id": rule.source.room_id, "sender": sample_actor, **extra_data},
        )
        if sample_actor in managed_entity_user_ids(rule.target.agent, context.config, context.runtime_paths):
            return (
                event,
                False,
                "Dry run only (no state changed or message sent); messages from the target agent or team are always skipped.",
            )
        if not rule.source.include_bots and not is_human_requester_id(
            sample_actor,
            context.config,
            context.runtime_paths,
        ):
            return (
                event,
                False,
                "Dry run only (no state changed or message sent); bot/non-human messages are skipped when include_bots=False.",
            )
        matched = matches_room_message(
            rule.source,
            event,
            config=context.config,
            runtime_paths=context.runtime_paths,
        )
        return event, matched, None

    if isinstance(rule.source, ExternalEventSourceSpec):
        sample_kind = raw_kind.strip() if raw_kind is not None and raw_kind.strip() else rule.source.kind
        event = TriggerEvent(
            source_type="external_event",
            source_id=rule.source.source_id,
            kind=sample_kind,
            event_id="dry_run",
            timestamp=now,
            actor=args["actor"],
            text=args["text"],
            data=extra_data,
        )
        return event, matches_external_event(rule.source, event), None

    if isinstance(rule.source, GitHubPRSourceSpec):
        return _dry_run_github_pr(rule.source, args, extra_data, raw_kind, now)

    raw_tag_val = extra_data.get("tag")
    raw_tag = raw_tag_val if isinstance(raw_tag_val, str) else rule.source.tag
    sample_tag = _normalize_watch_thread_tag(raw_tag)
    raw_transition_val = extra_data.get("transition")
    if isinstance(raw_transition_val, str):
        sample_transition = raw_transition_val.strip()
    elif raw_kind and raw_kind.strip().startswith("thread_tag."):
        sample_transition = raw_kind.strip().removeprefix("thread_tag.")
    else:
        sample_transition = rule.source.transition
    raw_thread_val = extra_data.get("thread_id")
    sample_thread_id = raw_thread_val if isinstance(raw_thread_val, str) else rule.source.thread_id
    sample_actor = args["actor"] if args["actor"] is not None else context.requester_id
    event = TriggerEvent(
        source_type="thread_tag",
        source_id=rule.source.room_id,
        kind=raw_kind.strip() if raw_kind and raw_kind.strip() else f"thread_tag.{sample_transition}",
        event_id="dry_run",
        timestamp=now,
        room_id=rule.source.room_id,
        thread_id=sample_thread_id,
        actor=sample_actor,
        text=args["text"],
        data={
            "room_id": rule.source.room_id,
            "thread_id": sample_thread_id,
            "tag": sample_tag,
            "transition": sample_transition,
            **extra_data,
        },
    )
    matched = (
        sample_thread_id == rule.source.thread_id
        and sample_tag.lower() == rule.source.tag.lower()
        and sample_transition == rule.source.transition
    )
    return event, matched, None


def _gh_pr_allowed(ctx: ToolRuntimeContext, owner: str, src: GitHubPRSourceSpec) -> bool:
    fn = ghpr.check_github_pr_source_access
    return fn(owner_user_id=owner, source_spec=src, config=ctx.config, runtime_paths=ctx.runtime_paths) is None


def _dry_run_github_pr(
    src: GitHubPRSourceSpec,
    args: _TriggerParams,
    extra: Mapping[str, Any],
    raw_kind: str | None,
    now: float,
) -> tuple[TriggerEvent, bool, str | None]:
    cats = {"activity": "activity", "ci": "ci", "merge": "merge", "close": "close"}
    cats |= {"github_pr.merged": "merge", "github_pr.closed": "close"}
    norm = raw_kind.strip().lower() if isinstance(raw_kind, str) and raw_kind.strip() else None
    cat = (
        cats.get(norm) if norm else (cats.get(str(extra["category"]).lower()) if "category" in extra else src.events[0])
    )
    ekind = "github_pr.merged" if cat == "merge" else ("github_pr.closed" if cat == "close" else "github_pr.update")
    data: dict[str, Any] = {"repo": src.repo, "number": src.number, "terminal": cat in {"merge", "close"}, **extra}
    ev_d: dict[str, Any] = {"source_type": "github_pr", "source_id": src.repo.lower(), "kind": ekind, "data": data}
    ev_d |= {"event_id": "dry_run", "timestamp": now, "actor": args["actor"], "title": f"{src.repo}#{src.number}"}
    ev = TriggerEvent.model_validate({**ev_d, "text": args["text"]})
    repo_ok = isinstance(data.get("repo"), str) and data["repo"].lower() == src.repo.lower()
    if not (cat in src.events and repo_ok and type(data.get("number")) is int and data["number"] == src.number):
        return ev, False, None
    atype = extra.get("actor_type") if isinstance(extra.get("actor_type"), str) else None
    if cat == "activity" and ghpr.is_ignored_activity_actor(src, actor=args["actor"], actor_type=atype):
        return ev, False, "Dry run only (no state changed or message sent); activity from this actor is ignored."
    return ev, True, None


async def _read_thread_tag_present(
    context: ToolRuntimeContext,
    source: ThreadTagSourceSpec,
    *,
    preferred_agent: str,
) -> bool:
    watch_client = _client_for_room(context, source.room_id, preferred_agent=preferred_agent) or context.client
    return await read_thread_tag_present(watch_client, source)


async def _read_reenable_tag_state(
    context: ToolRuntimeContext,
    record: ReactiveTriggerRecord,
    *,
    enabling: bool,
) -> bool | None:
    if not enabling or record.enabled or not isinstance(record.source, ThreadTagSourceSpec):
        return None
    return await _read_thread_tag_present(context, record.source, preferred_agent=record.target.agent)


def _validate_optional_trigger_id(trigger_id: str | None) -> str | None:
    if trigger_id is None:
        return None
    stripped = trigger_id.strip()
    if not stripped or TRIGGER_ID_PATTERN.fullmatch(stripped) is None:
        msg = "trigger_id must match ^[A-Za-z0-9_-]{1,64}$"
        raise ValueError(msg)
    return stripped


def _normalize_watch_thread_tag(tag: str) -> str:
    """Normalize user-provided thread tag names so underscores/case match stored canonical tags."""
    if not isinstance(tag, str):
        msg = "tag must be a non-empty string."
        raise ThreadTagsError(msg)
    return normalize_tag_name(tag.strip().replace("_", "-"))


def _resolve_unique_reactive_trigger_id(
    trigger_id: str | None,
    *,
    external_store: ExternalTriggerStore,
    reactive_store: ReactiveTriggerStore,
) -> str:
    if trigger_id is not None:
        stripped = trigger_id.strip()
        if not stripped or TRIGGER_ID_PATTERN.fullmatch(stripped) is None:
            msg = "trigger_id must match ^[A-Za-z0-9_-]{1,64}$"
            raise ValueError(msg)
        if external_store.get_record(stripped) is not None or reactive_store.get_trigger(stripped) is not None:
            msg = f"Trigger already exists: {stripped}"
            raise ReactiveTriggerDuplicateError(msg)
        return stripped

    for _ in range(10):
        candidate = generate_trigger_id()
        if external_store.get_record(candidate) is None and reactive_store.get_trigger(candidate) is None:
            return candidate
    msg = "Failed to generate a unique trigger_id."
    raise ReactiveTriggerStoreError(msg)


def _build_triage_spec(
    context: ToolRuntimeContext,
    *,
    triage_instructions: str | None,
    triage_model: str | None,
    triage_provider: Literal["llm", "typesafe", "openai_decisions"],
    triage_threshold: float | None,
) -> TriageSpec | None:
    if triage_instructions is None:
        if triage_model is not None or triage_threshold is not None:
            msg = "triage_instructions is required when triage_model or triage_threshold is specified."
            raise ValueError(msg)
        return None
    spec = TriageSpec(
        instructions=triage_instructions,
        provider=triage_provider,
        model=triage_model,
        threshold=triage_threshold,
    )
    if spec.provider == "llm":
        assert spec.model is not None
        if spec.model not in context.config.models:
            available = ", ".join(sorted(context.config.models)) or "(none)"
            msg = f"Unknown triage_model {spec.model!r}. Available model aliases: {available}"
            raise ValueError(msg)
    return spec


def _require_target_room_configured_for_entity(
    context: ToolRuntimeContext,
    *,
    agent_name: str,
    raw_room_id: str,
    resolved_room_id: str,
    current_room_id: str,
) -> None:
    entity_rooms = get_rooms_for_entity(agent_name, context.config)
    if raw_room_id in entity_rooms or resolved_room_id in entity_rooms:
        return
    configured_entities = configured_routable_entity_names_for_room(
        context.config,
        resolved_room_id,
        context.runtime_paths,
        room_aliases=(raw_room_id,),
    )
    if agent_name in configured_entities:
        return
    if not configured_entities and _targets_own_dynamic_current_room(
        context,
        agent_name=agent_name,
        resolved_room_id=resolved_room_id,
        current_room_id=current_room_id,
    ):
        return
    msg = f"Target room {resolved_room_id} must already be configured for target entity {agent_name!r}."
    raise ReactiveTriggerStoreError(msg)


def _targets_own_dynamic_current_room(
    context: ToolRuntimeContext,
    *,
    agent_name: str,
    resolved_room_id: str,
    current_room_id: str,
) -> bool:
    if agent_name != context.agent_name or resolved_room_id != current_room_id:
        return False
    agent_config = context.config.agents.get(agent_name)
    if agent_config is None:
        return False
    return agent_config.private is not None or context.config.generated_agent(agent_name) is not None


def _bot_joined_room(bot: AgentBot, room_id: str) -> bool:
    if not getattr(bot, "running", False) or bot.client is None:
        return False
    rooms = getattr(bot.client, "rooms", None)
    return isinstance(rooms, Mapping) and room_id in rooms


def _find_joined_bot_for_room(
    context: ToolRuntimeContext,
    room_id: str,
    *,
    preferred_agent: str | None = None,
) -> AgentBot | None:
    if context.orchestrator is None:
        return None
    bots = context.orchestrator.agent_bots
    if preferred_agent is not None:
        preferred = bots.get(preferred_agent)
        if preferred is not None and _bot_joined_room(preferred, room_id):
            return preferred
    for bot in bots.values():
        if _bot_joined_room(bot, room_id):
            return bot
    return None


def _client_for_room(
    context: ToolRuntimeContext,
    room_id: str,
    *,
    preferred_agent: str | None = None,
) -> nio.AsyncClient | None:
    bot = _find_joined_bot_for_room(context, room_id, preferred_agent=preferred_agent)
    if bot is not None and bot.client is not None:
        return bot.client
    current_room_id = resolve_room_id(context.room_id, context.runtime_paths)
    if room_id == current_room_id:
        return context.client
    rooms = getattr(context.client, "rooms", None)
    if isinstance(rooms, Mapping) and room_id in rooms:
        return context.client
    if hasattr(context.client, "room_get_state") or hasattr(context.client, "joined_members"):
        return context.client
    return None


def _observed_room_for(
    context: ToolRuntimeContext,
    room_id: str,
    *,
    preferred_agent: str | None = None,
) -> nio.MatrixRoom | None:
    client = _client_for_room(context, room_id, preferred_agent=preferred_agent)
    if client is None:
        return None
    rooms = getattr(client, "rooms", None)
    if not isinstance(rooms, Mapping):
        return None
    return rooms.get(room_id)


def _watch_room_candidate_entities(
    context: ToolRuntimeContext,
    *,
    raw_room_id: str,
    resolved_room_id: str,
) -> list[str]:
    candidates: list[str] = []
    if context.orchestrator is not None:
        for agent_name, bot in context.orchestrator.agent_bots.items():
            if _bot_joined_room(bot, resolved_room_id) and agent_name not in candidates:
                candidates.append(agent_name)
    for entity_name in configured_routable_entity_names_for_room(
        context.config,
        resolved_room_id,
        context.runtime_paths,
        room_aliases=(raw_room_id,),
    ):
        if entity_name not in candidates:
            candidates.append(entity_name)
    return candidates


async def _live_room_membership(
    client: nio.AsyncClient,
    room_id: str,
    owner_ids: set[str],
) -> bool | None:
    if hasattr(client, "room_get_state"):
        room_state = await read_room_state(client, room_id)
        if room_state is None:
            return None
        return any(room_state.events.get(("m.room.member", uid), {}).get("membership") == "join" for uid in owner_ids)
    if hasattr(client, "joined_members"):
        response = await client.joined_members(room_id)
        if isinstance(response, nio.JoinedMembersResponse):
            return any(member.user_id in owner_ids for member in response.members)
    return None


async def _verify_owner_joined_room(
    context: ToolRuntimeContext,
    *,
    room_id: str,
    preferred_agent: str | None = None,
    require_live: bool = False,
) -> None:
    """Verify that the requester is joined to room_id using preferred_agent when joined or another joined reader."""
    current_room_id = resolve_room_id(context.room_id, context.runtime_paths)
    owner_ids = equivalent_requester_ids(context.requester_id, context.config, context.runtime_paths)
    client = _client_for_room(context, room_id, preferred_agent=preferred_agent)
    if client is not None:
        live_joined = await _live_room_membership(client, room_id, owner_ids)
        if live_joined is True:
            return
        if live_joined is False:
            msg = f"You must be joined to room {room_id} to access this trigger."
            raise ReactiveTriggerStoreError(msg)
        if require_live:
            msg = f"Cannot verify that you are joined to room {room_id}."
            raise ReactiveTriggerStoreError(msg)
        rooms = getattr(client, "rooms", None)
        room = rooms.get(room_id) if isinstance(rooms, Mapping) else None
        users = getattr(room, "users", None)
        if isinstance(users, Mapping) and users:
            if any(uid in users for uid in owner_ids):
                return
            msg = f"You must be joined to room {room_id} to access this trigger."
            raise ReactiveTriggerStoreError(msg)

    if room_id == current_room_id and not require_live:
        return
    msg = f"Cannot verify that you are joined to room {room_id}."
    raise ReactiveTriggerStoreError(msg)


async def _read_initial_room_cursor(
    context: ToolRuntimeContext,
    *,
    room_id: str,
    preferred_agent: str | None,
    now: float,
) -> RoomCursor:
    reader: ConversationReader | None = None
    bot = _find_joined_bot_for_room(context, room_id, preferred_agent=preferred_agent)
    if bot is not None:
        reader = getattr(bot, "conversation_reader", None)
    if reader is None:
        reader = context.conversation_reader

    started = RoomCursor(created_ts=int(now * 1000), logical_event_id="")
    if reader is not None and hasattr(reader, "latest_room_cursor"):
        try:
            latest = await reader.latest_room_cursor(room_id)
        except Exception:
            logger.warning(
                "Failed to read latest room cursor for reactive trigger; falling back to current timestamp",
                room_id=room_id,
                exc_info=True,
            )
            latest = None
        if latest is not None:
            journal_head = RoomCursor(created_ts=latest.created_ts, logical_event_id=latest.logical_event_id)
            return max(
                (started, journal_head),
                key=lambda cursor: (cursor.created_ts, cursor.logical_event_id),
            )

    return started
