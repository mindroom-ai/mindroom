"""Hook-to-Matrix message sender helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mindroom.hooks.types import HookMessageSender  # noqa: TC001

if TYPE_CHECKING:
    import nio

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.client_delivery import DeliveredMatrixEvent
    from mindroom.matrix.conversation_cache import ConversationCacheProtocol


async def _send_message_result(
    client: nio.AsyncClient,
    room_id: str,
    content: dict[str, Any],
    *,
    config: Config,
) -> DeliveredMatrixEvent | None:
    """Late-bind Matrix delivery to avoid the hooks facade import cycle."""
    # why-lazy: client_delivery imports config through Matrix formatting helpers during facade startup.
    from mindroom.matrix.client_delivery import send_message_result  # noqa: PLC0415

    return await send_message_result(client, room_id, content, config=config)


async def send_and_track_message(
    client: nio.AsyncClient,
    room_id: str,
    content: dict[str, Any],
    config: Config,
    conversation_cache: ConversationCacheProtocol,
) -> DeliveredMatrixEvent | None:
    """Send already-built Matrix content and record successful delivery in the cache."""
    delivered = await _send_message_result(client, room_id, content, config=config)
    if delivered is not None:
        conversation_cache.notify_outbound_message(room_id, delivered.event_id, delivered.content_sent)
    return delivered


async def send_hook_message(
    client: nio.AsyncClient,
    config: Config,
    runtime_paths: RuntimePaths,
    room_id: str,
    body: str,
    thread_id: str | None,
    source_hook: str,
    extra_content: dict[str, Any] | None,
    *,
    trigger_dispatch: bool = False,
    conversation_cache: ConversationCacheProtocol,
) -> str | None:
    """Send one hook-originated Matrix message."""
    # why-lazy: mentions imports config during hooks facade startup.
    from mindroom.matrix.mentions import format_message_with_mentions  # noqa: PLC0415

    content_extra = dict(extra_content or {})
    content_extra["com.mindroom.source_kind"] = "hook_dispatch" if trigger_dispatch else "hook"
    content_extra["com.mindroom.hook_source"] = source_hook

    latest_thread_event_id = await conversation_cache.get_latest_thread_event_id_if_needed(
        room_id,
        thread_id,
        caller_label="hook_sender",
    )
    content = format_message_with_mentions(
        config,
        runtime_paths,
        body,
        thread_event_id=thread_id,
        latest_thread_event_id=latest_thread_event_id,
        extra_content=content_extra,
    )
    delivered = await send_and_track_message(client, room_id, content, config, conversation_cache)
    if delivered is not None:
        return delivered.event_id
    return None


def build_hook_message_sender(
    client: nio.AsyncClient,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    conversation_cache: ConversationCacheProtocol,
) -> HookMessageSender:
    """Return a sender bound to one Matrix client."""

    async def _send(
        room_id: str,
        body: str,
        thread_id: str | None,
        source_hook: str,
        extra_content: dict[str, Any] | None,
        *,
        trigger_dispatch: bool = False,
    ) -> str | None:
        return await send_hook_message(
            client,
            config,
            runtime_paths,
            room_id,
            body,
            thread_id,
            source_hook,
            extra_content,
            trigger_dispatch=trigger_dispatch,
            conversation_cache=conversation_cache,
        )

    return _send
