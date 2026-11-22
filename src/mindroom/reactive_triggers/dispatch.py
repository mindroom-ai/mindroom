"""Owner-scoped Matrix wake-agent dispatch for reactive triggers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from mindroom.authorization import ReplyMembershipPendingError, is_sender_allowed_for_agent_reply_in_room
from mindroom.constants import ORIGINAL_SENDER_KEY, PER_FIRE_THREAD_ROOT_KEY, SOURCE_KIND_KEY
from mindroom.dispatch_source import REACTIVE_TRIGGER_SOURCE_KIND
from mindroom.hooks.sender import send_matrix_message
from mindroom.logging_config import get_logger
from mindroom.matrix.mentions import format_entity_mention
from mindroom.matrix.message_builder import build_message_content, markdown_to_html
from mindroom.matrix.room_reconciliation import read_room_state
from mindroom.reactive_triggers.matching import REACTIVE_TRIGGER_DEDUPE_KEY, REACTIVE_TRIGGER_ID_KEY
from mindroom.reactive_triggers.store import ReactiveTriggerStore, ReactiveTriggerStoreError
from mindroom.requester_identity import equivalent_requester_ids, is_human_requester_id

if TYPE_CHECKING:
    import nio

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.bot import AgentBot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.reactive_triggers.models import FiringRecord, ReactiveTriggerRecord, TriggerEvent

logger = get_logger(__name__)

_MAX_DISPATCH_TEXT_CHARS = 8_000
_MAX_DISPATCH_DATA_CHARS = 8_000


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """Outcome of attempting one Matrix wake-agent delivery."""

    status: Literal["delivered", "retry", "skipped"]
    matrix_event_id: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class _ResolvedTargetThread:
    """Resolved thread placement for one reactive trigger dispatch."""

    thread_event_id: str | None
    per_fire_root: bool
    save_thread_key: bool


def build_reactive_trigger_body(
    *,
    target_text: str,
    rule: ReactiveTriggerRecord,
    event: TriggerEvent,
    triage_reason: str | None,
) -> str:
    """Build visible wake-agent text with owner prompt first and bounded untrusted event JSON."""
    context_parts = [
        f"trigger={rule.trigger_id}",
        f"source={event.source_type}",
        f"kind={event.kind}",
    ]
    if triage_reason:
        context_parts.append(f"triage={triage_reason}")
    context_line = "Reactive trigger context: " + ", ".join(context_parts)

    data_value: Any = event.data
    raw_data_json = json.dumps(event.data, ensure_ascii=False, sort_keys=True, default=str)
    if len(raw_data_json) > _MAX_DISPATCH_DATA_CHARS:
        data_value = _truncate_text(raw_data_json, limit=_MAX_DISPATCH_DATA_CHARS)

    event_payload = {
        "event_id": event.event_id,
        "source_type": event.source_type,
        "source_id": event.source_id,
        "kind": event.kind,
        "actor": event.actor,
        "room_id": event.room_id,
        "thread_id": event.thread_id,
        "title": _truncate_text(event.title),
        "summary": _truncate_text(event.summary),
        "text": _truncate_text(event.text),
        "data": data_value,
    }
    payload_json = (
        json.dumps(event_payload, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        .replace("</", r"<\/")
        .replace("```", r"\u0060\u0060\u0060")
    )
    sections = [
        f"{target_text} {rule.target.prompt}",
        context_line,
        f"<untrusted_event_data>\n```json\n{payload_json}\n```\n</untrusted_event_data>",
    ]
    return "\n\n".join(sections)


async def dispatch_reactive_trigger(
    *,
    rule: ReactiveTriggerRecord,
    firing: FiringRecord,
    triage_reason: str | None,
    store: ReactiveTriggerStore,
    target_bot: AgentBot | None,
    config: Config,
    runtime_paths: RuntimePaths,
    memberships: AgentReplyMembershipIndex,
) -> DispatchResult:
    """Verify owner-scoped policy/auth/membership and post one Matrix wake-agent message."""
    rejection = await _check_dispatch_preconditions(
        rule=rule,
        target_bot=target_bot,
        config=config,
        runtime_paths=runtime_paths,
        memberships=memberships,
    )
    if rejection is not None:
        return rejection
    assert target_bot is not None
    assert target_bot.client is not None

    resolved_thread = _resolve_target_thread(
        rule=rule,
        firing=firing,
        store=store,
    )
    if isinstance(resolved_thread, DispatchResult):
        return resolved_thread

    latest_thread_event_id: str | None = None
    if resolved_thread.thread_event_id is not None:
        latest_thread_event_id = (
            await target_bot.conversation_reader.latest_thread_event_id(
                room_id=rule.target.room_id,
                thread_id=resolved_thread.thread_event_id,
            )
            or resolved_thread.thread_event_id
        )

    content = _build_dispatch_content(
        rule=rule,
        firing=firing,
        triage_reason=triage_reason,
        thread_event_id=resolved_thread.thread_event_id,
        latest_thread_event_id=latest_thread_event_id,
        per_fire_root=resolved_thread.per_fire_root,
        config=config,
        runtime_paths=runtime_paths,
    )
    delivered = await send_matrix_message(target_bot.client, rule.target.room_id, content)
    if delivered is None:
        return DispatchResult(status="retry", reason="matrix_send_failed")

    if resolved_thread.save_thread_key and rule.target.thread_key is not None:
        _persist_thread_key_binding(
            store=store,
            trigger_id=rule.trigger_id,
            thread_key=rule.target.thread_key,
            matrix_event_id=delivered.event_id,
        )

    return DispatchResult(status="delivered", matrix_event_id=delivered.event_id)


async def _check_dispatch_preconditions(
    *,
    rule: ReactiveTriggerRecord,
    target_bot: AgentBot | None,
    config: Config,
    runtime_paths: RuntimePaths,
    memberships: AgentReplyMembershipIndex,
) -> DispatchResult | None:
    """Return a skip/retry DispatchResult when preconditions are not met, or None to proceed."""
    if not config.external_trigger_policy.enabled or not config.external_trigger_policy.reactive_enabled:
        return DispatchResult(status="skipped", reason="reactive_triggers_disabled")

    if target_bot is None or not target_bot.running or target_bot.client is None:
        return DispatchResult(status="retry", reason="target_bot_unavailable")

    auth_rejection = _check_owner_reply_authorization(
        rule=rule,
        config=config,
        runtime_paths=runtime_paths,
        memberships=memberships,
    )
    if auth_rejection is not None:
        return auth_rejection

    rooms = getattr(target_bot.client, "rooms", None)
    if not isinstance(rooms, Mapping) or rule.target.room_id not in rooms:
        return DispatchResult(status="retry", reason="target_bot_not_joined")

    owner_joined = await _is_owner_joined_target_room(
        target_bot.client,
        owner_user_id=rule.owner_user_id,
        room_id=rule.target.room_id,
        config=config,
        runtime_paths=runtime_paths,
    )
    if owner_joined is None:
        return DispatchResult(status="retry", reason="room_state_unreadable")
    return None if owner_joined else DispatchResult(status="skipped", reason="owner_not_joined")


def _check_owner_reply_authorization(
    *,
    rule: ReactiveTriggerRecord,
    config: Config,
    runtime_paths: RuntimePaths,
    memberships: AgentReplyMembershipIndex,
) -> DispatchResult | None:
    """Verify that the trigger owner is human and allowed to address the target agent in the target room."""
    if not is_human_requester_id(rule.owner_user_id, config, runtime_paths):
        return DispatchResult(status="skipped", reason="owner_not_human")
    try:
        allowed = is_sender_allowed_for_agent_reply_in_room(
            rule.owner_user_id,
            rule.target.agent,
            config,
            rule.target.room_id,
            runtime_paths,
            memberships,
            require_resolved_membership=True,
        )
    except ReplyMembershipPendingError:
        return DispatchResult(status="retry", reason="membership_pending")
    return None if allowed else DispatchResult(status="skipped", reason="owner_not_authorized")


def _build_dispatch_content(
    *,
    rule: ReactiveTriggerRecord,
    firing: FiringRecord,
    triage_reason: str | None,
    thread_event_id: str | None,
    latest_thread_event_id: str | None,
    per_fire_root: bool,
    config: Config,
    runtime_paths: RuntimePaths,
) -> dict[str, Any]:
    """Format mention, markdown/HTML body, thread relation, and trigger metadata."""
    plain_target, mentioned_user_ids, markdown_target = format_entity_mention(
        rule.target.agent,
        config,
        runtime_paths,
    )
    plain_text = build_reactive_trigger_body(
        target_text=plain_target,
        rule=rule,
        event=firing.event,
        triage_reason=triage_reason,
    )
    markdown_text = build_reactive_trigger_body(
        target_text=markdown_target,
        rule=rule,
        event=firing.event,
        triage_reason=triage_reason,
    )
    extra_content: dict[str, Any] = {
        ORIGINAL_SENDER_KEY: rule.owner_user_id,
        SOURCE_KIND_KEY: REACTIVE_TRIGGER_SOURCE_KIND,
        REACTIVE_TRIGGER_ID_KEY: rule.trigger_id,
        REACTIVE_TRIGGER_DEDUPE_KEY: firing.dedupe_key,
    }
    if per_fire_root:
        extra_content[PER_FIRE_THREAD_ROOT_KEY] = True

    return build_message_content(
        body=plain_text,
        formatted_body=markdown_to_html(markdown_text),
        mentioned_user_ids=mentioned_user_ids,
        thread_event_id=thread_event_id,
        latest_thread_event_id=latest_thread_event_id,
        extra_content=extra_content,
    )


def _persist_thread_key_binding(
    *,
    store: ReactiveTriggerStore,
    trigger_id: str,
    thread_key: str,
    matrix_event_id: str,
) -> None:
    """Best-effort save of a newly opened per-fire thread root for thread_key grouping."""
    try:
        store.save_thread_key_binding(trigger_id, thread_key, matrix_event_id)
    except ReactiveTriggerStoreError:
        logger.warning(
            "Failed to persist reactive trigger thread_key binding after delivery",
            trigger_id=trigger_id,
            thread_key=thread_key,
            matrix_event_id=matrix_event_id,
            exc_info=True,
        )


async def _is_owner_joined_target_room(
    client: nio.AsyncClient,
    *,
    owner_user_id: str,
    room_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> bool | None:
    """Return whether the trigger owner (or an equivalent alias) is currently joined to room_id."""
    room_state = await read_room_state(client, room_id)
    if room_state is None:
        return None
    return any(
        room_state.events.get(("m.room.member", user_id), {}).get("membership") == "join"
        for user_id in equivalent_requester_ids(owner_user_id, config, runtime_paths)
    )


def _resolve_target_thread(
    *,
    rule: ReactiveTriggerRecord,
    firing: FiringRecord,
    store: ReactiveTriggerStore,
) -> _ResolvedTargetThread | DispatchResult:
    """Resolve thread placement or return a skip/retry DispatchResult on mismatch or store failure."""
    if rule.target.new_thread:
        return _resolve_new_thread_target(rule=rule, store=store)
    if rule.target.thread_id == "origin":
        if firing.event.room_id != rule.target.room_id:
            return DispatchResult(status="skipped", reason="origin_room_mismatch")
        return _ResolvedTargetThread(firing.event.thread_id or firing.event.event_id, False, False)
    return _ResolvedTargetThread(rule.target.thread_id, False, False)


def _resolve_new_thread_target(
    *,
    rule: ReactiveTriggerRecord,
    store: ReactiveTriggerStore,
) -> _ResolvedTargetThread | DispatchResult:
    """Resolve per-fire or thread_key-grouped thread placement for new_thread targets."""
    if rule.target.thread_key is None:
        return _ResolvedTargetThread(None, True, False)
    try:
        existing_root = store.get_thread_key_binding(rule.trigger_id, rule.target.thread_key)
    except ReactiveTriggerStoreError:
        logger.warning(
            "Failed to read reactive trigger thread_key binding",
            trigger_id=rule.trigger_id,
            thread_key=rule.target.thread_key,
            exc_info=True,
        )
        return DispatchResult(status="retry", reason="store_error")
    if existing_root is not None:
        return _ResolvedTargetThread(existing_root, False, False)
    return _ResolvedTargetThread(None, True, True)


def _truncate_text(value: str | None, *, limit: int = _MAX_DISPATCH_TEXT_CHARS) -> str | None:
    """Cap oversized untrusted text fields to prevent oversized Matrix events."""
    if value is None or len(value) <= limit:
        return value
    omitted = len(value) - limit
    return f"{value[:limit]}\n[... {omitted} characters truncated ...]"
