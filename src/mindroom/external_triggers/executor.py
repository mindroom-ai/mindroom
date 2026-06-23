"""Matrix dispatch executor for accepted external triggers."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from mindroom.constants import SOURCE_KIND_KEY
from mindroom.dispatch_source import EXTERNAL_TRIGGER_SOURCE_KIND
from mindroom.hooks.sender import send_and_track_message
from mindroom.matrix.mentions import format_message_with_mentions
from mindroom.matrix.state import resolve_room_aliases

if TYPE_CHECKING:
    import nio

    from mindroom.config.external_triggers import ExternalTriggerConfig
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.external_triggers.models import ExternalTriggerPayload
    from mindroom.matrix.conversation_cache import ConversationCacheProtocol

_EXTERNAL_TRIGGER_ID_KEY = "io.mindroom.external_trigger.id"
_EXTERNAL_TRIGGER_KIND_KEY = "io.mindroom.external_trigger.kind"
_EXTERNAL_TRIGGER_EVENT_ID_KEY = "io.mindroom.external_trigger.event_id"


def _build_external_trigger_message_text(trigger: ExternalTriggerConfig, payload: ExternalTriggerPayload) -> str:
    """Build visible Matrix message text from a fixed trigger target and signed payload."""
    if payload.title:
        sections = [
            f"@{trigger.target.agent} {_escape_payload_mentions(payload.title)}",
            _escape_payload_mentions(payload.message),
        ]
    else:
        sections = [f"@{trigger.target.agent} {_escape_payload_mentions(payload.message)}"]

    if payload.data:
        data_json = json.dumps(payload.data, indent=2, sort_keys=True)
        sections.append(f"```json\n{_escape_payload_mentions(data_json)}\n```")

    return "\n\n".join(sections)


async def execute_external_trigger(
    *,
    client: nio.AsyncClient,
    trigger_id: str,
    trigger: ExternalTriggerConfig,
    payload: ExternalTriggerPayload,
    config: Config,
    runtime_paths: RuntimePaths,
    conversation_cache: ConversationCacheProtocol,
) -> str | None:
    """Post one authenticated external trigger payload to its configured Matrix target."""
    room_id = _resolve_trigger_room_id(trigger.target.room_id, runtime_paths)
    thread_event_id = None if trigger.target.new_thread else trigger.target.thread_id
    latest_thread_event_id = None
    if thread_event_id is not None:
        latest_thread_event_id = await conversation_cache.get_latest_thread_event_id_if_needed(
            room_id,
            thread_event_id,
            caller_label="external_trigger",
        )

    content = format_message_with_mentions(
        config,
        runtime_paths,
        _build_external_trigger_message_text(trigger, payload),
        thread_event_id=thread_event_id,
        latest_thread_event_id=latest_thread_event_id,
        extra_content=_external_trigger_content_metadata(trigger_id, payload),
    )
    delivered = await send_and_track_message(client, room_id, content, config, conversation_cache)
    if delivered is None:
        return None
    return delivered.event_id


def _resolve_trigger_room_id(room_id_or_alias: str, runtime_paths: RuntimePaths) -> str:
    """Resolve configured trigger room refs to Matrix room IDs when known."""
    resolved = resolve_room_aliases([room_id_or_alias], runtime_paths=runtime_paths)
    return resolved[0] if resolved else room_id_or_alias


def _external_trigger_content_metadata(trigger_id: str, payload: ExternalTriggerPayload) -> dict[str, Any]:
    """Return Matrix content metadata for one external trigger dispatch."""
    return {
        SOURCE_KIND_KEY: EXTERNAL_TRIGGER_SOURCE_KIND,
        _EXTERNAL_TRIGGER_ID_KEY: trigger_id,
        _EXTERNAL_TRIGGER_KIND_KEY: payload.kind,
        _EXTERNAL_TRIGGER_EVENT_ID_KEY: payload.event_id,
    }


def _escape_payload_mentions(text: str) -> str:
    """Escape payload-controlled mention markers before Matrix mention parsing."""
    return text.replace("@", "(at)")
