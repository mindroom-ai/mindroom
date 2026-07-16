"""Matrix dispatch executor for accepted callbacks and expiry notices."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from mindroom.constants import ORIGINAL_SENDER_KEY, SOURCE_KIND_KEY
from mindroom.dispatch_source import EXTERNAL_TRIGGER_SOURCE_KIND
from mindroom.external_triggers.executor import deliver_entity_mention_message

if TYPE_CHECKING:
    import nio

    from mindroom.callbacks.models import CallbackFirePayload
    from mindroom.callbacks.store import CallbackDeliverySnapshot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.conversation_cache import ConversationCacheProtocol

_CALLBACK_ID_KEY = "io.mindroom.callback.id"
_CALLBACK_STATUS_KEY = "io.mindroom.callback.status"


def _build_callback_fire_text(target_text: str, label: str, payload: CallbackFirePayload) -> str:
    """Build the visible wake-up message for one callback fire."""
    sections = [f"{target_text} 🤖 {label} → **{payload.status}**: {payload.message}"]
    if payload.data:
        data_json = json.dumps(payload.data, indent=2, sort_keys=True)
        sections.append(f"```json\n{data_json}\n```")
    return "\n\n".join(sections)


def _build_callback_expiry_text(target_text: str, label: str, created_at: int) -> str:
    """Build the visible timeout message for one expired unfired callback."""
    created_text = datetime.fromtimestamp(created_at, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
    return f"{target_text} ⏰ Callback '{label}' expired without firing (created {created_text})"


def _callback_content_metadata(snapshot: CallbackDeliverySnapshot, status: str) -> dict[str, Any]:
    """Return Matrix content metadata stamping the callback owner as trusted requester."""
    return {
        SOURCE_KIND_KEY: EXTERNAL_TRIGGER_SOURCE_KIND,
        ORIGINAL_SENDER_KEY: snapshot.owner_user_id,
        _CALLBACK_ID_KEY: snapshot.callback_id,
        _CALLBACK_STATUS_KEY: status,
    }


async def execute_callback_fire(
    *,
    client: nio.AsyncClient,
    snapshot: CallbackDeliverySnapshot,
    payload: CallbackFirePayload,
    config: Config,
    runtime_paths: RuntimePaths,
    conversation_cache: ConversationCacheProtocol,
) -> str | None:
    """Post one authenticated callback payload to its bound Matrix target."""
    return await deliver_entity_mention_message(
        client=client,
        room_id=snapshot.resolved_room_id,
        thread_event_id=snapshot.target_thread_id,
        entity_name=snapshot.target_agent,
        build_text=lambda target_text: _build_callback_fire_text(target_text, snapshot.label, payload),
        extra_content=_callback_content_metadata(snapshot, payload.status),
        config=config,
        runtime_paths=runtime_paths,
        conversation_cache=conversation_cache,
        caller_label="callback",
    )


async def execute_callback_expiry_notice(
    *,
    client: nio.AsyncClient,
    snapshot: CallbackDeliverySnapshot,
    created_at: int,
    config: Config,
    runtime_paths: RuntimePaths,
    conversation_cache: ConversationCacheProtocol,
) -> str | None:
    """Post one expiry timeout notice to the callback's bound Matrix target."""
    return await deliver_entity_mention_message(
        client=client,
        room_id=snapshot.resolved_room_id,
        thread_event_id=snapshot.target_thread_id,
        entity_name=snapshot.target_agent,
        build_text=lambda target_text: _build_callback_expiry_text(target_text, snapshot.label, created_at),
        extra_content=_callback_content_metadata(snapshot, "expired"),
        config=config,
        runtime_paths=runtime_paths,
        conversation_cache=conversation_cache,
        caller_label="callback",
    )
