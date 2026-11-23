"""Local-only unified `triggers` toolkit for reactive triggers and direct signed webhooks."""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from functools import partial
from typing import TYPE_CHECKING, ClassVar, Literal

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.authorization import ReplyMembershipPendingError, is_sender_allowed_for_agent_reply_in_room
from mindroom.background_tasks import run_blocking_until_complete
from mindroom.custom_tools.attachment_helpers import explicit_thread_root
from mindroom.custom_tools.external_trigger_context import (
    ExternalTriggerContextError,
    require_external_trigger_owner_context,
)
from mindroom.custom_tools.tool_payloads import custom_tool_payload
from mindroom.entity_resolution import configured_routable_entity_names_for_room, names_room
from mindroom.entity_rooms import get_rooms_for_entity
from mindroom.external_triggers.policy import is_external_trigger_administrator
from mindroom.external_triggers.store import (
    ExternalTriggerRecord,
    ExternalTriggerStore,
    ExternalTriggerStoreError,
    ExternalTriggerTarget,
)
from mindroom.logging_config import get_logger
from mindroom.matrix.room_reconciliation import read_room_state
from mindroom.matrix.state import resolve_room_id
from mindroom.reactive_triggers.external_sources import (
    is_source_subscriber_allowed,
    validate_subscription_against_source,
)
from mindroom.reactive_triggers.models import (
    TRIGGER_ID_PATTERN,
    ExternalEventSourceSpec,
    ReactiveTriggerRecord,
    RoomCursor,
    RoomMessageSourceSpec,
    ThreadCheckpoint,
    ThreadTagSourceSpec,
    TriageSpec,
    TriggerTargetSpec,
    generate_trigger_id,
)
from mindroom.reactive_triggers.store import (
    ReactiveTriggerDuplicateError,
    ReactiveTriggerStore,
    ReactiveTriggerStoreError,
)
from mindroom.requester_identity import equivalent_requester_ids
from mindroom.thread_tags import ThreadTagsError, get_thread_tags
from mindroom.tool_system.authoring import Toolkit

if TYPE_CHECKING:
    from collections.abc import Callable

    import nio

    from mindroom.bot import AgentBot
    from mindroom.config.external_trigger_policy import ExternalTriggerPolicyConfig
    from mindroom.matrix.conversation_reads import ConversationReader
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

logger = get_logger(__name__)

_MAX_EXPIRES_IN_HOURS = 8760.0
_TOOL_ERRORS = (
    ExternalTriggerContextError,
    ExternalTriggerStoreError,
    ReactiveTriggerStoreError,
    ThreadTagsError,
    ValueError,
)


