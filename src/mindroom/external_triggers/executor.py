"""Matrix dispatch executor for accepted external triggers."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from mindroom.constants import ORIGINAL_SENDER_KEY, SOURCE_KIND_KEY
from mindroom.dispatch_source import EXTERNAL_TRIGGER_SOURCE_KIND
from mindroom.hooks.sender import send_and_track_message
from mindroom.matrix.client_room_admin import get_room_members
from mindroom.matrix.mentions import parse_mentions_in_text
from mindroom.matrix.message_builder import build_message_content, markdown_to_html

if TYPE_CHECKING:
    import nio

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.external_triggers.models import ExternalTriggerPayload
    from mindroom.external_triggers.store import TriggerDeliverySnapshot
    from mindroom.matrix.conversation_cache import ConversationCacheProtocol

_EXTERNAL_TRIGGER_ID_KEY = "io.mindroom.external_trigger.id"
_EXTERNAL_TRIGGER_KIND_KEY = "io.mindroom.external_trigger.kind"
_EXTERNAL_TRIGGER_EVENT_ID_KEY = "io.mindroom.external_trigger.event_id"


def _build_external_trigger_text(target_text: str, payload: ExternalTriggerPayload) -> str:
    """Build visible trigger text from a target mention and unmodified signed payload."""
    if payload.title:
        sections = [
            f"{target_text} {payload.title}",
            payload.message,
        ]
    else:
        sections = [f"{target_text} {payload.message}"]

    if payload.data:
        data_json = json.dumps(payload.data, indent=2, sort_keys=True)
        sections.append(f"```json\n{data_json}\n```")

    return "\n\n".join(sections)


async def execute_external_trigger(
    *,
    client: nio.AsyncClient,
    snapshot: TriggerDeliverySnapshot,
    payload: ExternalTriggerPayload,
    config: Config,
    runtime_paths: RuntimePaths,
    conversation_cache: ConversationCacheProtocol,
) -> str | None:
    """Post one authenticated external trigger payload to its configured Matrix target."""
    room_id = snapshot.resolved_room_id
    thread_event_id = None if snapshot.target.new_thread else snapshot.target.thread_id
    latest_thread_event_id = None
    if thread_event_id is not None:
        latest_thread_event_id = await conversation_cache.get_latest_thread_event_id_if_needed(
            room_id,
            thread_event_id,
            caller_label="external_trigger",
        )

    plain_target, mentioned_user_ids, markdown_target = parse_mentions_in_text(
        f"@{snapshot.target.agent}",
        config,
        runtime_paths,
    )
    plain_text = _build_external_trigger_text(plain_target, payload)
    markdown_text = _build_external_trigger_text(markdown_target, payload)
    content = build_message_content(
        body=plain_text,
        formatted_body=markdown_to_html(markdown_text),
        mentioned_user_ids=mentioned_user_ids,
        thread_event_id=thread_event_id,
        latest_thread_event_id=latest_thread_event_id,
        extra_content=_external_trigger_content_metadata(snapshot, payload),
    )
    delivered = await send_and_track_message(client, room_id, content, config, conversation_cache)
    if delivered is None:
        return None
    return delivered.event_id


async def is_external_trigger_owner_joined_target_room(
    client: nio.AsyncClient,
    snapshot: TriggerDeliverySnapshot,
) -> bool:
    """Return whether the trigger owner is currently joined to the delivery room."""
    member_ids = await get_room_members(client, snapshot.resolved_room_id)
    return snapshot.owner_user_id in member_ids


def _external_trigger_content_metadata(
    snapshot: TriggerDeliverySnapshot,
    payload: ExternalTriggerPayload,
) -> dict[str, Any]:
    """Return Matrix content metadata for one external trigger dispatch."""
    return {
        SOURCE_KIND_KEY: EXTERNAL_TRIGGER_SOURCE_KIND,
        ORIGINAL_SENDER_KEY: snapshot.owner_user_id,
        _EXTERNAL_TRIGGER_ID_KEY: snapshot.trigger_id,
        _EXTERNAL_TRIGGER_KIND_KEY: payload.kind,
        _EXTERNAL_TRIGGER_EVENT_ID_KEY: payload.event_id,
    }
