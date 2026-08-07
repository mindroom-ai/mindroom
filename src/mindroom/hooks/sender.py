"""Hook-to-Matrix message sender helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mindroom.constants import HOOK_SOURCE_KEY, SOURCE_KIND_KEY
from mindroom.dispatch_source import HOOK_DISPATCH_SOURCE_KIND, HOOK_SOURCE_KIND
from mindroom.hooks.types import HookMessageSender  # noqa: TC001

if TYPE_CHECKING:
    import nio

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.client_delivery import DeliveredMatrixEvent
    from mindroom.matrix.conversation_reads import ConversationReader


async def send_matrix_message(
    client: nio.AsyncClient,
    room_id: str,
    content: dict[str, Any],
) -> DeliveredMatrixEvent | None:
    """Send already-built Matrix content, late-binding to avoid an import cycle."""
    # why-lazy: client_delivery imports config through Matrix formatting helpers during facade startup.
    from mindroom.matrix.client_delivery import send_message_result  # noqa: PLC0415

    return await send_message_result(client, room_id, content)


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
    conversation_reader: ConversationReader,
) -> str | None:
    """Send one hook-originated Matrix message."""
    # why-lazy: mentions imports config during hooks facade startup.
    from mindroom.matrix.mentions import format_message_with_mentions  # noqa: PLC0415

    content_extra = dict(extra_content or {})
    content_extra[SOURCE_KIND_KEY] = HOOK_DISPATCH_SOURCE_KIND if trigger_dispatch else HOOK_SOURCE_KIND
    content_extra[HOOK_SOURCE_KEY] = source_hook

    latest_thread_event_id = await conversation_reader.latest_thread_event_id(
        room_id=room_id,
        thread_id=thread_id,
    )
    content = format_message_with_mentions(
        config,
        runtime_paths,
        body,
        thread_event_id=thread_id,
        latest_thread_event_id=latest_thread_event_id,
        extra_content=content_extra,
    )
    delivered = await send_matrix_message(client, room_id, content)
    if delivered is not None:
        return delivered.event_id
    return None


def build_hook_message_sender(
    client: nio.AsyncClient,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    conversation_reader: ConversationReader,
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
            conversation_reader=conversation_reader,
        )

    return _send
