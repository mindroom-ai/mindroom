"""Matrix dispatch executor for accepted external triggers."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from mindroom.constants import ORIGINAL_SENDER_KEY, SOURCE_KIND_KEY
from mindroom.dispatch_source import EXTERNAL_TRIGGER_SOURCE_KIND
from mindroom.hooks.sender import send_and_track_message
from mindroom.matrix.client_room_admin import get_room_members
from mindroom.matrix.mentions import format_entity_mention
from mindroom.matrix.message_builder import build_message_content, markdown_to_html

if TYPE_CHECKING:
    from collections.abc import Callable

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


async def deliver_entity_mention_message(
    *,
    client: nio.AsyncClient,
    room_id: str,
    thread_event_id: str | None,
    entity_name: str,
    build_text: Callable[[str], str],
    extra_content: dict[str, Any],
    config: Config,
    runtime_paths: RuntimePaths,
    conversation_cache: ConversationCacheProtocol,
    caller_label: str = "external_trigger",
) -> str | None:
    """Post one entity-mention message into a room or thread with trusted metadata."""
    latest_thread_event_id = None
    if thread_event_id is not None:
        latest_thread_event_id = await conversation_cache.get_latest_thread_event_id_if_needed(
            room_id,
            thread_event_id,
            caller_label=caller_label,
        )

    plain_target, mentioned_user_ids, markdown_target = format_entity_mention(entity_name, config, runtime_paths)
    content = build_message_content(
        body=build_text(plain_target),
        formatted_body=markdown_to_html(build_text(markdown_target)),
        mentioned_user_ids=mentioned_user_ids,
        thread_event_id=thread_event_id,
        latest_thread_event_id=latest_thread_event_id,
        extra_content=extra_content,
    )
    delivered = await send_and_track_message(client, room_id, content, conversation_cache)
    if delivered is None:
        return None
    return delivered.event_id


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
    return await deliver_entity_mention_message(
        client=client,
        room_id=snapshot.resolved_room_id,
        thread_event_id=None if snapshot.target.new_thread else snapshot.target.thread_id,
        entity_name=snapshot.target.agent,
        build_text=lambda target_text: _build_external_trigger_text(target_text, payload),
        extra_content=_external_trigger_content_metadata(snapshot, payload),
        config=config,
        runtime_paths=runtime_paths,
        conversation_cache=conversation_cache,
    )


async def is_user_joined_room(client: nio.AsyncClient, room_id: str, user_id: str) -> bool:
    """Return whether one user is currently joined to one room.

    A failed membership fetch counts as not joined so delivery stays fail-closed.
    """
    member_ids = await get_room_members(client, room_id)
    return member_ids is not None and user_id in member_ids


async def is_external_trigger_owner_joined_target_room(
    client: nio.AsyncClient,
    snapshot: TriggerDeliverySnapshot,
) -> bool:
    """Return whether the trigger owner is currently joined to the delivery room."""
    return await is_user_joined_room(client, snapshot.resolved_room_id, snapshot.owner_user_id)


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