class TriggersTools(Toolkit):
    """Manage reactive triggers and signed direct external triggers in the primary runtime."""

    _TOOL_NAME: ClassVar[str] = "triggers"
    _CONTEXT_LABEL: ClassVar[str] = "Triggers"

    def __init__(self) -> None:
        super().__init__(
            name=self._TOOL_NAME,
            tools=[
                self.watch_room,
                self.watch_thread,
                self.subscribe_event,
                self.create_trigger,
                self.list_triggers,
                self.disable_trigger,
                self.delete_trigger,
                self.rotate_trigger_key,
            ],
        )

    @classmethod
    def _payload(cls, status: str, **fields: object) -> str:
        return custom_tool_payload(cls._TOOL_NAME, status, **fields)

    @classmethod
    def _with_stores(
        cls,
        action: Callable[[ToolRuntimeContext, ExternalTriggerStore, ReactiveTriggerStore], str],
    ) -> str:
        try:
            context = require_external_trigger_owner_context(cls._CONTEXT_LABEL)
            external_store = ExternalTriggerStore(context.runtime_paths)
            reactive_store = ReactiveTriggerStore(context.runtime_paths)
            return action(context, external_store, reactive_store)
        except _TOOL_ERRORS as exc:
            return cls._payload("error", message=str(exc))

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
        *,
        external_store: ExternalTriggerStore,
        reactive_store: ReactiveTriggerStore,
        policy: ExternalTriggerPolicyConfig,
        trigger_id: str | None,
        max_fires_per_hour: int,
        expires_in_hours: float | None,
        triage_instructions: str | None = None,
        triage_model: str | None = None,
        triage_provider: Literal["llm", "typesafe", "openai_decisions"] = "llm",
        triage_threshold: float | None = None,
        now: float,
    ) -> tuple[str, TriageSpec | None, float | None]:
        resolved_trigger_id = _resolve_unique_reactive_trigger_id(
            trigger_id,
            external_store=external_store,
            reactive_store=reactive_store,
        )
        if isinstance(max_fires_per_hour, bool) or max_fires_per_hour < 1:
            msg = "max_fires_per_hour must be an integer of at least 1."
            raise ValueError(msg)
        if max_fires_per_hour > policy.max_fires_per_hour:
            msg = f"max_fires_per_hour ({max_fires_per_hour}) exceeds configured policy cap ({policy.max_fires_per_hour})."
            raise ValueError(msg)

        expires_at: float | None = None
        if expires_in_hours is not None:
            if (
                isinstance(expires_in_hours, bool)
                or not math.isfinite(expires_in_hours)
                or expires_in_hours <= 0
                or expires_in_hours > _MAX_EXPIRES_IN_HOURS
            ):
                msg = f"expires_in_hours must be positive, finite, and at most {int(_MAX_EXPIRES_IN_HOURS)}."
                raise ValueError(msg)
            expires_at = now + expires_in_hours * 3600.0

        triage = _build_triage_spec(
            context,
            triage_instructions=triage_instructions,
            triage_model=triage_model,
            triage_provider=triage_provider,
            triage_threshold=triage_threshold,
        )
        return resolved_trigger_id, triage, expires_at

    @classmethod
    async def _direct_target(
        cls,
        context: ToolRuntimeContext,
        *,
        target_agent: str | None,
        target_room_id: str | None,
        target_thread_id: str | None,
        new_thread: bool,
    ) -> ExternalTriggerTarget:
        is_admin = cls._is_admin(context)
        if not is_admin and target_agent not in (None, context.agent_name):
            msg = "Only external trigger admins can target a different agent or team."
            raise ExternalTriggerStoreError(msg)
        target = ExternalTriggerTarget(
            room_id=target_room_id or context.room_id,
            thread_id=target_thread_id,
            agent=target_agent or context.agent_name,
            new_thread=new_thread,
        )
        resolved_room_id = resolve_room_id(target.room_id, context.runtime_paths)
        if not is_admin and resolved_room_id != context.room_id:
            msg = "Only external trigger admins can target a different room."
            raise ExternalTriggerStoreError(msg)
        if target.thread_id is None:
            return target
        thread_root = await explicit_thread_root(context, room_id=resolved_room_id, thread_id=target.thread_id)
        return ExternalTriggerTarget.model_validate({**target.model_dump(), "thread_id": thread_root})

    @classmethod
    async def _resolve_reactive_target(
        cls,
        context: ToolRuntimeContext,
        *,
        target_agent: str | None,
        target_room_id: str | None,
        target_thread_id: str | None,
        new_thread: bool,
        thread_key: str | None,
        prompt: str,
        default_thread_id: Callable[[str], str | None],
        allow_origin: bool,
    ) -> TriggerTargetSpec:
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
        if new_thread:
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
            prompt=prompt,
        )

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
        current_room_id = resolve_room_id(context.room_id, context.runtime_paths)
        if resolved_watch_room_id == current_room_id:
            return resolved_watch_room_id

        await _verify_owner_joined_room(
            context,
            room_id=resolved_watch_room_id,
            preferred_agent=preferred_agent,
        )
        candidate_entities = _watch_room_candidate_entities(
            context,
            raw_room_id=raw_watch_room,
            resolved_room_id=resolved_watch_room_id,
        )
        if not candidate_entities:
            msg = f"No configured or running agent is available to watch room {resolved_watch_room_id}."
            raise ReactiveTriggerStoreError(msg)

        observed_room = _observed_room_for(context, resolved_watch_room_id, preferred_agent=preferred_agent)
        memberships = cls._memberships(context)
        authorized = False
        for entity_name in candidate_entities:
            try:
                if is_sender_allowed_for_agent_reply_in_room(
                    context.requester_id,
                    entity_name,
                    context.config,
                    resolved_watch_room_id,
                    context.runtime_paths,
                    memberships,
                    observed_room=observed_room,
                ):
                    authorized = True
                    break
            except ReplyMembershipPendingError:
                continue
        if not authorized:
            msg = f"You are not authorized to watch room {resolved_watch_room_id}."
            raise ReactiveTriggerStoreError(msg)
        return resolved_watch_room_id

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
        }

    @staticmethod
    def _reactive_record_payload(record: ReactiveTriggerRecord) -> dict[str, object]:
        return {
            "trigger_id": record.trigger_id,
            "trigger_kind": "reactive",
            "enabled": record.enabled,
            "description": record.description,
            "owner_user_id": record.owner_user_id,
            "source": record.source.model_dump(mode="json"),
            "triage": record.triage.model_dump(mode="json") if record.triage is not None else None,
            "target": record.target.model_dump(mode="json"),
            "once": record.once,
            "max_fires_per_hour": record.max_fires_per_hour,
            "cooldown_seconds": record.cooldown_seconds,
            "expires_at": record.expires_at,
            "fire_count": record.fire_count,
            "last_fired_at": record.last_fired_at,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }

    @staticmethod
    def _notify_reactive_service(context: ToolRuntimeContext) -> None:
        if context.orchestrator is not None:
            context.orchestrator.reactive_triggers.notify_work()

    @classmethod
    def _require_reactive_owner_or_admin(
        cls,
        context: ToolRuntimeContext,
        record: ReactiveTriggerRecord,
    ) -> None:
        actor_ids = set(equivalent_requester_ids(context.requester_id, context.config, context.runtime_paths))
        if record.owner_user_id not in actor_ids and not cls._is_admin(context):
            msg = "trigger can only be changed by its owner or an external trigger admin"
            raise ReactiveTriggerStoreError(msg)

    async def watch_room(
        self,
        prompt: str,
        room_id: str | None = None,
        thread_id: str | None = None,
        senders: list[str] | None = None,
        pattern: str | None = None,
        triage_instructions: str | None = None,
        triage_model: str | None = None,
        triage_provider: Literal["llm", "typesafe", "openai_decisions"] = "llm",
        triage_threshold: float | None = None,
        target_agent: str | None = None,
        target_room_id: str | None = None,
        target_thread_id: str | None = None,
        new_thread: bool = False,
        thread_key: str | None = None,
        once: bool = False,
        max_fires_per_hour: int = 10,
        cooldown_seconds: int = 0,
        expires_in_hours: float | None = None,
        trigger_id: str | None = None,
        description: str = "",
    ) -> str:
        """Create a reactive trigger watching new visible messages in a Matrix room.

        Args:
            prompt: Instruction delivered to the woken agent when the trigger fires.
            room_id: Room key, alias, or Matrix room ID to watch; defaults to the current room.
            thread_id: Optional Matrix thread root (or reply inside it) to restrict watching.
            senders: Optional list of Matrix user IDs to match.
            pattern: Optional case-insensitive regular expression pattern to match message text.
            triage_instructions: Optional plain-English criteria for tool-less cheap triage.
            triage_model: Model alias from config.models; required when triage_provider='llm'.
            triage_provider: Triage evaluator backend ('llm', 'typesafe', or 'openai_decisions').
            triage_threshold: Confidence threshold for 'typesafe' or 'openai_decisions' triage.
            target_agent: Agent or team to wake; defaults to the current agent.
            target_room_id: Room to wake the target in; defaults to the current room.
            target_thread_id: Target thread root, 'origin' (reply in triggering message's thread),
                or None (defaults to 'origin' for same-room watch, or room timeline cross-room).
            new_thread: Start a fresh thread per firing; mutually exclusive with target_thread_id.
            thread_key: Optional grouping key when new_thread=True.
            once: Disable automatically after firing once.
            max_fires_per_hour: Per-rule hourly firing cap (bounded by policy).
            cooldown_seconds: Minimum seconds between consecutive firings.
            expires_in_hours: Optional lifetime in hours (positive, up to 8760).
            trigger_id: Optional route-safe trigger ID; auto-generated if omitted.
            description: Human-readable description of the rule.

        """
        try:
            context = require_external_trigger_owner_context(self._CONTEXT_LABEL)
            policy = self._require_reactive_policy(context)
            external_store = ExternalTriggerStore(context.runtime_paths)
            reactive_store = ReactiveTriggerStore(context.runtime_paths)
            now = time.time()

            resolved_trigger_id, triage, expires_at = self._validate_reactive_common(
                context,
                external_store=external_store,
                reactive_store=reactive_store,
                policy=policy,
                trigger_id=trigger_id,
                max_fires_per_hour=max_fires_per_hour,
                expires_in_hours=expires_in_hours,
                triage_instructions=triage_instructions,
                triage_model=triage_model,
                triage_provider=triage_provider,
                triage_threshold=triage_threshold,
                now=now,
            )

            resolved_watch_room_id = await self._resolve_watched_room(
                context,
                room_id=room_id,
                preferred_agent=target_agent or context.agent_name,
            )
            resolved_watch_thread_id = (
                await explicit_thread_root(context, room_id=resolved_watch_room_id, thread_id=thread_id)
                if thread_id is not None
                else None
            )

            target = await self._resolve_reactive_target(
                context,
                target_agent=target_agent,
                target_room_id=target_room_id,
                target_thread_id=target_thread_id,
                new_thread=new_thread,
                thread_key=thread_key,
                prompt=prompt,
                default_thread_id=lambda dest_room: "origin" if dest_room == resolved_watch_room_id else None,
                allow_origin=True,
            )

            source = RoomMessageSourceSpec(
                room_id=resolved_watch_room_id,
                thread_id=resolved_watch_thread_id,
                senders=list(senders or ()),
                pattern=pattern,
            )
            record = ReactiveTriggerRecord(
                trigger_id=resolved_trigger_id,
                owner_user_id=context.requester_id,
                description=description,
                source=source,
                triage=triage,
                target=target,
                once=once,
                max_fires_per_hour=max_fires_per_hour,
                cooldown_seconds=cooldown_seconds,
                expires_at=expires_at,
                created_at=now,
                updated_at=now,
            )
            initial_cursor = await _read_initial_room_cursor(
                context,
                room_id=resolved_watch_room_id,
                preferred_agent=target.agent,
                now=now,
            )
            created = await run_blocking_until_complete(
                partial(
                    reactive_store.create_trigger,
                    record,
                    initial_cursor=initial_cursor,
                    max_per_owner=policy.max_reactive_triggers_per_owner,
                ),
            )
            self._notify_reactive_service(context)
        except _TOOL_ERRORS as exc:
            return self._payload("error", message=str(exc))

        return self._payload(
            "ok",
            action="watch_room",
            trigger=self._reactive_record_payload(created),
        )

    async def watch_thread(
        self,
        prompt: str,
        tag: str = "resolved",
        transition: Literal["added", "removed"] = "added",
        room_id: str | None = None,
        thread_id: str | None = None,
        target_agent: str | None = None,
        target_room_id: str | None = None,
        target_thread_id: str | None = None,
        new_thread: bool = False,
        thread_key: str | None = None,
        once: bool = True,
        max_fires_per_hour: int = 10,
        cooldown_seconds: int = 0,
        expires_in_hours: float | None = None,
        trigger_id: str | None = None,
        description: str = "",
    ) -> str:
        """Create a reactive trigger watching a thread tag state transition.

        Args:
            prompt: Instruction delivered to the woken agent when the transition occurs.
            tag: Thread tag name to watch (default 'resolved').
            transition: Whether to fire when the tag is 'added' or 'removed'.
            room_id: Room key, alias, or Matrix room ID containing the thread; defaults to current room.
            thread_id: Thread root (or reply inside it) to watch; defaults to current thread in current room.
            target_agent: Agent or team to wake; defaults to the current agent.
            target_room_id: Room to wake the target in; defaults to the current room.
            target_thread_id: Target thread root; defaults to the current thread when targeting current room.
            new_thread: Start a fresh thread per firing; mutually exclusive with target_thread_id.
            thread_key: Optional grouping key when new_thread=True.
            once: Disable automatically after firing once (default True).
            max_fires_per_hour: Per-rule hourly firing cap (bounded by policy).
            cooldown_seconds: Minimum seconds between consecutive firings.
            expires_in_hours: Optional lifetime in hours (positive, up to 8760).
            trigger_id: Optional route-safe trigger ID; auto-generated if omitted.
            description: Human-readable description of the rule.

        """
        try:
            context = require_external_trigger_owner_context(self._CONTEXT_LABEL)
            policy = self._require_reactive_policy(context)
            external_store = ExternalTriggerStore(context.runtime_paths)
            reactive_store = ReactiveTriggerStore(context.runtime_paths)
            now = time.time()

            resolved_trigger_id, _triage, expires_at = self._validate_reactive_common(
                context,
                external_store=external_store,
                reactive_store=reactive_store,
                policy=policy,
                trigger_id=trigger_id,
                max_fires_per_hour=max_fires_per_hour,
                expires_in_hours=expires_in_hours,
                now=now,
            )

            resolved_watch_room_id = await self._resolve_watched_room(
                context,
                room_id=room_id,
                preferred_agent=target_agent or context.agent_name,
            )
            current_room_id = resolve_room_id(context.room_id, context.runtime_paths)
            raw_watch_thread_id = thread_id
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
                target_agent=target_agent,
                target_room_id=target_room_id,
                target_thread_id=target_thread_id,
                new_thread=new_thread,
                thread_key=thread_key,
                prompt=prompt,
                default_thread_id=lambda dest_room: current_thread_id if dest_room == current_room_id else None,
                allow_origin=False,
            )

            source = ThreadTagSourceSpec(
                room_id=resolved_watch_room_id,
                thread_id=resolved_watch_thread_id,
                tag=tag,
                transition=transition,
            )
            record = ReactiveTriggerRecord(
                trigger_id=resolved_trigger_id,
                owner_user_id=context.requester_id,
                description=description,
                source=source,
                triage=None,
                target=target,
                once=once,
                max_fires_per_hour=max_fires_per_hour,
                cooldown_seconds=cooldown_seconds,
                expires_at=expires_at,
                created_at=now,
                updated_at=now,
            )

            watch_client = (
                _client_for_room(
                    context,
                    resolved_watch_room_id,
                    preferred_agent=target.agent,
                )
                or context.client
            )
            tag_state = await get_thread_tags(watch_client, resolved_watch_room_id, resolved_watch_thread_id)
            current_tags = tag_state.tags if tag_state is not None else {}
            tag_present = source.tag.lower() in {existing.lower() for existing in current_tags}
            initial_checkpoint = ThreadCheckpoint(
                tag_present=tag_present,
                state_event_id=None,
                updated_at=now,
            )
            already_in_state = (source.transition == "added" and tag_present) or (
                source.transition == "removed" and not tag_present
            )

            created = await run_blocking_until_complete(
                partial(
                    reactive_store.create_trigger,
                    record,
                    initial_checkpoint=initial_checkpoint,
                    max_per_owner=policy.max_reactive_triggers_per_owner,
                ),
            )
            self._notify_reactive_service(context)
        except _TOOL_ERRORS as exc:
            return self._payload("error", message=str(exc))

        extra: dict[str, object] = {"already_in_state": already_in_state}
        if already_in_state:
            extra["note"] = "Thread already has this state; trigger will fire only on a future transition."
        return self._payload(
            "ok",
            action="watch_thread",
            trigger=self._reactive_record_payload(created),
            **extra,
        )

    async def subscribe_event(
        self,
        source_id: str,
        kind: str,
        prompt: str,
        match: dict[str, str | bool | int] | None = None,
        triage_instructions: str | None = None,
        triage_model: str | None = None,
        triage_provider: Literal["llm", "typesafe", "openai_decisions"] = "llm",
        triage_threshold: float | None = None,
        target_agent: str | None = None,
        target_room_id: str | None = None,
        target_thread_id: str | None = None,
        new_thread: bool = False,
        thread_key: str | None = None,
        once: bool = True,
        max_fires_per_hour: int = 10,
        cooldown_seconds: int = 0,
        expires_in_hours: float | None = None,
        trigger_id: str | None = None,
        description: str = "",
    ) -> str:
        """Subscribe a reactive trigger to a configured shared external event source.

        Args:
            source_id: Configured source ID under external_trigger_policy.event_sources.
            kind: Event kind to match (e.g. 'pull_request.merged', 'release.published').
            prompt: Instruction delivered to the woken agent when a matching event arrives.
            match: Optional exact-match key/value filter over structured event data.
            triage_instructions: Optional plain-English criteria for tool-less cheap triage.
            triage_model: Model alias from config.models; required when triage_provider='llm'.
            triage_provider: Triage evaluator backend ('llm', 'typesafe', or 'openai_decisions').
            triage_threshold: Confidence threshold for 'typesafe' or 'openai_decisions' triage.
            target_agent: Agent or team to wake; defaults to the current agent.
            target_room_id: Room to wake the target in; defaults to the current room.
            target_thread_id: Target thread root; defaults to the current thread when targeting current room.
            new_thread: Start a fresh thread per firing; mutually exclusive with target_thread_id.
            thread_key: Optional grouping key when new_thread=True.
            once: Disable automatically after firing once (default True).
            max_fires_per_hour: Per-rule hourly firing cap (bounded by policy).
            cooldown_seconds: Minimum seconds between consecutive firings.
            expires_in_hours: Optional lifetime in hours (positive, up to 8760).
            trigger_id: Optional route-safe trigger ID; auto-generated if omitted.
            description: Human-readable description of the rule.

        """
        try:
            context = require_external_trigger_owner_context(self._CONTEXT_LABEL)
            policy = self._require_reactive_policy(context)
            external_store = ExternalTriggerStore(context.runtime_paths)
            reactive_store = ReactiveTriggerStore(context.runtime_paths)
            now = time.time()

            resolved_trigger_id, triage, expires_at = self._validate_reactive_common(
                context,
                external_store=external_store,
                reactive_store=reactive_store,
                policy=policy,
                trigger_id=trigger_id,
                max_fires_per_hour=max_fires_per_hour,
                expires_in_hours=expires_in_hours,
                triage_instructions=triage_instructions,
                triage_model=triage_model,
                triage_provider=triage_provider,
                triage_threshold=triage_threshold,
                now=now,
            )

            normalized_source_id = source_id.strip()
            source_config = policy.event_sources.get(normalized_source_id)
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
                        f"Event source {normalized_source_id!r} is unavailable. "
                        f"Available event sources: {', '.join(allowed_source_ids)}"
                    )
                else:
                    msg = (
                        f"Event source {normalized_source_id!r} is unavailable. No event sources are available to you."
                    )
                raise ValueError(msg)

            source_spec = ExternalEventSourceSpec(
                source_id=normalized_source_id,
                kind=kind,
                match=dict(match) if match is not None else {},
            )
            validate_subscription_against_source(
                source_config,
                kind=source_spec.kind,
                match=source_spec.match,
            )

            current_room_id = resolve_room_id(context.room_id, context.runtime_paths)
            current_thread_id = context.resolved_thread_id or context.thread_id
            target = await self._resolve_reactive_target(
                context,
                target_agent=target_agent,
                target_room_id=target_room_id,
                target_thread_id=target_thread_id,
                new_thread=new_thread,
                thread_key=thread_key,
                prompt=prompt,
                default_thread_id=lambda dest_room: current_thread_id if dest_room == current_room_id else None,
                allow_origin=False,
            )

            record = ReactiveTriggerRecord(
                trigger_id=resolved_trigger_id,
                owner_user_id=context.requester_id,
                description=description,
                source=source_spec,
                triage=triage,
                target=target,
                once=once,
                max_fires_per_hour=max_fires_per_hour,
                cooldown_seconds=cooldown_seconds,
                expires_at=expires_at,
                created_at=now,
                updated_at=now,
            )
            created = await run_blocking_until_complete(
                partial(
                    reactive_store.create_trigger,
                    record,
                    max_per_owner=policy.max_reactive_triggers_per_owner,
                ),
            )
            self._notify_reactive_service(context)
        except _TOOL_ERRORS as exc:
            return self._payload("error", message=str(exc))

        return self._payload(
            "ok",
            action="subscribe_event",
            trigger=self._reactive_record_payload(created),
        )

    async def create_trigger(
        self,
        trigger_id: str,
        public_key: str,
        key_id: str = "default",
        description: str = "",
        target_agent: str | None = None,
        target_room_id: str | None = None,
        target_thread_id: str | None = None,
        new_thread: bool = False,
        allowed_kinds: list[str] | None = None,
        replay_window_seconds: int | None = None,
        max_body_bytes: int | None = None,
    ) -> str:
        """Create a direct external trigger endpoint using caller-supplied public key material.

        Args:
            trigger_id: Route-safe trigger id for `/api/triggers/{trigger_id}`.
            public_key: Ed25519 public key for request verification. Accepts raw base64 of the
                32 key bytes, OpenSSH single-line format (`ssh-ed25519 <base64> [comment]`, e.g.
                the contents of `id_ed25519.pub`), or PEM SubjectPublicKeyInfo
                (`-----BEGIN PUBLIC KEY-----`). All formats are normalized to the raw 32-byte key.
            key_id: Expected signing key id header.
            description: Human-readable purpose.
            target_agent: Optional target agent/team; non-admin callers use the current one.
            target_room_id: Optional target room; non-admin callers use the current room.
            target_thread_id: Optional Matrix thread id for delivery; mutually exclusive with new_thread.
            new_thread: Start a fresh thread per delivery instead of appending to
                target_thread_id; mutually exclusive with target_thread_id. Each
                delivery posts a room-level root and the responding agent answers
                in a new thread under it with a fresh session.
            allowed_kinds: Optional list of accepted payload kind values.
            replay_window_seconds: Optional replay window capped by config policy.
            max_body_bytes: Optional body cap capped by config policy.

        """
        try:
            context = require_external_trigger_owner_context(self._CONTEXT_LABEL)
            external_store = ExternalTriggerStore(context.runtime_paths)
            reactive_store = ReactiveTriggerStore(context.runtime_paths)
            target = await self._direct_target(
                context,
                target_agent=target_agent,
                target_room_id=target_room_id,
                target_thread_id=target_thread_id,
                new_thread=new_thread,
            )
            stripped_id = trigger_id.strip()
            if (
                stripped_id
                and await run_blocking_until_complete(partial(reactive_store.get_trigger, stripped_id)) is not None
            ):
                msg = f"external trigger already exists: {stripped_id}"
                raise ExternalTriggerStoreError(msg)
            record = await run_blocking_until_complete(
                partial(
                    external_store.create_record,
                    trigger_id=trigger_id,
                    owner_user_id=context.requester_id,
                    created_by_agent_name=context.agent_name,
                    created_in_room_id=context.room_id,
                    created_in_thread_id=context.resolved_thread_id or context.thread_id,
                    target=target,
                    public_key=public_key,
                    key_id=key_id,
                    description=description,
                    allowed_kinds=allowed_kinds or (),
                    replay_window_seconds=replay_window_seconds,
                    max_body_bytes=max_body_bytes,
                    config=context.config,
                ),
            )
        except _TOOL_ERRORS as exc:
            return self._payload("error", message=str(exc))
        return self._payload(
            "ok",
            action="create",
            trigger=self._record_payload(record),
            endpoint_path=f"/api/triggers/{record.trigger_id}",
            public_key_fingerprint=record.public_key_fingerprint,
        )

    def list_triggers(self, include_disabled: bool = True) -> str:
        """List direct external triggers and reactive triggers owned by the requester, or all for admins.

        Args:
            include_disabled: Whether to include disabled triggers (default True).

        """

        def list_records(
            context: ToolRuntimeContext,
            external_store: ExternalTriggerStore,
            reactive_store: ReactiveTriggerStore,
        ) -> str:
            if self._is_admin(context):
                external_records = external_store.list_records(owner_user_id=None)
                reactive_records = reactive_store.list_triggers(
                    owner_user_id=None,
                    include_disabled=include_disabled,
                )
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
            )

        return self._with_stores(list_records)

    def disable_trigger(self, trigger_id: str, enabled: bool = False) -> str:
        """Enable or disable a direct external trigger or reactive trigger owned by the requester.

        Args:
            trigger_id: Identifier of the direct or reactive trigger to update.
            enabled: Target enabled state (False to disable, True to enable).

        """

        def set_enabled(
            context: ToolRuntimeContext,
            external_store: ExternalTriggerStore,
            reactive_store: ReactiveTriggerStore,
        ) -> str:
            if _has_external_trigger(external_store, trigger_id):
                record = external_store.set_enabled(
                    trigger_id,
                    enabled=enabled,
                    actor_user_id=context.requester_id,
                    config=context.config,
                )
                return self._payload("ok", action="set_enabled", trigger=self._record_payload(record))

            reactive_record = reactive_store.get_trigger(trigger_id)
            if reactive_record is None:
                msg = f"external trigger not found: {trigger_id}"
                raise ExternalTriggerStoreError(msg)
            self._require_reactive_owner_or_admin(context, reactive_record)
            was_disabled = not reactive_record.enabled
            if enabled and was_disabled and isinstance(reactive_record.source, RoomMessageSourceSpec):
                reactive_store.advance_room_cursor(
                    trigger_id,
                    RoomCursor(created_ts=int(time.time() * 1000), logical_event_id=""),
                )
            updated = reactive_store.set_enabled(trigger_id, enabled)
            if updated is None:
                msg = f"external trigger not found: {trigger_id}"
                raise ExternalTriggerStoreError(msg)
            if enabled:
                self._notify_reactive_service(context)
            return self._payload("ok", action="set_enabled", trigger=self._reactive_record_payload(updated))

        return self._with_stores(set_enabled)

    def delete_trigger(self, trigger_id: str) -> str:
        """Delete a direct external trigger or reactive trigger owned by the requester.

        Args:
            trigger_id: Identifier of the direct or reactive trigger to delete.

        """

        def delete(
            context: ToolRuntimeContext,
            external_store: ExternalTriggerStore,
            reactive_store: ReactiveTriggerStore,
        ) -> str:
            if _has_external_trigger(external_store, trigger_id):
                external_store.delete_record(
                    trigger_id,
                    actor_user_id=context.requester_id,
                    config=context.config,
                )
                return self._payload("ok", action="delete", trigger_id=trigger_id)

            reactive_record = reactive_store.get_trigger(trigger_id)
            if reactive_record is None:
                msg = f"external trigger not found: {trigger_id}"
                raise ExternalTriggerStoreError(msg)
            self._require_reactive_owner_or_admin(context, reactive_record)
            reactive_store.delete_trigger(trigger_id)
            return self._payload("ok", action="delete", trigger_id=trigger_id)

        return self._with_stores(delete)

    def rotate_trigger_key(self, trigger_id: str, public_key: str, key_id: str = "default") -> str:
        """Rotate a direct Ed25519 trigger public key without accepting or returning private key material.

        Args:
            trigger_id: The direct signed external trigger whose signing key should be rotated.
            public_key: New Ed25519 public key. Accepts raw base64 of the 32 key bytes, OpenSSH
                single-line format (`ssh-ed25519 <base64> [comment]`, e.g. the contents of
                `id_ed25519.pub`), or PEM SubjectPublicKeyInfo (`-----BEGIN PUBLIC KEY-----`).
                All formats are normalized to the raw 32-byte key.
            key_id: Expected signing key id header.

        """

        def rotate(
            context: ToolRuntimeContext,
            external_store: ExternalTriggerStore,
            reactive_store: ReactiveTriggerStore,
        ) -> str:
            if (
                not _has_external_trigger(external_store, trigger_id)
                and reactive_store.get_trigger(trigger_id) is not None
            ):
                msg = "Key rotation applies only to direct signed external triggers."
                raise ExternalTriggerStoreError(msg)
            record = external_store.rotate_key(
                trigger_id,
                public_key=public_key,
                key_id=key_id,
                actor_user_id=context.requester_id,
                config=context.config,
            )
            return self._payload(
                "ok",
                action="rotate_key",
                trigger=self._record_payload(record),
                public_key_fingerprint=record.public_key_fingerprint,
            )

        return self._with_stores(rotate)


