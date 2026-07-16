"""Matrix delivery for completed callbacks."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mindroom.constants import ORIGINAL_SENDER_KEY, SOURCE_KIND_KEY
from mindroom.dispatch_source import EXTERNAL_TRIGGER_SOURCE_KIND
from mindroom.external_triggers.executor import deliver_entity_mention_message

if TYPE_CHECKING:
    import nio

    from mindroom.callbacks.store import CallbackRecord
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.conversation_cache import ConversationCacheProtocol

_CALLBACK_ID_KEY = "io.mindroom.callback.id"


def _callback_content_metadata(record: CallbackRecord) -> dict[str, Any]:
    return {
        SOURCE_KIND_KEY: EXTERNAL_TRIGGER_SOURCE_KIND,
        ORIGINAL_SENDER_KEY: record.owner_user_id,
        _CALLBACK_ID_KEY: record.callback_id,
    }


async def execute_callback_fire(
    *,
    client: nio.AsyncClient,
    record: CallbackRecord,
    message: str,
    config: Config,
    runtime_paths: RuntimePaths,
    conversation_cache: ConversationCacheProtocol,
) -> str | None:
    """Wake the agent in the conversation that minted the callback."""
    return await deliver_entity_mention_message(
        client=client,
        room_id=record.room_id,
        thread_event_id=record.thread_id,
        entity_name=record.agent_name,
        build_text=lambda target_text: f"{target_text} ✅ {record.label}: {message}",
        extra_content=_callback_content_metadata(record),
        config=config,
        runtime_paths=runtime_paths,
        conversation_cache=conversation_cache,
        caller_label="callback",
    )