def _has_external_trigger(external_store: ExternalTriggerStore, trigger_id: str) -> bool:
    return any(record.trigger_id == trigger_id for record in external_store.list_records())


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
        if _has_external_trigger(external_store, stripped) or reactive_store.get_trigger(stripped) is not None:
            msg = f"Trigger already exists: {stripped}"
            raise ReactiveTriggerDuplicateError(msg)
        return stripped

    for _ in range(10):
        candidate = generate_trigger_id()
        if not _has_external_trigger(external_store, candidate) and reactive_store.get_trigger(candidate) is None:
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
    if hasattr(context.client, "room_get_state"):
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


async def _verify_owner_joined_room(
    context: ToolRuntimeContext,
    *,
    room_id: str,
    preferred_agent: str | None = None,
) -> None:
    current_room_id = resolve_room_id(context.room_id, context.runtime_paths)
    owner_ids = equivalent_requester_ids(context.requester_id, context.config, context.runtime_paths)
    client = _client_for_room(context, room_id, preferred_agent=preferred_agent)
    if client is not None:
        if hasattr(client, "room_get_state"):
            room_state = await read_room_state(client, room_id)
            if room_state is not None:
                if any(
                    room_state.events.get(("m.room.member", uid), {}).get("membership") == "join" for uid in owner_ids
                ):
                    return
                msg = f"You must be joined to room {room_id} to create this trigger."
                raise ReactiveTriggerStoreError(msg)
        rooms = getattr(client, "rooms", None)
        room = rooms.get(room_id) if isinstance(rooms, Mapping) else None
        users = getattr(room, "users", None)
        if isinstance(users, Mapping) and users:
            if any(uid in users for uid in owner_ids):
                return
            msg = f"You must be joined to room {room_id} to create this trigger."
            raise ReactiveTriggerStoreError(msg)

    if room_id == current_room_id:
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
